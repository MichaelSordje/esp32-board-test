from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import test_runner

from linux_label import (
    load_effective_settings,
    resolve_label_mode,
    run_label_from_summary,
)


ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = ROOT / "results"


def capture_result_state() -> dict[str, int]:
    state: dict[str, int] = {}
    if not RESULTS_ROOT.is_dir():
        return state

    for path in RESULTS_ROOT.rglob("summary.json"):
        try:
            state[str(path.resolve())] = path.stat().st_mtime_ns
        except OSError:
            continue

    return state


def _newest_changed_summary(before_state: dict[str, int]) -> Path | None:
    if not RESULTS_ROOT.is_dir():
        return None

    candidates: list[tuple[int, Path]] = []
    for path in RESULTS_ROOT.rglob("summary.json"):
        try:
            modified_ns = path.stat().st_mtime_ns
            key = str(path.resolve())
        except OSError:
            continue

        if key not in before_state or before_state[key] != modified_ns:
            candidates.append((modified_ns, path))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _open_report(report_path: Path) -> None:
    if not report_path.is_file():
        return

    # Headless Linux systems should keep the report local without producing
    # a desktop-opening error.
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print(f"Report: {report_path}")
        return

    command: list[str] | None = None
    xdg_open = shutil.which("xdg-open")
    if xdg_open:
        command = [xdg_open, str(report_path)]
    else:
        gio = shutil.which("gio")
        if gio:
            command = [gio, "open", str(report_path)]

    if not command:
        print(f"Report: {report_path}")
        return

    try:
        subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        print(f"Report: {report_path}")


def postprocess(before_state: dict[str, int]) -> None:
    if not sys.platform.startswith("linux"):
        return

    summary_path = _newest_changed_summary(before_state)
    if summary_path is None:
        return

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Note: Linux post-processing could not read {summary_path}: {exc}")
        return

    settings = load_effective_settings()
    label_mode = resolve_label_mode(settings)

    # Label printing follows label.mode after every normally completed run
    # with a PASS/FAIL summary, independent of which tests were selected.
    # Linux supplies the CUPS action that the Windows label workflow omits.
    if test_runner.should_run_label(label_mode):
        print("\nGenerating and printing label through CUPS ...")
        try:
            run_label_from_summary(
                summary_path,
                ask_before_print=(label_mode == "ask"),
            )
        except Exception as exc:
            print(
                "Note: Linux label generation/printing failed. "
                "The hardware test result remains unchanged."
            )
            print(f"Detail: {exc}")

    _open_report(summary_path.parent / "report.html")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Linux post-processing helper for ESP32 Board Test."
    )
    parser.add_argument(
        "--show-state",
        action="store_true",
        help="Print the current summary-file state and exit.",
    )
    args = parser.parse_args()

    if args.show_state:
        print(json.dumps(capture_result_state(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
