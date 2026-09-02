from __future__ import annotations

import base64
import copy
import csv
import html
import json
import os
import queue
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from board_ids import (
    board_id_number,
    normalize_board_id,
    try_normalize_board_id,
)

import serial
from serial.tools import list_ports


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / "config" / "test-settings.json"
SECRETS_PATH = ROOT / "secrets.ini"
BOARD_REGISTRY_PATH = ROOT / "config" / "board-registry.local.json"
RESULTS_ROOT = ROOT / "results"
GENERATED_TEST_CONFIG_PATH = ROOT / "firmware" / "include" / "test_config.generated.h"
FIRMWARE_TEST_MACROS = {
    "ram": "HWTEST_RUN_RAM",
    "psram": "HWTEST_RUN_PSRAM",
    "flash": "HWTEST_RUN_FLASH",
    "cpu": "HWTEST_RUN_CPU",
    "timer": "HWTEST_RUN_TIMER",
    "rng": "HWTEST_RUN_RNG",
    "nvs": "HWTEST_RUN_NVS",
    "ble": "HWTEST_RUN_BLE",
}

QUALITY_REQUIRED_TESTS = (
    "ram",
    "psram",
    "flash",
    "cpu",
    "timer",
    "rng",
    "nvs",
    "ble",
    "deep_sleep",
    "wifi",
    "rf_quality",
    "soak",
    "ping",
    "udp",
    "reconnect",
    "ble_coexistence",
    "heap_integrity",
)


@dataclass
class ProbeResult:
    timestamp: float
    kind: str
    sequence: int
    success: bool
    latency_ms: float | None = None
    detail: str = ""


@dataclass
class RunState:
    protocol_events: list[dict[str, Any]] = field(default_factory=list)
    self_tests: dict[str, str] = field(default_factory=dict)
    self_test_reasons: dict[str, str] = field(default_factory=dict)
    self_test_metrics: dict[str, dict[str, str]] = field(default_factory=dict)
    system_info: dict[str, str] = field(default_factory=dict)
    serial_boot_ids: list[str] = field(default_factory=list)
    heartbeat_samples: list[dict[str, Any]] = field(default_factory=list)
    udp_heartbeat_samples: list[dict[str, Any]] = field(default_factory=list)
    disconnect_reasons: list[str] = field(default_factory=list)
    wifi_ip: str = ""
    bssid: str = ""
    channel: int | None = None
    wifi_scan_aps: int | None = None
    wifi_scan_target_rssi: int | None = None
    wifi_scan_target_channel: int | None = None
    wifi_scan_target_bssid: str = ""
    connected: bool = False
    serial_error: str = ""
    selftest_complete: bool = False
    network_stats_reset: bool = False
    heap_integrity_checks: list[dict[str, Any]] = field(default_factory=list)
    ble_coex_events: list[dict[str, Any]] = field(default_factory=list)
    reconnect_events: list[dict[str, Any]] = field(default_factory=list)
    reconnect_test: dict[str, Any] = field(default_factory=dict)
    ble_coexistence: dict[str, Any] = field(default_factory=dict)
    rf_events: list[dict[str, Any]] = field(default_factory=list)
    rf_quality: dict[str, Any] = field(default_factory=dict)
    planned_reboots: int = 0
    pending_planned_reboots: int = 0
    current_phase: str = "startup"
    unexpected_restarts_by_phase: dict[str, int] = field(default_factory=dict)
    deep_sleep_requested: bool = False
    deep_sleep_completed: bool = False


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_key_value_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(
            "secrets.ini is missing. Copy secrets.example.ini to secrets.ini "
            "and enter WIFI_SSID and WIFI_PASSWORD."
        )

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        if "=" not in line:
            raise RuntimeError(
                f"Invalid line {line_number} in secrets.ini. Expected NAME=VALUE."
            )

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            raise RuntimeError(
                f"Invalid line {line_number} in secrets.ini. Expected NAME=VALUE."
            )
        values[name] = value

    return values


def resolve_wifi_credentials(
    test_config: dict[str, bool],
) -> tuple[str, str]:
    """Return WiFi credentials only when the resolved profile needs WiFi."""
    if not bool(test_config.get("wifi", False)):
        return "", ""

    ssid = os.environ.get("ESP_TEST_WIFI_SSID", "").strip()
    password_is_set = "ESP_TEST_WIFI_PASSWORD" in os.environ
    password = os.environ.get("ESP_TEST_WIFI_PASSWORD", "")

    if ssid and password_is_set:
        return ssid, password

    values = _read_key_value_file(SECRETS_PATH)

    if not ssid:
        ssid = values.get("WIFI_SSID", "").strip()
    if not password_is_set:
        if "WIFI_PASSWORD" not in values:
            raise RuntimeError("WIFI_PASSWORD is missing in secrets.ini.")
        password = values["WIFI_PASSWORD"]

    if not ssid:
        raise RuntimeError("WIFI_SSID is missing in secrets.ini.")

    return ssid, password


def requires_ping_command(test_config: dict[str, bool]) -> bool:
    """Whether an enabled host-side test needs the system ping executable."""
    return bool(
        test_config.get("reconnect", False)
        or test_config.get("ble_coexistence", False)
        or (
            test_config.get("soak", False)
            and test_config.get("ping", False)
        )
    )


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge_dict(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _settings_object(settings: dict[str, Any], key: str) -> dict[str, Any]:
    value = settings.get(key, {})
    if not isinstance(value, dict):
        raise RuntimeError(f"test-settings.json: '{key}' must be an object.")
    return value


def resolve_test_settings(settings: dict[str, Any], environment: str) -> dict[str, Any]:
    """Resolve nested test options for one PlatformIO profile.

    Simple hardware tests remain booleans. Tests with their own parameters are
    objects with an `enabled` switch. Profile objects are recursively merged over
    `tests.default`, so a profile can override one option without duplicating the
    complete test configuration.
    """
    allowed_root = {"label", "serial", "network", "peer_comparison", "tests"}
    unknown_root = sorted(set(settings) - allowed_root)
    if unknown_root:
        raise RuntimeError(
            "test-settings.json: unknown top-level setting(s): "
            + ", ".join(unknown_root)
        )

    serial_settings = _settings_object(settings, "serial")
    unknown_serial = sorted(set(serial_settings) - {"dut_baud", "reference_baud"})
    if unknown_serial:
        raise RuntimeError(
            "test-settings.json: unknown serial setting(s): "
            + ", ".join(unknown_serial)
        )

    network_settings = _settings_object(settings, "network")
    unknown_network = sorted(set(network_settings) - {"udp_port", "ping_timeout_ms"})
    if unknown_network:
        raise RuntimeError(
            "test-settings.json: unknown network setting(s): "
            + ", ".join(unknown_network)
        )

    peer_settings = _settings_object(settings, "peer_comparison")
    unknown_peer = sorted(
        set(peer_settings) - {"minimum_samples", "warn_ratio", "outlier_ratio"}
    )
    if unknown_peer:
        raise RuntimeError(
            "test-settings.json: unknown peer_comparison setting(s): "
            + ", ".join(unknown_peer)
        )

    tests_section = _settings_object(settings, "tests")
    default_section = tests_section.get("default")
    profiles_section = tests_section.get("profiles", {})
    if not isinstance(default_section, dict):
        raise RuntimeError("test-settings.json: 'tests.default' must be an object.")
    if not isinstance(profiles_section, dict):
        raise RuntimeError("test-settings.json: 'tests.profiles' must be an object.")

    simple_tests = {
        "ram", "psram", "flash", "cpu", "timer", "rng", "nvs", "ble", "deep_sleep"
    }
    configured_tests = {"wifi", "rf_quality", "soak", "reconnect", "ble_coexistence"}
    allowed = simple_tests | configured_tests

    unknown = sorted(set(default_section) - allowed)
    if unknown:
        raise RuntimeError(
            "test-settings.json: unknown tests in tests.default: " + ", ".join(unknown)
        )

    profile_section = profiles_section.get(environment, {})
    if not isinstance(profile_section, dict):
        raise RuntimeError(
            f"test-settings.json: tests.profiles.{environment} must be an object."
        )
    unknown = sorted(set(profile_section) - allowed)
    if unknown:
        raise RuntimeError(
            f"test-settings.json: unknown tests in tests.profiles.{environment}: "
            + ", ".join(unknown)
        )

    for key in simple_tests:
        if key not in default_section or not isinstance(default_section[key], bool):
            raise RuntimeError(
                f"test-settings.json: tests.default.{key} must be true or false."
            )
        if key in profile_section and not isinstance(profile_section[key], bool):
            raise RuntimeError(
                f"test-settings.json: tests.profiles.{environment}.{key} must be true or false."
            )

    for key in configured_tests:
        if key not in default_section or not isinstance(default_section[key], dict):
            raise RuntimeError(
                f"test-settings.json: tests.default.{key} must be an object with an enabled switch."
            )
        if key in profile_section and not isinstance(profile_section[key], dict):
            raise RuntimeError(
                f"test-settings.json: tests.profiles.{environment}.{key} must be an object."
            )

    resolved = _deep_merge_dict(default_section, profile_section)

    for key in configured_tests:
        section = resolved.get(key, {})
        if not isinstance(section.get("enabled"), bool):
            raise RuntimeError(
                f"test-settings.json: resolved tests.{key}.enabled must be true or false."
            )

    allowed_options = {
        "wifi": {"enabled", "warmup_seconds", "output_power_dbm"},
        "rf_quality": {
            "enabled", "reference_mac", "channel", "reference_tx_power_dbm",
            "packets_per_repetition", "packet_interval_ms",
            "repetitions_per_direction", "thresholds",
        },
        "soak": {
            "enabled", "duration_minutes", "probe_interval_seconds", "ping",
            "udp", "heap_integrity", "thresholds",
        },
        "reconnect": {
            "enabled", "timeout_seconds", "recovery_timeout_seconds",
            "settle_seconds",
        },
        "ble_coexistence": {
            "enabled", "duration_seconds", "probe_interval_seconds",
        },
    }
    wifi_power = resolved["wifi"].get("output_power_dbm")
    if wifi_power is not None and (
        isinstance(wifi_power, bool) or not isinstance(wifi_power, int) or not 2 <= wifi_power <= 20
    ):
        raise RuntimeError(
            "test-settings.json: tests.*.wifi.output_power_dbm "
            "must be null or an integer from 2 to 20."
        )

    for key, allowed_keys in allowed_options.items():
        unknown_options = sorted(set(resolved[key]) - allowed_keys)
        if unknown_options:
            raise RuntimeError(
                f"test-settings.json: unknown option(s) in tests.{key}: "
                + ", ".join(unknown_options)
            )

    soak = resolved["soak"]
    if not isinstance(soak.get("ping"), bool):
        raise RuntimeError("test-settings.json: tests.*.soak.ping must be true or false.")
    if not isinstance(soak.get("udp"), bool):
        raise RuntimeError("test-settings.json: tests.*.soak.udp must be true or false.")
    heap = soak.get("heap_integrity")
    if not isinstance(heap, dict) or not isinstance(heap.get("enabled"), bool):
        raise RuntimeError(
            "test-settings.json: tests.*.soak.heap_integrity must be an object with enabled=true/false."
        )
    if not isinstance(soak.get("thresholds"), dict):
        raise RuntimeError("test-settings.json: tests.*.soak.thresholds must be an object.")
    if not isinstance(resolved["rf_quality"].get("thresholds"), dict):
        raise RuntimeError("test-settings.json: tests.*.rf_quality.thresholds must be an object.")

    heap_unknown = sorted(set(heap) - {"enabled", "interval_seconds"})
    if heap_unknown:
        raise RuntimeError(
            "test-settings.json: unknown option(s) in tests.soak.heap_integrity: "
            + ", ".join(heap_unknown)
        )
    soak_threshold_keys = {
        "ping_loss_warn_percent", "ping_loss_fail_percent",
        "udp_loss_warn_percent", "udp_loss_fail_percent",
        "longest_outage_warn_seconds", "longest_outage_fail_seconds",
        "disconnects_warn", "disconnects_fail", "rssi_warn_dbm",
        "heap_drop_warn_bytes", "heap_drop_fail_bytes",
        "serial_heartbeat_warn_seconds", "serial_heartbeat_fail_seconds",
    }
    unknown_soak_thresholds = sorted(set(soak["thresholds"]) - soak_threshold_keys)
    if unknown_soak_thresholds:
        raise RuntimeError(
            "test-settings.json: unknown soak threshold(s): "
            + ", ".join(unknown_soak_thresholds)
        )
    rf_threshold_keys = {
        "reference_to_dut_min_rssi_dbm",
        "dut_to_reference_min_rssi_dbm",
        "max_loss_percent",
    }
    unknown_rf_thresholds = sorted(
        set(resolved["rf_quality"]["thresholds"]) - rf_threshold_keys
    )
    if unknown_rf_thresholds:
        raise RuntimeError(
            "test-settings.json: unknown RF threshold(s): "
            + ", ".join(unknown_rf_thresholds)
        )

    return resolved


def resolve_test_config(settings: dict[str, Any], environment: str) -> dict[str, bool]:
    resolved_settings = resolve_test_settings(settings, environment)
    soak = resolved_settings["soak"]
    heap = soak["heap_integrity"]

    resolved: dict[str, bool] = {
        key: bool(resolved_settings[key])
        for key in ("ram", "psram", "flash", "cpu", "timer", "rng", "nvs", "ble", "deep_sleep")
    }
    for key in ("wifi", "rf_quality", "soak", "reconnect", "ble_coexistence"):
        resolved[key] = bool(resolved_settings[key]["enabled"])
    resolved["ping"] = bool(soak["ping"])
    resolved["udp"] = bool(soak["udp"])
    resolved["heap_integrity"] = bool(heap["enabled"])

    wifi_dependents = ["soak", "reconnect", "ble_coexistence"]
    if not resolved["wifi"] and any(resolved[name] for name in wifi_dependents):
        raise RuntimeError(
            "test-settings.json: soak/reconnect/ble_coexistence cannot be enabled when wifi.enabled=false."
        )
    if resolved["ble_coexistence"] and not resolved["ble"]:
        raise RuntimeError(
            "test-settings.json: ble_coexistence.enabled=true requires ble=true."
        )

    return resolved


def resolve_execution_settings(settings: dict[str, Any], environment: str) -> dict[str, Any]:
    """Resolve the grouped public configuration for one PlatformIO profile."""
    return {
        "serial": copy.deepcopy(_settings_object(settings, "serial")),
        "network": copy.deepcopy(_settings_object(settings, "network")),
        "peer_comparison": copy.deepcopy(_settings_object(settings, "peer_comparison")),
        "tests": resolve_test_settings(settings, environment),
    }

def is_complete_quality_suite(tests: dict[str, bool]) -> bool:
    return all(bool(tests.get(name, False)) for name in QUALITY_REQUIRED_TESTS)


def resolve_label_mode(settings: dict[str, Any]) -> str:
    label = settings.get("label", {})
    if not isinstance(label, dict):
        raise RuntimeError("test-settings.json: 'label' must be an object.")
    mode = str(label.get("mode", "off")).strip().lower()
    if mode not in {"auto", "ask", "off"}:
        raise RuntimeError(
            "test-settings.json: 'label.mode' must be 'auto', 'ask', or 'off'."
        )
    return mode


def should_run_label(label_mode: str) -> bool:
    if label_mode not in {"auto", "ask", "off"}:
        raise RuntimeError(
            "label_mode must be 'auto', 'ask', or 'off'."
        )
    return label_mode != "off"


def write_generated_test_config(environment: str, tests: dict[str, bool]) -> None:
    GENERATED_TEST_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "#pragma once",
        "",
        "// Automatically generated by tools/test_runner.py.",
        "// Do not edit manually; source is config/test-settings.json.",
        f"// Active PlatformIO profile: {environment}",
        "",
    ]

    for key, macro in FIRMWARE_TEST_MACROS.items():
        lines.append(f"#define {macro} {1 if tests[key] else 0}")

    lines.append("")
    GENERATED_TEST_CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")


def run_command(args: list[str], timeout: int = 120, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd or ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def esptool_flash_id(port: str, attempts: int = 3) -> tuple[bool, str]:
    failures: list[str] = []

    for attempt in range(1, max(1, attempts) + 1):
        try:
            flash_result = run_command(
                [sys.executable, "-m", "esptool", "--port", port, "flash_id"],
                timeout=20,
            )
            output = flash_result.stdout or ""
            detected = flash_result.returncode == 0 and bool(
                re.search(r"ESP32", output, re.IGNORECASE)
            )

            if detected:
                mac_result = run_command(
                    [sys.executable, "-m", "esptool", "--port", port, "read_mac"],
                    timeout=20,
                )
                if mac_result.returncode == 0 and mac_result.stdout:
                    output = output.rstrip() + "\n" + mac_result.stdout

                return True, output

            failures.append(
                f"Attempt {attempt}/{attempts}, exit code {flash_result.returncode}:\n"
                f"{output.strip() or '(no esptool output)'}"
            )
        except Exception as exc:
            failures.append(
                f"Attempt {attempt}/{attempts}: {type(exc).__name__}: {exc}"
            )

        if attempt < attempts:
            time.sleep(0.75)

    return False, "\n\n".join(failures)


def likely_esp_port(port_info: Any) -> bool:
    text = " ".join(
        str(value or "")
        for value in [
            port_info.device,
            port_info.description,
            port_info.manufacturer,
            port_info.product,
            port_info.hwid,
        ]
    ).lower()

    # Windows also creates virtual COM ports for Bluetooth/RFCOMM.
    # These must never be opened by esptool during automatic ESP32 detection.
    # Localized Windows device names are intentionally retained as match data.
    excluded_hints = [
        "bluetooth",
        "rfcomm",
        "seriell-ueber-bluetooth",
        "seriell-über-bluetooth",
        "serial over bluetooth",
        "standard serial over bluetooth",
    ]
    if any(hint in text for hint in excluded_hints):
        return False

    # USB VIDs used by the USB/UART bridges commonly found on these ESP32 boards.
    known_vids = {
        0x303A,  # Espressif USB/JTAG/CDC
        0x1A86,  # WCH: CH340/CH341/CH343/CH910x
        0x10C4,  # Silicon Labs: CP210x
        0x0403,  # FTDI
    }

    known_hints = [
        "espressif",
        "esp32",
        "usb jtag",
        "usb-jtag",
        "jtag/serial",
        "jtag serial",
        "cp210",
        "silicon labs",
        "ch340",
        "ch341",
        "ch343",
        "ch910",
        "wch",
        "usb-enhanced-serial",
        "usb enhanced serial",
        "ftdi",
    ]

    return port_info.vid in known_vids or any(hint in text for hint in known_hints)


def detect_esp_port(
    requested_port: str,
    exclude_ports: set[str] | None = None,
) -> tuple[str, str]:
    excluded = {item.lower() for item in (exclude_ports or set())}
    if requested_port:
        if requested_port.lower() in excluded:
            raise RuntimeError(
                f"{requested_port} is the reference ESP port and cannot be used as DUT."
            )
        ok, output = esptool_flash_id(requested_port)
        if not ok:
            raise RuntimeError(f"No responsive ESP32 was found on {requested_port}.\n{output}")
        return requested_port, output

    ports = [
        item
        for item in list_ports.comports()
        if likely_esp_port(item) and item.device.lower() not in excluded
    ]
    if not ports:
        raise RuntimeError(
            "No suitable ESP32 USB port was found. Connect the ESP32 and try again. "
            "Bluetooth/RFCOMM and other virtual COM ports are intentionally ignored."
        )

    found: list[tuple[Any, str]] = []
    failed: list[tuple[Any, str]] = []

    print("Searching for ESP32 automatically ...")
    for item in ports:
        print(f"  checking {item.device} ({item.description})")
        ok, output = esptool_flash_id(item.device)

        if ok:
            print(f"    OK: ESP32 detected on {item.device}")
            found.append((item, output))
        else:
            print(f"    ERROR: esptool could not communicate with {item.device}.")
            failed.append((item, output))

    if not found:
        details = []

        for item, output in failed:
            details.append(
                "\n".join(
                    [
                        "",
                        f"--- {item.device} / {item.description} ---",
                        output.strip() or "(no esptool output)",
                    ]
                )
            )

        detail_text = "\n".join(details)

        combined_output = "\n".join(output for _item, output in failed).lower()
        if (
            "permissionerror(13" in combined_output
            or "cannot configure port" in combined_output
            # Localized Windows error text retained for detection.
            or "ein an das system angeschlossenes geraet funktioniert nicht" in combined_output
        ):
            first_item = failed[0][0]
            raise RuntimeError(
                f"{first_item.device} / {first_item.description} cannot be opened or "
                "configured by Windows. Disconnect the board from USB briefly, "
                "reconnect it, and try again."
                f"{detail_text}"
            )

        raise RuntimeError(
            "A suitable COM port was found, but esptool could not put an ESP32 "
            "into the bootloader or synchronize with it."
            f"{detail_text}"
        )
    if len(found) == 1:
        item, output = found[0]
        return item.device, output

    print("\nMultiple ESP32 devices found:")
    for index, (item, _) in enumerate(found, start=1):
        print(f"  {index}: {item.device} - {item.description}")

    while True:
        try:
            choice = int(input("Number of the board to test: ").strip())
            if 1 <= choice <= len(found):
                item, output = found[choice - 1]
                return item.device, output
        except ValueError:
            pass
        print("Enter only one of the displayed numbers.")


def parse_esptool_info(output: str) -> dict[str, Any]:
    info: dict[str, Any] = {"raw": output}

    patterns = {
        "chip_family": [
            r"Chip type:\s*(ESP32(?:-[A-Z0-9]+)?)",
            r"Detecting chip type\.\.\.\s*(ESP32(?:-[A-Z0-9]+)?)",
            r"Chip is\s+(ESP32(?:-[A-Z0-9]+)?)",
        ],
        "revision": [r"revision\s+v([0-9.]+)", r"revision:\s*([^\r\n]+)"],
        "mac": [r"MAC:\s*([0-9a-fA-F:]{17})"],
        "flash_size": [r"Detected flash size:\s*([^\r\n]+)"],
        "flash_manufacturer": [r"Manufacturer:\s*([^\r\n]+)"],
        "flash_device": [r"Device:\s*([^\r\n]+)"],
        "features": [r"Features:\s*([^\r\n]+)"],
    }

    for key, candidates in patterns.items():
        for pattern in candidates:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                info[key] = match.group(1).strip()
                break

    psram_match = re.search(r"Embedded PSRAM\s+(\d+)\s*MB", output, re.IGNORECASE)
    info["embedded_psram_mb"] = int(psram_match.group(1)) if psram_match else 0
    return info


def choose_environment(info: dict[str, Any]) -> str:
    chip = str(info.get("chip_family", "")).upper()

    if chip == "ESP32":
        return "esp32"

    if chip == "ESP32-C3":
        return "esp32-c3"

    if chip == "ESP32-S3":
        psram_mb = int(info.get("embedded_psram_mb", 0))
        flash_size = str(info.get("flash_size", "")).upper().replace(" ", "")

        if psram_mb == 0:
            return "esp32-s3"

        if psram_mb == 8 and flash_size in {"16MB", "16M"}:
            return "esp32-s3-n16r8"

        raise RuntimeError(
            f"ESP32-S3 configuration is not defined yet: "
            f"Flash={flash_size or 'unknown'}, PSRAM={psram_mb} MB. "
            "Do not continue with a guessed PSRAM profile."
        )

    raise RuntimeError(
        f"Chip {chip or 'unknown'} is not yet supported automatically. "
        "Supported chips are ESP32, ESP32-C3, and the defined ESP32-S3 variants."
    )


def board_id_prefix(info: dict[str, Any]) -> str:
    chip = str(info.get("chip_family", "")).upper()
    if chip == "ESP32":
        return "E32"
    if chip == "ESP32-C3":
        return "C3"
    if chip == "ESP32-S3":
        return "S3"
    return "ESP"


def _write_board_registry(registry: dict[str, Any]) -> None:
    BOARD_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    BOARD_REGISTRY_PATH.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_board_registry() -> dict[str, Any]:
    if not BOARD_REGISTRY_PATH.exists():
        return {"boards": {}, "next_numbers": {}, "reserved_ids": []}

    try:
        registry = load_json(BOARD_REGISTRY_PATH)
    except (OSError, json.JSONDecodeError):
        return {"boards": {}, "next_numbers": {}, "reserved_ids": []}

    if not isinstance(registry.get("boards"), dict):
        registry["boards"] = {}
    if not isinstance(registry.get("next_numbers"), dict):
        registry["next_numbers"] = {}
    if not isinstance(registry.get("reserved_ids"), list):
        registry["reserved_ids"] = []

    changed = False
    reserved_ids: set[str] = set()
    unknown_reserved: set[str] = set()

    # Old TYPE-NNN IDs are migrated automatically to the new canonical NNN-TYPE
    # format. Legacy input remains readable for compatibility only.
    for value in registry["reserved_ids"]:
        raw = str(value or "").strip().upper()
        if not raw:
            continue

        normalized = try_normalize_board_id(raw)
        if normalized:
            reserved_ids.add(normalized)
            changed = changed or normalized != raw
        else:
            unknown_reserved.add(raw)

    for entry in registry["boards"].values():
        if not isinstance(entry, dict):
            continue

        raw = str(entry.get("board_id") or "").strip().upper()
        if not raw:
            continue

        normalized = try_normalize_board_id(raw)
        if normalized:
            reserved_ids.add(normalized)
            if normalized != raw:
                entry["board_id"] = normalized
                changed = True

    new_reserved = sorted(reserved_ids | unknown_reserved)
    if registry["reserved_ids"] != new_reserved:
        registry["reserved_ids"] = new_reserved
        changed = True

    reserved_numbers = {
        board_id_number(value)
        for value in reserved_ids
    }
    highest_reserved = max(reserved_numbers, default=0)

    legacy_next_numbers: list[int] = []
    for value in registry["next_numbers"].values():
        try:
            legacy_next_numbers.append(int(value))
        except (TypeError, ValueError):
            pass

    global_next = max([1, highest_reserved + 1, *legacy_next_numbers])
    while global_next <= 999 and global_next in reserved_numbers:
        global_next += 1

    new_next_numbers = {"GLOBAL": global_next}
    if registry["next_numbers"] != new_next_numbers:
        registry["next_numbers"] = new_next_numbers
        changed = True

    if changed:
        _write_board_registry(registry)

    return registry


def normalize_mac(value: str) -> str:
    return value.strip().lower()


def validate_board_id(value: str, expected_prefix: str) -> str:
    try:
        return normalize_board_id(
            value,
            expected_type=expected_prefix,
            allow_number=True,
        )
    except ValueError as exc:
        raise ValueError(
            f"Invalid board ID. Allowed values are 1 to 999 or "
            f"001-{expected_prefix} to 999-{expected_prefix}. "
            f"The legacy format {expected_prefix}-001 is also accepted when reading."
        ) from exc


def get_or_assign_board_id(info: dict[str, Any]) -> str:
    registry = load_board_registry()
    boards: dict[str, Any] = registry["boards"]
    next_numbers: dict[str, Any] = registry["next_numbers"]

    reserved_ids = {
        normalized
        for value in registry.get("reserved_ids", [])
        if (normalized := try_normalize_board_id(str(value or "")))
    }

    mac = normalize_mac(str(info.get("mac") or ""))
    if mac and mac in boards:
        known_raw = str(boards[mac].get("board_id") or "").strip().upper()
        known_id = try_normalize_board_id(known_raw)
        if known_id:
            if known_id != known_raw:
                boards[mac]["board_id"] = known_id
                reserved_ids.add(known_id)
                registry["reserved_ids"] = sorted(reserved_ids)
                _write_board_registry(registry)
            print(f"Board ID: {known_id} (already known)")
            return known_id

    prefix = board_id_prefix(info)

    # Shared global number range for E32, C3, and S3.
    # Historical duplicate numbers remain unchanged; new global numbers are
    # never reused.
    reserved_numbers = {
        board_id_number(value)
        for value in reserved_ids
    }
    highest_reserved = max(reserved_numbers, default=0)

    legacy_next_numbers: list[int] = []
    for value in next_numbers.values():
        try:
            legacy_next_numbers.append(int(value))
        except (TypeError, ValueError):
            pass

    next_number = max([1, highest_reserved + 1, *legacy_next_numbers])

    while next_number <= 999 and next_number in reserved_numbers:
        next_number += 1

    if next_number > 999:
        raise RuntimeError("No free global board number from 001 to 999 remains.")

    suggested_id = f"{next_number:03d}-{prefix}"

    while True:
        entered = input(f"New board ID [{suggested_id}]: ").strip()
        try:
            board_id = validate_board_id(entered or suggested_id, prefix)
        except ValueError as exc:
            print(str(exc))
            continue

        assigned_number = board_id_number(board_id)

        if assigned_number in reserved_numbers:
            print(
                f"Board number {assigned_number:03d} is already assigned and remains permanently reserved. "
                "Use a different number."
            )
            continue

        break

    now = datetime.now().isoformat(timespec="seconds")
    reserved_ids.add(board_id)
    registry["reserved_ids"] = sorted(reserved_ids)

    if mac:
        boards[mac] = {
            "board_id": board_id,
            "chip_family": info.get("chip_family"),
            "created_at": now,
            "last_test_at": None,
            "last_result": None,
            "test_count": 0,
        }

    assigned_number = board_id_number(board_id)
    registry["next_numbers"] = {
        "GLOBAL": max(next_number + 1, assigned_number + 1)
    }

    _write_board_registry(registry)
    print(f"Board ID: {board_id}")
    return board_id


def update_board_registry_result(info: dict[str, Any], board_id: str, result: str) -> None:
    registry = load_board_registry()
    boards: dict[str, Any] = registry["boards"]
    mac = normalize_mac(str(info.get("mac") or ""))

    if not mac:
        return

    entry = boards.get(mac)
    if not isinstance(entry, dict):
        return

    stored_id = try_normalize_board_id(str(entry.get("board_id") or ""))
    current_id = normalize_board_id(board_id)
    if stored_id != current_id:
        return

    entry["board_id"] = current_id
    entry["last_test_at"] = datetime.now().isoformat(timespec="seconds")
    entry["last_result"] = result
    try:
        entry["test_count"] = int(entry.get("test_count", 0)) + 1
    except (TypeError, ValueError):
        entry["test_count"] = 1

    reserved_ids = {
        normalized
        for value in registry.get("reserved_ids", [])
        if (normalized := try_normalize_board_id(str(value or "")))
    }
    reserved_ids.add(current_id)
    registry["reserved_ids"] = sorted(reserved_ids)

    _write_board_registry(registry)


def safe_path_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned or "BOARD"



def cleanup_pending_result_dirs(board_id: str) -> None:
    prefix = f".pending_{normalize_board_id(board_id)}_"
    if not RESULTS_ROOT.exists():
        return

    for path in RESULTS_ROOT.iterdir():
        if path.is_dir() and path.name.startswith(prefix):
            shutil.rmtree(path)


def finalize_result_directory(run_dir: Path, board_id: str) -> Path:
    canonical_id = normalize_board_id(board_id)
    final_dir = RESULTS_ROOT / canonical_id
    backup_dir = RESULTS_ROOT / f".old_{canonical_id}_{int(time.time())}"

    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    old_moved = False
    try:
        if final_dir.exists():
            final_dir.rename(backup_dir)
            old_moved = True

        run_dir.rename(final_dir)
    except Exception:
        if old_moved and backup_dir.exists() and not final_dir.exists():
            backup_dir.rename(final_dir)
        raise

    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    return final_dir



def platformio_command(*arguments: str) -> list[str]:
    """Run PlatformIO from the same Python environment as the tester.

    The launchers install PlatformIO into the repository-local test environment.
    Using ``sys.executable -m platformio`` prevents a global PlatformIO
    installation from being mixed with the project-local ``core_dir``.
    """
    return [sys.executable, "-m", "platformio", *arguments]



def _run_platformio_build(
    command: list[str],
    cwd: Path,
    description: str,
    timeout: int = 900,
    process_env: dict[str, str] | None = None,
) -> None:
    print(f"  {description} ...")
    result = subprocess.run(
        command,
        cwd=str(cwd),
        timeout=timeout,
        creationflags=0,
        env=process_env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{description} failed. The PlatformIO error is shown directly above."
        )


def preflight_test_environment(
    environment: str,
    test_config: dict[str, bool],
    settings: dict[str, Any],
) -> None:
    """Verify host tools and the single Arduino test firmware build."""
    print("")
    print("Preflight check ...")

    if requires_ping_command(test_config):
        ping_executable = shutil.which("ping")
        if not ping_executable:
            raise RuntimeError(
                "The host 'ping' command is required by the enabled "
                "ping/reconnect/BLE coexistence tests but was not found in PATH."
            )
        print(f"  Host ping: PASS / {ping_executable}")
    else:
        print("  Host ping: SKIP / not required by selected tests")

    try:
        version_result = subprocess.run(
            platformio_command("--version"),
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"PlatformIO check failed: {exc}") from exc

    if version_result.returncode != 0:
        raise RuntimeError(
            "PlatformIO is installed but could not be executed successfully."
        )

    pio_version = (
        (version_result.stdout or version_result.stderr or "").strip().splitlines()
        or ["unknown version"]
    )[0]
    print(f"  PlatformIO: PASS / {pio_version}")

    write_generated_test_config(environment, test_config)
    try:
        _run_platformio_build(
            platformio_command("run", "-e", environment),
            ROOT,
            f"test firmware build ({environment})",
            timeout=600,
        )
    finally:
        try:
            GENERATED_TEST_CONFIG_PATH.unlink()
        except FileNotFoundError:
            pass

    print("  Test firmware: PASS")
    print("Preflight: PASS")
    print("")


def flash_firmware(port: str, environment: str, log_path: Path) -> None:
    command = platformio_command(
        "run",
        "-e",
        environment,
        "-t",
        "upload",
        "--upload-port",
        port,
    )

    print(f"Building and flashing firmware ({environment}) ...")
    print("PlatformIO/esptool output:")
    print("-" * 60)
    started = time.monotonic()

    # Intentionally do NOT redirect stdout/stderr.
    # PlatformIO and esptool write directly to the current console.
    # This preserves their native progress display and timing.
    result = subprocess.run(
        command,
        cwd=str(ROOT),
        timeout=600,
        creationflags=0,
    )

    elapsed = int(time.monotonic() - started)
    minutes, seconds = divmod(elapsed, 60)

    print("-" * 60)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                "PlatformIO/esptool output was displayed live in the console.",
                f"Environment: {environment}",
                f"Port: {port}",
                f"Exit code: {result.returncode}",
                f"Duration: {minutes:02d}:{seconds:02d}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    if result.returncode != 0:
        raise RuntimeError("Build/flash failed. The PlatformIO error output is shown directly above in the console.")

    print(f"Firmware flashed successfully ({minutes:02d}:{seconds:02d}).")

def parse_protocol_line(line: str) -> tuple[str, dict[str, str]] | None:
    if not line.startswith("HTEST|"):
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


def parse_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def process_protocol_event(state: RunState, category: str, values: dict[str, str], now: float) -> None:
    event = {"timestamp": now, "category": category, **values}
    state.protocol_events.append(event)

    if category == "SYSTEM":
        state.system_info.update(values)
    elif category == "BOOT":
        boot_id = values.get("id", "")
        if boot_id:
            is_restart = bool(state.serial_boot_ids)
            state.serial_boot_ids.append(boot_id)
            if is_restart:
                if state.pending_planned_reboots > 0:
                    state.pending_planned_reboots -= 1
                else:
                    phase = state.current_phase or "unknown"
                    state.unexpected_restarts_by_phase[phase] = (
                        state.unexpected_restarts_by_phase.get(phase, 0) + 1
                    )
    elif category == "TEST":
        name = values.get("name", "")
        status = values.get("status", "")
        if name and status in {"PASS", "FAIL", "SKIP"}:
            state.self_tests[name] = status
            state.self_test_metrics[name] = dict(values)
            reason = values.get("reason", "")
            if reason:
                state.self_test_reasons[name] = reason
            else:
                state.self_test_reasons.pop(name, None)
            if name == "DEEP_SLEEP":
                state.deep_sleep_completed = True
    elif category == "SELFTEST":
        state.selftest_complete = True
    elif category == "WIFI_EVENT":
        event_name = values.get("event", "")
        if event_name == "GOT_IP":
            state.wifi_ip = values.get("ip", state.wifi_ip)
            state.bssid = values.get("bssid", state.bssid)
            state.channel = parse_int(values.get("channel"), state.channel)
            state.connected = True
        elif event_name == "DISCONNECTED":
            state.connected = False
            if (
                values.get("phase", "runtime") != "startup"
                and values.get("counted", "1") != "0"
            ):
                state.disconnect_reasons.append(values.get("reason", "unknown"))
    elif category == "NET_STATS" and values.get("status") == "RESET":
        state.network_stats_reset = True
    elif category == "HEAP_CHECK":
        state.heap_integrity_checks.append({"timestamp": now, **values})
    elif category == "BLE_COEX":
        state.ble_coex_events.append({"timestamp": now, **values})
    elif category == "RECONNECT_TEST":
        state.reconnect_events.append({"timestamp": now, **values})
    elif category == "RF_DUT":
        state.rf_events.append({"timestamp": now, **values})
    elif category == "WIFI":
        if values.get("status") == "CONNECTED":
            state.connected = True
            state.wifi_ip = values.get("ip", state.wifi_ip)
            state.bssid = values.get("bssid", state.bssid)
            state.channel = parse_int(values.get("channel"), state.channel)
    elif category == "WIFI_SCAN" and values.get("status") in {"PASS", "FAIL"}:
        state.wifi_scan_aps = parse_int(values.get("aps"), state.wifi_scan_aps)
        state.wifi_scan_target_rssi = parse_int(values.get("target_rssi"), state.wifi_scan_target_rssi)
        state.wifi_scan_target_channel = parse_int(values.get("target_channel"), state.wifi_scan_target_channel)
        state.wifi_scan_target_bssid = values.get("target_bssid", state.wifi_scan_target_bssid)
    elif category == "HEARTBEAT":
        state.heartbeat_samples.append(
            {
                "timestamp": now,
                "wifi": parse_int(values.get("wifi"), 0),
                "rssi": parse_int(values.get("rssi"), -127),
                "heap_free": parse_int(values.get("heap_free"), 0),
                "heap_min": parse_int(values.get("heap_min"), 0),
                "disconnects": parse_int(values.get("disconnects"), 0),
                "reconnects": parse_int(values.get("reconnects"), 0),
                "uptime_ms": parse_int(values.get("uptime_ms"), 0),
            }
        )


def wait_for_firmware(serial_port: serial.Serial, state: RunState, serial_log: Any, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = serial_port.readline()
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace").strip()
        timestamp = datetime.now().isoformat(timespec="milliseconds")
        serial_log.write(f"{timestamp} {line}\n")
        serial_log.flush()
        parsed = parse_protocol_line(line)
        if parsed:
            process_protocol_event(state, parsed[0], parsed[1], time.time())
            if parsed[0] == "READY":
                return
    raise RuntimeError("The test firmware did not respond correctly over serial (READY is missing).")


def open_serial_without_reset(
    port: str,
    baudrate: int,
    timeout: float = 0.15,
    write_timeout: float = 2.0,
) -> serial.Serial:
    """Open an existing serial port without asserting DTR/RTS.

    This is required after the controlled deep-sleep wake-up. Opening a USB/UART
    bridge with the default DTR/RTS state can reset a classic ESP32 and hide the
    real ESP_RST_DEEPSLEEP reset reason.
    """
    serial_port = serial.Serial(
        port=None,
        baudrate=baudrate,
        timeout=timeout,
        write_timeout=write_timeout,
        rtscts=False,
        dsrdtr=False,
    )
    serial_port.dtr = False
    serial_port.rts = False
    serial_port.port = port
    serial_port.open()
    return serial_port


def send_wifi_config(
    serial_port: serial.Serial,
    ssid: str,
    password: str,
    output_power_dbm: int | None = None,
) -> None:
    encoded_ssid = base64.b64encode(ssid.encode("utf-8")).decode("ascii")
    encoded_password = base64.b64encode(password.encode("utf-8")).decode("ascii")
    if output_power_dbm is None:
        command = f"WIFI64|{encoded_ssid}|{encoded_password}\n"
    else:
        command = f"WIFI64|{encoded_ssid}|{encoded_password}|{output_power_dbm}\n"
    serial_port.write(command.encode("ascii"))
    serial_port.flush()


def determine_local_ip(remote_ip: str) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_ip, 9))
        return str(sock.getsockname()[0])
    finally:
        sock.close()


def parse_windows_ping_result(
    output: str,
    returncode: int,
) -> tuple[bool, float | None]:
    # Windows ping.exe may return exit code 0 for localized "destination host
    # unreachable" responses because an ICMP error packet was received. A real
    # echo reply from the ESP32 contains TTL=<value>.
    success = returncode == 0 and bool(
        re.search(r"\bttl\s*=", output, re.IGNORECASE)
    )

    if not success:
        return False, None

    # Keep both English and German keywords because ping.exe output is localized.
    match = re.search(
        r"(?:time|zeit)\s*([=<])\s*(\d+(?:[.,]\d+)?)\s*ms",
        output,
        re.IGNORECASE,
    )
    if not match:
        return True, None

    comparator = match.group(1)
    value = float(match.group(2).replace(",", "."))

    # "time<1ms" / localized equivalent is represented by a plausible midpoint
    # for statistics, preserving the previous behavior.
    if comparator == "<":
        value /= 2.0

    return True, value


def ping_once(ip: str, timeout_ms: int) -> tuple[bool, float | None, str]:
    if os.name == "nt":
        command = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        timeout_seconds = max(1, int((timeout_ms + 999) / 1000))
        command = ["ping", "-c", "1", "-W", str(timeout_seconds), ip]

    try:
        result = run_command(command, timeout=max(3, int(timeout_ms / 1000) + 3))
        output = result.stdout or ""

        if os.name == "nt":
            success, latency = parse_windows_ping_result(
                output,
                result.returncode,
            )
        else:
            success = result.returncode == 0
            match = re.search(
                r"(?:time|zeit)\s*([=<])\s*(\d+(?:[.,]\d+)?)\s*ms",
                output,
                re.IGNORECASE,
            )
            if success and match:
                latency = float(match.group(2).replace(",", "."))
                if match.group(1) == "<":
                    latency /= 2.0
            else:
                latency = None

        detail = output.strip().replace("\r", " ").replace("\n", " ")[-300:]
        return success, latency, detail
    except Exception as exc:
        return False, None, str(exc)


def ping_worker(ip: str, timeout_ms: int, interval: float, stop_event: threading.Event, output_queue: queue.Queue[ProbeResult]) -> None:
    sequence = 0
    next_time = time.monotonic()
    while not stop_event.is_set():
        now = time.monotonic()
        if now < next_time:
            stop_event.wait(min(0.1, next_time - now))
            continue
        sequence += 1
        started = time.time()
        success, latency, detail = ping_once(ip, timeout_ms)
        output_queue.put(ProbeResult(started, "icmp", sequence, success, latency, detail))
        next_time += interval


def read_serial_available(serial_port: serial.Serial, state: RunState, serial_log: Any) -> None:
    while serial_port.in_waiting > 0:
        raw = serial_port.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        timestamp = datetime.now().isoformat(timespec="milliseconds")
        serial_log.write(f"{timestamp} {line}\n")
        parsed = parse_protocol_line(line)
        if parsed:
            process_protocol_event(state, parsed[0], parsed[1], time.time())
    serial_log.flush()


def serial_reader_worker(
    serial_port: serial.Serial,
    stop_event: threading.Event,
    output_queue: queue.Queue[tuple[float, str, str]],
) -> None:
    """Read Serial continuously without relying on in_waiting."""
    while not stop_event.is_set():
        try:
            raw = serial_port.readline()
        except (serial.SerialException, OSError) as exc:
            output_queue.put((time.time(), "", str(exc)))
            return

        if not raw:
            continue

        line = raw.decode("utf-8", errors="replace").strip()
        output_queue.put((time.time(), line, ""))


def drain_serial_queue(
    state: RunState,
    serial_log: Any,
    input_queue: queue.Queue[tuple[float, str, str]],
) -> None:
    wrote_line = False

    while True:
        try:
            received_at, line, error = input_queue.get_nowait()
        except queue.Empty:
            break

        if error:
            state.serial_error = error
            continue

        timestamp = datetime.fromtimestamp(received_at).isoformat(timespec="milliseconds")
        serial_log.write(f"{timestamp} {line}\n")
        wrote_line = True

        parsed = parse_protocol_line(line)
        if parsed:
            process_protocol_event(state, parsed[0], parsed[1], received_at)

    if wrote_line:
        serial_log.flush()


def wait_for_wifi(serial_port: serial.Serial, state: RunState, serial_log: Any, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        read_serial_available(serial_port, state, serial_log)
        # wifi_ip is intentionally retained across disconnects for reporting.
        # A reconnect/recovery wait must therefore also require the live
        # connection state instead of accepting a stale IP from earlier.
        if state.connected and state.wifi_ip:
            return
        time.sleep(0.05)
    raise RuntimeError(
        f"The ESP32 did not obtain a WiFi IP address within {timeout:.0f} seconds."
    )


def run_wifi_warmup(
    serial_port: serial.Serial,
    state: RunState,
    serial_log: Any,
    seconds: float,
) -> None:
    warmup_seconds = max(0.0, float(seconds))
    if warmup_seconds <= 0:
        return

    print(f"WiFi warm-up: {warmup_seconds:.0f} seconds (not scored) ...")
    deadline = time.monotonic() + warmup_seconds
    next_message = time.monotonic()

    while time.monotonic() < deadline:
        read_serial_available(serial_port, state, serial_log)

        now = time.monotonic()
        if now >= next_message:
            remaining = max(0, int(round(deadline - now)))
            print(f"  Warm-up: {remaining:2d} s remaining")
            next_message = now + 10.0

        time.sleep(0.05)

    # The scored test must not start while a reconnect is still in progress.
    reconnect_deadline = time.monotonic() + 15.0
    while not state.connected and time.monotonic() < reconnect_deadline:
        read_serial_available(serial_port, state, serial_log)
        time.sleep(0.05)

    if not state.connected:
        raise RuntimeError(
            "WiFi is not stably connected after the warm-up phase."
        )


def reset_network_measurement(
    serial_port: serial.Serial,
    state: RunState,
    serial_log: Any,
    timeout: float = 3.0,
) -> None:
    state.network_stats_reset = False
    serial_port.write(b"RESET_NET_STATS\n")
    serial_port.flush()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        read_serial_available(serial_port, state, serial_log)
        if state.network_stats_reset:
            # Everything collected during association, warm-up and the
            # controlled setup activity is excluded from the diagnostic home-network soak.
            state.heartbeat_samples.clear()
            state.udp_heartbeat_samples.clear()
            state.disconnect_reasons.clear()
            state.heap_integrity_checks.clear()
            print("WiFi measurement counters reset to 0.")
            return
        time.sleep(0.05)

    raise RuntimeError(
        "Firmware did not confirm RESET_NET_STATS; WiFi measurement will not start."
    )



def run_deep_sleep_cycle(
    serial_port: serial.Serial,
    port: str,
    baudrate: int,
    state: RunState,
    serial_log: Any,
    timeout: float = 20.0,
) -> serial.Serial:
    print("Deep-sleep / RTC wake-up test ...")
    state.deep_sleep_requested = True
    event_start = len(state.protocol_events)
    serial_port.write(b"DEEP_SLEEP_TEST\n")
    serial_port.flush()

    start_seen = False
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        read_serial_available(serial_port, state, serial_log)
        for event in state.protocol_events[event_start:]:
            if event.get("category") == "DEEP_SLEEP" and event.get("status") == "START":
                start_seen = True
                break
        if start_seen or state.self_tests.get("DEEP_SLEEP") == "FAIL":
            break
        time.sleep(0.05)

    if not start_seen:
        if state.self_tests.get("DEEP_SLEEP") != "FAIL":
            state.self_tests["DEEP_SLEEP"] = "FAIL"
            state.self_test_reasons["DEEP_SLEEP"] = "sleep_not_started"
        return serial_port

    state.planned_reboots += 1
    state.pending_planned_reboots += 1
    try:
        serial_port.close()
    except Exception:
        pass

    reopen_deadline = time.monotonic() + timeout
    reopened: serial.Serial | None = None
    while time.monotonic() < reopen_deadline:
        try:
            reopened = open_serial_without_reset(
                port,
                baudrate,
                timeout=0.15,
                write_timeout=2,
            )
            break
        except (serial.SerialException, OSError):
            time.sleep(0.25)

    if reopened is None:
        state.self_tests["DEEP_SLEEP"] = "FAIL"
        state.self_test_reasons["DEEP_SLEEP"] = "serial_port_did_not_return"
        raise RuntimeError("ESP32 serial port did not return after deep sleep.")

    wait_for_firmware(reopened, state, serial_log, timeout=timeout)
    if state.self_tests.get("DEEP_SLEEP") != "PASS":
        state.self_tests["DEEP_SLEEP"] = "FAIL"
        state.self_test_reasons.setdefault("DEEP_SLEEP", "wake_verification_failed")
    else:
        print("  Deep-sleep wake-up: PASS")
    return reopened


def udp_roundtrip_once(
    udp_socket: socket.socket,
    ip: str,
    port: int,
    sequence: int,
    timeout: float = 1.0,
) -> tuple[bool, float | None]:
    payload = f"PING|{sequence}".encode("ascii")
    started = time.monotonic()
    try:
        udp_socket.sendto(payload, (ip, port))
    except OSError:
        return False, None

    deadline = started + timeout
    while time.monotonic() < deadline:
        try:
            data, _ = udp_socket.recvfrom(512)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        except OSError:
            return False, None

        text = data.decode("utf-8", errors="replace")
        if text.startswith(f"PONG|{sequence}|"):
            return True, (time.monotonic() - started) * 1000.0
    return False, None


def run_controlled_reconnect_test(
    serial_port: serial.Serial,
    state: RunState,
    serial_log: Any,
    udp_socket: socket.socket,
    settings: dict[str, Any],
) -> None:
    print("Controlled WiFi reconnect test ...")
    target_bssid = state.bssid.strip().lower()
    target_channel = state.channel
    state.reconnect_test = {
        "status": "RUNNING",
        "target_bssid": state.bssid,
        "target_channel": target_channel,
    }
    event_start = len(state.reconnect_events)
    started = time.monotonic()
    serial_port.write(b"FORCE_RECONNECT\n")
    serial_port.flush()

    disconnected_at: float | None = None
    got_ip_at: float | None = None
    failure_event: dict[str, Any] | None = None
    timeout = float(settings["tests"]["reconnect"].get("timeout_seconds", 15.0))
    deadline = started + timeout

    while time.monotonic() < deadline:
        read_serial_available(serial_port, state, serial_log)
        for event in state.reconnect_events[event_start:]:
            status = event.get("status")
            event_time = float(event.get("timestamp", time.time()))
            # Convert wall-clock event time into an elapsed value by using the
            # event ordering; monotonic is used for the total host timeout.
            if status == "DISCONNECTED" and disconnected_at is None:
                disconnected_at = time.monotonic()
            elif status == "GOT_IP":
                got_ip_at = time.monotonic()
            elif status == "FAIL":
                failure_event = event
        if failure_event is not None or (got_ip_at is not None and state.connected):
            break
        time.sleep(0.02)

    if failure_event is not None:
        state.reconnect_test = {
            "status": "FAIL",
            "reason": failure_event.get("reason", "firmware_reconnect_failed"),
            "disconnect_seen": disconnected_at is not None,
            "target_bssid": target_bssid or None,
            "target_channel": target_channel,
        }
        print(
            "  Reconnect: FAIL / "
            + str(state.reconnect_test["reason"])
        )
        return

    if got_ip_at is None:
        recent_disconnects = [
            str(event.get("reason", ""))
            for event in state.reconnect_events[event_start:]
            if event.get("status") == "DISCONNECTED" and event.get("reason")
        ]
        state.reconnect_test = {
            "status": "FAIL",
            "reason": "got_ip_timeout",
            "timeout_seconds": timeout,
            "disconnect_seen": disconnected_at is not None,
            "disconnect_reasons": recent_disconnects[-8:],
            "target_bssid": target_bssid or None,
            "target_channel": target_channel,
        }
        print(
            "  Reconnect: FAIL / no GOT_IP within "
            f"{timeout:.0f} s / reasons "
            + (",".join(recent_disconnects[-8:]) if recent_disconnects else "none")
        )
        return

    actual_bssid = state.bssid.strip().lower()
    actual_channel = state.channel
    target_matches = bool(
        target_bssid
        and actual_bssid == target_bssid
        and target_channel is not None
        and actual_channel == target_channel
    )
    if not target_matches:
        state.reconnect_test = {
            "status": "FAIL",
            "reason": "bssid_or_channel_changed",
            "disconnect_seen": disconnected_at is not None,
            "target_bssid": target_bssid or None,
            "target_channel": target_channel,
            "actual_bssid": actual_bssid or None,
            "actual_channel": actual_channel,
            "got_ip_ms": round((got_ip_at - started) * 1000.0, 1),
        }
        print(
            "  Reconnect: FAIL / AP changed / "
            f"target {target_bssid or '-'} ch {target_channel or '-'} / "
            f"actual {actual_bssid or '-'} ch {actual_channel or '-'}"
        )
        return

    ping_recovered_at: float | None = None
    udp_recovered_at: float | None = None
    recovery_deadline = time.monotonic() + 5.0
    sequence = int(time.time() * 1000) & 0x7FFFFFFF
    while time.monotonic() < recovery_deadline and (ping_recovered_at is None or udp_recovered_at is None):
        if ping_recovered_at is None:
            ok, _latency, _detail = ping_once(state.wifi_ip, int(settings["network"].get("ping_timeout_ms", 1000)))
            if ok:
                ping_recovered_at = time.monotonic()
        if udp_recovered_at is None:
            ok, _latency = udp_roundtrip_once(
                udp_socket,
                state.wifi_ip,
                int(settings["network"].get("udp_port", 33333)),
                sequence,
                timeout=0.5,
            )
            sequence += 1
            if ok:
                udp_recovered_at = time.monotonic()
        if ping_recovered_at is None or udp_recovered_at is None:
            time.sleep(0.05)

    recovered = (
        disconnected_at is not None
        and ping_recovered_at is not None
        and udp_recovered_at is not None
    )
    state.reconnect_test = {
        "status": "PASS" if recovered else "FAIL",
        "reason": None if recovered else (
            "disconnect_event_missing" if disconnected_at is None
            else "ping_or_udp_recovery_timeout"
        ),
        "disconnect_seen": disconnected_at is not None,
        "target_bssid": target_bssid or None,
        "target_channel": target_channel,
        "actual_bssid": actual_bssid or None,
        "actual_channel": actual_channel,
        "got_ip_ms": round((got_ip_at - started) * 1000.0, 1),
        "disconnect_to_got_ip_ms": None if disconnected_at is None else round((got_ip_at - disconnected_at) * 1000.0, 1),
        "ping_recovery_ms": None if ping_recovered_at is None else round((ping_recovered_at - started) * 1000.0, 1),
        "udp_recovery_ms": None if udp_recovered_at is None else round((udp_recovered_at - started) * 1000.0, 1),
        "got_ip_to_ping_ms": None if ping_recovered_at is None else round(max(0.0, ping_recovered_at - got_ip_at) * 1000.0, 1),
        "got_ip_to_udp_ms": None if udp_recovered_at is None else round(max(0.0, udp_recovered_at - got_ip_at) * 1000.0, 1),
    }
    print(
        "  Reconnect: "
        f"{state.reconnect_test['status']} / GOT_IP {state.reconnect_test['got_ip_ms']} ms / "
        f"Ping {state.reconnect_test['ping_recovery_ms']} ms / UDP {state.reconnect_test['udp_recovery_ms']} ms"
    )



def run_short_network_probe(
    ip: str,
    udp_socket: socket.socket,
    settings: dict[str, Any],
    duration_seconds: float,
) -> dict[str, Any]:
    interval = max(0.15, float(settings["tests"]["ble_coexistence"].get("probe_interval_seconds", 0.25)))
    deadline = time.monotonic() + duration_seconds
    ping_total = ping_ok = udp_total = udp_ok = 0
    ping_latencies: list[float] = []
    udp_latencies: list[float] = []
    sequence = int(time.time() * 1000) & 0x7FFFFFFF

    while time.monotonic() < deadline:
        cycle_started = time.monotonic()
        ok, latency, _detail = ping_once(ip, int(settings["network"].get("ping_timeout_ms", 1000)))
        ping_total += 1
        if ok:
            ping_ok += 1
            if latency is not None:
                ping_latencies.append(latency)

        ok, latency = udp_roundtrip_once(
            udp_socket,
            ip,
            int(settings["network"].get("udp_port", 33333)),
            sequence,
            timeout=min(0.5, interval * 1.5),
        )
        sequence += 1
        udp_total += 1
        if ok:
            udp_ok += 1
            if latency is not None:
                udp_latencies.append(latency)

        remaining = interval - (time.monotonic() - cycle_started)
        if remaining > 0:
            time.sleep(remaining)

    return {
        "ping_packets": ping_total,
        "ping_loss_percent": 100.0 * (ping_total - ping_ok) / ping_total if ping_total else None,
        "ping_latency_ms": statistics.mean(ping_latencies) if ping_latencies else None,
        "udp_packets": udp_total,
        "udp_loss_percent": 100.0 * (udp_total - udp_ok) / udp_total if udp_total else None,
        "udp_latency_ms": statistics.mean(udp_latencies) if udp_latencies else None,
    }


def run_ble_coexistence_test(
    serial_port: serial.Serial,
    state: RunState,
    serial_log: Any,
    udp_socket: socket.socket,
    settings: dict[str, Any],
) -> None:
    duration = max(2.0, min(10.0, float(settings["tests"]["ble_coexistence"].get("duration_seconds", 5.0))))
    print("WiFi + BLE coexistence test ...")
    baseline = run_short_network_probe(state.wifi_ip, udp_socket, settings, duration)

    def probe_usable(probe: dict[str, Any]) -> bool:
        ping_packets = int(probe.get("ping_packets") or 0)
        udp_packets = int(probe.get("udp_packets") or 0)
        ping_loss = probe.get("ping_loss_percent")
        udp_loss = probe.get("udp_loss_percent")

        return (
            ping_packets > 0
            and udp_packets > 0
            and isinstance(ping_loss, (int, float))
            and isinstance(udp_loss, (int, float))
            and float(ping_loss) <= 20.0
            and float(udp_loss) <= 20.0
        )

    # A coexistence comparison is meaningless when the network path is already
    # unusable before BLE starts.
    if not probe_usable(baseline):
        state.ble_coexistence = {
            "status": "SKIP",
            "reason": "baseline_network_unusable",
            "devices": None,
            "baseline": baseline,
            "active": {},
            "ping_loss_delta_percent": None,
            "udp_loss_delta_percent": None,
        }
        print(
            "  BLE coexistence: SKIP / baseline network unusable / "
            f"Ping loss {fmt(baseline.get('ping_loss_percent'), 1, ' %')} / "
            f"UDP loss {fmt(baseline.get('udp_loss_percent'), 1, ' %')}"
        )
        return

    event_start = len(state.ble_coex_events)
    serial_port.write(f"BLE_COEX|{int(duration * 1000)}\n".encode("ascii"))
    serial_port.flush()

    # Wait until firmware confirms that BLE scanning has started.
    start_deadline = time.monotonic() + 3.0
    started = False
    while time.monotonic() < start_deadline:
        read_serial_available(serial_port, state, serial_log)
        if any(event.get("status") == "START" for event in state.ble_coex_events[event_start:]):
            started = True
            break
        if any(event.get("status") in {"FAIL", "SKIP"} for event in state.ble_coex_events[event_start:]):
            break
        time.sleep(0.02)

    active = run_short_network_probe(state.wifi_ip, udp_socket, settings, duration) if started else {}

    done_event: dict[str, Any] | None = None
    done_deadline = time.monotonic() + 5.0
    while time.monotonic() < done_deadline:
        read_serial_available(serial_port, state, serial_log)
        for event in state.ble_coex_events[event_start:]:
            if event.get("status") in {"DONE", "FAIL", "SKIP"}:
                done_event = event
        if done_event is not None:
            break
        time.sleep(0.02)

    def delta(key: str) -> float | None:
        left = baseline.get(key)
        right = active.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return float(right) - float(left)
        return None

    ble_done = bool(
        started
        and done_event
        and done_event.get("status") == "DONE"
    )
    active_ok = probe_usable(active)

    if not ble_done:
        reason = (done_event or {}).get("reason") or "ble_scan_failed"
    elif not active_ok:
        reason = "active_network_unusable"
    else:
        reason = None

    state.ble_coexistence = {
        "status": "PASS" if reason is None else "FAIL",
        "reason": reason,
        "devices": parse_int((done_event or {}).get("devices"), None),
        "baseline": baseline,
        "active": active,
        "ping_loss_delta_percent": delta("ping_loss_percent"),
        "udp_loss_delta_percent": delta("udp_loss_percent"),
    }
    print(
        "  BLE coexistence: "
        f"{state.ble_coexistence['status']} / "
        f"Ping loss {fmt(active.get('ping_loss_percent'), 1, ' %')} / "
        f"UDP loss {fmt(active.get('udp_loss_percent'), 1, ' %')}"
    )


def numeric_metric(values: dict[str, str], key: str) -> float | None:
    try:
        return float(values.get(key, ""))
    except (TypeError, ValueError):
        return None


def build_performance_summary(state: RunState) -> dict[str, Any]:
    ram = state.self_test_metrics.get("RAM", {})
    psram = state.self_test_metrics.get("PSRAM", {})
    flash = state.self_test_metrics.get("FLASH", {})
    cpu = state.self_test_metrics.get("CPU", {})
    cores = state.self_test_metrics.get("CPU_CORES", {})
    return {
        "ram": {
            "write_mb_s": numeric_metric(ram, "write_mb_s"),
            "read_mb_s": numeric_metric(ram, "read_mb_s"),
        },
        "psram": {
            "write_mb_s": numeric_metric(psram, "write_mb_s"),
            "read_mb_s": numeric_metric(psram, "read_mb_s"),
        },
        "flash": {
            "erase_ms": numeric_metric(flash, "erase_ms"),
            "write_mb_s": numeric_metric(flash, "write_mb_s"),
            "read_mb_s": numeric_metric(flash, "read_mb_s"),
        },
        "cpu": {
            "elapsed_ms": numeric_metric(cpu, "elapsed_ms"),
            "iterations_per_ms": numeric_metric(cpu, "iterations_per_ms"),
            "core0_ms": numeric_metric(cores, "core0_ms"),
            "core1_ms": numeric_metric(cores, "core1_ms"),
        },
    }


def _nested_value(data: dict[str, Any], path: str) -> float | None:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return float(current) if isinstance(current, (int, float)) else None


def _normalize_mac_for_comparison(value: Any) -> str:
    compact = "".join(
        character for character in str(value or "").upper()
        if character in "0123456789ABCDEF"
    )
    if len(compact) != 12:
        return str(value or "").strip().upper()
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def _rf_direction_metric(summary: dict[str, Any], direction_key: str, metric: str) -> float | None:
    rf = summary.get("rf_quality", {})
    if not isinstance(rf, dict):
        return None
    direction = rf.get(direction_key, {})
    if not isinstance(direction, dict):
        return None

    value = direction.get(metric)
    if isinstance(value, (int, float)):
        return float(value)

    # Compatibility bridge for the first fixed-power baseline result created
    # before the compact RF result schema was introduced.
    legacy_key = {
        "rssi_average_dbm": "full_power_rssi_average_dbm",
        "loss_percent": "full_power_loss_percent",
    }.get(metric)
    legacy = direction.get(legacy_key) if legacy_key else None
    return float(legacy) if isinstance(legacy, (int, float)) else None


def _rf_peer_signature(
    summary: dict[str, Any],
    expected_reference_mac: str = "",
) -> tuple[str, str, str, str] | None:
    rf = summary.get("rf_quality", {})
    if not isinstance(rf, dict):
        rf = {}

    reference = rf.get("reference", {}) if isinstance(rf.get("reference"), dict) else {}
    reference_mac = _normalize_mac_for_comparison(
        reference.get("mac") or expected_reference_mac
    )
    if not reference_mac:
        return None

    mode = str(rf.get("measurement_mode") or rf.get("mode") or "").strip().lower()
    radio = rf.get("radio_control", {}) if isinstance(rf.get("radio_control"), dict) else {}
    if (
        not mode
        and expected_reference_mac
        and str(rf.get("execution_status") or "").upper() == "SKIP"
    ):
        mode = "fixed_full_power"
    if mode != "fixed_full_power":
        return None

    protocol = str(radio.get("protocol") or "802.11b-only")
    fixed_rate = str(radio.get("fixed_rate") or "WIFI_PHY_RATE_1M_L")
    return reference_mac, mode, protocol, fixed_rate


def _peer_metric(
    current: float | None,
    values: list[float],
    minimum_samples: int,
    higher_is_better: bool,
    unit: str,
    warn_ratio: float,
    outlier_ratio: float,
) -> dict[str, Any]:
    ordered = sorted(values)
    median = statistics.median(ordered) if ordered else None
    q1 = percentile(ordered, 0.25) if ordered else None
    q3 = percentile(ordered, 0.75) if ordered else None
    common = {
        "value": current,
        "median": median,
        "q1": q1,
        "q3": q3,
        "samples": len(values),
        "unit": unit,
        "higher_is_better": higher_is_better,
    }
    if len(values) < minimum_samples:
        return {"status": "INSUFFICIENT_DATA", **common}

    if current is None:
        return {"status": "CURRENT_SKIPPED", **common}
    if median == 0 or current <= 0:
        return {"status": "UNAVAILABLE", **common}

    ratio = current / median if higher_is_better else median / current
    status = "NORMAL"
    if ratio < outlier_ratio:
        status = "OUTLIER"
    elif ratio < warn_ratio:
        status = "WARN"
    return {"status": status, "ratio": ratio, **common}


def _rf_peer_metric(
    current: float | None,
    values: list[float],
    minimum_samples: int,
    unit: str,
) -> dict[str, Any]:
    ordered = sorted(values)
    median = statistics.median(ordered) if ordered else None
    q1 = percentile(ordered, 0.25) if ordered else None
    q3 = percentile(ordered, 0.75) if ordered else None
    if len(values) < minimum_samples:
        return {
            "status": "INSUFFICIENT_DATA",
            "value": current,
            "median": median,
            "q1": q1,
            "q3": q3,
            "samples": len(values),
            "unit": unit,
            "comparison": "informational",
        }

    return {
        "status": "CURRENT_SKIPPED" if current is None else "REFERENCE",
        "value": current,
        "median": median,
        "q1": q1,
        "q3": q3,
        "delta_from_median": None if current is None else current - median,
        "samples": len(values),
        "unit": unit,
        "comparison": "informational",
    }


def build_peer_comparison(summary: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    peer_settings = settings.get("peer_comparison", {})
    minimum_samples = max(2, int(peer_settings.get("minimum_samples", 3)))
    warn_ratio = float(peer_settings.get("warn_ratio", 0.65))
    outlier_ratio = float(peer_settings.get("outlier_ratio", 0.45))

    metric_specs = {
        "CPU score": ("performance.cpu.iterations_per_ms", True, "iterations/ms"),
        "RAM write": ("performance.ram.write_mb_s", True, "MB/s"),
        "RAM read": ("performance.ram.read_mb_s", True, "MB/s"),
        "PSRAM write": ("performance.psram.write_mb_s", True, "MB/s"),
        "PSRAM read": ("performance.psram.read_mb_s", True, "MB/s"),
        "Flash erase": ("performance.flash.erase_ms", False, "ms"),
        "Flash write": ("performance.flash.write_mb_s", True, "MB/s"),
        "Flash read": ("performance.flash.read_mb_s", True, "MB/s"),
    }

    history: list[dict[str, Any]] = []
    if RESULTS_ROOT.exists():
        for summary_path in RESULTS_ROOT.glob("*/summary.json"):
            if summary_path.parent.name.startswith("."):
                continue
            try:
                item = load_json(summary_path)
            except Exception:
                continue
            if item.get("environment") != summary.get("environment"):
                continue
            if item.get("board_id") == summary.get("board_id"):
                continue
            if item.get("result") != "PASS":
                continue
            history.append(item)

    metrics: dict[str, Any] = {}
    warnings: list[str] = []
    for label, (path, higher_is_better, unit) in metric_specs.items():
        current = _nested_value(summary, path)
        values = [
            value for item in history
            if (value := _nested_value(item, path)) is not None
        ]
        metric = _peer_metric(
            current,
            values,
            minimum_samples,
            higher_is_better,
            unit,
            warn_ratio,
            outlier_ratio,
        )
        metrics[label] = metric
        if metric.get("status") in {"WARN", "OUTLIER"} and current is not None:
            warnings.append(
                f"Peer comparison {label}: {current:.2f} {unit} vs median "
                f"{float(metric['median']):.2f} {unit} "
                f"({float(metric['ratio']) * 100:.0f} %)"
            )

    rf_config = settings.get("tests", {}).get("rf_quality", {})
    expected_reference_mac = (
        str(rf_config.get("reference_mac", "")) if isinstance(rf_config, dict) else ""
    )
    current_rf_signature = _rf_peer_signature(summary, expected_reference_mac)
    compatible_rf_history = [
        item for item in history
        if current_rf_signature is not None
        and _rf_peer_signature(item) == current_rf_signature
    ]

    rf_specs = {
        "RF REF->DUT RSSI": ("reference_to_dut", "rssi_average_dbm", "dBm"),
        "RF DUT->REF RSSI": ("dut_to_reference", "rssi_average_dbm", "dBm"),
        "RF REF->DUT loss": ("reference_to_dut", "loss_percent", "%"),
        "RF DUT->REF loss": ("dut_to_reference", "loss_percent", "%"),
    }
    for label, (direction, metric_name, unit) in rf_specs.items():
        current = _rf_direction_metric(summary, direction, metric_name)
        values = [
            value for item in compatible_rf_history
            if (value := _rf_direction_metric(item, direction, metric_name)) is not None
        ]
        metrics[label] = _rf_peer_metric(current, values, minimum_samples, unit)

    return {
        "environment": summary.get("environment"),
        "minimum_samples": minimum_samples,
        "performance_peer_boards": len(history),
        "rf_peer_boards": len(compatible_rf_history),
        "rf_reference_mac": (
            current_rf_signature[0] if current_rf_signature is not None else ""
        ),
        "metrics": metrics,
        "warnings": warnings,
    }

def generate_results_index() -> None:
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    if RESULTS_ROOT.exists():
        for summary_path in RESULTS_ROOT.glob("*/summary.json"):
            if summary_path.parent.name.startswith("."):
                continue
            try:
                summary = load_json(summary_path)
            except Exception:
                continue

            wifi = summary.get("wifi", {})
            perf = summary.get("performance", {})
            cpu = perf.get("cpu", {}) if isinstance(perf, dict) else {}
            ram = perf.get("ram", {}) if isinstance(perf, dict) else {}
            psram = perf.get("psram", {}) if isinstance(perf, dict) else {}
            flash = perf.get("flash", {}) if isinstance(perf, dict) else {}
            rf = summary.get("rf_quality", {})
            rf_signature = _rf_peer_signature(summary)
            fixed_rf = rf_signature is not None

            rows.append(
                {
                    "board_id": summary.get("board_id", summary_path.parent.name),
                    "environment": summary.get("environment", ""),
                    "timestamp": summary.get("timestamp", ""),
                    "result": summary.get("result", ""),
                    "rf_quality": rf.get("quality_status", "") if isinstance(rf, dict) else "",
                    "rf_reference_mac": (rf.get("reference", {}).get("mac", "") if fixed_rf and isinstance(rf, dict) else ""),
                    "rf_ref_to_dut_rssi": (_rf_direction_metric(summary, "reference_to_dut", "rssi_average_dbm") if fixed_rf else None),
                    "rf_ref_to_dut_loss": (_rf_direction_metric(summary, "reference_to_dut", "loss_percent") if fixed_rf else None),
                    "rf_dut_to_ref_rssi": (_rf_direction_metric(summary, "dut_to_reference", "rssi_average_dbm") if fixed_rf else None),
                    "rf_dut_to_ref_loss": (_rf_direction_metric(summary, "dut_to_reference", "loss_percent") if fixed_rf else None),
                    "wifi": summary.get("network_quality", {}).get("status", ""),
                    "ping_loss": wifi.get("icmp", {}).get("loss_percent"),
                    "udp_loss": wifi.get("udp", {}).get("loss_percent"),
                    "rssi": wifi.get("rssi_average_dbm"),
                    "disconnects": wifi.get("disconnects"),
                    "cpu_score": cpu.get("iterations_per_ms") if isinstance(cpu, dict) else None,
                    "ram_write": ram.get("write_mb_s") if isinstance(ram, dict) else None,
                    "ram_read": ram.get("read_mb_s") if isinstance(ram, dict) else None,
                    "psram_write": psram.get("write_mb_s") if isinstance(psram, dict) else None,
                    "psram_read": psram.get("read_mb_s") if isinstance(psram, dict) else None,
                    "flash_write": flash.get("write_mb_s") if isinstance(flash, dict) else None,
                    "flash_read": flash.get("read_mb_s") if isinstance(flash, dict) else None,
                    "report": summary_path.parent / "report.html",
                }
            )

    rows.sort(key=lambda item: str(item.get("board_id", "")))
    csv_path = RESULTS_ROOT / "boards.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        fieldnames = [
            "board_id", "environment", "timestamp", "result", "rf_quality",
            "rf_reference_mac", "rf_ref_to_dut_rssi", "rf_ref_to_dut_loss",
            "rf_dut_to_ref_rssi", "rf_dut_to_ref_loss",
            "wifi", "ping_loss", "udp_loss", "rssi", "disconnects", "cpu_score",
            "ram_write", "ram_read", "psram_write", "psram_read",
            "flash_write", "flash_read",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})

    table_rows = []
    for row in rows:
        result_class = "pass" if row["result"] == "PASS" else "fail"
        rf_class = (
            "pass" if row["rf_quality"] == "PASS"
            else "fail" if row["rf_quality"] == "FAIL"
            else "warn" if row["rf_quality"] == "WARN"
            else ""
        )
        wifi_class = (
            "pass" if row["wifi"] == "PASS"
            else "fail" if row["wifi"] == "FAIL"
            else "warn" if row["wifi"] == "WARN"
            else ""
        )
        rel_report = Path(row["report"]).relative_to(RESULTS_ROOT).as_posix()
        table_rows.append(
            "<tr>"
            f"<td><a href='{html.escape(rel_report)}'>{html.escape(str(row['board_id']))}</a></td>"
            f"<td>{html.escape(str(row['environment']))}</td>"
            f"<td>{html.escape(str(row['timestamp']))}</td>"
            f"<td class='{result_class}'>{html.escape(str(row['result']))}</td>"
            f"<td class='{rf_class}'>{html.escape(str(row['rf_quality']))}</td>"
            f"<td>{fmt(row['rf_ref_to_dut_rssi'], 1, ' dBm')}</td>"
            f"<td>{fmt(row['rf_ref_to_dut_loss'], 2, ' %')}</td>"
            f"<td>{fmt(row['rf_dut_to_ref_rssi'], 1, ' dBm')}</td>"
            f"<td>{fmt(row['rf_dut_to_ref_loss'], 2, ' %')}</td>"
            f"<td class='{wifi_class}'>{html.escape(str(row['wifi']))}</td>"
            f"<td>{fmt(row['ping_loss'], 2, ' %')}</td>"
            f"<td>{fmt(row['udp_loss'], 2, ' %')}</td>"
            f"<td>{fmt(row['rssi'], 1, ' dBm')}</td>"
            f"<td>{html.escape(str(row['disconnects']))}</td>"
            f"<td>{fmt(row['cpu_score'], 0)}</td>"
            f"<td>{fmt(row['ram_write'], 1)}</td>"
            f"<td>{fmt(row['ram_read'], 1)}</td>"
            f"<td>{fmt(row['psram_write'], 1)}</td>"
            f"<td>{fmt(row['psram_read'], 1)}</td>"
            f"<td>{fmt(row['flash_write'], 1)}</td>"
            f"<td>{fmt(row['flash_read'], 1)}</td>"
            "</tr>"
        )

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>ESP32 Board Test Results</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:32px;background:#f5f6f8;color:#1f2328}}
main{{max-width:1800px;margin:auto;background:white;padding:24px;border-radius:12px;box-shadow:0 2px 12px #0001}}
.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}}
th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:right}}th{{position:sticky;top:0;background:white}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),th:nth-child(3),td:nth-child(3),th:nth-child(4),td:nth-child(4),th:nth-child(5),td:nth-child(5){{text-align:left}}
.pass{{color:#16713a;font-weight:700}}.fail{{color:#a51d1d;font-weight:700}}.warn{{color:#9a6700;font-weight:700}}
a{{color:#0969da;text-decoration:none}}small{{color:#667}}
</style></head><body><main><h1>ESP32 Board Test Results</h1>
<p>{len(rows)} completed board result(s). Memory/flash values are MB/s.</p>
<div class="table-wrap"><table><thead><tr>
<th>Board</th><th>Profile</th><th>Date</th><th>Result</th><th>RF Quality</th><th>REF→DUT RSSI</th><th>REF→DUT loss</th><th>DUT→REF RSSI</th><th>DUT→REF loss</th><th>WiFi</th>
<th>Ping loss</th><th>UDP loss</th><th>RSSI</th><th>Disc.</th><th>CPU score</th>
<th>RAM W</th><th>RAM R</th><th>PSRAM W</th><th>PSRAM R</th><th>Flash W</th><th>Flash R</th>
</tr></thead><tbody>{''.join(table_rows)}</tbody></table></div>
<p><small>Missing values indicate older results or unsupported hardware.</small></p>
</main></body></html>"""
    (RESULTS_ROOT / "index.html").write_text(document, encoding="utf-8")


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def longest_failure_window(
    results: list[ProbeResult],
    interval_seconds: float,
) -> tuple[float, float | None, float | None]:
    longest_count = 0
    longest_start: float | None = None
    longest_end: float | None = None

    current_count = 0
    current_start: float | None = None
    current_end: float | None = None

    for item in sorted(results, key=lambda x: x.sequence):
        if item.success:
            if current_count > longest_count:
                longest_count = current_count
                longest_start = current_start
                longest_end = current_end
            current_count = 0
            current_start = None
            current_end = None
            continue

        if current_count == 0:
            current_start = item.timestamp
        current_count += 1
        current_end = item.timestamp + interval_seconds

    if current_count > longest_count:
        longest_count = current_count
        longest_start = current_start
        longest_end = current_end

    return longest_count * interval_seconds, longest_start, longest_end


def longest_failure_run(results: list[ProbeResult], interval_seconds: float) -> float:
    duration, _start, _end = longest_failure_window(results, interval_seconds)
    return duration


def summarize_probes(results: list[ProbeResult], kind: str, interval_seconds: float) -> dict[str, Any]:
    items = [item for item in results if item.kind == kind]
    successful = [item for item in items if item.success]
    latencies = [item.latency_ms for item in successful if item.latency_ms is not None]
    total = len(items)
    loss = (100.0 * (total - len(successful)) / total) if total else 100.0
    outage_seconds, outage_start, outage_end = longest_failure_window(items, interval_seconds)
    return {
        "packets": total,
        "success": len(successful),
        "loss_percent": loss,
        "latency_average_ms": statistics.mean(latencies) if latencies else None,
        "latency_median_ms": statistics.median(latencies) if latencies else None,
        "latency_p95_ms": percentile(latencies, 0.95),
        "latency_max_ms": max(latencies) if latencies else None,
        "longest_outage_seconds": outage_seconds,
        "longest_outage_start_timestamp": outage_start,
        "longest_outage_end_timestamp": outage_end,
    }


def average_valid_rssi(samples: list[dict[str, Any]]) -> float | None:
    values = [int(item["rssi"]) for item in samples if int(item.get("wifi", 0)) == 1 and -126 < int(item.get("rssi", -127)) < 0]
    return statistics.mean(values) if values else None


def summarize_probes_while_connected(
    results: list[ProbeResult],
    kind: str,
    interval_seconds: float,
    heartbeat_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize host probes only while firmware reports WL_CONNECTED.

    Probe failures that occur while the station is disconnected/reconnecting are
    excluded from data-path scoring. The disconnect/reconnect itself is scored
    separately, which prevents one WiFi drop from being counted again as Ping
    and UDP failures.
    """
    items = sorted(
        (item for item in results if item.kind == kind),
        key=lambda item: item.timestamp,
    )
    heartbeats = sorted(
        (
            item
            for item in heartbeat_samples
            if isinstance(item.get("timestamp"), (int, float))
        ),
        key=lambda item: float(item["timestamp"]),
    )

    connected: list[ProbeResult] = []
    heartbeat_index = -1
    max_heartbeat_age = max(2.5, interval_seconds * 2.5)

    longest_failures = 0
    current_failures = 0
    previous_connected_sequence: int | None = None

    for item in items:
        while (
            heartbeat_index + 1 < len(heartbeats)
            and float(heartbeats[heartbeat_index + 1]["timestamp"]) <= item.timestamp
        ):
            heartbeat_index += 1

        is_connected = False
        if heartbeat_index >= 0:
            heartbeat = heartbeats[heartbeat_index]
            heartbeat_age = item.timestamp - float(heartbeat["timestamp"])
            is_connected = (
                0.0 <= heartbeat_age <= max_heartbeat_age
                and int(heartbeat.get("wifi", 0)) == 1
            )

        if not is_connected:
            current_failures = 0
            previous_connected_sequence = None
            continue

        connected.append(item)

        if item.success:
            current_failures = 0
        else:
            if (
                previous_connected_sequence is None
                or item.sequence != previous_connected_sequence + 1
            ):
                current_failures = 0
            current_failures += 1
            longest_failures = max(longest_failures, current_failures)

        previous_connected_sequence = item.sequence

    successful = [item for item in connected if item.success]
    latencies = [
        item.latency_ms
        for item in successful
        if item.latency_ms is not None
    ]
    total = len(connected)

    return {
        "packets": total,
        "success": len(successful),
        "loss_percent": (
            100.0 * (total - len(successful)) / total if total else None
        ),
        "latency_average_ms": statistics.mean(latencies) if latencies else None,
        "latency_median_ms": statistics.median(latencies) if latencies else None,
        "latency_p95_ms": percentile(latencies, 0.95),
        "latency_max_ms": max(latencies) if latencies else None,
        "longest_outage_seconds": longest_failures * interval_seconds,
    }


def summarize_heartbeat_stream(
    samples: list[dict[str, Any]],
    test_end_timestamp: float,
) -> dict[str, Any]:
    timestamps = sorted(
        float(item["timestamp"])
        for item in samples
        if isinstance(item.get("timestamp"), (int, float))
    )

    if not timestamps:
        return {
            "samples": 0,
            "longest_gap_seconds": None,
            "last_gap_seconds": None,
        }

    gaps = [
        max(0.0, current - previous)
        for previous, current in zip(timestamps, timestamps[1:])
    ]
    trailing_gap = max(0.0, test_end_timestamp - timestamps[-1])
    longest_gap = max([trailing_gap, *gaps])

    return {
        "samples": len(timestamps),
        "longest_gap_seconds": longest_gap,
        "last_gap_seconds": trailing_gap,
    }


def build_summary(
    settings: dict[str, Any],
    esp_info: dict[str, Any],
    state: RunState,
    probe_results: list[ProbeResult],
    duration_seconds: float,
    environment: str,
    board_id: str,
    test_config: dict[str, bool],
) -> dict[str, Any]:
    soak_settings = settings["tests"]["soak"]
    wifi_settings = settings["tests"]["wifi"]
    interval = float(soak_settings.get("probe_interval_seconds", 1.0))
    thresholds = soak_settings.get("thresholds", {})
    wifi_enabled = bool(test_config["wifi"])
    rf_quality_enabled = bool(test_config.get("rf_quality", False))
    ping_enabled = bool(test_config["ping"])
    udp_enabled = bool(test_config["udp"])
    soak_enabled = bool(test_config.get("soak", True))

    icmp = summarize_probes(probe_results, "icmp", interval)
    udp = summarize_probes(probe_results, "udp", interval)
    icmp_connected = summarize_probes_while_connected(
        probe_results, "icmp", interval, state.heartbeat_samples
    )
    udp_connected = summarize_probes_while_connected(
        probe_results, "udp", interval, state.heartbeat_samples
    )
    icmp["while_connected"] = icmp_connected
    udp["while_connected"] = udp_connected
    icmp["enabled"] = ping_enabled and soak_enabled
    udp["enabled"] = udp_enabled and soak_enabled

    if not ping_enabled or not soak_enabled:
        for key in [
            "loss_percent",
            "latency_average_ms",
            "latency_median_ms",
            "latency_p95_ms",
            "latency_max_ms",
            "longest_outage_seconds",
            "longest_outage_start_timestamp",
            "longest_outage_end_timestamp",
        ]:
            icmp[key] = None

    if not udp_enabled or not soak_enabled:
        for key in [
            "loss_percent",
            "latency_average_ms",
            "latency_median_ms",
            "latency_p95_ms",
            "latency_max_ms",
            "longest_outage_seconds",
            "longest_outage_start_timestamp",
            "longest_outage_end_timestamp",
        ]:
            udp[key] = None

    rssi_avg = average_valid_rssi(state.heartbeat_samples)
    test_end_timestamp = max(
        (item.timestamp for item in probe_results),
        default=time.time(),
    )
    serial_heartbeat = summarize_heartbeat_stream(state.heartbeat_samples, test_end_timestamp)
    udp_heartbeat = summarize_heartbeat_stream(state.udp_heartbeat_samples, test_end_timestamp)
    heap_values = [int(item.get("heap_free", 0)) for item in state.heartbeat_samples if int(item.get("heap_free", 0)) > 0]
    heap_start = heap_values[0] if heap_values else None
    heap_end = heap_values[-1] if heap_values else None
    heap_min = min(heap_values) if heap_values else None
    heap_drop = max(0, (heap_start or 0) - (heap_end or 0)) if heap_start is not None and heap_end is not None else None
    disconnects = max([int(item.get("disconnects", 0)) for item in state.heartbeat_samples] + [len(state.disconnect_reasons)])
    reconnects = max([int(item.get("reconnects", 0)) for item in state.heartbeat_samples] + [0])

    fail_reasons: list[str] = []
    warnings: list[str] = []
    stability_fail_reasons: list[str] = []
    stability_warnings: list[str] = []
    network_quality_fail_reasons: list[str] = []
    network_quality_warnings: list[str] = []

    failed_selftests = sorted(name for name, status in state.self_tests.items() if status == "FAIL")
    for name in failed_selftests:
        fail_reasons.append(f"Internal test {name} failed")

    restart_counts = {
        str(phase): int(count)
        for phase, count in state.unexpected_restarts_by_phase.items()
        if int(count) > 0
    }
    soak_boots = restart_counts.get("soak", 0)
    restart_phase_labels = {
        "startup": "startup/self-test",
        "self_tests": "internal hardware tests",
        "deep_sleep": "deep-sleep test",
        "rf_quality": "RF quality test",
        "wifi_setup": "WiFi setup/warm-up",
        "reconnect": "controlled reconnect test",
        "ble_coexistence": "WiFi + BLE coexistence test",
        "complete": "test finalization",
    }
    for phase, count in sorted(restart_counts.items()):
        if phase in {"soak", "reconnect", "ble_coexistence"}:
            continue
        label = restart_phase_labels.get(phase, phase.replace("_", " "))
        fail_reasons.append(
            f"Unexpected restart during {label} ({count} restart(s))"
        )

    uptimes = [int(item.get("uptime_ms", 0)) for item in state.heartbeat_samples if int(item.get("uptime_ms", 0)) > 0]
    if soak_boots > 0:
        stability_fail_reasons.append(
            f"Unexpected restart detected during the soak test ({soak_boots} restart(s))"
        )
    elif any(current < previous for previous, current in zip(uptimes, uptimes[1:])):
        stability_fail_reasons.append("Unexpected restart detected during the soak test")
    if state.serial_error:
        fail_reasons.append(f"Serial connection: {state.serial_error}")

    serial_gap = serial_heartbeat.get("longest_gap_seconds")
    serial_warn_seconds = float(thresholds.get("serial_heartbeat_warn_seconds", 3.0))
    serial_fail_seconds = float(thresholds.get("serial_heartbeat_fail_seconds", 10.0))

    if soak_enabled:
        if serial_gap is None:
            stability_fail_reasons.append("No serial heartbeats received")
        elif float(serial_gap) >= serial_fail_seconds:
            stability_fail_reasons.append(f"Serial heartbeat unavailable for {float(serial_gap):.1f} s")
        elif float(serial_gap) >= serial_warn_seconds:
            stability_warnings.append(f"Serial heartbeat interrupted for {float(serial_gap):.1f} s")

    if wifi_enabled and not state.wifi_ip:
        fail_reasons.append("No WiFi connection/IP address obtained")

    if ping_enabled and soak_enabled:
        if icmp["packets"] == 0:
            stability_fail_reasons.append("No ping measurements received")
        elif icmp_connected["packets"] > 0:
            connected_loss = float(icmp_connected["loss_percent"] or 0.0)
            connected_outage = float(icmp_connected["longest_outage_seconds"] or 0.0)

            if connected_loss >= thresholds["ping_loss_fail_percent"]:
                network_quality_fail_reasons.append(
                    f"Ping loss while WL_CONNECTED {connected_loss:.2f} %"
                )
            elif connected_loss >= thresholds["ping_loss_warn_percent"]:
                network_quality_warnings.append(
                    f"Ping loss while WL_CONNECTED {connected_loss:.2f} %"
                )

            if connected_outage >= thresholds["longest_outage_fail_seconds"]:
                network_quality_fail_reasons.append(
                    f"Ping outage while WL_CONNECTED {connected_outage:.1f} s"
                )
            elif connected_outage >= thresholds["longest_outage_warn_seconds"]:
                network_quality_warnings.append(
                    f"Ping outage while WL_CONNECTED {connected_outage:.1f} s"
                )

            raw_loss = float(icmp["loss_percent"] or 0.0)
            raw_outage = float(icmp["longest_outage_seconds"] or 0.0)
            if (
                disconnects > 0
                and (
                    raw_loss >= thresholds["ping_loss_warn_percent"]
                    or raw_outage >= thresholds["longest_outage_warn_seconds"]
                )
                and connected_loss < thresholds["ping_loss_warn_percent"]
                and connected_outage < thresholds["longest_outage_warn_seconds"]
            ):
                network_quality_warnings.append(
                    "Ping interruption occurred during WiFi disconnect/reconnect "
                    f"(raw loss {raw_loss:.2f} %, max outage {raw_outage:.1f} s)"
                )

    connected_heartbeats = [item for item in state.heartbeat_samples if int(item.get("wifi", 0)) == 1]
    connected_ratio = (len(connected_heartbeats) / len(state.heartbeat_samples)) if state.heartbeat_samples else 0.0
    data_path_stall = False
    if udp_enabled and soak_enabled:
        if udp["packets"] == 0:
            stability_fail_reasons.append("No UDP measurements received")
        elif udp_connected["packets"] > 0:
            connected_loss = float(udp_connected["loss_percent"] or 0.0)
            connected_outage = float(udp_connected["longest_outage_seconds"] or 0.0)
            data_path_stall = connected_outage >= thresholds["longest_outage_fail_seconds"]

            if connected_loss >= thresholds["udp_loss_fail_percent"]:
                network_quality_fail_reasons.append(
                    f"UDP data-path loss while WL_CONNECTED {connected_loss:.2f} %"
                )
            elif connected_loss >= thresholds["udp_loss_warn_percent"]:
                network_quality_warnings.append(
                    f"UDP data-path loss while WL_CONNECTED {connected_loss:.2f} %"
                )

            if connected_outage >= thresholds["longest_outage_fail_seconds"]:
                network_quality_fail_reasons.append(
                    f"Data-path stall despite WL_CONNECTED {connected_outage:.1f} s"
                )
            elif connected_outage >= thresholds["longest_outage_warn_seconds"]:
                network_quality_warnings.append(
                    f"UDP outage while WL_CONNECTED {connected_outage:.1f} s"
                )

            raw_loss = float(udp["loss_percent"] or 0.0)
            raw_outage = float(udp["longest_outage_seconds"] or 0.0)
            if (
                disconnects > 0
                and (
                    raw_loss >= thresholds["udp_loss_warn_percent"]
                    or raw_outage >= thresholds["longest_outage_warn_seconds"]
                )
                and connected_loss < thresholds["udp_loss_warn_percent"]
                and connected_outage < thresholds["longest_outage_warn_seconds"]
            ):
                network_quality_warnings.append(
                    "UDP interruption occurred during WiFi disconnect/reconnect "
                    f"(raw loss {raw_loss:.2f} %, max outage {raw_outage:.1f} s)"
                )

    if wifi_enabled and soak_enabled:
        # Disconnects are scored exactly once. A transient drop that reconnects
        # can be WARN, while a drop with no matching reconnect is a hard FAIL.
        if disconnects > reconnects:
            network_quality_fail_reasons.append(
                f"WiFi did not recover from runtime disconnect ({disconnects} disconnect(s), {reconnects} reconnect(s))"
            )
        elif disconnects >= thresholds["disconnects_fail"]:
            network_quality_fail_reasons.append(f"WiFi disconnects {disconnects}")
        elif disconnects >= thresholds["disconnects_warn"]:
            network_quality_warnings.append(f"WiFi disconnects {disconnects}")

        if rssi_avg is not None and rssi_avg <= thresholds["rssi_warn_dbm"]:
            network_quality_warnings.append(f"Weak average RSSI {rssi_avg:.1f} dBm")

    if rf_quality_enabled:
        rf_result = state.rf_quality or {}
        execution_status = rf_result.get("execution_status")
        quality_status = rf_result.get("quality_status")
        if execution_status != "PASS":
            reasons = rf_result.get("quality_fail_reasons") or ["RF quality test did not complete"]
            fail_reasons.extend(f"RF quality: {reason}" for reason in reasons)
        elif quality_status == "FAIL":
            reasons = rf_result.get("quality_fail_reasons") or ["RF quality thresholds failed"]
            fail_reasons.extend(f"RF quality: {reason}" for reason in reasons)
        elif quality_status == "UNRATED":
            warnings.append(
                "RF quality is UNRATED until reference-station thresholds are calibrated from known boards"
            )
        for warning in rf_result.get("quality_warnings", []):
            if warning not in warnings:
                warnings.append(f"RF quality: {warning}")

    if test_config.get("deep_sleep", False):
        if state.self_tests.get("DEEP_SLEEP") not in {"PASS", "FAIL"}:
            fail_reasons.append("Deep-sleep / RTC wake-up test was not completed")

    reconnect_restarts = restart_counts.get("reconnect", 0)
    if reconnect_restarts > 0:
        state.reconnect_test = {
            **state.reconnect_test,
            "status": "FAIL",
            "reason": "unexpected_restart",
            "unexpected_restarts": reconnect_restarts,
        }

    if test_config.get("reconnect", False):
        if state.reconnect_test.get("status") != "PASS":
            reconnect_reason = state.reconnect_test.get("reason")
            if reconnect_reason == "unexpected_restart":
                fail_reasons.append(
                    "Controlled WiFi reconnect test failed: unexpected restart "
                    f"({reconnect_restarts} restart(s))"
                )
            else:
                fail_reasons.append(
                    "Controlled WiFi reconnect test failed"
                    + (f": {reconnect_reason}" if reconnect_reason else "")
                )

    ble_coex_restarts = restart_counts.get("ble_coexistence", 0)
    if ble_coex_restarts > 0:
        state.ble_coexistence = {
            **state.ble_coexistence,
            "status": "FAIL",
            "reason": "unexpected_restart",
            "unexpected_restarts": ble_coex_restarts,
        }

    if test_config.get("ble_coexistence", False):
        ble_status = state.ble_coexistence.get("status")
        ble_reason = state.ble_coexistence.get("reason")
        if ble_status == "FAIL":
            if ble_reason == "unexpected_restart":
                fail_reasons.append(
                    "WiFi + BLE coexistence test failed: unexpected restart "
                    f"({ble_coex_restarts} restart(s))"
                )
            else:
                fail_reasons.append(
                    "WiFi + BLE coexistence test failed"
                    + (f": {ble_reason}" if ble_reason else "")
                )
        elif ble_status == "SKIP":
            warnings.append(
                "BLE coexistence not rated"
                + (f": {ble_reason}" if ble_reason else "")
            )
        elif ble_status != "PASS":
            fail_reasons.append("WiFi + BLE coexistence test was not completed")
        else:
            ping_delta = state.ble_coexistence.get("ping_loss_delta_percent")
            udp_delta = state.ble_coexistence.get("udp_loss_delta_percent")
            # Coexistence degradation is intentionally a warning only. Dedicated
            # RF quality is evaluated separately against the reference fixture.
            if isinstance(ping_delta, (int, float)) and ping_delta >= 20.0:
                warnings.append(f"BLE coexistence increased Ping loss by {ping_delta:.1f} percentage points")
            if isinstance(udp_delta, (int, float)) and udp_delta >= 20.0:
                warnings.append(f"BLE coexistence increased UDP loss by {udp_delta:.1f} percentage points")

    heap_integrity_failures = [
        item for item in state.heap_integrity_checks if item.get("status") == "FAIL"
    ]
    if test_config.get("heap_integrity", False) and soak_enabled:
        expected_interval = float(soak_settings.get("heap_integrity", {}).get("interval_seconds", 30.0))
        if heap_integrity_failures:
            stability_fail_reasons.append(f"Heap integrity check failed ({len(heap_integrity_failures)} time(s))")
        elif duration_seconds >= max(5.0, expected_interval * 1.5) and not state.heap_integrity_checks:
            stability_fail_reasons.append("No periodic heap integrity checks received during soak")

    if soak_enabled and heap_drop is not None:
        if heap_drop >= thresholds["heap_drop_fail_bytes"]:
            stability_fail_reasons.append(f"Heap drop {heap_drop} bytes")
        elif heap_drop >= thresholds["heap_drop_warn_bytes"]:
            stability_warnings.append(f"Heap drop {heap_drop} bytes")

    if not wifi_enabled or not soak_enabled:
        network_quality_status = "SKIP"
    elif network_quality_fail_reasons:
        network_quality_status = "FAIL"
    elif network_quality_warnings:
        network_quality_status = "WARN"
    else:
        network_quality_status = "PASS"

    # Long-run home-network stability is diagnostic and deliberately separate
    # from RF board quality. Antenna/radio screening is performed against the
    # dedicated reference ESP, while the soak still exposes runtime/network
    # problems without misclassifying AP/host/environment effects as RF defects.
    stability_fail_reasons.extend(network_quality_fail_reasons)
    stability_warnings.extend(network_quality_warnings)
    if not soak_enabled:
        stability_status = "SKIP"
    elif stability_fail_reasons:
        stability_status = "FAIL"
    elif stability_warnings:
        stability_status = "WARN"
    else:
        stability_status = "PASS"

    if soak_enabled:
        warnings.extend(
            f"Stability FAIL (diagnostic): {reason}"
            for reason in stability_fail_reasons
        )
        warnings.extend(
            f"Stability WARN: {warning}"
            for warning in stability_warnings
        )

    result = "FAIL" if fail_reasons else "PASS"
    return {
        "result": result,
        "warnings": warnings,
        "fail_reasons": fail_reasons,
        "stability": {
            "status": stability_status,
            "fail_reasons": stability_fail_reasons,
            "warnings": stability_warnings,
        },
        "rf_quality": state.rf_quality if rf_quality_enabled else {"enabled": False, "execution_status": "SKIP", "quality_status": "SKIP"},
        "network_quality": {
            "status": network_quality_status,
            "fail_reasons": network_quality_fail_reasons,
            "warnings": network_quality_warnings,
        },
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "duration_seconds": round(duration_seconds, 1),
        "soak_enabled": soak_enabled,
        "environment": environment,
        "board_id": board_id,
        "chip_family": esp_info.get("chip_family"),
        "test_config": dict(test_config),
        "quality_suite_complete": is_complete_quality_suite(test_config),
        "esptool": {key: value for key, value in esp_info.items() if key != "raw"},
        "self_tests": state.self_tests,
        "self_test_reasons": state.self_test_reasons,
        "self_test_metrics": state.self_test_metrics,
        "system": state.system_info,
        "performance": build_performance_summary(state),
        "preflight": {
            "deep_sleep": {
                "enabled": bool(test_config.get("deep_sleep", False)),
                "status": state.self_tests.get("DEEP_SLEEP", "SKIP" if not test_config.get("deep_sleep", False) else "MISSING"),
                "planned_reboots": state.planned_reboots,
                "unexpected_restarts": restart_counts.get("deep_sleep", 0),
            },
            "reconnect": {
                **state.reconnect_test,
                "unexpected_restarts": restart_counts.get("reconnect", 0),
            },
            "ble_coexistence": {
                **state.ble_coexistence,
                "unexpected_restarts": restart_counts.get("ble_coexistence", 0),
            },
            "unexpected_restarts_by_phase": restart_counts,
        },
        "wifi": {
            "enabled": wifi_enabled,
            "warmup_seconds": float(wifi_settings.get("warmup_seconds", 30.0)) if wifi_enabled else 0.0,
            "ip": state.wifi_ip,
            "bssid": state.bssid,
            "channel": state.channel,
            "scan_aps": state.wifi_scan_aps,
            "scan_target_rssi_dbm": state.wifi_scan_target_rssi,
            "scan_target_channel": state.wifi_scan_target_channel,
            "scan_target_bssid": state.wifi_scan_target_bssid,
            "rssi_average_dbm": rssi_avg,
            "disconnects": disconnects,
            "reconnects": reconnects,
            "disconnect_reasons": state.disconnect_reasons,
            "connected_heartbeat_ratio": connected_ratio,
            "data_path_stall_detected": data_path_stall,
            "icmp": icmp,
            "udp": udp,
        },
        "heap": {
            "start_bytes": heap_start,
            "end_bytes": heap_end,
            "minimum_observed_bytes": heap_min,
            "drop_bytes": heap_drop,
            "integrity_checks": len(state.heap_integrity_checks),
            "integrity_failures": len(heap_integrity_failures),
        },
        "monitoring": {
            "serial_heartbeat": serial_heartbeat,
            "udp_heartbeat": udp_heartbeat,
        },
    }


def fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def write_network_csv(path: Path, results: list[ProbeResult]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["timestamp", "type", "sequence", "success", "latency_ms", "detail"])
        for item in results:
            writer.writerow(
                [
                    datetime.fromtimestamp(item.timestamp).isoformat(timespec="milliseconds"),
                    item.kind,
                    item.sequence,
                    "1" if item.success else "0",
                    "" if item.latency_ms is None else f"{item.latency_ms:.3f}",
                    item.detail,
                ]
            )


def write_events_csv(path: Path, state: RunState) -> None:
    keys: set[str] = {"timestamp", "category"}
    for event in state.protocol_events:
        keys.update(event.keys())
    ordered = ["timestamp", "category"] + sorted(key for key in keys if key not in {"timestamp", "category"})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=ordered, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        for event in state.protocol_events:
            row = dict(event)
            if isinstance(row.get("timestamp"), (int, float)):
                row["timestamp"] = datetime.fromtimestamp(float(row["timestamp"])).isoformat(timespec="milliseconds")
            writer.writerow(row)


def _rf_direction_text(direction: dict[str, Any], label: str) -> str:
    if not isinstance(direction, dict) or not direction:
        return f"  {label}: not measured"
    return (
        f"  {label}: TX {fmt(direction.get('tx_power_dbm'), 0, ' dBm')} / "
        f"packets {direction.get('packets_received', '-')}/{direction.get('packets_sent', '-')} / "
        f"loss {fmt(direction.get('loss_percent'), 2, ' %')} / "
        f"RSSI {fmt(direction.get('rssi_average_dbm'), 1, ' dBm')} "
        f"(min {fmt(direction.get('rssi_min_dbm'), 1, ' dBm')} / "
        f"max {fmt(direction.get('rssi_max_dbm'), 1, ' dBm')})"
    )


def _rf_text_lines(rf: dict[str, Any], rf_ref: dict[str, Any]) -> list[str]:
    ref_to_dut = rf.get("reference_to_dut", {}) if isinstance(rf, dict) else {}
    dut_to_ref = rf.get("dut_to_reference", {}) if isinstance(rf, dict) else {}
    radio = rf.get("radio_control", {}) if isinstance(rf.get("radio_control"), dict) else {}
    return [
        "RF quality:",
        f"  Execution: {rf.get('execution_status', 'SKIP')}",
        f"  Quality: {rf.get('quality_status', 'SKIP')}",
        f"  Mode: {rf.get('measurement_mode', '-')}",
        f"  Reference: {rf_ref.get('mac', '-')} / {rf_ref.get('port', '-')} / FW {rf_ref.get('version', '-')}",
        f"  Channel: {rf.get('channel', '-')}",
        f"  Radio: {radio.get('protocol', '-')} / {radio.get('fixed_rate', '-')}",
        f"  Repetitions per direction: {rf.get('repetitions_per_direction', '-')}",
        f"  Packets per repetition: {rf.get('packets_per_repetition', '-')}",
        _rf_direction_text(ref_to_dut, "REF -> DUT"),
        _rf_direction_text(dut_to_ref, "DUT -> REF"),
    ]


def _rf_html_summary(rf: dict[str, Any], rf_ref: dict[str, Any], rf_class: str, rf_status: str) -> str:
    ref_to_dut = rf.get("reference_to_dut", {}) if isinstance(rf, dict) else {}
    dut_to_ref = rf.get("dut_to_reference", {}) if isinstance(rf, dict) else {}
    radio = rf.get("radio_control", {}) if isinstance(rf.get("radio_control"), dict) else {}
    return (
        f'<div class="card"><b>RF Quality</b><br>'
        f'Status: <span class="{rf_class}">{html.escape(rf_status)}</span><br>'
        f'Execution: {html.escape(str(rf.get("execution_status", "SKIP")))}<br>'
        f'Mode: {html.escape(str(rf.get("measurement_mode", "-")))}<br>'
        f'Reference: {html.escape(str(rf_ref.get("mac", "-")))}<br>'
        f'Reference port: {html.escape(str(rf_ref.get("port", "-")))}<br>'
        f'Channel: {html.escape(str(rf.get("channel", "-")))}<br>'
        f'Radio: {html.escape(str(radio.get("protocol", "-")))} / {html.escape(str(radio.get("fixed_rate", "-")))}<br>'
        f'Repetitions/direction: {html.escape(str(rf.get("repetitions_per_direction", "-")))}<br>'
        f'Packets/repetition: {html.escape(str(rf.get("packets_per_repetition", "-")))}<br>'
        f'REF→DUT: TX {fmt(ref_to_dut.get("tx_power_dbm"),0," dBm")} / '
        f'loss {fmt(ref_to_dut.get("loss_percent"),2," %")} / RSSI {fmt(ref_to_dut.get("rssi_average_dbm"),1," dBm")}<br>'
        f'DUT→REF: TX {fmt(dut_to_ref.get("tx_power_dbm"),0," dBm")} / '
        f'loss {fmt(dut_to_ref.get("loss_percent"),2," %")} / RSSI {fmt(dut_to_ref.get("rssi_average_dbm"),1," dBm")}</div>'
    )


def _rf_html_details(rf: dict[str, Any]) -> str:
    rows: list[str] = []
    for direction_key, label in (
        ("reference_to_dut", "REF → DUT"),
        ("dut_to_reference", "DUT → REF"),
    ):
        direction = rf.get(direction_key, {}) if isinstance(rf, dict) else {}
        if not isinstance(direction, dict):
            continue
        power = direction.get("tx_power_dbm")
        for repetition in direction.get("repetitions", []):
            if not isinstance(repetition, dict):
                continue
            timeout = bool(repetition.get("measurement_timeout", False))
            rows.append(
                f"<tr><td>{html.escape(label)}</td>"
                f"<td>{html.escape(str(repetition.get('repetition', '-')))}</td>"
                f"<td>{fmt(power,0,' dBm')}</td>"
                f"<td>{html.escape(str(repetition.get('packets_received', '-')))} / {html.escape(str(repetition.get('packets_sent', '-')))}</td>"
                f"<td>{fmt(repetition.get('loss_percent'),2,' %')}</td>"
                f"<td>{fmt(repetition.get('rssi_average_dbm'),1,' dBm')}</td>"
                f"<td>{fmt(repetition.get('rssi_min_dbm'),1,' dBm')} / {fmt(repetition.get('rssi_max_dbm'),1,' dBm')}</td>"
                f"<td class='{('fail' if timeout else 'pass')}'>{'TIMEOUT' if timeout else 'OK'}</td></tr>"
            )
    if not rows:
        return "<p>No RF repetition data available.</p>"
    return (
        "<div class='table-wrap'><table><tr>"
        "<th>Direction</th><th>Repetition</th><th>TX</th><th>Received / sent</th>"
        "<th>Loss</th><th>RSSI avg</th><th>RSSI min/max</th><th>Status</th></tr>"
        + "".join(rows)
        + "</table></div>"
    )

def write_text_report(path: Path, summary: dict[str, Any]) -> None:
    wifi = summary["wifi"]
    heap_info = summary["heap"]
    system_info = summary.get("system", {})
    performance = summary.get("performance", {})
    ram_perf = performance.get("ram", {})
    psram_perf = performance.get("psram", {})
    flash_perf = performance.get("flash", {})
    cpu_perf = performance.get("cpu", {})
    preflight = summary.get("preflight", {})
    reconnect = preflight.get("reconnect", {})
    coex = preflight.get("ble_coexistence", {})
    deep_sleep = preflight.get("deep_sleep", {})
    peer = summary.get("peer_comparison", {})
    stability = summary.get("stability", summary.get("network_quality", {}))
    rf = summary.get("rf_quality", {})
    rf_ref = rf.get("reference", {}) if isinstance(rf, dict) else {}

    lines = [
        "ESP32 Hardware / Quality Test",
        "=" * 36,
        "",
        f"BOARD: {summary['result']}",
        f"RF QUALITY: {rf.get('quality_status', 'SKIP')}",
        f"STABILITY: {stability.get('status', '-')}",
        f"WiFi quality: {summary.get('network_quality', {}).get('status', '-')}",
        f"Board ID: {summary.get('board_id') or '-'}",
        f"Chip: {summary.get('chip_family') or '-'}",
        f"Revision: {summary['esptool'].get('revision', '-')}",
        f"Flash: {summary['esptool'].get('flash_size', '-')}",
        f"PSRAM (esptool): {summary['esptool'].get('embedded_psram_mb', 0)} MB",
        f"CPU: {system_info.get('cpu_mhz', '-')} MHz / {system_info.get('cores', '-')} core(s)",
        f"MAC: {system_info.get('mac', summary['esptool'].get('mac', '-'))}",
        f"Reset reason: {system_info.get('reset', '-')}",
        f"Internal temperature: {system_info.get('temperature_c', '-')} C",
        ("Home-network stability: OFF" if not summary.get("soak_enabled", True) else f"Home-network stability duration: {summary['duration_seconds'] / 60.0:.1f} min"),
        "",
        f"Complete quality test: {'YES' if summary.get('quality_suite_complete', True) else 'NO'}",
        "",
        "Test selection:",
        "  " + ", ".join(
            f"{name.upper()}={'ON' if enabled else 'OFF'}"
            for name, enabled in summary.get("test_config", {}).items()
        ),
        "",
        "Performance:",
        f"  CPU: {fmt(cpu_perf.get('elapsed_ms'), 3, ' ms')} / {fmt(cpu_perf.get('iterations_per_ms'), 1, ' iterations/ms')}",
        f"  CPU Core 0/1: {fmt(cpu_perf.get('core0_ms'), 3, ' ms')} / {fmt(cpu_perf.get('core1_ms'), 3, ' ms')}",
        f"  RAM write/read: {fmt(ram_perf.get('write_mb_s'), 2, ' MB/s')} / {fmt(ram_perf.get('read_mb_s'), 2, ' MB/s')}",
        f"  PSRAM write/read: {fmt(psram_perf.get('write_mb_s'), 2, ' MB/s')} / {fmt(psram_perf.get('read_mb_s'), 2, ' MB/s')}",
        f"  Flash erase: {fmt(flash_perf.get('erase_ms'), 2, ' ms')}",
        f"  Flash write/read: {fmt(flash_perf.get('write_mb_s'), 2, ' MB/s')} / {fmt(flash_perf.get('read_mb_s'), 2, ' MB/s')}",
        "",
        "Preflight:",
        f"  Deep sleep / RTC wake: {deep_sleep.get('status', '-')}",
        f"  Controlled reconnect: {reconnect.get('status', '-')} / disconnect->GOT_IP {fmt(reconnect.get('disconnect_to_got_ip_ms'), 1, ' ms')} / GOT_IP->Ping {fmt(reconnect.get('got_ip_to_ping_ms'), 1, ' ms')} / GOT_IP->UDP {fmt(reconnect.get('got_ip_to_udp_ms'), 1, ' ms')}",
        f"    AP target/actual: {reconnect.get('target_bssid') or '-'} ch {reconnect.get('target_channel') or '-'} / {reconnect.get('actual_bssid') or '-'} ch {reconnect.get('actual_channel') or '-'}",
        f"  WiFi + BLE coexistence: {coex.get('status', '-')} / BLE devices {coex.get('devices', '-')}",
        "  Unexpected restarts by phase: " + (
            ", ".join(
                f"{phase}={count}"
                for phase, count in sorted(
                    preflight.get("unexpected_restarts_by_phase", {}).items()
                )
            )
            or "none"
        ),
        f"    baseline Ping/UDP loss: {fmt(coex.get('baseline', {}).get('ping_loss_percent'), 1, ' %')} / {fmt(coex.get('baseline', {}).get('udp_loss_percent'), 1, ' %')}",
        f"    active   Ping/UDP loss: {fmt(coex.get('active', {}).get('ping_loss_percent'), 1, ' %')} / {fmt(coex.get('active', {}).get('udp_loss_percent'), 1, ' %')}",
        "",
        *_rf_text_lines(rf, rf_ref),
        "",
        "WiFi soak / home-network stability:",
        f"  Stability status: {stability.get('status', '-')}",
        f"  Network quality: {summary.get('network_quality', {}).get('status', '-')}",
        f"  Test: {'ACTIVE' if wifi.get('enabled', True) else 'SKIP (disabled_by_config)'}",
        f"  Warm-up: {fmt(wifi.get('warmup_seconds'), 0, ' s')} (not scored)",
        f"  Ping: {'ACTIVE' if wifi.get('icmp', {}).get('enabled', True) else 'SKIP (disabled_by_config)'}",
        f"  UDP: {'ACTIVE' if wifi.get('udp', {}).get('enabled', True) else 'SKIP (disabled_by_config)'}",
        f"  Connected BSSID/channel: {wifi.get('bssid') or '-'} / {wifi.get('channel') or '-'}",
        f"  Scan target: {wifi.get('scan_target_bssid') or '-'} / channel {wifi.get('scan_target_channel') or '-'} / {fmt(wifi.get('scan_target_rssi_dbm'), 1, ' dBm')}",
        f"  Average RSSI: {fmt(wifi.get('rssi_average_dbm'), 1, ' dBm')}",
        f"  APs found: {wifi.get('scan_aps') if wifi.get('scan_aps') is not None else '-'}",
        f"  Disconnects: {wifi.get('disconnects', 0)}",
        f"  Ping loss raw / WL_CONNECTED: {fmt(wifi['icmp'].get('loss_percent'), 2, ' %')} / {fmt(wifi['icmp'].get('while_connected', {}).get('loss_percent'), 2, ' %')}",
        f"  Ping latency average/P95: {fmt(wifi['icmp'].get('latency_average_ms'), 1, ' ms')} / {fmt(wifi['icmp'].get('latency_p95_ms'), 1, ' ms')}",
        f"  Longest ping outage raw / WL_CONNECTED: {fmt(wifi['icmp'].get('longest_outage_seconds'), 1, ' s')} / {fmt(wifi['icmp'].get('while_connected', {}).get('longest_outage_seconds'), 1, ' s')}",
        f"  UDP loss raw / WL_CONNECTED: {fmt(wifi['udp'].get('loss_percent'), 2, ' %')} / {fmt(wifi['udp'].get('while_connected', {}).get('loss_percent'), 2, ' %')}",
        f"  Longest data-path outage raw / WL_CONNECTED: {fmt(wifi['udp'].get('longest_outage_seconds'), 1, ' s')} / {fmt(wifi['udp'].get('while_connected', {}).get('longest_outage_seconds'), 1, ' s')}",
        "",
        "Heap:",
        f"  Start: {heap_info.get('start_bytes') or '-'} bytes",
        f"  End: {heap_info.get('end_bytes') or '-'} bytes",
        f"  Minimum: {heap_info.get('minimum_observed_bytes') or '-'} bytes",
        f"  Drop: {heap_info.get('drop_bytes') if heap_info.get('drop_bytes') is not None else '-'} bytes",
        f"  Periodic integrity checks: {heap_info.get('integrity_checks', 0)} / failures {heap_info.get('integrity_failures', 0)}",
        "",
        "Monitoring:",
        f"  Serial heartbeats: {summary.get('monitoring', {}).get('serial_heartbeat', {}).get('samples', 0)}",
        f"  Longest serial gap: {fmt(summary.get('monitoring', {}).get('serial_heartbeat', {}).get('longest_gap_seconds'), 1, ' s')}",
        f"  UDP heartbeats: {summary.get('monitoring', {}).get('udp_heartbeat', {}).get('samples', 0)}",
        f"  Longest UDP-HB gap: {fmt(summary.get('monitoring', {}).get('udp_heartbeat', {}).get('longest_gap_seconds'), 1, ' s')}",
        "",
        "Internal tests:",
    ]
    for name, status in sorted(summary["self_tests"].items()):
        reason = summary.get("self_test_reasons", {}).get(name, "")
        suffix = f" ({reason})" if reason else ""
        lines.append(f"  {name}: {status}{suffix}")

    if peer:
        lines.extend([
            "",
            f"Peer comparison ({peer.get('environment', '-')}, performance peers: {peer.get('performance_peer_boards', 0)}, "
            f"RF peers: {peer.get('rf_peer_boards', 0)}, RF reference: {peer.get('rf_reference_mac') or '-'}):",
        ])
        for name, metric in peer.get("metrics", {}).items():
            unit = metric.get("unit", "")
            suffix = " " + unit if unit else ""
            status = str(metric.get("status") or "-")
            current_text = "SKIP" if metric.get("value") is None else fmt(metric.get("value"), 2, suffix)
            if status == "INSUFFICIENT_DATA":
                historical = (
                    f" / median {fmt(metric.get('median'), 2, suffix)} / "
                    f"Q1-Q3 {fmt(metric.get('q1'), 2, suffix)}-{fmt(metric.get('q3'), 2, suffix)}"
                    if metric.get("median") is not None
                    else ""
                )
                lines.append(
                    f"  {name}: {current_text}{historical} / insufficient reference data "
                    f"({metric.get('samples', 0)}/{peer.get('minimum_samples', 3)})"
                )
                continue
            comparison_text = (
                f"delta {fmt(metric.get('delta_from_median'), 2, suffix)}"
                if metric.get("comparison") == "informational"
                else fmt(
                    (metric.get('ratio') or 0) * 100
                    if metric.get('ratio') is not None else None,
                    0,
                    ' % of median',
                )
            )
            lines.append(
                f"  {name}: {status} / current {current_text} / "
                f"median {fmt(metric.get('median'), 2, suffix)} / "
                f"Q1-Q3 {fmt(metric.get('q1'), 2, suffix)}-{fmt(metric.get('q3'), 2, suffix)} / "
                f"{comparison_text}"
            )

    if summary["fail_reasons"]:
        lines.extend(["", "FAIL reasons board/test:"])
        lines.extend(f"  - {reason}" for reason in summary["fail_reasons"])

    if stability.get("fail_reasons"):
        lines.extend(["", "STABILITY FAIL:"])
        lines.extend(f"  - {reason}" for reason in stability["fail_reasons"])
    if stability.get("warnings"):
        lines.extend(["", "STABILITY WARN:"])
        lines.extend(f"  - {warning}" for warning in stability["warnings"])

    if summary["warnings"]:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in summary["warnings"])

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_html_report(path: Path, summary: dict[str, Any]) -> None:
    wifi = summary["wifi"]
    heap_info = summary["heap"]
    system_info = summary.get("system", {})
    performance = summary.get("performance", {})
    ram_perf = performance.get("ram", {})
    psram_perf = performance.get("psram", {})
    flash_perf = performance.get("flash", {})
    cpu_perf = performance.get("cpu", {})
    preflight = summary.get("preflight", {})
    reconnect = preflight.get("reconnect", {})
    coex = preflight.get("ble_coexistence", {})
    deep_sleep = preflight.get("deep_sleep", {})
    peer = summary.get("peer_comparison", {})
    rf = summary.get("rf_quality", {})
    rf_ref = rf.get("reference", {}) if isinstance(rf, dict) else {}
    restart_phase_text = ", ".join(
        f"{phase}={count}"
        for phase, count in sorted(
            preflight.get("unexpected_restarts_by_phase", {}).items()
        )
    ) or "none"

    result_class = "pass" if summary["result"] == "PASS" else "fail"
    network_quality = summary.get("network_quality", {})
    network_quality_status = str(network_quality.get("status") or "-")
    network_quality_class = (
        "pass" if network_quality_status == "PASS"
        else "fail" if network_quality_status == "FAIL"
        else "warn" if network_quality_status == "WARN"
        else "skip"
    )
    stability = summary.get("stability", network_quality)
    stability_status = str(stability.get("status") or "-")
    stability_class = (
        "pass" if stability_status == "PASS"
        else "fail" if stability_status == "FAIL"
        else "warn" if stability_status == "WARN"
        else "skip"
    )
    rf_status = str(rf.get("quality_status") or "SKIP")
    rf_class = (
        "pass" if rf_status == "PASS"
        else "fail" if rf_status == "FAIL"
        else "warn" if rf_status == "WARN"
        else "skip"
    )

    def status_class(value: Any) -> str:
        text = str(value or "").upper()
        if text in {"PASS", "NORMAL", "DONE", "REFERENCE"}:
            return "pass"
        if text in {"FAIL", "OUTLIER"}:
            return "fail"
        if text == "WARN":
            return "warn"
        return "skip"

    tests = "".join(
        f"<tr><td>{html.escape(name)}</td><td class='{status_class(status)}'>{html.escape(status)}"
        f"{(' (' + html.escape(summary.get('self_test_reasons', {}).get(name, '')) + ')') if summary.get('self_test_reasons', {}).get(name, '') else ''}</td></tr>"
        for name, status in sorted(summary["self_tests"].items())
    )
    reasons = "".join(f"<li>{html.escape(item)}</li>" for item in summary["fail_reasons"]) or "<li>None</li>"
    warnings = "".join(f"<li>{html.escape(item)}</li>" for item in summary["warnings"]) or "<li>None</li>"
    stability_reasons = (
        "".join(f"<li>{html.escape(item)}</li>" for item in stability.get("fail_reasons", []))
        or "<li>None</li>"
    )
    stability_warnings = (
        "".join(f"<li>{html.escape(item)}</li>" for item in stability.get("warnings", []))
        or "<li>None</li>"
    )

    peer_rows: list[str] = []
    for name, metric in peer.get("metrics", {}).items():
        unit = str(metric.get("unit") or "")
        unit_suffix = f" {unit}" if unit else ""
        status = str(metric.get("status") or "-")
        current_text = "SKIP" if metric.get("value") is None else fmt(metric.get("value"), 2, unit_suffix)
        if status == "INSUFFICIENT_DATA":
            sample_text = f"{metric.get('samples', 0)}/{peer.get('minimum_samples', 3)}"
            peer_rows.append(
                f"<tr><td>{html.escape(name)}</td><td class='skip'>{html.escape(status)}</td>"
                f"<td>{current_text}</td>"
                f"<td>{fmt(metric.get('median'), 2, unit_suffix)}</td>"
                f"<td>{fmt(metric.get('q1'), 2, unit_suffix)}</td>"
                f"<td>{fmt(metric.get('q3'), 2, unit_suffix)}</td>"
                f"<td>{html.escape(sample_text)}</td></tr>"
            )
            continue
        comparison_text = (
            fmt(metric.get("delta_from_median"), 2, unit_suffix)
            if metric.get("comparison") == "informational"
            else fmt(
                (metric.get("ratio") or 0) * 100
                if metric.get("ratio") is not None else None,
                0,
                " %",
            )
        )
        peer_rows.append(
            f"<tr><td>{html.escape(name)}</td><td class='{status_class(status)}'>{html.escape(status)}</td>"
            f"<td>{current_text}</td>"
            f"<td>{fmt(metric.get('median'), 2, unit_suffix)}</td>"
            f"<td>{fmt(metric.get('q1'), 2, unit_suffix)}</td>"
            f"<td>{fmt(metric.get('q3'), 2, unit_suffix)}</td>"
            f"<td>{comparison_text}</td></tr>"
        )
    peer_table = "".join(peer_rows) or "<tr><td colspan='7'>No comparable measurements available yet.</td></tr>"
    rf_summary_card = _rf_html_summary(rf, rf_ref, rf_class, rf_status)
    rf_details = _rf_html_details(rf)

    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ESP32 Hardware Test - {html.escape(str(summary.get('board_id') or '-'))} - {summary['result']}</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 32px; background:#f5f6f8; color:#1f2328; }}
main {{ max-width: 1180px; margin:auto; background:white; padding:28px; border-radius:12px; box-shadow:0 2px 12px #0001; }}
h1 {{ margin-top:0; }}
.result {{ font-size:34px; font-weight:700; padding:14px 18px; border-radius:10px; display:inline-block; }}
.result.pass {{ background:#d9fbe4; color:#116329; }} .result.fail {{ background:#ffe0e0; color:#9a1c1c; }}
.grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; margin:22px 0; }}
@media screen and (max-width:900px) {{ .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
@media screen and (max-width:620px) {{ .grid {{ grid-template-columns:1fr; }} }}
.card {{ background:#f7f8fa; border:1px solid #e4e7eb; border-radius:10px; padding:16px; }}
table {{ border-collapse:collapse; width:100%; }} th,td {{ text-align:left; padding:9px; border-bottom:1px solid #e5e7eb; }}
.pass {{ color:#16713a; font-weight:700; }} .fail {{ color:#a51d1d; font-weight:700; }} .warn {{ color:#9a6700; font-weight:700; }} .skip {{ color:#666; }}
small {{ color:#667; }} .table-wrap {{ overflow-x:auto; }}
@page {{ size: A4 portrait; margin: 8mm; }}
@media print {{
  * {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
  html, body {{ width: 210mm; min-height: 297mm; }}
  body {{ margin:0; background:white; font-size:9pt; line-height:1.18; }}
  main {{ max-width:none; margin:0; padding:0; border-radius:0; box-shadow:none; }}
  h1 {{ margin:0 0 2mm; font-size:18pt; }} h2 {{ margin:3mm 0 1.5mm; font-size:12pt; break-after:avoid; }}
  p {{ margin:2mm 0; }} .result {{ font-size:23pt; padding:2mm 4mm; border-radius:2mm; }}
  .grid {{ grid-template-columns:repeat(3,1fr); gap:2.5mm; margin:3mm 0; }}
  .card {{ padding:2.5mm; border-radius:2mm; break-inside:avoid; }}
  table {{ font-size:8.3pt; }} th,td {{ padding:1mm 1.5mm; line-height:1.12; }}
  ul {{ margin:1mm 0 2mm 5mm; padding-left:4mm; }} li {{ margin:0.4mm 0; }} small {{ font-size:7.5pt; }}
}}
</style>
</head>
<body><main>
<h1>ESP32 Hardware / Quality Test</h1>
<div class="result {result_class}">BOARD: {summary['result']}</div>
<p><b>RF QUALITY: <span class="{rf_class}">{html.escape(rf_status)}</span></b> &middot; STABILITY: <span class="{stability_class}">{html.escape(stability_status)}</span> &middot; WiFi quality: <span class="{network_quality_class}">{html.escape(network_quality_status)}</span></p>
<p><small>{html.escape(summary['timestamp'])} &middot; home-network stability {('OFF' if not summary.get('soak_enabled', True) else ('%.1f min' % (summary['duration_seconds']/60.0)))} &middot; profile {html.escape(summary['environment'])}</small></p>

<div class="grid">
<div class="card"><b>Board</b><br><b>ID: {html.escape(str(summary.get('board_id') or '-'))}</b><br>Chip: {html.escape(str(summary.get('chip_family') or '-'))}<br>Revision: {html.escape(str(summary['esptool'].get('revision','-')))}<br>CPU: {html.escape(str(system_info.get('cpu_mhz','-')))} MHz / {html.escape(str(system_info.get('cores','-')))} core(s)<br>Flash: {html.escape(str(summary['esptool'].get('flash_size','-')))}<br>PSRAM: {summary['esptool'].get('embedded_psram_mb',0)} MB<br>Reset: {html.escape(str(system_info.get('reset','-')))}<br>Temp.: {html.escape(str(system_info.get('temperature_c','-')))} C</div>
{rf_summary_card}
<div class="card"><b>Home-network Stability</b><br>Stability: <span class="{stability_class}">{html.escape(stability_status)}</span><br>Network quality: <span class="{network_quality_class}">{html.escape(network_quality_status)}</span><br>Warm-up: {fmt(wifi.get('warmup_seconds'),0,' s')}<br>RSSI avg.: {fmt(wifi.get('rssi_average_dbm'),1,' dBm')}<br>Disconnects/Reconnects: {wifi.get('disconnects',0)} / {wifi.get('reconnects',0)}<br>Ping loss raw/connected: {fmt(wifi['icmp'].get('loss_percent'),2,' %')} / {fmt(wifi['icmp'].get('while_connected', {}).get('loss_percent'),2,' %')}<br>UDP loss raw/connected: {fmt(wifi['udp'].get('loss_percent'),2,' %')} / {fmt(wifi['udp'].get('while_connected', {}).get('loss_percent'),2,' %')}<br>Ping outage raw/connected: {fmt(wifi['icmp'].get('longest_outage_seconds'),1,' s')} / {fmt(wifi['icmp'].get('while_connected', {}).get('longest_outage_seconds'),1,' s')}<br>UDP outage raw/connected: {fmt(wifi['udp'].get('longest_outage_seconds'),1,' s')} / {fmt(wifi['udp'].get('while_connected', {}).get('longest_outage_seconds'),1,' s')}</div>
<div class="card"><b>Memory / Flash Performance</b><br>RAM W/R: {fmt(ram_perf.get('write_mb_s'),1,' / ')}{fmt(ram_perf.get('read_mb_s'),1,' MB/s')}<br>PSRAM W/R: {fmt(psram_perf.get('write_mb_s'),1,' / ')}{fmt(psram_perf.get('read_mb_s'),1,' MB/s')}<br>Flash erase: {fmt(flash_perf.get('erase_ms'),1,' ms')}<br>Flash W/R: {fmt(flash_perf.get('write_mb_s'),1,' / ')}{fmt(flash_perf.get('read_mb_s'),1,' MB/s')}<br>CPU kernel: {fmt(cpu_perf.get('elapsed_ms'),3,' ms')}<br>CPU score: {fmt(cpu_perf.get('iterations_per_ms'),1,' iter/ms')}<br>Core0/Core1: {fmt(cpu_perf.get('core0_ms'),3,' / ')}{fmt(cpu_perf.get('core1_ms'),3,' ms')}</div>
<div class="card"><b>Preflight</b><br>Deep sleep: <span class="{status_class(deep_sleep.get('status'))}">{html.escape(str(deep_sleep.get('status','-')))}</span><br>Reconnect: <span class="{status_class(reconnect.get('status'))}">{html.escape(str(reconnect.get('status','-')))}</span><br>Reconnect AP: {html.escape(str(reconnect.get('target_bssid') or '-'))} / ch {html.escape(str(reconnect.get('target_channel') or '-'))} → {html.escape(str(reconnect.get('actual_bssid') or '-'))} / ch {html.escape(str(reconnect.get('actual_channel') or '-'))}<br>Disconnect→IP: {fmt(reconnect.get('disconnect_to_got_ip_ms'),1,' ms')}<br>IP→Ping: {fmt(reconnect.get('got_ip_to_ping_ms'),1,' ms')}<br>IP→UDP: {fmt(reconnect.get('got_ip_to_udp_ms'),1,' ms')}<br>WiFi+BLE: <span class="{status_class(coex.get('status'))}">{html.escape(str(coex.get('status','-')))}</span><br>BLE devices: {html.escape(str(coex.get('devices','-')))}<br>Unexpected restarts: {html.escape(restart_phase_text)}</div>
<div class="card"><b>Heap / Monitoring</b><br>Heap start/end: {heap_info.get('start_bytes') or '-'} / {heap_info.get('end_bytes') or '-'} B<br>Heap min: {heap_info.get('minimum_observed_bytes') or '-'} B<br>Heap drop: {heap_info.get('drop_bytes') if heap_info.get('drop_bytes') is not None else '-'} B<br>Integrity checks: {heap_info.get('integrity_checks',0)}<br>Integrity failures: {heap_info.get('integrity_failures',0)}<br>Serial max gap: {fmt(summary.get('monitoring', {}).get('serial_heartbeat', {}).get('longest_gap_seconds'),1,' s')}<br>UDP-HB max gap: {fmt(summary.get('monitoring', {}).get('udp_heartbeat', {}).get('longest_gap_seconds'),1,' s')}</div>
</div>

<h2>RF Measurement Details</h2>
{rf_details}

<h2>Internal Tests</h2><table><tr><th>Test</th><th>Status</th></tr>{tests}</table>

<h2>Peer Comparison</h2>
<p><small>Profile {html.escape(str(peer.get('environment') or summary.get('environment') or '-'))}; {peer.get('performance_peer_boards',0)} performance peers; {peer.get('rf_peer_boards',0)} RF peers for reference {html.escape(str(peer.get('rf_reference_mac') or '-'))}. Q1–Q3 shows the middle 50% range. RF peer values are informational; performance deviations can create warnings but do not cause a hardware FAIL by themselves.</small></p>
<div class="table-wrap"><table><tr><th>Metric</th><th>Status</th><th>Current</th><th>Median</th><th>Q1</th><th>Q3</th><th>Relative / delta</th></tr>{peer_table}</table></div>

<h2>FAIL Reasons Board/Test</h2><ul>{reasons}</ul>
<h2>STABILITY FAIL</h2><ul>{stability_reasons}</ul>
<h2>STABILITY WARN</h2><ul>{stability_warnings}</ul>
<h2>Warnings</h2><ul>{warnings}</ul>
<p><small>RF QUALITY is measured directly between the DUT and the dedicated USB-connected reference ESP. Home-network soak/stability remains diagnostic because AP, host, routing, and local interference are not controlled RF references. RF thresholds should be calibrated from independently verified good DUTs and known-bad/weak DUTs before RF QUALITY is used as an absolute production limit.</small></p>
</main></body></html>"""
    path.write_text(document, encoding="utf-8")


def open_report(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
    except Exception:
        pass


def run_label_workflow(summary_path: Path, label_mode: str) -> None:
    if os.name != "nt":
        return

    label_script = ROOT / "scripts" / "label.ps1"
    if not label_script.is_file():
        print(f"Note: label script is missing: {label_script}")
        return

    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(label_script),
        "-SummaryPath",
        str(summary_path),
    ]

    if label_mode == "ask":
        command.append("-AskBeforePrint")

    print("\nGenerating and printing label ...")

    try:
        result = subprocess.run(command, cwd=str(ROOT), check=False)
    except Exception as exc:
        print(f"Note: label generation/printing could not be started: {exc}")
        return

    if result.returncode != 0:
        print(
            "Note: label generation/printing failed. "
            "The hardware test result remains unchanged."
        )


def main() -> int:
    """Compatibility entry point; the supported orchestrator lives in run_test.py."""
    runner = ROOT / "tools" / "run_test.py"
    command = [sys.executable, str(runner), *sys.argv[1:]]
    result = subprocess.run(
        command,
        cwd=str(ROOT),
        check=False,
    )
    return int(result.returncode)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nTest aborted.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        raise SystemExit(1)
