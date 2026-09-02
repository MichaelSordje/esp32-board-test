from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import serial
from serial.tools import list_ports


ESPRESSIF_NATIVE_USB_VID = 0x303A


@dataclass(frozen=True)
class NativeUsbIdentity:
    device: str
    vid: int
    pid: int | None
    serial_number: str
    location: str
    hwid: str


def _normalized(value: Any) -> str:
    return str(value or "").strip().casefold()


def _find_port_info(port: str) -> Any | None:
    target = _normalized(port)
    for item in list_ports.comports():
        if _normalized(getattr(item, "device", "")) == target:
            return item
    return None


def _is_native_espressif_usb(port_info: Any | None) -> bool:
    if port_info is None:
        return False
    try:
        return int(getattr(port_info, "vid", -1)) == ESPRESSIF_NATIVE_USB_VID
    except (TypeError, ValueError):
        return False


def _capture_identity(port_info: Any) -> NativeUsbIdentity:
    return NativeUsbIdentity(
        device=str(getattr(port_info, "device", "") or ""),
        vid=int(getattr(port_info, "vid", ESPRESSIF_NATIVE_USB_VID)),
        pid=(
            int(getattr(port_info, "pid"))
            if getattr(port_info, "pid", None) is not None
            else None
        ),
        serial_number=str(getattr(port_info, "serial_number", "") or "").strip(),
        location=str(getattr(port_info, "location", "") or "").strip(),
        hwid=str(getattr(port_info, "hwid", "") or "").strip(),
    )


def _strong_identity_match(
    candidate: Any,
    identity: NativeUsbIdentity,
) -> bool:
    try:
        if int(getattr(candidate, "vid", -1)) != identity.vid:
            return False
    except (TypeError, ValueError):
        return False

    candidate_serial = _normalized(getattr(candidate, "serial_number", ""))
    expected_serial = _normalized(identity.serial_number)
    if expected_serial and candidate_serial:
        return candidate_serial == expected_serial

    candidate_location = _normalized(getattr(candidate, "location", ""))
    expected_location = _normalized(identity.location)
    if expected_location and candidate_location:
        return candidate_location == expected_location

    return False


def _find_same_native_device(
    identity: NativeUsbIdentity,
) -> Any | None:
    candidates = [
        item
        for item in list_ports.comports()
        if _is_native_espressif_usb(item)
    ]

    strong_matches = [
        item
        for item in candidates
        if _strong_identity_match(item, identity)
    ]
    if len(strong_matches) == 1:
        return strong_matches[0]
    if len(strong_matches) > 1:
        return None

    # If Windows kept the same COM number, it is safe to use that exact port
    # when VID/PID still match.
    same_port = [
        item
        for item in candidates
        if _normalized(getattr(item, "device", "")) == _normalized(identity.device)
        and (
            identity.pid is None
            or getattr(item, "pid", None) is None
            or int(getattr(item, "pid")) == identity.pid
        )
    ]
    if len(same_port) == 1:
        return same_port[0]

    # Last resort only when the original USB device exposed neither a serial
    # number nor a physical USB location. Never guess between multiple devices.
    if not identity.serial_number and not identity.location:
        same_vid_pid = [
            item
            for item in candidates
            if (
                identity.pid is None
                or getattr(item, "pid", None) is None
                or int(getattr(item, "pid")) == identity.pid
            )
        ]
        if len(same_vid_pid) == 1:
            return same_vid_pid[0]

    return None


def _log_and_process_line(
    test_runner_module: Any,
    state: Any,
    serial_log: Any,
    raw: bytes,
) -> tuple[str, dict[str, str]] | None:
    line = raw.decode("utf-8", errors="replace").strip()
    timestamp = datetime.now().isoformat(timespec="milliseconds")
    serial_log.write(f"{timestamp} {line}\n")
    serial_log.flush()

    parsed = test_runner_module.parse_protocol_line(line)
    if parsed:
        test_runner_module.process_protocol_event(
            state,
            parsed[0],
            parsed[1],
            time.time(),
        )
    return parsed


def _wait_for_sleep_start_or_disconnect(
    test_runner_module: Any,
    serial_port: serial.Serial,
    state: Any,
    serial_log: Any,
    timeout: float,
) -> str:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            raw = serial_port.readline()
        except (serial.SerialException, OSError):
            # Native ESP USB-Serial/JTAG is physically removed from the host
            # when the chip enters deep sleep. This is expected here.
            return "DISCONNECTED"

        if not raw:
            continue

        parsed = _log_and_process_line(
            test_runner_module,
            state,
            serial_log,
            raw,
        )
        if (
            parsed
            and parsed[0] == "DEEP_SLEEP"
            and parsed[1].get("status") == "START"
        ):
            return "START"

        if state.self_tests.get("DEEP_SLEEP") == "FAIL":
            return "FAIL"

    return "TIMEOUT"


def _close_serial_quietly(serial_port: serial.Serial) -> None:
    try:
        serial_port.close()
    except (serial.SerialException, OSError):
        pass


def _wait_for_native_usb_return(
    test_runner_module: Any,
    identity: NativeUsbIdentity,
    baudrate: int,
    command_sent_at: float,
    already_disconnected: bool,
    timeout: float,
) -> serial.Serial:
    deadline = time.monotonic() + timeout
    disappearance_seen = already_disconnected

    # Firmware reports START, flushes Serial, waits 50 ms and then sleeps for
    # 1000 ms. If polling happens to miss the short USB-absent interval, never
    # reopen the still-old device before a complete sleep/wake cycle could have
    # elapsed.
    earliest_reopen_without_seen_disconnect = command_sent_at + 1.10

    while time.monotonic() < deadline:
        current = _find_same_native_device(identity)

        if current is None:
            disappearance_seen = True
            time.sleep(0.10)
            continue

        if (
            not disappearance_seen
            and time.monotonic() < earliest_reopen_without_seen_disconnect
        ):
            time.sleep(0.10)
            continue

        device = str(getattr(current, "device", "") or "").strip()
        if not device:
            time.sleep(0.10)
            continue

        try:
            return test_runner_module.open_serial_without_reset(
                device,
                baudrate,
                timeout=0.15,
                write_timeout=2.0,
            )
        except (serial.SerialException, OSError):
            time.sleep(0.10)

    raise RuntimeError(
        "Native Espressif USB serial port did not return after deep sleep."
    )


def _run_native_usb_deep_sleep_cycle(
    test_runner_module: Any,
    serial_port: serial.Serial,
    port: str,
    baudrate: int,
    state: Any,
    serial_log: Any,
    timeout: float,
    identity: NativeUsbIdentity,
) -> serial.Serial:
    print("Deep-sleep / RTC wake-up test ...")
    print(
        "  Native Espressif USB detected; "
        "USB disconnect/re-enumeration during deep sleep is expected."
    )

    state.deep_sleep_requested = True
    serial_port.write(b"DEEP_SLEEP_TEST\n")
    serial_port.flush()
    command_sent_at = time.monotonic()

    outcome = _wait_for_sleep_start_or_disconnect(
        test_runner_module,
        serial_port,
        state,
        serial_log,
        timeout=4.0,
    )

    if outcome == "FAIL":
        return serial_port

    if outcome == "TIMEOUT":
        if state.self_tests.get("DEEP_SLEEP") != "FAIL":
            state.self_tests["DEEP_SLEEP"] = "FAIL"
            state.self_test_reasons["DEEP_SLEEP"] = "sleep_not_started"
        return serial_port

    # START was received, or Windows already removed the native USB device.
    # Either case means the following boot is expected and must not be counted
    # as an unexpected board restart.
    state.planned_reboots += 1
    state.pending_planned_reboots += 1

    _close_serial_quietly(serial_port)

    reopened = _wait_for_native_usb_return(
        test_runner_module,
        identity,
        baudrate,
        command_sent_at,
        already_disconnected=(outcome == "DISCONNECTED"),
        timeout=timeout,
    )

    reopened_port = str(getattr(reopened, "port", "") or "")
    if reopened_port and _normalized(reopened_port) != _normalized(port):
        print(f"  Native USB returned as {reopened_port} (was {port}).")

    try:
        test_runner_module.wait_for_firmware(
            reopened,
            state,
            serial_log,
            timeout=timeout,
        )
    except Exception:
        _close_serial_quietly(reopened)
        raise

    if state.self_tests.get("DEEP_SLEEP") != "PASS":
        state.self_tests["DEEP_SLEEP"] = "FAIL"
        state.self_test_reasons.setdefault(
            "DEEP_SLEEP",
            "wake_verification_failed",
        )
    else:
        print("  Deep-sleep wake-up: PASS")

    return reopened


def install(test_runner_module: Any) -> None:
    """Handle deep-sleep USB re-enumeration only for native Espressif USB."""
    if getattr(
        test_runner_module,
        "_native_usb_deep_sleep_installed",
        False,
    ):
        return

    original_run_deep_sleep_cycle = test_runner_module.run_deep_sleep_cycle

    def run_deep_sleep_cycle(
        serial_port: serial.Serial,
        port: str,
        baudrate: int,
        state: Any,
        serial_log: Any,
        timeout: float = 20.0,
    ) -> serial.Serial:
        port_info = _find_port_info(port)

        # Preserve the existing, proven ESP32/S3 path byte-for-byte in
        # behavior. CH340/CP210x/FTDI and other USB-UART bridges never enter
        # this native-USB branch.
        if not _is_native_espressif_usb(port_info):
            return original_run_deep_sleep_cycle(
                serial_port,
                port,
                baudrate,
                state,
                serial_log,
                timeout,
            )

        identity = _capture_identity(port_info)
        return _run_native_usb_deep_sleep_cycle(
            test_runner_module,
            serial_port,
            port,
            baudrate,
            state,
            serial_log,
            timeout,
            identity,
        )

    test_runner_module.run_deep_sleep_cycle = run_deep_sleep_cycle
    test_runner_module._native_usb_deep_sleep_installed = True
