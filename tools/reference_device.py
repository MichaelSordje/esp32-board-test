from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import serial
from serial.tools import list_ports


REFERENCE_PREFIX = "REF|"


def parse_reference_line(line: str) -> tuple[str, dict[str, str]] | None:
    if not line.startswith(REFERENCE_PREFIX):
        return None
    parts = line.strip().split("|")
    if len(parts) < 2:
        return None
    category = parts[1]
    values: dict[str, str] = {}
    for part in parts[2:]:
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    return category, values


def _normalize_mac(value: str) -> str:
    compact = "".join(
        character
        for character in str(value).upper()
        if character in "0123456789ABCDEF"
    )
    if len(compact) != 12:
        return str(value).strip().upper()
    return ":".join(
        compact[index:index + 2]
        for index in range(0, 12, 2)
    )


def _configured_reference_mac() -> str:
    """Return the effective configured reference MAC.

    The normal Windows/Linux launchers load config/test-settings.local.json
    through test_runner before the orchestrator starts. Reading the settings
    through test_runner here therefore uses the same effective configuration
    as the rest of the test run.
    """
    try:
        import test_runner as tr

        settings = tr.load_json(tr.SETTINGS_PATH)
    except Exception:
        return ""

    if not isinstance(settings, dict):
        return ""

    tests = settings.get("tests", {})
    if not isinstance(tests, dict):
        return ""

    default = tests.get("default", {})
    if not isinstance(default, dict):
        return ""

    rf_quality = default.get("rf_quality", {})
    if not isinstance(rf_quality, dict):
        return ""

    return _normalize_mac(str(rf_quality.get("reference_mac", "")))


def _open_serial_no_reset(port: str, baudrate: int) -> serial.Serial:
    handle = serial.Serial(
        port=None,
        baudrate=baudrate,
        timeout=0.12,
        write_timeout=1.0,
        rtscts=False,
        dsrdtr=False,
    )
    handle.dtr = False
    handle.rts = False
    handle.port = port
    handle.open()
    return handle


def _serial_candidates() -> list[Any]:
    ignored = ("bluetooth", "rfcomm", "virtual")
    result: list[Any] = []
    for item in list_ports.comports():
        text = f"{item.device} {item.description} {item.manufacturer or ''}".lower()
        if any(token in text for token in ignored):
            continue
        if (
            item.vid is not None
            or "usb" in text
            or "com" in item.device.lower()
            or item.device.startswith("/dev/tty")
        ):
            result.append(item)
    return result


def _probe_reference_port(
    port: str,
    baudrate: int,
    timeout: float,
) -> dict[str, str] | None:
    try:
        handle = _open_serial_no_reset(port, baudrate)
    except (serial.SerialException, OSError):
        return None

    try:
        deadline = time.monotonic() + timeout
        next_info = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_info:
                try:
                    handle.write(b"INFO\n")
                    handle.flush()
                except (serial.SerialException, OSError):
                    return None
                next_info = now + 0.4

            try:
                raw = handle.readline()
            except (serial.SerialException, OSError):
                return None
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            parsed = parse_reference_line(line)
            if not parsed:
                continue
            category, values = parsed
            if (
                category in {"READY", "INFO"}
                and values.get("role") == "reference"
            ):
                return {"category": category, **values}
        return None
    finally:
        try:
            handle.close()
        except Exception:
            pass


@dataclass
class ReferenceIdentity:
    port: str
    version: str
    mac: str


class ReferenceDevice:
    def __init__(
        self,
        port: str,
        baudrate: int,
        identity: dict[str, str],
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.identity = ReferenceIdentity(
            port=port,
            version=identity.get("version", ""),
            mac=identity.get("mac", ""),
        )
        self.serial = _open_serial_no_reset(port, baudrate)
        self.log_handle: Any | None = None

    def set_log_path(self, path: Path) -> None:
        if self.log_handle is not None:
            self.log_handle.close()
        self.log_handle = path.open(
            "w",
            encoding="utf-8",
            buffering=1,
        )

    def _log(self, direction: str, text: str) -> None:
        if self.log_handle is None:
            return
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.log_handle.write(
            f"{timestamp} {direction} {text}\n"
        )

    def close(self) -> None:
        try:
            self.serial.close()
        except Exception:
            pass
        if self.log_handle is not None:
            try:
                self.log_handle.close()
            finally:
                self.log_handle = None

    def send(self, command: str) -> None:
        self._log(">", command)
        self.serial.write(
            (command + "\n").encode("ascii")
        )
        self.serial.flush()

    def read_event(
        self,
        timeout: float = 0.2,
    ) -> tuple[str, dict[str, str]] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.serial.readline()
            if not raw:
                continue
            line = raw.decode(
                "utf-8",
                errors="replace",
            ).strip()
            self._log("<", line)
            parsed = parse_reference_line(line)
            if parsed:
                return parsed
        return None

    def wait_for(
        self,
        predicate: Callable[
            [str, dict[str, str]],
            bool,
        ],
        timeout: float,
        description: str,
    ) -> tuple[str, dict[str, str]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.read_event(
                timeout=min(
                    0.2,
                    max(
                        0.01,
                        deadline - time.monotonic(),
                    ),
                )
            )
            if event is None:
                continue
            if predicate(event[0], event[1]):
                return event
        raise RuntimeError(
            f"Reference ESP on {self.port} did not answer "
            f"in time ({description})."
        )

    def request(
        self,
        command: str,
        predicate: Callable[
            [str, dict[str, str]],
            bool,
        ],
        timeout: float,
        description: str,
        attempts: int = 3,
    ) -> tuple[str, dict[str, str]]:
        """Send an idempotent command and retry lost/corrupted serial replies.

        The RF fixture can be operating close to the link limit while USB serial
        remains perfectly healthy. A single malformed serial line must not turn
        a bad RF measurement into a technical test abort.
        """
        last_error: RuntimeError | None = None
        for attempt in range(
            1,
            max(1, attempts) + 1,
        ):
            self.send(command)
            try:
                return self.wait_for(
                    predicate,
                    timeout,
                    description,
                )
            except RuntimeError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._log(
                    "!",
                    f"{description} retry "
                    f"{attempt + 1}/{attempts}",
                )
                time.sleep(0.08)

        raise RuntimeError(
            f"Reference ESP on {self.port} did not answer "
            f"after {max(1, attempts)} attempt(s) "
            f"({description})."
        ) from last_error

    def info(self) -> dict[str, str]:
        _category, values = self.request(
            "INFO",
            lambda category, values: (
                category in {"INFO", "READY"}
                and values.get("role") == "reference"
            ),
            1.2,
            "INFO",
        )
        return values

    def restart(
        self,
        timeout: float = 10.0,
    ) -> dict[str, str]:
        previous_mac = self.identity.mac

        self.send("RESTART")
        self.wait_for(
            lambda category, values: (
                category == "RESTART"
                and values.get("status") == "OK"
            ),
            1.2,
            "RESTART acknowledgement",
        )

        try:
            self.serial.close()
        except Exception:
            pass

        # The firmware acknowledges RESTART immediately before ESP.restart().
        # Do not reopen the port so quickly that INFO could still come from the
        # pre-restart firmware instance.
        time.sleep(0.25)

        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.serial = _open_serial_no_reset(
                    self.port,
                    self.baudrate,
                )
                info = self.info()
                if info.get("role") != "reference":
                    raise RuntimeError(
                        "restarted device did not identify as reference"
                    )
                if (
                    previous_mac
                    and info.get("mac", "")
                    != previous_mac
                ):
                    raise RuntimeError(
                        "reference MAC changed after restart: "
                        f"{previous_mac} -> "
                        f"{info.get('mac', '-')}"
                    )

                self.identity = ReferenceIdentity(
                    port=self.port,
                    version=info.get(
                        "version",
                        "",
                    ),
                    mac=info.get(
                        "mac",
                        previous_mac,
                    ),
                )
                return info
            except (
                serial.SerialException,
                OSError,
                RuntimeError,
            ) as exc:
                last_error = exc
                try:
                    self.serial.close()
                except Exception:
                    pass
                time.sleep(0.2)

        raise RuntimeError(
            f"Reference ESP on {self.port} "
            "did not return after RESTART."
        ) from last_error

    def start_ap(
        self,
        channel: int,
        tx_power_dbm: int,
    ) -> dict[str, str]:
        _category, values = self.request(
            f"AP_START|{channel}|{tx_power_dbm}",
            lambda category, values: (
                category == "AP"
                and values.get("status")
                in {"STARTED", "FAIL"}
            ),
            5.0,
            "AP_START",
            attempts=2,
        )
        if values.get("status") != "STARTED":
            raise RuntimeError(
                "Reference ESP could not start its RF "
                "test access point: "
                + values.get(
                    "reason",
                    "unknown error",
                )
            )
        return values

    def stop_ap(self) -> None:
        self.send("AP_STOP")
        try:
            self.wait_for(
                lambda category, values: (
                    category == "AP"
                    and values.get("status") == "STOPPED"
                ),
                2.0,
                "AP_STOP",
            )
        except RuntimeError:
            # Cleanup must not hide the original test result.
            pass

    def set_tx_power(self, dbm: int) -> int:
        _category, values = self.request(
            f"SET_TX_POWER|{dbm}",
            lambda category, values: (
                category == "TX_POWER"
                and values.get("status")
                in {"OK", "FAIL"}
            ),
            1.2,
            "SET_TX_POWER",
        )
        if values.get("status") != "OK":
            raise RuntimeError(
                "Reference ESP rejected TX-power change: "
                + values.get(
                    "reason",
                    "unknown error",
                )
            )
        return int(
            values.get(
                "actual_dbm",
                dbm,
            )
        )

    def reset_stats(self) -> None:
        self.request(
            "RESET_STATS",
            lambda category, values: (
                category == "RESET"
                and values.get("status") == "OK"
            ),
            1.2,
            "RESET_STATS",
        )

    def stats(self) -> dict[str, str]:
        _category, values = self.request(
            "STATS",
            lambda category, _values: (
                category == "STATS"
            ),
            1.2,
            "STATS",
        )
        return values

    def abort_tx(self) -> dict[str, str]:
        _category, values = self.request(
            "ABORT_TX",
            lambda category, values: (
                category == "TX_ABORT"
                and values.get("status") == "OK"
            ),
            1.2,
            "ABORT_TX",
        )
        return values

    def wait_for_dut(
        self,
        timeout: float = 8.0,
    ) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            stats = self.stats()
            if (
                int(
                    stats.get(
                        "station_count",
                        "0",
                    )
                    or 0
                )
                >= 1
                and stats.get("dut_known") == "1"
            ):
                return stats
            time.sleep(0.15)
        raise RuntimeError(
            "The reference ESP sees no ready DUT "
            "on its RF test access point."
        )

    def start_tx(
        self,
        count: int,
        interval_ms: int,
        timeout: float,
    ) -> dict[str, str]:
        self.send(
            f"TX|{count}|{interval_ms}"
        )
        _category, values = self.wait_for(
            lambda category, _values: (
                category == "TX_DONE"
            ),
            timeout,
            "RF TX",
        )
        if values.get("status") == "FAIL":
            raise RuntimeError(
                "Reference ESP RF transmit failed: "
                + values.get(
                    "reason",
                    "unknown error",
                )
            )
        return values


def find_reference_device(
    requested_port: str = "",
    baudrate: int = 115200,
    required: bool = False,
) -> ReferenceDevice | None:
    expected_mac = _configured_reference_mac()
    candidates = _serial_candidates()

    if requested_port:
        candidates = [
            item
            for item in candidates
            if item.device.lower()
            == requested_port.lower()
        ]
        if not candidates:
            if required:
                raise RuntimeError(
                    f"Reference serial port "
                    f"{requested_port} was not found."
                )
            return None

    detected_references: list[
        tuple[str, dict[str, str]]
    ] = []
    matches: list[
        tuple[str, dict[str, str]]
    ] = []

    for item in candidates:
        identity = _probe_reference_port(
            item.device,
            baudrate,
            1.8,
        )
        if identity is None:
            continue

        detected_references.append(
            (item.device, identity)
        )

        # When a reference MAC is configured, identity is authoritative:
        # only that physical ESP is the fixture reference. Any other ESP that
        # still happens to contain old reference firmware is deliberately
        # ignored here so the DUT detector can select and overwrite it.
        if expected_mac:
            current_mac = _normalize_mac(
                identity.get("mac", "")
            )
            if current_mac != expected_mac:
                continue

        matches.append(
            (item.device, identity)
        )

    if not matches:
        if required:
            if expected_mac:
                raise RuntimeError(
                    "The configured reference ESP was not found. "
                    "Connect the ESP whose MAC is configured in "
                    "tests.default.rf_quality.reference_mac."
                )

            raise RuntimeError(
                "No ESP32 Board Test reference ESP was found "
                "automatically. Connect the USB cable of the "
                "reference ESP and flash the reference firmware "
                "once with scripts/flash-reference.cmd (Windows) "
                "or scripts/flash-reference.sh (Linux)."
            )
        return None

    if len(matches) > 1:
        ports = ", ".join(
            port
            for port, _identity in matches
        )
        if expected_mac:
            raise RuntimeError(
                "The configured reference MAC was detected on "
                "multiple serial ports. Found: "
                f"{ports}"
            )
        raise RuntimeError(
            "Multiple reference ESPs were found. Leave only one "
            "reference device connected or select one with "
            "--reference-port. Found: "
            f"{ports}"
        )

    port, identity = matches[0]
    device = ReferenceDevice(
        port,
        baudrate,
        identity,
    )

    # Refresh identity after reopening. This also verifies that the port stayed
    # valid. If a fixed MAC is configured, verify it again after reopening.
    info = device.info()
    refreshed_mac = _normalize_mac(
        info.get(
            "mac",
            identity.get("mac", ""),
        )
    )
    if (
        expected_mac
        and refreshed_mac != expected_mac
    ):
        device.close()
        raise RuntimeError(
            "The selected reference ESP no longer matches "
            "the configured reference MAC."
        )

    device.identity = ReferenceIdentity(
        port=port,
        version=info.get(
            "version",
            identity.get("version", ""),
        ),
        mac=info.get(
            "mac",
            identity.get("mac", ""),
        ),
    )
    return device
