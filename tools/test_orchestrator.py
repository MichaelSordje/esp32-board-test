from __future__ import annotations

import argparse
import copy
import json
import queue
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import serial

import test_runner as tr
from reference_device import ReferenceDevice, find_reference_device
import rf_quality_test


# Soak is a host-side quality-test phase. It uses the same configuration model
# as every other test, but has no firmware self-test macro of its own.
QUALITY_REQUIRED_TESTS = tr.QUALITY_REQUIRED_TESTS
SOAK_COMPONENT_TESTS = ("ping", "udp", "heap_integrity")


def resolve_effective_test_config(
    settings: dict[str, Any],
    environment: str,
) -> dict[str, bool]:
    return tr.resolve_test_config(settings, environment)


def is_complete_quality_suite(test_config: dict[str, bool]) -> bool:
    return tr.is_complete_quality_suite(test_config)


def _unexpected_restart_count(state: tr.RunState) -> int:
    return sum(
        max(0, int(count))
        for count in state.unexpected_restarts_by_phase.values()
    )


def _latest_boot_timestamp(state: tr.RunState) -> float:
    for event in reversed(state.protocol_events):
        if event.get("category") == "BOOT":
            value = event.get("timestamp")
            if isinstance(value, (int, float)):
                return float(value)
    return time.time()


def _record_primary_restart_failure(
    state: tr.RunState,
    phase: str,
) -> None:
    if getattr(state, "primary_failure", None):
        return

    state.primary_failure = {
        "type": "unexpected_restart",
        "phase": phase or "unknown",
        "timestamp": _latest_boot_timestamp(state),
        "reset_reason": "",
    }
    state.abort_remaining_tests = True
    # The pre-reset IP remains useful for the report, but the current runtime
    # connection no longer exists after an unexpected reboot.
    state.connected = False


def _refresh_primary_restart_reason(state: tr.RunState) -> None:
    failure = getattr(state, "primary_failure", None)
    if not isinstance(failure, dict) or not failure:
        return
    if failure.get("reset_reason"):
        return

    restart_timestamp = float(failure.get("timestamp") or 0.0)
    for event in state.protocol_events:
        if event.get("category") != "SYSTEM":
            continue
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, (int, float)):
            continue
        if float(timestamp) < restart_timestamp:
            continue
        reset_reason = str(event.get("reset") or "").strip()
        if not reset_reason:
            continue
        failure["reset_reason"] = reset_reason
        failure["system_timestamp"] = float(timestamp)
        return


def _primary_failure_text(state: tr.RunState) -> str:
    failure = getattr(state, "primary_failure", None)
    if not isinstance(failure, dict) or not failure:
        return ""

    reset_reason = str(failure.get("reset_reason") or "UNKNOWN").strip().upper()
    phase = str(failure.get("phase") or "unknown").strip()
    phase_labels = {
        "startup": "startup/self-test",
        "self_tests": "internal hardware tests",
        "deep_sleep": "deep-sleep test",
        "rf_quality": "RF quality test",
        "wifi_setup": "WiFi setup/warm-up",
        "soak": "soak test",
        "reconnect": "controlled reconnect test",
        "ble_coexistence": "WiFi + BLE coexistence test",
        "complete": "test finalization",
    }
    phase_text = phase_labels.get(phase, phase.replace("_", " "))
    return f"Unexpected {reset_reason} reset during {phase_text}"


def _mark_followup_tests_skipped(
    state: tr.RunState,
    test_config: dict[str, bool],
) -> None:
    if test_config.get("reconnect", False):
        state.reconnect_test = {
            "status": "SKIP",
            "reason": "aborted_after_unexpected_restart",
        }
        print(
            "Controlled WiFi reconnect test: SKIP / "
            "aborted after unexpected DUT restart"
        )

    if test_config.get("ble_coexistence", False):
        state.ble_coexistence = {
            "status": "SKIP",
            "reason": "aborted_after_unexpected_restart",
            "devices": None,
            "baseline": {},
            "active": {},
            "ping_loss_delta_percent": None,
            "udp_loss_delta_percent": None,
        }
        print(
            "WiFi + BLE coexistence test: SKIP / "
            "aborted after unexpected DUT restart"
        )


def _apply_primary_failure_to_summary(
    summary: dict[str, Any],
    state: tr.RunState,
) -> None:
    failure = getattr(state, "primary_failure", None)
    if not isinstance(failure, dict) or not failure:
        return

    _refresh_primary_restart_reason(state)
    failure = getattr(state, "primary_failure")
    reason = _primary_failure_text(state)
    phase = str(failure.get("phase") or "unknown")

    # Keep genuine failures that happened independently, but remove generic
    # restart/follow-up failures that are only consequences of the same reboot.
    filtered_fail_reasons: list[str] = []
    for item in summary.get("fail_reasons", []):
        text = str(item)
        if text.startswith("Unexpected restart during "):
            continue
        if (
            text.startswith("Controlled WiFi reconnect test failed")
            and state.reconnect_test.get("reason")
            == "aborted_after_unexpected_restart"
        ):
            continue
        if (
            text.startswith("WiFi + BLE coexistence test failed")
            and state.ble_coexistence.get("reason")
            == "aborted_after_unexpected_restart"
        ):
            continue
        filtered_fail_reasons.append(text)

    if reason and reason not in filtered_fail_reasons:
        filtered_fail_reasons.append(reason)

    summary["fail_reasons"] = filtered_fail_reasons
    summary["result"] = "FAIL"
    summary["primary_failure"] = {
        **failure,
        "message": reason,
    }

    preflight = summary.setdefault("preflight", {})
    preflight["primary_failure"] = copy.deepcopy(summary["primary_failure"])

    # A reboot during the soak invalidates all probes after that point. The raw
    # pre-reset measurements remain in CSV/JSON, but network thresholds are not
    # used to manufacture secondary failures from a runtime state that no
    # longer exists.
    if phase == "soak":
        summary["network_quality"] = {
            "status": "SKIP",
            "fail_reasons": [],
            "warnings": [],
            "skip_reason": "aborted_after_unexpected_restart",
        }
        summary["stability"] = {
            "status": "FAIL",
            "fail_reasons": [reason],
            "warnings": [],
        }

        soak = summary.setdefault("soak", {})
        soak["status"] = "FAIL"
        soak["aborted"] = True
        soak["abort_reason"] = "unexpected_restart"

        wifi = summary.setdefault("wifi", {})
        wifi["soak_aborted"] = True
        wifi["soak_abort_reason"] = "unexpected_restart"
        for key in ("icmp", "udp"):
            probe = wifi.get(key)
            if isinstance(probe, dict):
                probe["scored"] = False
                probe["score_skip_reason"] = (
                    "aborted_after_unexpected_restart"
                )

        heap = summary.get("heap")
        if isinstance(heap, dict):
            heap["soak_aborted"] = True
            heap["score_skip_reason"] = (
                "aborted_after_unexpected_restart"
            )

        summary["warnings"] = [
            str(item)
            for item in summary.get("warnings", [])
            if not str(item).startswith("Stability FAIL (diagnostic):")
            and not str(item).startswith("Stability WARN:")
            and not (
                str(item).startswith("BLE coexistence not rated")
                and "aborted_after_unexpected_restart" in str(item)
            )
        ]


def _summary_state_without_soak(state: tr.RunState) -> tr.RunState:
    summary_state = copy.deepcopy(state)
    now = time.time()

    latest = (
        dict(summary_state.heartbeat_samples[-1])
        if summary_state.heartbeat_samples
        else {}
    )
    latest.update(
        {
            "timestamp": now,
            "wifi": 1 if state.wifi_ip else 0,
            "disconnects": 0,
            "reconnects": 0,
            "uptime_ms": max(
                1,
                int(latest.get("uptime_ms", 0)) + 1,
            ),
        }
    )

    if "rssi" not in latest:
        latest["rssi"] = (
            int(state.wifi_scan_target_rssi)
            if state.wifi_scan_target_rssi is not None
            else -127
        )

    # Heap/disconnect/heartbeat trends are soak metrics. Preserve one current
    # sample so preflight runtime cannot be mistaken for a long-soak failure.
    latest_heap = int(latest.get("heap_free", 0) or 0)
    latest["heap_free"] = latest_heap
    latest["heap_min"] = latest_heap

    summary_state.heartbeat_samples = [latest]
    summary_state.udp_heartbeat_samples = []
    summary_state.disconnect_reasons = []
    summary_state.heap_integrity_checks = []
    return summary_state


def build_summary(
    settings: dict[str, Any],
    esp_info: dict[str, Any],
    state: tr.RunState,
    probe_results: list[tr.ProbeResult],
    duration_seconds: float,
    environment: str,
    board_id: str,
    test_config: dict[str, bool],
) -> dict[str, Any]:
    soak_enabled = bool(test_config.get("soak", True))

    if soak_enabled:
        summary = tr.build_summary(
            settings,
            esp_info,
            state,
            probe_results,
            duration_seconds,
            environment,
            board_id,
            test_config,
        )
    else:
        summary_state = _summary_state_without_soak(state)

        # tr.build_summary evaluates the long-run WiFi quality metrics based
        # on these switches. Disable them only in the temporary evaluation
        # copy; the actual configured test_config is preserved below.
        summary_config = dict(test_config)
        summary_config["wifi"] = False
        for name in SOAK_COMPONENT_TESTS:
            summary_config[name] = False

        summary = tr.build_summary(
            settings,
            esp_info,
            summary_state,
            [],
            0.0,
            environment,
            board_id,
            summary_config,
        )
        summary["test_config"] = dict(test_config)
        summary["network_quality"] = {
            "status": "SKIP",
            "fail_reasons": [],
            "warnings": [],
        }
        summary["wifi"]["enabled"] = bool(
            test_config.get("wifi", False)
        )
        summary["wifi"]["icmp"]["configured"] = bool(
            test_config.get("ping", False)
        )
        summary["wifi"]["icmp"]["skip_reason"] = (
            "soak_disabled"
            if test_config.get("ping", False)
            else "disabled_by_config"
        )
        summary["wifi"]["udp"]["configured"] = bool(
            test_config.get("udp", False)
        )
        summary["wifi"]["udp"]["skip_reason"] = (
            "soak_disabled"
            if test_config.get("udp", False)
            else "disabled_by_config"
        )
        summary["heap"]["configured"] = bool(
            test_config.get("heap_integrity", False)
        )
        summary["heap"]["skip_reason"] = (
            "soak_disabled"
            if test_config.get("heap_integrity", False)
            else "disabled_by_config"
        )

    summary["duration_seconds"] = (
        round(duration_seconds, 1)
        if soak_enabled
        else 0.0
    )
    summary["quality_suite_complete"] = (
        is_complete_quality_suite(test_config)
    )
    summary["soak"] = {
        "enabled": soak_enabled,
        "duration_seconds": summary["duration_seconds"],
        "status": summary.get("stability", {}).get("status", "SKIP"),
    }
    summary.setdefault("wifi", {})["soak_enabled"] = soak_enabled

    _apply_primary_failure_to_summary(summary, state)
    return summary


def _postprocess_report_text(
    path: Path,
    summary: dict[str, Any],
) -> None:
    if summary.get("soak", {}).get("enabled", True):
        return

    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "Home-network stability duration: 0.0 min",
        "Home-network stability: SKIP (soak_disabled)",
        1,
    )

    marker = "WiFi soak / home-network stability:\n"
    index = text.find(marker)
    if index >= 0:
        before = text[: index + len(marker)]
        after = text[index + len(marker) :]
        after = after.replace(
            "  Test: ACTIVE",
            "  Test: SKIP (soak_disabled)",
            1,
        )
        after = after.replace(
            "  Test: SKIP (disabled_by_config)",
            "  Test: SKIP (soak_disabled)",
            1,
        )

        test_config = summary.get("test_config", {})
        if test_config.get("ping", False):
            after = after.replace(
                "  Ping: SKIP (disabled_by_config)",
                "  Ping: SKIP (soak_disabled)",
                1,
            )
        if test_config.get("udp", False):
            after = after.replace(
                "  UDP: SKIP (disabled_by_config)",
                "  UDP: SKIP (soak_disabled)",
                1,
            )
        text = before + after

    if summary.get("test_config", {}).get(
        "heap_integrity",
        False,
    ):
        text = text.replace(
            "Heap:\n",
            "Heap:\n"
            "  Soak monitoring: SKIP (soak_disabled)\n",
            1,
        )

    path.write_text(text, encoding="utf-8")


def _postprocess_report_html(
    path: Path,
    summary: dict[str, Any],
) -> None:
    if summary.get("soak", {}).get("enabled", True):
        return

    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "home-network stability 0.0 min",
        "soak disabled",
        1,
    )
    text = text.replace(
        "<b>Home-network Stability</b>",
        "<b>Home-network Stability (SKIP)</b>"
        "<br>Test: soak_disabled",
        1,
    )

    if (
        summary.get("test_config", {}).get("ping", False)
        or summary.get("test_config", {}).get("udp", False)
    ):
        text = text.replace(
            "Ping loss: -<br>UDP loss: -",
            "Ping/UDP: SKIP (soak_disabled)"
            "<br>Ping loss: -<br>UDP loss: -",
            1,
        )

    if summary.get("test_config", {}).get(
        "heap_integrity",
        False,
    ):
        text = text.replace(
            "<b>Heap / Monitoring</b>",
            "<b>Heap / Monitoring</b>"
            "<br>Soak monitoring: SKIP (soak_disabled)",
            1,
        )

    path.write_text(text, encoding="utf-8")


def write_reports(
    run_dir: Path,
    summary: dict[str, Any],
) -> None:
    text_path = run_dir / "report.txt"
    html_path = run_dir / "report.html"

    tr.write_text_report(text_path, summary)
    tr.write_html_report(html_path, summary)
    _postprocess_report_text(text_path, summary)
    _postprocess_report_html(html_path, summary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--duration-minutes",
        type=int,
        default=0,
    )
    parser.add_argument("--port", default="")
    parser.add_argument("--reference-port", default="")
    args = parser.parse_args()

    raw_settings = tr.load_json(tr.SETTINGS_PATH)
    label_mode = tr.resolve_label_mode(raw_settings)
    serial_settings = raw_settings.get("serial", {})
    if not isinstance(serial_settings, dict):
        raise RuntimeError("test-settings.json: 'serial' must be an object.")
    reference_baud = int(serial_settings.get("reference_baud", 115200))

    tr.RESULTS_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )
    started_at = datetime.now()

    reference: ReferenceDevice | None = find_reference_device(
        requested_port=args.reference_port,
        baudrate=reference_baud,
        required=False,
    )
    if reference is not None:
        print(
            "Reference ESP found automatically: "
            f"{reference.identity.port} / {reference.identity.mac} / "
            f"FW {reference.identity.version}"
        )

    port, esptool_output = tr.detect_esp_port(
        args.port,
        exclude_ports={reference.identity.port} if reference is not None else set(),
    )
    esp_info = tr.parse_esptool_info(esptool_output)
    environment = tr.choose_environment(esp_info)
    test_config = resolve_effective_test_config(
        raw_settings,
        environment,
    )
    resolved_test_settings = tr.resolve_test_settings(raw_settings, environment)
    settings = tr.resolve_execution_settings(raw_settings, environment)
    duration_minutes = (
        args.duration_minutes
        if args.duration_minutes != 0
        else int(resolved_test_settings["soak"].get("duration_minutes", 15))
    )
    if duration_minutes < 0:
        raise RuntimeError("Test duration must not be negative.")

    soak_enabled = bool(test_config["soak"])
    rf_quality_enabled = bool(test_config.get("rf_quality", False))
    if rf_quality_enabled and reference is None:
        # Re-run with required=True only to produce the precise setup error.
        reference = find_reference_device(
            requested_port=args.reference_port,
            baudrate=int(settings["serial"].get("reference_baud", 115200)),
            required=True,
        )
    if not rf_quality_enabled and reference is not None:
        reference.close()
        reference = None

    ssid, password = tr.resolve_wifi_credentials(test_config)

    if soak_enabled and duration_minutes < 1:
        raise RuntimeError(
            "Test duration must be at least 1 minute "
            "when soak=true."
        )

    if test_config["wifi"] and not ssid:
        raise RuntimeError(
            "WiFi configuration is missing although "
            "a WiFi-dependent test is enabled."
        )

    print(
        f"Soak test: "
        f"{'enabled' if soak_enabled else 'disabled'}"
        + (
            f" / {duration_minutes} minutes"
            if soak_enabled
            else ""
        )
    )
    print(
        f"Found: "
        f"{esp_info.get('chip_family','ESP32')} "
        f"on {port}"
    )
    if esp_info.get("flash_size"):
        print(f"Flash: {esp_info['flash_size']}")
    if esp_info.get("embedded_psram_mb"):
        print(
            f"PSRAM: "
            f"{esp_info['embedded_psram_mb']} MB"
        )

    if not soak_enabled:
        configured_components = [
            name.upper()
            for name in SOAK_COMPONENT_TESTS
            if test_config.get(name, False)
        ]
        print(
            "Soak components not executed: "
            + (
                ", ".join(configured_components)
                if configured_components
                else "-"
            )
        )

    enabled_tests = ", ".join(
        name.upper()
        for name, enabled in test_config.items()
        if enabled
    )
    disabled_tests = ", ".join(
        name.upper()
        for name, enabled in test_config.items()
        if not enabled
    )
    print(
        f"Tests configured enabled:  "
        f"{enabled_tests or '-'}"
    )
    print(
        f"Tests configured disabled: "
        f"{disabled_tests or '-'}"
    )

    # Tool/build failures are test-environment failures, not board failures.
    # Verify them before assigning a board ID, creating a result directory,
    # or flashing anything to the DUT.
    tr.preflight_test_environment(
        environment,
        test_config,
        settings,
    )

    board_id = tr.get_or_assign_board_id(esp_info)
    board_id = tr.normalize_board_id(board_id)

    tr.cleanup_pending_result_dirs(board_id)
    run_dir = tr.RESULTS_ROOT / (
        f".pending_{tr.safe_path_component(board_id)}_"
        f"{started_at.strftime('%Y-%m-%d_%H-%M-%S')}"
    )
    run_dir.mkdir(
        parents=True,
        exist_ok=False,
    )
    if reference is not None:
        reference.set_log_path(run_dir / "reference-serial.log")
    (run_dir / "esptool.txt").write_text(
        esptool_output,
        encoding="utf-8",
    )
    (run_dir / "test-config.json").write_text(
        json.dumps(
            {
                "environment": environment,
                "tests": test_config,
                "resolved_test_settings": resolved_test_settings,
                "soak_duration_minutes": (
                    duration_minutes
                    if soak_enabled
                    else 0
                ),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Build and flash the normal Arduino quality-test firmware.
    tr.write_generated_test_config(
        environment,
        test_config,
    )
    try:
        tr.flash_firmware(
            port,
            environment,
            run_dir / "build-flash.log",
        )
    finally:
        try:
            tr.GENERATED_TEST_CONFIG_PATH.unlink()
        except FileNotFoundError:
            pass

    state = tr.RunState()
    probe_results: list[tr.ProbeResult] = []
    serial_log_path = run_dir / "serial.log"
    serial_port: serial.Serial | None = None
    udp_socket: socket.socket | None = None
    stop_ping = threading.Event()
    ping_queue: queue.Queue[tr.ProbeResult] = (
        queue.Queue()
    )
    ping_thread: threading.Thread | None = None
    stop_serial = threading.Event()
    serial_queue: queue.Queue[
        tuple[float, str, str]
    ] = queue.Queue()
    serial_thread: threading.Thread | None = None
    measurement_started_monotonic: float | None = (
        None
    )
    measurement_finished_monotonic: float | None = (
        None
    )
    soak_state_for_summary: tr.RunState | None = None
    soak_aborted_by_restart = False

    try:
        time.sleep(0.3)
        serial_port = serial.Serial(
            port,
            int(settings["serial"].get("dut_baud", 115200)),
            timeout=0.15,
            write_timeout=2,
            rtscts=False,
            dsrdtr=False,
        )
        serial_port.dtr = False
        serial_port.rts = False

        with serial_log_path.open(
            "w",
            encoding="utf-8",
            buffering=1,
        ) as serial_log:
            state.current_phase = "self_tests"
            print(
                "Running internal hardware tests ..."
            )
            tr.wait_for_firmware(
                serial_port,
                state,
                serial_log,
            )

            if test_config.get(
                "deep_sleep",
                False,
            ):
                state.current_phase = "deep_sleep"
                serial_port = (
                    tr.run_deep_sleep_cycle(
                        serial_port,
                        port,
                        int(settings["serial"].get("dut_baud", 115200)),
                        state,
                        serial_log,
                    )
                )
            else:
                state.self_tests[
                    "DEEP_SLEEP"
                ] = "SKIP"
                state.self_test_reasons[
                    "DEEP_SLEEP"
                ] = "disabled_by_config"

            # Run the dedicated RF fixture test before joining the normal home
            # network. This keeps antenna/radio screening independent from any
            # prior home-AP soak, reconnect or BLE activity. The reference AP is
            # stopped by the RF runner before normal WiFi testing continues.
            if rf_quality_enabled:
                state.current_phase = "rf_quality"
                if reference is None:
                    raise RuntimeError("RF quality test requires a reference ESP.")
                state.rf_quality = rf_quality_test.run_rf_quality_test(
                    serial_port,
                    state,
                    serial_log,
                    reference,
                    settings,
                )

            if test_config["wifi"]:
                state.current_phase = "wifi_setup"
                tr.send_wifi_config(
                    serial_port,
                    ssid,
                    password,
                    resolved_test_settings["wifi"].get("output_power_dbm"),
                )
                print(
                    "Connecting WiFi and measuring "
                    "the radio environment ..."
                )
                tr.wait_for_wifi(
                    serial_port,
                    state,
                    serial_log,
                )

                target_bssid = (
                    state.wifi_scan_target_bssid
                    .strip()
                    .lower()
                )
                connected_bssid = (
                    state.bssid.strip().lower()
                )
                if (
                    environment != "esp32"
                    and target_bssid
                    and target_bssid != "none"
                    and connected_bssid
                    and target_bssid
                    != connected_bssid
                ):
                    raise RuntimeError(
                        "Invalid WiFi test: the ESP32 "
                        "is not connected to the "
                        "strongest scanned AP. "
                        f"Target="
                        f"{state.wifi_scan_target_bssid}, "
                        f"connected={state.bssid}."
                    )

                tr.run_wifi_warmup(
                    serial_port,
                    state,
                    serial_log,
                    float(resolved_test_settings["wifi"].get("warmup_seconds", 30.0)),
                )

                local_ip = tr.determine_local_ip(
                    state.wifi_ip
                )
                needs_probe_socket = any(
                    test_config.get(name, False)
                    for name in (
                        "reconnect",
                        "ble_coexistence",
                    )
                ) or (
                    soak_enabled
                    and test_config.get(
                        "udp",
                        False,
                    )
                )

                if needs_probe_socket:
                    udp_socket = socket.socket(
                        socket.AF_INET,
                        socket.SOCK_DGRAM,
                    )
                    udp_socket.bind(
                        (local_ip, 0)
                    )
                    udp_socket.setblocking(False)

                if soak_enabled:
                    if (
                        test_config["udp"]
                        and udp_socket is not None
                    ):
                        local_port = int(
                            udp_socket.getsockname()[
                                1
                            ]
                        )
                        serial_port.write(
                            (
                                f"HOST|{local_ip}|"
                                f"{local_port}\n"
                            ).encode("ascii")
                        )
                        serial_port.flush()

                    heap_interval = (
                        int(resolved_test_settings["soak"].get("heap_integrity", {}).get("interval_seconds", 30))
                        if test_config.get(
                            "heap_integrity",
                            False,
                        )
                        else 0
                    )
                    serial_port.write(
                        (
                            "HEAP_CHECK_INTERVAL|"
                            f"{heap_interval}\n"
                        ).encode("ascii")
                    )
                    serial_port.flush()

                    # Warm-up activity is deliberately excluded from the
                    # diagnostic home-network soak. Reconnect/BLE tests run only afterwards so
                    # they cannot change the WiFi state being quality-tested.
                    state.current_phase = "soak"
                    tr.reset_network_measurement(
                        serial_port,
                        state,
                        serial_log,
                    )
                else:
                    serial_port.write(
                        b"HEAP_CHECK_INTERVAL|0\n"
                    )
                    serial_port.flush()
                    print(
                        "Soak test: SKIP "
                        "(soak_disabled)"
                    )
            else:
                print(
                    "WiFi test: SKIP "
                    "(disabled_by_config)"
                )

            if soak_enabled:
                serial_thread = threading.Thread(
                    target=tr.serial_reader_worker,
                    args=(
                        serial_port,
                        stop_serial,
                        serial_queue,
                    ),
                    daemon=True,
                )
                serial_thread.start()

                if test_config["ping"]:
                    ping_thread = (
                        threading.Thread(
                            target=tr.ping_worker,
                            args=(
                                state.wifi_ip,
                                int(
                                    settings["network"].get("ping_timeout_ms", 1000)
                                ),
                                float(
                                    resolved_test_settings["soak"].get("probe_interval_seconds", 1.0)
                                ),
                                stop_ping,
                                ping_queue,
                            ),
                            daemon=True,
                        )
                    )
                    ping_thread.start()

                total_seconds = (
                    duration_minutes * 60
                )
                soak_started = time.monotonic()
                measurement_started_monotonic = (
                    soak_started
                )
                next_udp_probe = soak_started
                udp_sequence = 0
                pending_udp: dict[
                    int,
                    tuple[float, float],
                ] = {}
                last_progress = -1
                restart_count_at_soak_start = _unexpected_restart_count(state)

                print(
                    "Soak test started: "
                    f"{duration_minutes} minutes"
                )

                while (
                    time.monotonic()
                    - soak_started
                    < total_seconds
                ):
                    now_mono = time.monotonic()
                    tr.drain_serial_queue(
                        state,
                        serial_log,
                        serial_queue,
                    )

                    if (
                        _unexpected_restart_count(state)
                        > restart_count_at_soak_start
                    ):
                        soak_aborted_by_restart = True
                        measurement_finished_monotonic = time.monotonic()
                        _record_primary_restart_failure(state, "soak")
                        # Freeze the scored soak data immediately. Serial is
                        # kept alive briefly below only to capture the reboot
                        # reason (for example BROWNOUT) for the report.
                        soak_state_for_summary = copy.deepcopy(state)
                        stop_ping.set()
                        print(
                            "Soak test aborted: unexpected DUT restart detected. "
                            "Remaining network tests will be skipped."
                        )
                        break

                    while True:
                        try:
                            probe_results.append(
                                ping_queue.get_nowait()
                            )
                        except queue.Empty:
                            break

                    if (
                        test_config["udp"]
                        and udp_socket is not None
                    ):
                        if (
                            now_mono
                            >= next_udp_probe
                        ):
                            udp_sequence += 1
                            payload = (
                                f"PING|{udp_sequence}"
                            ).encode("ascii")
                            try:
                                udp_socket.sendto(
                                    payload,
                                    (
                                        state.wifi_ip,
                                        int(
                                            settings["network"].get("udp_port", 33333)
                                        ),
                                    ),
                                )
                                pending_udp[
                                    udp_sequence
                                ] = (
                                    time.time(),
                                    now_mono,
                                )
                            except OSError as exc:
                                probe_results.append(
                                    tr.ProbeResult(
                                        time.time(),
                                        "udp",
                                        udp_sequence,
                                        False,
                                        None,
                                        str(exc),
                                    )
                                )
                            next_udp_probe += float(
                                resolved_test_settings["soak"].get("probe_interval_seconds", 1.0)
                            )

                        while True:
                            try:
                                data, _address = (
                                    udp_socket.recvfrom(
                                        512
                                    )
                                )
                            except BlockingIOError:
                                break
                            except OSError:
                                break

                            text = data.decode(
                                "utf-8",
                                errors="replace",
                            )
                            if text.startswith(
                                "PONG|"
                            ):
                                parts = text.split("|")
                                try:
                                    sequence = int(
                                        parts[1]
                                    )
                                except (
                                    IndexError,
                                    ValueError,
                                ):
                                    continue

                                pending = (
                                    pending_udp.pop(
                                        sequence,
                                        None,
                                    )
                                )
                                if pending:
                                    latency = (
                                        time.monotonic()
                                        - pending[1]
                                    ) * 1000.0
                                    probe_results.append(
                                        tr.ProbeResult(
                                            pending[0],
                                            "udp",
                                            sequence,
                                            True,
                                            latency,
                                            "",
                                        )
                                    )
                            elif text.startswith(
                                "HB|"
                            ):
                                parts = text.split("|")
                                if len(parts) >= 6:
                                    try:
                                        state.udp_heartbeat_samples.append(
                                            {
                                                "timestamp": time.time(),
                                                "sequence": int(parts[1]),
                                                "uptime_ms": int(parts[2]),
                                                "rssi": int(parts[3]),
                                                "heap_free": int(parts[4]),
                                                "heap_min": int(parts[5]),
                                            }
                                        )
                                    except ValueError:
                                        pass

                        timeout_seconds = max(
                            2.0,
                            float(
                                resolved_test_settings["soak"].get("probe_interval_seconds", 1.0)
                            )
                            * 2.5,
                        )
                        expired = [
                            seq
                            for seq, (
                                _sent_wall,
                                sent_mono,
                            ) in pending_udp.items()
                            if (
                                now_mono
                                - sent_mono
                                >= timeout_seconds
                            )
                        ]
                        for sequence in expired:
                            sent_wall, _ = (
                                pending_udp.pop(
                                    sequence
                                )
                            )
                            probe_results.append(
                                tr.ProbeResult(
                                    sent_wall,
                                    "udp",
                                    sequence,
                                    False,
                                    None,
                                    "timeout",
                                )
                            )

                    elapsed = (
                        now_mono - soak_started
                    )
                    progress = int(
                        (
                            elapsed
                            / total_seconds
                        )
                        * 100
                    )
                    if (
                        progress // 5
                        != last_progress // 5
                    ):
                        last_progress = progress
                        if test_config["wifi"]:
                            rssi = (
                                tr.average_valid_rssi(
                                    state.heartbeat_samples
                                )
                            )
                            disconnects = max(
                                [
                                    int(
                                        item.get(
                                            "disconnects",
                                            0,
                                        )
                                    )
                                    for item in state.heartbeat_samples
                                ]
                                + [0]
                            )
                            print(
                                f"  {min(progress,100):3d} %  "
                                f"RSSI "
                                f"{tr.fmt(rssi,1,' dBm')}  "
                                f"Disconnects "
                                f"{disconnects}"
                            )
                        else:
                            print(
                                f"  {min(progress,100):3d} %  "
                                "WiFi SKIP"
                            )

                    time.sleep(0.03)

                if measurement_finished_monotonic is None:
                    measurement_finished_monotonic = (
                        time.monotonic()
                    )

                stop_ping.set()
                if ping_thread:
                    ping_thread.join(timeout=3)

                restart_cutoff = None
                failure = getattr(state, "primary_failure", None)
                if (
                    soak_aborted_by_restart
                    and isinstance(failure, dict)
                    and isinstance(failure.get("timestamp"), (int, float))
                ):
                    restart_cutoff = float(failure["timestamp"])

                while True:
                    try:
                        ping_result = ping_queue.get_nowait()
                    except queue.Empty:
                        break
                    if (
                        restart_cutoff is None
                        or ping_result.timestamp < restart_cutoff
                    ):
                        probe_results.append(ping_result)

                if soak_aborted_by_restart:
                    # Outstanding UDP requests belong to an interrupted
                    # runtime. Do not convert them into artificial timeout or
                    # test_end losses after the DUT rebooted.
                    pending_udp.clear()

                    # Keep serial reading alive briefly so BOOT is followed by
                    # SYSTEM|reset=... and the primary error can be named
                    # precisely in the final report.
                    reason_deadline = time.monotonic() + 3.0
                    while time.monotonic() < reason_deadline:
                        tr.drain_serial_queue(
                            state,
                            serial_log,
                            serial_queue,
                        )
                        _refresh_primary_restart_reason(state)
                        current_failure = getattr(
                            state,
                            "primary_failure",
                            {},
                        )
                        if (
                            isinstance(current_failure, dict)
                            and current_failure.get("reset_reason")
                        ):
                            break
                        time.sleep(0.03)
                else:
                    for sequence, (
                        sent_wall,
                        _sent_mono,
                    ) in pending_udp.items():
                        probe_results.append(
                            tr.ProbeResult(
                                sent_wall,
                                "udp",
                                sequence,
                                False,
                                None,
                                "test_end",
                            )
                        )

                stop_serial.set()
                if serial_thread:
                    serial_thread.join(timeout=2)
                tr.drain_serial_queue(
                    state,
                    serial_log,
                    serial_queue,
                )
                _refresh_primary_restart_reason(state)

                # Freeze the actual soak measurements before intentionally
                # changing the WiFi state in reconnect/BLE functional tests.
                if soak_state_for_summary is None:
                    soak_state_for_summary = copy.deepcopy(state)

                if not soak_aborted_by_restart:
                    serial_port.write(b"HEAP_CHECK_INTERVAL|0\n")
                    serial_port.flush()

            # Deliberately run disruptive home-WiFi functional tests after the
            # diagnostic stability soak. The dedicated RF fixture already ran
            # independently before the DUT joined the home network.
            if test_config.get("wifi", False):
                if getattr(state, "abort_remaining_tests", False):
                    _mark_followup_tests_skipped(
                        state,
                        test_config,
                    )
                else:
                    if test_config.get(
                        "reconnect",
                        False,
                    ):
                        state.current_phase = "reconnect"
                        if udp_socket is None:
                            raise RuntimeError(
                                "Controlled reconnect requires "
                                "a local UDP probe socket."
                            )
                        tr.run_controlled_reconnect_test(
                            serial_port,
                            state,
                            serial_log,
                            udp_socket,
                            settings,
                        )

                        # A failed controlled reconnect is a test result, not a
                        # reason to poison every test that follows it. Restore the
                        # already validated home-WiFi configuration so BLE
                        # coexistence can still be assessed independently.
                        if not state.connected:
                            print("Restoring home WiFi after reconnect failure ...")
                            tr.send_wifi_config(
                                serial_port,
                                ssid,
                                password,
                                resolved_test_settings["wifi"].get("output_power_dbm"),
                            )
                            try:
                                tr.wait_for_wifi(
                                    serial_port,
                                    state,
                                    serial_log,
                                    timeout=float(
                                        resolved_test_settings["reconnect"].get("recovery_timeout_seconds", 30.0)
                                    ),
                                )
                                print("  Home WiFi restored for remaining tests.")
                            except RuntimeError as exc:
                                print(f"  Home WiFi recovery: FAIL / {exc}")

                        if state.connected:
                            settle_deadline = (
                                time.monotonic()
                                + float(
                                    resolved_test_settings["reconnect"].get("settle_seconds", 2.0)
                                )
                            )
                            while time.monotonic() < settle_deadline:
                                tr.read_serial_available(
                                    serial_port,
                                    state,
                                    serial_log,
                                )
                                time.sleep(0.03)

                    if test_config.get(
                        "ble_coexistence",
                        False,
                    ):
                        state.current_phase = "ble_coexistence"
                        if udp_socket is None:
                            raise RuntimeError(
                                "BLE coexistence test requires "
                                "a local UDP probe socket."
                            )
                        if not state.connected:
                            state.ble_coexistence = {
                                "status": "SKIP",
                                "reason": "wifi_unavailable_after_reconnect",
                                "devices": None,
                                "baseline": {},
                                "active": {},
                                "ping_loss_delta_percent": None,
                                "udp_loss_delta_percent": None,
                            }
                            print(
                                "WiFi + BLE coexistence test: SKIP / "
                                "WiFi unavailable after reconnect test"
                            )
                        else:
                            tr.run_ble_coexistence_test(
                                serial_port,
                                state,
                                serial_log,
                                udp_socket,
                                settings,
                            )

            state.current_phase = "complete"

    finally:
        stop_ping.set()
        stop_serial.set()
        if (
            serial_thread
            and serial_thread.is_alive()
        ):
            serial_thread.join(timeout=2)
        if udp_socket:
            udp_socket.close()
        if serial_port:
            try:
                serial_port.close()
            except Exception:
                pass
        if reference is not None:
            reference.close()

    if (
        soak_enabled
        and measurement_started_monotonic is not None
        and measurement_finished_monotonic is not None
    ):
        duration_seconds = (
            measurement_finished_monotonic
            - measurement_started_monotonic
        )
    else:
        duration_seconds = 0.0

    summary_state = state
    if soak_state_for_summary is not None:
        summary_state = copy.deepcopy(state)
        summary_state.heartbeat_samples = copy.deepcopy(
            soak_state_for_summary.heartbeat_samples
        )
        summary_state.udp_heartbeat_samples = copy.deepcopy(
            soak_state_for_summary.udp_heartbeat_samples
        )
        summary_state.disconnect_reasons = list(
            soak_state_for_summary.disconnect_reasons
        )
        summary_state.heap_integrity_checks = copy.deepcopy(
            soak_state_for_summary.heap_integrity_checks
        )

    summary = build_summary(
        settings,
        esp_info,
        summary_state,
        probe_results,
        duration_seconds,
        environment,
        board_id,
        test_config,
    )
    summary["peer_comparison"] = (
        tr.build_peer_comparison(
            summary,
            settings,
        )
    )
    summary["warnings"].extend(
        summary["peer_comparison"].get(
            "warnings",
            [],
        )
    )

    tr.write_network_csv(
        run_dir / "network.csv",
        probe_results,
    )
    tr.write_events_csv(
        run_dir / "events.csv",
        state,
    )
    if state.rf_quality:
        rf_quality_test.write_rf_csv(
            run_dir / "rf-quality.csv",
            state.rf_quality,
        )
    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            summary,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    write_reports(run_dir, summary)
    tr.update_board_registry_result(
        esp_info,
        board_id,
        summary["result"],
    )

    print("\n==========================================")
    print(
        f" BOARD:     "
        f"{summary['result']} / "
        f"{summary.get('board_id') or '-'}"
    )
    print(
        f" RF QUALITY: "
        f"{summary.get('rf_quality', {}).get('quality_status', 'SKIP')}"
    )
    print(
        f" STABILITY: "
        f"{summary.get('stability', {}).get('status', '-')}"
    )
    print(
        f" SOAK:      "
        f"{'SCORED' if soak_enabled else 'SKIP'}"
    )
    print("==========================================")

    if summary.get("primary_failure"):
        print("Primary failure:")
        print(
            " - "
            + str(
                summary["primary_failure"].get(
                    "message",
                    "Unexpected DUT restart",
                )
            )
        )

    if summary["fail_reasons"]:
        print("Reasons:")
        for item in summary["fail_reasons"]:
            print(f" - {item}")

    stability = summary.get(
        "stability",
        {},
    )
    if stability.get("fail_reasons"):
        print("STABILITY FAIL:")
        for item in stability[
            "fail_reasons"
        ]:
            print(f" - {item}")

    if stability.get("warnings"):
        print("STABILITY WARN:")
        for item in stability[
            "warnings"
        ]:
            print(f" - {item}")

    if summary["warnings"]:
        print("Warnings:")
        for item in summary["warnings"]:
            print(f" - {item}")

    if not summary.get(
        "quality_suite_complete",
        True,
    ):
        disabled_required = [
            name.upper()
            for name in QUALITY_REQUIRED_TESTS
            if not test_config.get(
                name,
                False,
            )
        ]
        print(
            "Quality suite: INCOMPLETE - disabled: "
            + (", ".join(disabled_required) or "-")
        )

    # Label handling is intentionally independent from the selected test set.
    # Once the run completed normally and produced PASS/FAIL, label.mode alone
    # decides whether the resulting label is printed.
    if not tr.should_run_label(label_mode):
        print(
            "Label: SKIP - "
            "label.mode=off"
        )
    else:
        tr.run_label_workflow(
            summary_path,
            label_mode,
        )

    final_run_dir = (
        tr.finalize_result_directory(
            run_dir,
            board_id,
        )
    )
    tr.generate_results_index()

    print(
        f"\nReport: "
        f"{final_run_dir / 'report.html'}"
    )
    print(f"Logs:   {final_run_dir}")

    tr.open_report(
        final_run_dir / "report.html"
    )
    return (
        0
        if summary["result"] == "PASS"
        else 2
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nTest aborted.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        raise SystemExit(1)
