from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from board_ids import normalize_board_id
from label_generator import create_png_label_from_summary


ROOT = Path(__file__).resolve().parents[1]
BASE_SETTINGS_PATH = ROOT / "config" / "test-settings.json"
LOCAL_SETTINGS_PATH = ROOT / "config" / "test-settings.local.json"
RESULTS_ROOT = ROOT / "results"


@dataclass
class LinuxLabelConfig:
    printer_name: str
    cups_media: str
    cups_options: dict[str, str]
    width_mm: float
    height_mm: float
    dpi: int
    margin_mm: float


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must contain a JSON object.")
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_effective_settings() -> dict[str, Any]:
    settings = _load_json(BASE_SETTINGS_PATH)
    if LOCAL_SETTINGS_PATH.is_file():
        settings = _deep_merge(settings, _load_json(LOCAL_SETTINGS_PATH))
    return settings


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


def resolve_linux_label_config(settings: dict[str, Any]) -> LinuxLabelConfig:
    label = settings.get("label", {})
    if not isinstance(label, dict):
        raise RuntimeError("test-settings.json: 'label' must be an object.")

    backend = str(label.get("linux_backend", "cups")).strip().lower()
    if backend != "cups":
        raise RuntimeError(
            f"Unsupported Linux label backend '{backend}'. "
            "The supported Linux backend is 'cups'."
        )

    printer_name = str(label.get("linux_printer_name", "") or "").strip()
    cups_media = str(label.get("cups_media", "") or "").strip()

    raw_options = label.get("cups_options", {})
    if not isinstance(raw_options, dict):
        raise RuntimeError("label.cups_options must be a JSON object.")

    cups_options: dict[str, str] = {}
    for key, value in raw_options.items():
        name = str(key).strip()
        if not name:
            raise RuntimeError("label.cups_options contains an empty option name.")
        if isinstance(value, (dict, list)):
            raise RuntimeError(
                f"label.cups_options.{name} must be a string, number, or boolean."
            )
        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        cups_options[name] = text

    width_mm = float(label.get("width_mm", 62.0))
    height_mm = float(label.get("height_mm", 0.0))
    dpi = int(label.get("dpi", 300))
    margin_mm = float(label.get("margin_mm", 2.0))

    if width_mm <= 0:
        raise RuntimeError("label.width_mm must be greater than 0.")
    if height_mm < 0:
        raise RuntimeError(
            "label.height_mm must be 0 (automatic) or greater than 0."
        )
    if dpi < 72 or dpi > 1200:
        raise RuntimeError("label.dpi must be between 72 and 1200.")
    if margin_mm < 0 or margin_mm * 2 >= width_mm:
        raise RuntimeError("label.margin_mm is invalid for the configured width.")

    return LinuxLabelConfig(
        printer_name=printer_name,
        cups_media=cups_media,
        cups_options=cups_options,
        width_mm=width_mm,
        height_mm=height_mm,
        dpi=dpi,
        margin_mm=margin_mm,
    )


def _read_summary(path: Path) -> dict[str, Any]:
    summary = _load_json(path)
    board_id = normalize_board_id(str(summary.get("board_id") or ""))
    result = str(summary.get("result") or "").strip().upper()
    timestamp = str(summary.get("timestamp") or "").strip()

    if result not in {"PASS", "FAIL", "UNRATED"}:
        raise RuntimeError(f"Invalid test result in {path}: {result}")
    if not timestamp:
        raise RuntimeError(f"Test timestamp is missing in {path}.")

    summary["board_id"] = board_id
    summary["result"] = result
    return summary


def _summary_timestamp(summary: dict[str, Any]) -> datetime:
    text = str(summary.get("timestamp") or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _all_summaries() -> list[tuple[Path, dict[str, Any]]]:
    entries: list[tuple[Path, dict[str, Any]]] = []
    if not RESULTS_ROOT.is_dir():
        return entries

    for path in RESULTS_ROOT.rglob("summary.json"):
        try:
            entries.append((path, _read_summary(path)))
        except Exception:
            continue

    entries.sort(key=lambda item: _summary_timestamp(item[1]).timestamp(), reverse=True)
    return entries


def select_summary(requested: str = "") -> tuple[Path, dict[str, Any]]:
    entries = _all_summaries()
    if not entries:
        raise RuntimeError("No test results found under results.")

    requested = requested.strip().upper()
    if not requested:
        return entries[0]

    if re.fullmatch(r"\d{1,3}", requested):
        number = int(requested)
        if number < 1 or number > 999:
            raise RuntimeError(f"Invalid board number: {requested}")
        prefix = f"{number:03d}-"
        matches = [
            item
            for item in entries
            if str(item[1].get("board_id") or "").startswith(prefix)
        ]
        if not matches:
            raise RuntimeError(f"No test result found for board number {number:03d}.")
        ids = sorted({str(item[1]["board_id"]) for item in matches})
        if len(ids) > 1:
            raise RuntimeError(
                f"Board number {number:03d} exists more than once in legacy data: "
                + ", ".join(ids)
                + ". Enter the full board ID."
            )
        return matches[0]

    canonical = normalize_board_id(requested)
    for item in entries:
        if item[1]["board_id"] == canonical:
            return item

    raise RuntimeError(f"No test result found for {canonical}.")


def _label_height_mm(path: Path, dpi: int) -> float:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for Linux label printing. "
            "Run scripts/start.sh once to install the project requirements."
        ) from exc

    with Image.open(path) as image:
        height_px = int(image.height)
        image_dpi = image.info.get("dpi")
        dpi_y = float(image_dpi[1]) if image_dpi else float(dpi)
        if dpi_y <= 1:
            dpi_y = float(dpi)
        return (height_px / dpi_y) * 25.4


def _cups_command(
    label_path: Path,
    config: LinuxLabelConfig,
) -> tuple[list[str], str]:
    lp = shutil.which("lp")
    if not lp:
        raise RuntimeError(
            "CUPS command 'lp' was not found. Install the CUPS client tools "
            "(for example `sudo apt install cups-client`) and configure a printer."
        )

    command = [lp]
    if config.printer_name:
        command += ["-d", config.printer_name]

    if config.cups_media:
        command += ["-o", f"media={config.cups_media}"]
    else:
        actual_height_mm = _label_height_mm(label_path, config.dpi)
        custom_media = f"Custom.{config.width_mm:.2f}x{actual_height_mm:.2f}mm"
        command += ["-o", f"media={custom_media}"]

    for name, value in config.cups_options.items():
        command += ["-o", f"{name}={value}"]

    command.append(str(label_path))
    return command, config.printer_name


def print_cups(label_path: Path, config: LinuxLabelConfig) -> str:
    command, configured_printer = _cups_command(label_path, config)
    result = subprocess.run(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        check=False,
    )

    output = (result.stdout or "").strip()
    if result.returncode != 0:
        raise RuntimeError(
            "CUPS printing failed"
            + (f": {output}" if output else f" (exit code {result.returncode})")
        )

    if output:
        print(output)

    if configured_printer:
        return configured_printer

    # Typical CUPS output: "request id is QUEUE-123 (1 file(s))".
    match = re.search(r"request\s+id\s+is\s+([^\s]+)-\d+", output, re.IGNORECASE)
    if match:
        return match.group(1)

    return "<default CUPS printer>"


def _get_print_count(directory: Path) -> int:
    path = directory / "label-print.json"
    if not path.is_file():
        return 0
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return int(value.get("print_count", 0))
    except Exception:
        return 0


def _save_print_state(
    summary_path: Path,
    summary: dict[str, Any],
    label_path: Path,
    previous_count: int,
    printer_name: str,
) -> None:
    state = {
        "board_id": summary["board_id"],
        "result": summary["result"],
        "test_timestamp": str(summary.get("timestamp") or ""),
        "print_count": previous_count + 1,
        "last_printed_at": datetime.now().isoformat(timespec="seconds"),
        "backend": "cups",
        "printer_name": printer_name,
        "label_file": label_path.name,
    }
    (summary_path.parent / "label-print.json").write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


def run_label_from_summary(
    summary_path: Path,
    *,
    ask_before_print: bool = False,
    no_print: bool = False,
) -> Path:
    summary_path = summary_path.resolve()
    if not summary_path.is_file():
        raise RuntimeError(f"summary.json not found: {summary_path}")

    summary = _read_summary(summary_path)
    settings = load_effective_settings()
    config = resolve_linux_label_config(settings)

    board_id = summary["board_id"]
    label_path = summary_path.parent / f"label_{board_id}.png"
    existed = label_path.is_file()

    try:
        create_png_label_from_summary(
            label_path,
            summary,
            width_mm=config.width_mm,
            height_mm=config.height_mm,
            dpi=config.dpi,
            margin_mm=config.margin_mm,
        )
    except RuntimeError as exc:
        message = str(exc).replace("scripts\\start.cmd", "scripts/start.sh")
        raise RuntimeError(message) from exc

    previous_count = _get_print_count(summary_path.parent)
    timestamp = _summary_timestamp(summary).strftime("%m/%y")
    printer_display = config.printer_name or "CUPS default printer"

    print("")
    print("==========================================")
    print(f" BOARD:   {board_id}")
    print(f" RESULT:  {summary['result']}")
    print(f" DATE:    {timestamp}")
    print(" BACKEND: cups")
    print(f" PRINTER: {printer_display}")
    print(f" LABEL:   {'regenerated / reprint' if existed else 'newly created'}")
    print(f" FILE:    {label_path.name}")
    print(f" PRINTED: {previous_count}x")
    print("==========================================")
    print("")

    if no_print:
        print("Label generated; printing skipped (--no-print).")
        return label_path

    if ask_before_print:
        answer = input("Print label now? [Y/N] ").strip().lower()
        if answer not in {"y", "yes", "j", "ja"}:
            print("Not printed. The label remains saved.")
            return label_path
    else:
        print("Label is being printed automatically ...")

    printer_name = print_cups(label_path, config)
    _save_print_state(
        summary_path,
        summary,
        label_path,
        previous_count,
        printer_name,
    )
    print("")
    print(f"Label printed: {board_id}")
    return label_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create/print an ESP32 Board Test label on Linux through CUPS."
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="Path to a specific summary.json.",
    )
    parser.add_argument(
        "--board",
        default="",
        help="Board ID or number. If omitted, an interactive/latest selection is used.",
    )
    parser.add_argument(
        "--ask-before-print",
        action="store_true",
        help="Ask before sending the label to CUPS.",
    )
    parser.add_argument(
        "--no-print",
        action="store_true",
        help="Only generate the PNG label; do not send it to CUPS.",
    )
    args = parser.parse_args()

    if args.summary is not None:
        summary_path = args.summary
    else:
        requested = args.board.strip()
        if not requested:
            requested = input(
                "Which label should be created/printed? "
                "Board ID/number [Enter = latest] "
            ).strip()
        summary_path, _summary = select_summary(requested)

    run_label_from_summary(
        summary_path,
        ask_before_print=args.ask_before_print,
        no_print=args.no_print,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nPrinting aborted.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        raise SystemExit(1)
