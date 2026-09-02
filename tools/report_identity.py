from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any


def _parse_reference_log(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}

    best: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}

    for line in lines:
        marker = line.find("REF|")
        if marker < 0:
            continue
        parts = line[marker:].strip().split("|")
        if len(parts) < 3 or parts[0] != "REF" or parts[1] not in {"INFO", "READY"}:
            continue

        values: dict[str, str] = {}
        for part in parts[2:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            values[key] = value

        if values.get("role") != "reference":
            continue

        # Prefer the richest INFO/READY record. Reference firmware 1.0.13+
        # reports runtime/build metadata in addition to version and MAC.
        if len(values) >= len(best):
            best = values

    return best


def _value(mapping: dict[str, Any], key: str, fallback: Any = "-") -> Any:
    value = mapping.get(key)
    if value is None or value == "":
        return fallback
    return value


def _build_identity(summary: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    system = summary.get("system", {})
    if not isinstance(system, dict):
        system = {}
    esptool = summary.get("esptool", {})
    if not isinstance(esptool, dict):
        esptool = {}

    dut = {
        "firmware_version": _value(system, "firmware_version"),
        "build_date": _value(system, "build_date"),
        "build_time": _value(system, "build_time"),
        "profile": _value(system, "profile", summary.get("environment", "-")),
        "chip": _value(system, "chip", summary.get("chip_family", "-")),
        "revision": _value(system, "revision", esptool.get("revision", "-")),
        "mac": _value(system, "mac", esptool.get("mac", "-")),
        "arduino": _value(system, "arduino"),
        "sdk": _value(system, "sdk"),
    }

    rf = summary.get("rf_quality", {})
    if not isinstance(rf, dict):
        rf = {}
    existing_reference = rf.get("reference", {})
    if not isinstance(existing_reference, dict):
        existing_reference = {}

    runtime_reference = _parse_reference_log(run_dir / "reference-serial.log")
    reference_used = bool(existing_reference or runtime_reference)
    merged_reference = dict(existing_reference)
    merged_reference.update(runtime_reference)

    reference = {
        "used": reference_used,
        "port": _value(merged_reference, "port"),
        "firmware_version": _value(
            merged_reference,
            "version",
            merged_reference.get("firmware_version", "-"),
        ),
        "build_date": _value(merged_reference, "build_date"),
        "build_time": _value(merged_reference, "build_time"),
        "chip": _value(merged_reference, "chip"),
        "revision": _value(merged_reference, "revision"),
        "mac": _value(merged_reference, "mac"),
        "arduino": _value(merged_reference, "arduino"),
        "sdk": _value(merged_reference, "sdk"),
    }

    # Keep the richer runtime identity with the RF result as well, so summary.json
    # remains self-contained for later analysis.
    if reference_used:
        rf_reference = rf.setdefault("reference", {})
        if isinstance(rf_reference, dict):
            rf_reference.update(runtime_reference)

    return {"dut": dut, "reference": reference}


def _build_text_block(identity: dict[str, Any]) -> list[str]:
    dut = identity.get("dut", {})
    reference = identity.get("reference", {})

    lines = [
        "Firmware / runtime identity:",
        "  DUT / test device:",
        f"    Firmware: {_value(dut, 'firmware_version')}",
        f"    Build: {_value(dut, 'build_date')} {_value(dut, 'build_time')}",
        f"    Profile: {_value(dut, 'profile')}",
        f"    Chip / revision: {_value(dut, 'chip')} / {_value(dut, 'revision')}",
        f"    MAC: {_value(dut, 'mac')}",
        f"    Arduino: {_value(dut, 'arduino')}",
        f"    ESP-IDF / SDK: {_value(dut, 'sdk')}",
        "  Reference ESP:",
    ]

    if not bool(reference.get("used", False)):
        lines.append("    Not used")
        return lines

    lines.extend(
        [
            f"    Firmware: {_value(reference, 'firmware_version')}",
            f"    Build: {_value(reference, 'build_date')} {_value(reference, 'build_time')}",
            f"    Port: {_value(reference, 'port')}",
            f"    Chip / revision: {_value(reference, 'chip')} / {_value(reference, 'revision')}",
            f"    MAC: {_value(reference, 'mac')}",
            f"    Arduino: {_value(reference, 'arduino')}",
            f"    ESP-IDF / SDK: {_value(reference, 'sdk')}",
        ]
    )
    return lines


def _postprocess_text(path: Path, identity: dict[str, Any]) -> None:
    text = path.read_text(encoding="utf-8")
    if "Firmware / runtime identity:" in text:
        return

    lines = text.splitlines()
    insert_at = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("Home-network stability:")
            or line.startswith("Home-network stability duration:")
        ),
        0,
    )
    block = ["", *_build_text_block(identity), ""]
    lines[insert_at:insert_at] = block
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _card(title: str, values: list[tuple[str, Any]]) -> str:
    body = "<br>".join(
        f"{html.escape(label)}: {html.escape(str(value))}"
        for label, value in values
    )
    return f'<div class="card"><b>{html.escape(title)}</b><br>{body}</div>'


def _postprocess_html(
    path: Path,
    identity: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    text = path.read_text(encoding="utf-8")
    if "Reference ESP Identity" in text:
        return

    dut = identity.get("dut", {})
    reference = identity.get("reference", {})
    system = summary.get("system", {}) if isinstance(summary.get("system"), dict) else {}
    esptool = summary.get("esptool", {}) if isinstance(summary.get("esptool"), dict) else {}

    dut_card = _card(
        "DUT / Test Device",
        [
            ("ID", summary.get("board_id") or "-"),
            ("Firmware", _value(dut, "firmware_version")),
            ("Build", f"{_value(dut, 'build_date')} {_value(dut, 'build_time')}"),
            ("Profile", _value(dut, "profile")),
            ("Chip / revision", f"{_value(dut, 'chip')} / {_value(dut, 'revision')}"),
            ("CPU", f"{_value(system, 'cpu_mhz')} MHz / {_value(system, 'cores')} core(s)"),
            ("Flash", esptool.get("flash_size", "-")),
            ("PSRAM", f"{esptool.get('embedded_psram_mb', 0)} MB"),
            ("MAC", _value(dut, "mac")),
            ("Arduino", _value(dut, "arduino")),
            ("ESP-IDF / SDK", _value(dut, "sdk")),
            ("Reset", _value(system, "reset")),
            ("Temperature", f"{_value(system, 'temperature_c')} C"),
        ],
    )

    if bool(reference.get("used", False)):
        reference_values = [
            ("Firmware", _value(reference, "firmware_version")),
            ("Build", f"{_value(reference, 'build_date')} {_value(reference, 'build_time')}"),
            ("Port", _value(reference, "port")),
            ("Chip / revision", f"{_value(reference, 'chip')} / {_value(reference, 'revision')}"),
            ("MAC", _value(reference, "mac")),
            ("Arduino", _value(reference, "arduino")),
            ("ESP-IDF / SDK", _value(reference, "sdk")),
        ]
    else:
        reference_values = [("Status", "Not used")]

    reference_card = _card("Reference ESP Identity", reference_values)

    board_pattern = re.compile(
        r'<div class="card"><b>Board</b><br>.*?</div>',
        re.DOTALL,
    )
    text, count = board_pattern.subn(dut_card + "\n" + reference_card, text, count=1)
    if count == 0:
        marker = '<div class="grid">\n'
        if marker in text:
            text = text.replace(marker, marker + dut_card + "\n" + reference_card + "\n", 1)

    path.write_text(text, encoding="utf-8")

def install(test_orchestrator_module: Any) -> None:
    """Add immutable run identity data to summary.json and final TXT/HTML reports."""
    if getattr(test_orchestrator_module, "_report_identity_installed", False):
        return

    original_write_reports = test_orchestrator_module.write_reports

    def write_reports_with_identity(run_dir: Path, summary: dict[str, Any]) -> None:
        identity = _build_identity(summary, run_dir)
        summary["test_identity"] = identity

        # test_orchestrator writes summary.json immediately before write_reports().
        # Rewrite it once with the added identity so reports and machine-readable
        # results refer to exactly the same DUT/reference runtime.
        (run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        original_write_reports(run_dir, summary)
        _postprocess_text(run_dir / "report.txt", identity)
        _postprocess_html(run_dir / "report.html", identity, summary)

    test_orchestrator_module.write_reports = write_reports_with_identity
    test_orchestrator_module._report_identity_installed = True
