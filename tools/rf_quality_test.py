from __future__ import annotations

import base64
import csv
import time
from pathlib import Path
from typing import Any

import test_runner as tr
from reference_device import ReferenceDevice


MIN_REFERENCE_VERSION = (1, 0, 12)


def _number(values: dict[str, Any], key: str, default: float | None = None) -> float | None:
    try:
        return float(values[key])
    except (KeyError, TypeError, ValueError):
        return default


def _integer(values: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(values[key])
    except (KeyError, TypeError, ValueError):
        return default


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in str(value).split("."):
        try:
            parts.append(int(part))
        except ValueError:
            break
    return tuple(parts)


def _normalize_mac(value: str) -> str:
    compact = "".join(character for character in str(value).upper() if character in "0123456789ABCDEF")
    if len(compact) != 12:
        return str(value).strip().upper()
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def _send_rf_wifi_config(
    serial_port: Any,
    ssid: str,
    password: str,
    channel: int,
    bssid: str,
) -> None:
    encoded_ssid = base64.b64encode(ssid.encode("utf-8")).decode("ascii")
    encoded_password = base64.b64encode(password.encode("utf-8")).decode("ascii")
    serial_port.write(
        (
            f"RF_WIFI64|{encoded_ssid}|{encoded_password}|"
            f"{int(channel)}|{bssid}\n"
        ).encode("ascii")
    )
    serial_port.flush()


def _wait_dut_event(
    serial_port: Any,
    state: tr.RunState,
    serial_log: Any,
    start_index: int,
    statuses: set[str],
    timeout: float,
    description: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tr.read_serial_available(serial_port, state, serial_log)
        for event in state.rf_events[start_index:]:
            if event.get("status") in statuses:
                return event
        time.sleep(0.02)
    raise RuntimeError(f"DUT did not complete RF step in time ({description}).")


def _reset_dut_stats(
    serial_port: Any,
    state: tr.RunState,
    serial_log: Any,
    run_id: int,
) -> None:
    last_error: RuntimeError | None = None
    for _attempt in range(3):
        start = len(state.rf_events)
        serial_port.write(f"RF_RESET|{run_id}\n".encode("ascii"))
        serial_port.flush()
        try:
            event = _wait_dut_event(
                serial_port, state, serial_log, start, {"RESET"}, 1.2, "RF_RESET"
            )
            if _integer(event, "run_id") == run_id:
                return
        except RuntimeError as exc:
            last_error = exc
    raise RuntimeError("DUT did not confirm RF_RESET after 3 attempts.") from last_error


def _reset_reference_stats(reference: ReferenceDevice, run_id: int) -> None:
    reference.request(
        f"RESET_STATS|{run_id}",
        lambda category, values: (
            category == "RESET"
            and values.get("status") == "OK"
            and _integer(values, "run_id") == run_id
        ),
        1.2,
        "RESET_STATS",
    )


def _query_dut_stats(
    serial_port: Any,
    state: tr.RunState,
    serial_log: Any,
) -> dict[str, Any]:
    last_error: RuntimeError | None = None
    for _attempt in range(3):
        start = len(state.rf_events)
        serial_port.write(b"RF_STATS\n")
        serial_port.flush()
        try:
            return _wait_dut_event(
                serial_port, state, serial_log, start, {"STATS"}, 1.2, "RF_STATS"
            )
        except RuntimeError as exc:
            last_error = exc
    raise RuntimeError("DUT did not answer RF_STATS after 3 attempts.") from last_error


def _stop_dut_rf(serial_port: Any, state: tr.RunState, serial_log: Any) -> None:
    start = len(state.rf_events)
    serial_port.write(b"RF_STOP\n")
    serial_port.flush()
    _wait_dut_event(serial_port, state, serial_log, start, {"STOPPED"}, 3.0, "RF_STOP")


def _abort_dut_tx(
    serial_port: Any,
    state: tr.RunState,
    serial_log: Any,
) -> dict[str, Any]:
    last_error: RuntimeError | None = None
    for _attempt in range(3):
        start = len(state.rf_events)
        serial_port.write(b"RF_ABORT_TX\n")
        serial_port.flush()
        try:
            return _wait_dut_event(
                serial_port,
                state,
                serial_log,
                start,
                {"TX_ABORTED"},
                1.2,
                "RF_ABORT_TX",
            )
        except RuntimeError as exc:
            last_error = exc
    raise RuntimeError("DUT did not confirm RF_ABORT_TX after 3 attempts.") from last_error


def _start_dut_tx(
    serial_port: Any,
    state: tr.RunState,
    serial_log: Any,
    run_id: int,
    count: int,
    interval_ms: int,
    timeout: float,
) -> dict[str, Any]:
    start = len(state.rf_events)
    serial_port.write(f"RF_TX|{run_id}|{count}|{interval_ms}\n".encode("ascii"))
    serial_port.flush()
    event = _wait_dut_event(
        serial_port, state, serial_log, start, {"TX_DONE"}, timeout, "RF_TX"
    )
    if _integer(event, "run_id") != run_id:
        raise RuntimeError("DUT RF transmit returned a stale run_id.")
    if event.get("result") != "PASS":
        raise RuntimeError(
            "DUT RF transmit failed: " + str(event.get("reason", "unknown error"))
        )
    return event


def _run_reference_tx_resilient(
    reference: ReferenceDevice,
    run_id: int,
    count: int,
    interval_ms: int,
    timeout: float,
) -> tuple[dict[str, Any], bool]:
    reference.send(f"TX|{run_id}|{count}|{interval_ms}")
    try:
        _category, values = reference.wait_for(
            lambda category, values: (
                category == "TX_DONE" and _integer(values, "run_id") == run_id
            ),
            timeout,
            "RF TX",
        )
        if values.get("status") == "FAIL":
            raise RuntimeError(
                "Reference ESP RF transmit failed: "
                + values.get("reason", "unknown error")
            )
        return dict(values), False
    except RuntimeError:
        aborted = reference.abort_tx()
        stats = reference.stats()
        sent = _integer(aborted, "sent", _integer(stats, "tx_packets", 0))
        return {
            "status": "RECOVERED_TIMEOUT",
            "requested": str(count),
            "sent": str(sent),
            "run_id": str(run_id),
        }, True


def _run_dut_tx_resilient(
    serial_port: Any,
    state: tr.RunState,
    serial_log: Any,
    run_id: int,
    count: int,
    interval_ms: int,
    timeout: float,
) -> tuple[dict[str, Any], bool]:
    try:
        return _start_dut_tx(
            serial_port, state, serial_log, run_id, count, interval_ms, timeout
        ), False
    except RuntimeError:
        aborted = _abort_dut_tx(serial_port, state, serial_log)
        stats = _query_dut_stats(serial_port, state, serial_log)
        sent = _integer(aborted, "sent", _integer(stats, "tx_packets", 0))
        return {
            "status": "RECOVERED_TIMEOUT",
            "requested": str(count),
            "sent": str(sent),
            "run_id": str(run_id),
        }, True


def _loss_percent(total: int, received: int) -> float:
    if total <= 0:
        return 100.0
    received = max(0, min(total, received))
    return 100.0 * float(total - received) / float(total)


def _aggregate_measurement(
    requested_power: int | None,
    actual_power: int | None,
    packets_per_repetition: int,
    repetitions: list[dict[str, Any]],
) -> dict[str, Any]:
    sent = sum(int(item.get("packets_sent", 0)) for item in repetitions)
    received = sum(int(item.get("packets_received", 0)) for item in repetitions)
    weighted_rssi_sum = 0.0
    weighted_rssi_samples = 0
    rssi_min_values: list[float] = []
    rssi_max_values: list[float] = []

    for item in repetitions:
        samples = int(item.get("rssi_samples", 0) or 0)
        average = item.get("rssi_average_dbm")
        if samples > 0 and isinstance(average, (int, float)):
            weighted_rssi_sum += float(average) * samples
            weighted_rssi_samples += samples

        minimum = item.get("rssi_min_dbm")
        maximum = item.get("rssi_max_dbm")
        if isinstance(minimum, (int, float)) and float(minimum) > -127:
            rssi_min_values.append(float(minimum))
        if isinstance(maximum, (int, float)) and float(maximum) > -127:
            rssi_max_values.append(float(maximum))

    average_rssi = (
        weighted_rssi_sum / weighted_rssi_samples
        if weighted_rssi_samples > 0
        else None
    )
    timeouts = sum(
        1 for item in repetitions if bool(item.get("measurement_timeout", False))
    )

    return {
        "measurement_status": "TIMEOUT" if timeouts else "OK",
        "measurement_timeouts": timeouts,
        "tx_power_requested_dbm": requested_power,
        "tx_power_dbm": actual_power,
        "repetition_count": len(repetitions),
        "packets_requested": packets_per_repetition * len(repetitions),
        "packets_sent": sent,
        "packets_received": received,
        "loss_percent": round(_loss_percent(sent, received), 3),
        "rssi_average_dbm": round(average_rssi, 3) if average_rssi is not None else None,
        "rssi_min_dbm": min(rssi_min_values) if rssi_min_values else None,
        "rssi_max_dbm": max(rssi_max_values) if rssi_max_values else None,
        "rssi_samples": weighted_rssi_samples,
        "repetitions": repetitions,
    }


def _evaluate_quality(result: dict[str, Any], config: dict[str, Any]) -> None:
    thresholds = config.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise RuntimeError("test-settings.json: tests.*.rf_quality.thresholds must be an object.")

    ref_to_dut = result.get("reference_to_dut", {})
    dut_to_ref = result.get("dut_to_reference", {})
    fail_reasons: list[str] = []
    warnings: list[str] = []

    for label, direction in (
        ("Reference->DUT", ref_to_dut),
        ("DUT->Reference", dut_to_ref),
    ):
        timeouts = int(direction.get("measurement_timeouts", 0) or 0)
        if timeouts:
            fail_reasons.append(
                f"{label} measurement timed out {timeouts} time(s)"
            )

    ref_rssi_threshold = thresholds.get("reference_to_dut_min_rssi_dbm")
    dut_rssi_threshold = thresholds.get("dut_to_reference_min_rssi_dbm")
    loss_threshold = thresholds.get("max_loss_percent")
    configured = any(
        isinstance(value, (int, float))
        for value in (ref_rssi_threshold, dut_rssi_threshold, loss_threshold)
    )

    if isinstance(ref_rssi_threshold, (int, float)):
        value = ref_to_dut.get("rssi_average_dbm")
        if value is None or float(value) < float(ref_rssi_threshold):
            fail_reasons.append(
                f"Reference->DUT RSSI {value} dBm; required >= {ref_rssi_threshold} dBm"
            )

    if isinstance(dut_rssi_threshold, (int, float)):
        value = dut_to_ref.get("rssi_average_dbm")
        if value is None or float(value) < float(dut_rssi_threshold):
            fail_reasons.append(
                f"DUT->Reference RSSI {value} dBm; required >= {dut_rssi_threshold} dBm"
            )

    if isinstance(loss_threshold, (int, float)):
        for label, direction in (
            ("Reference->DUT", ref_to_dut),
            ("DUT->Reference", dut_to_ref),
        ):
            value = direction.get("loss_percent")
            if value is None or float(value) > float(loss_threshold):
                fail_reasons.append(
                    f"{label} packet loss {value} %; allowed <= {loss_threshold} %"
                )

    if fail_reasons:
        result["quality_status"] = "FAIL"
    elif configured:
        result["quality_status"] = "PASS"
    else:
        result["quality_status"] = "UNRATED"
        warnings.append(
            "RF thresholds are not calibrated yet; RSSI/loss are diagnostic only"
        )

    result["quality_fail_reasons"] = fail_reasons
    result["quality_warnings"] = warnings


def _print_direction(label: str, direction: dict[str, Any]) -> None:
    rssi = direction.get("rssi_average_dbm")
    loss = direction.get("loss_percent")
    power = direction.get("tx_power_dbm")
    sent = direction.get("packets_sent")
    received = direction.get("packets_received")
    rssi_text = f"{float(rssi):.1f} dBm" if isinstance(rssi, (int, float)) else "-"
    loss_text = f"{float(loss):.2f} %" if isinstance(loss, (int, float)) else "-"
    power_text = f"{int(power)} dBm" if isinstance(power, (int, float)) else "default max"
    print(
        f"  {label}: RSSI {rssi_text} / loss {loss_text} / "
        f"packets {received}/{sent} / TX {power_text}"
    )


def run_rf_quality_test(
    serial_port: Any,
    state: tr.RunState,
    serial_log: Any,
    reference: ReferenceDevice,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Run one fixed-power bidirectional RF comparison against the reference ESP."""
    config = settings.get("tests", {}).get("rf_quality", {})
    if not isinstance(config, dict):
        raise RuntimeError("test-settings.json: tests.*.rf_quality must be an object.")

    if _version_tuple(reference.identity.version) < MIN_REFERENCE_VERSION:
        raise RuntimeError(
            "Reference ESP firmware 1.0.12 or newer is required. "
            "Reflash it once with scripts/flash-reference.cmd."
        )

    expected_reference_mac = _normalize_mac(str(config.get("reference_mac", "")))
    current_reference_mac = _normalize_mac(reference.identity.mac)
    if expected_reference_mac and current_reference_mac != expected_reference_mac:
        raise RuntimeError(
            "Wrong reference ESP connected: "
            f"expected {expected_reference_mac}, found {current_reference_mac or '-'}"
        )

    previous_mac = current_reference_mac
    print(
        f"Reference ESP: restarting {reference.identity.port} / "
        f"firmware {reference.identity.version} ..."
    )
    restarted = reference.restart()
    restarted_version = restarted.get("version", "")
    restarted_mac = _normalize_mac(restarted.get("mac", ""))
    if restarted.get("role") != "reference":
        raise RuntimeError("Reference ESP did not identify as reference after restart.")
    if _version_tuple(restarted_version) < MIN_REFERENCE_VERSION:
        raise RuntimeError(
            f"Reference ESP reported firmware {restarted_version or '-'} after restart; "
            "1.0.12 or newer is required. Reflash it with scripts/flash-reference.cmd."
        )
    if previous_mac and restarted_mac != previous_mac:
        raise RuntimeError(
            f"Reference ESP identity changed after restart ({previous_mac} -> {restarted_mac or '-'})."
        )
    if expected_reference_mac and restarted_mac != expected_reference_mac:
        raise RuntimeError(
            "Wrong reference ESP after restart: "
            f"expected {expected_reference_mac}, found {restarted_mac or '-'}"
        )
    print(
        f"Reference ESP: ready / firmware {restarted_version} / mac {restarted_mac}"
    )

    channel = int(config.get("channel", 6))
    reference_tx_power = max(8, min(20, int(config.get("reference_tx_power_dbm", 20))))
    packets = max(10, int(config.get("packets_per_repetition", 100)))
    interval_ms = max(5, int(config.get("packet_interval_ms", 20)))
    repetitions = max(1, min(5, int(config.get("repetitions_per_direction", 3))))

    print(
        "RF quality: fixed-power bidirectional reference measurement / "
        f"{repetitions} rep(s) x {packets} packet(s) / 802.11b fixed 1 Mbps"
    )

    result: dict[str, Any] = {
        "enabled": True,
        "measurement_mode": "fixed_full_power",
        "execution_status": "RUNNING",
        "quality_status": "UNRATED",
        "reference": {
            "port": reference.identity.port,
            "version": restarted_version,
            "mac": restarted_mac,
        },
        "channel": channel,
        "reference_tx_power_dbm": reference_tx_power,
        "packets_per_repetition": packets,
        "packet_interval_ms": interval_ms,
        "repetitions_per_direction": repetitions,
        "reference_to_dut": {},
        "dut_to_reference": {},
        "quality_fail_reasons": [],
        "quality_warnings": [],
    }

    ap_started = False
    run_id = 1

    def allocate_run_id() -> int:
        nonlocal run_id
        value = run_id
        run_id += 1
        return value

    try:
        ap = reference.start_ap(channel, reference_tx_power)
        ap_started = True
        ap_ssid = ap.get("ssid", "")
        ap_channel = int(ap.get("channel", channel))
        ap_bssid = ap.get("bssid", "").strip()
        if not ap_ssid or not ap_bssid:
            raise RuntimeError(
                "Reference ESP did not report the SSID/BSSID required for the direct RF connection."
            )

        result["reference"].update(
            {
                "ssid": ap_ssid,
                "channel": ap_channel,
                "bssid": ap_bssid,
                "tx_power_dbm": int(ap.get("tx_power_dbm", reference_tx_power)),
            }
        )

        reference_radio_ok = (
            ap.get("protocol") == "11b"
            and ap.get("protocol_ok") == "1"
            and ap.get("fixed_rate") == "1M_L"
            and ap.get("fixed_rate_ok") == "1"
        )
        if not reference_radio_ok:
            raise RuntimeError(
                "Reference ESP did not enable controlled 802.11b-only RF mode "
                f"(protocol={ap.get('protocol', '-')}, "
                f"protocol_ok={ap.get('protocol_ok', '-')}, "
                f"fixed_rate={ap.get('fixed_rate', '-')}, "
                f"fixed_rate_ok={ap.get('fixed_rate_ok', '-')})."
            )

        start = len(state.rf_events)
        _send_rf_wifi_config(
            serial_port,
            ap_ssid,
            ap.get("password", ""),
            ap_channel,
            ap_bssid,
        )
        try:
            association = _wait_dut_event(
                serial_port,
                state,
                serial_log,
                start,
                {"ASSOCIATED", "CONNECTED", "FAIL"},
                10.0,
                "associate with reference AP",
            )
        except RuntimeError:
            result["execution_status"] = "FAIL"
            result["quality_status"] = "FAIL"
            result["quality_fail_reasons"] = [
                "DUT could not associate with the dedicated reference AP"
            ]
            return result

        if association.get("status") == "FAIL":
            result["execution_status"] = "FAIL"
            result["quality_status"] = "FAIL"
            result["quality_fail_reasons"] = [
                "DUT rejected the dedicated reference-AP configuration"
            ]
            return result

        if association.get("status") == "CONNECTED":
            connected = association
        else:
            connected = _wait_dut_event(
                serial_port,
                state,
                serial_log,
                start,
                {"CONNECTED", "FAIL"},
                10.0,
                "obtain IP on reference AP",
            )

        if connected.get("status") != "CONNECTED" or connected.get("udp") != "1":
            result["execution_status"] = "FAIL"
            result["quality_status"] = "FAIL"
            result["quality_fail_reasons"] = [
                "DUT reference-AP connection or RF UDP socket failed"
            ]
            return result

        dut_radio_ok = (
            connected.get("protocol") == "11b"
            and connected.get("protocol_ok") == "1"
            and connected.get("power_save_off") == "1"
            and connected.get("fixed_rate") == "1M_L"
            and connected.get("fixed_rate_ok") == "1"
        )
        if not dut_radio_ok:
            raise RuntimeError(
                "DUT did not enable controlled 802.11b-only RF mode "
                f"(protocol={connected.get('protocol', '-')}, "
                f"protocol_ok={connected.get('protocol_ok', '-')}, "
                f"power_save_off={connected.get('power_save_off', '-')}, "
                f"fixed_rate={connected.get('fixed_rate', '-')}, "
                f"fixed_rate_ok={connected.get('fixed_rate_ok', '-')})."
            )

        reference.wait_for_dut(timeout=8.0)
        result["radio_control"] = {
            "protocol": "802.11b-only",
            "phy_rate": "1 Mbps long preamble",
            "fixed_rate": "WIFI_PHY_RATE_1M_L",
            "dut_power_save": "off",
            "tx_power_mode": "fixed_maximum",
        }

        reference_driver_stats = reference.stats()
        reference_actual_power = _integer(
            reference_driver_stats,
            "tx_power_driver_dbm",
            _integer(reference_driver_stats, "tx_power_dbm", reference_tx_power),
        )
        ref_repetitions: list[dict[str, Any]] = []
        for repeat in range(1, repetitions + 1):
            current_run_id = allocate_run_id()
            _reset_reference_stats(reference, current_run_id)
            _reset_dut_stats(serial_port, state, serial_log, current_run_id)
            time.sleep(0.1)
            timeout = packets * interval_ms / 1000.0 + 4.0
            tx_done, measurement_timeout = _run_reference_tx_resilient(
                reference, current_run_id, packets, interval_ms, timeout
            )
            time.sleep(0.15)
            dut_stats = _query_dut_stats(serial_port, state, serial_log)
            sent = _integer(tx_done, "sent", packets)
            received = _integer(dut_stats, "rx_packets")
            ref_repetitions.append(
                {
                    "repetition": repeat,
                    "measurement_timeout": measurement_timeout,
                    "packets_sent": sent,
                    "packets_received": received,
                    "loss_percent": round(_loss_percent(sent, received), 3),
                    "rssi_average_dbm": _number(dut_stats, "rssi_avg"),
                    "rssi_min_dbm": _number(dut_stats, "rssi_min"),
                    "rssi_max_dbm": _number(dut_stats, "rssi_max"),
                    "rssi_samples": _integer(dut_stats, "rssi_samples"),
                }
            )

        result["reference_to_dut"] = _aggregate_measurement(
            reference_tx_power,
            reference_actual_power,
            packets,
            ref_repetitions,
        )

        # The DUT stays at the driver's normal maximum. No RF_TX_POWER command is
        # issued anywhere in the fixed-power reference test.
        dut_driver_stats = _query_dut_stats(serial_port, state, serial_log)
        dut_power = _integer(dut_driver_stats, "tx_power_dbm", 0)
        dut_actual_power: int | None = dut_power if dut_power else None
        dut_repetitions: list[dict[str, Any]] = []
        for repeat in range(1, repetitions + 1):
            current_run_id = allocate_run_id()
            _reset_reference_stats(reference, current_run_id)
            _reset_dut_stats(serial_port, state, serial_log, current_run_id)
            time.sleep(0.1)
            timeout = packets * interval_ms / 1000.0 + 4.0
            tx_done, measurement_timeout = _run_dut_tx_resilient(
                serial_port,
                state,
                serial_log,
                current_run_id,
                packets,
                interval_ms,
                timeout,
            )
            time.sleep(0.15)
            reference_stats = reference.stats()
            sent = _integer(tx_done, "sent", packets)
            received = _integer(reference_stats, "rx_packets")
            dut_repetitions.append(
                {
                    "repetition": repeat,
                    "measurement_timeout": measurement_timeout,
                    "packets_sent": sent,
                    "packets_received": received,
                    "loss_percent": round(_loss_percent(sent, received), 3),
                    "rssi_average_dbm": _number(reference_stats, "rssi_avg"),
                    "rssi_min_dbm": _number(reference_stats, "rssi_min"),
                    "rssi_max_dbm": _number(reference_stats, "rssi_max"),
                    "rssi_samples": _integer(reference_stats, "rssi_samples"),
                }
            )

        result["dut_to_reference"] = _aggregate_measurement(
            None,
            dut_actual_power,
            packets,
            dut_repetitions,
        )
        result["execution_status"] = "PASS"
        _evaluate_quality(result, config)

        _print_direction("REF -> DUT", result["reference_to_dut"])
        _print_direction("DUT -> REF", result["dut_to_reference"])
        print(f"RF quality overall: {result['quality_status']} / fixed-power comparison")
        return result
    except Exception as exc:
        result["execution_status"] = "FAIL"
        result["quality_status"] = "FAIL"
        result["quality_fail_reasons"] = [str(exc)]
        raise
    finally:
        try:
            _stop_dut_rf(serial_port, state, serial_log)
        except Exception:
            pass
        if ap_started:
            try:
                reference.stop_ap()
            except Exception:
                pass
        time.sleep(0.25)


def write_rf_csv(path: Path, result: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = []
    for direction_key in ("reference_to_dut", "dut_to_reference"):
        direction = result.get(direction_key, {})
        if not isinstance(direction, dict):
            continue
        power = direction.get("tx_power_dbm")
        for repetition in direction.get("repetitions", []):
            if not isinstance(repetition, dict):
                continue
            rows.append(
                {
                    "direction": direction_key,
                    "repetition": repetition.get("repetition"),
                    "tx_power_dbm": power,
                    **repetition,
                }
            )

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        fieldnames = [
            "direction",
            "repetition",
            "tx_power_dbm",
            "measurement_timeout",
            "packets_sent",
            "packets_received",
            "loss_percent",
            "rssi_average_dbm",
            "rssi_min_dbm",
            "rssi_max_dbm",
            "rssi_samples",
        ]
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            delimiter=";",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)

