from __future__ import annotations

import json
from pathlib import Path
from typing import Any


RF_UNRATED_REASON = (
    "RF quality thresholds are not calibrated for this board profile"
)


def _apply_overall_result(summary: dict[str, Any]) -> dict[str, Any]:
    """Apply the tri-state board result without changing any test measurement.

    Priority is intentional: a real hard failure always remains FAIL. Only a
    run with no hard failure can become UNRATED when an enabled RF quality test
    completed successfully but cannot be judged because calibration thresholds
    are missing. PASS therefore means every decisive enabled test was ratable
    and passed.
    """
    fail_reasons = summary.get("fail_reasons", [])
    if not isinstance(fail_reasons, list):
        fail_reasons = []

    unrated_reasons: list[str] = []
    test_config = summary.get("test_config", {})
    rf = summary.get("rf_quality", {})

    rf_enabled = bool(
        isinstance(test_config, dict)
        and test_config.get("rf_quality", False)
    )
    rf_unrated = bool(
        rf_enabled
        and isinstance(rf, dict)
        and str(rf.get("execution_status") or "").upper() == "PASS"
        and str(rf.get("quality_status") or "").upper() == "UNRATED"
    )

    if rf_unrated:
        unrated_reasons.append(RF_UNRATED_REASON)

    summary["unrated_reasons"] = unrated_reasons

    if fail_reasons:
        summary["result"] = "FAIL"
    elif unrated_reasons:
        summary["result"] = "UNRATED"
    else:
        summary["result"] = "PASS"

    return summary


def _postprocess_text_report(path: Path, summary: dict[str, Any]) -> None:
    reasons = summary.get("unrated_reasons", [])
    if not isinstance(reasons, list) or not reasons or not path.is_file():
        return

    text = path.read_text(encoding="utf-8")
    if "UNRATED reasons board/test:" in text:
        return

    block = "\nUNRATED reasons board/test:\n" + "\n".join(
        f"  - {reason}" for reason in reasons
    ) + "\n"

    marker = "\nSTABILITY FAIL:\n"
    index = text.find(marker)
    if index < 0:
        marker = "\nSTABILITY WARN:\n"
        index = text.find(marker)
    if index < 0:
        marker = "\nWarnings:\n"
        index = text.find(marker)

    if index >= 0:
        text = text[:index] + block + text[index:]
    else:
        text = text.rstrip() + "\n" + block

    path.write_text(text, encoding="utf-8")


def _postprocess_html_report(path: Path, summary: dict[str, Any]) -> None:
    if not path.is_file():
        return

    text = path.read_text(encoding="utf-8")

    if summary.get("result") == "UNRATED":
        text = text.replace(
            '<div class="result fail">BOARD: UNRATED</div>',
            '<div class="result warn">BOARD: UNRATED</div>',
            1,
        )
        if ".result.warn" not in text:
            text = text.replace(
                ".result.pass { background:#d9fbe4; color:#116329; } .result.fail { background:#ffe0e0; color:#9a1c1c; }",
                ".result.pass { background:#d9fbe4; color:#116329; } .result.fail { background:#ffe0e0; color:#9a1c1c; } .result.warn { background:#fff4cc; color:#7a5d00; }",
                1,
            )

    # RF UNRATED should be visually neutral/warning, not look like a skipped
    # measurement. The RF measurement did run; only its production limit is
    # missing.
    text = text.replace('class="skip">UNRATED', 'class="warn">UNRATED')

    reasons = summary.get("unrated_reasons", [])
    if isinstance(reasons, list) and reasons and "UNRATED Reasons Board/Test" not in text:
        items = "".join(f"<li>{_html_escape(str(reason))}</li>" for reason in reasons)
        marker = "<h2>FAIL Reasons Board/Test</h2>"
        block = f"<h2>UNRATED Reasons Board/Test</h2><ul>{items}</ul>\n"
        if marker in text:
            text = text.replace(marker, block + marker, 1)
        else:
            text = text.replace("</main></body></html>", block + "</main></body></html>", 1)

    path.write_text(text, encoding="utf-8")


def _html_escape(value: str) -> str:
    import html

    return html.escape(value)


def _postprocess_results_index(results_root: Path) -> None:
    path = results_root / "index.html"
    if not path.is_file():
        return

    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "<td class='fail'>UNRATED</td>",
        "<td class='warn'>UNRATED</td>",
    )
    text = text.replace(
        "<td class=''>UNRATED</td>",
        "<td class='warn'>UNRATED</td>",
    )
    path.write_text(text, encoding="utf-8")


def _capture_summary_state(results_root: Path) -> dict[str, int]:
    state: dict[str, int] = {}
    if not results_root.is_dir():
        return state

    for path in results_root.glob("*/summary.json"):
        try:
            state[str(path.resolve())] = path.stat().st_mtime_ns
        except OSError:
            continue
    return state


def _changed_summary(
    results_root: Path,
    before: dict[str, int],
) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    if not results_root.is_dir():
        return None

    for path in results_root.glob("*/summary.json"):
        try:
            modified = path.stat().st_mtime_ns
            key = str(path.resolve())
        except OSError:
            continue
        if key not in before or before[key] != modified:
            candidates.append((modified, path))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def install(test_runner_module: Any, test_orchestrator_module: Any) -> None:
    """Install tri-state PASS/FAIL/UNRATED handling into the existing runner."""
    if getattr(test_runner_module, "_tri_state_result_installed", False):
        return

    original_build_summary = test_runner_module.build_summary
    original_write_text_report = test_runner_module.write_text_report
    original_write_html_report = test_runner_module.write_html_report
    original_generate_results_index = test_runner_module.generate_results_index
    original_orchestrator_main = test_orchestrator_module.main

    def build_summary_with_unrated(*args: Any, **kwargs: Any) -> dict[str, Any]:
        summary = original_build_summary(*args, **kwargs)
        return _apply_overall_result(summary)

    def write_text_report_with_unrated(path: Path, summary: dict[str, Any]) -> None:
        original_write_text_report(path, summary)
        _postprocess_text_report(path, summary)

    def write_html_report_with_unrated(path: Path, summary: dict[str, Any]) -> None:
        original_write_html_report(path, summary)
        _postprocess_html_report(path, summary)

    def generate_results_index_with_unrated() -> None:
        original_generate_results_index()
        _postprocess_results_index(Path(test_runner_module.RESULTS_ROOT))

    def orchestrator_main_with_unrated_exit() -> int:
        results_root = Path(test_runner_module.RESULTS_ROOT)
        before = _capture_summary_state(results_root)
        exit_code = int(original_orchestrator_main())

        # Keep the existing meanings unchanged: PASS=0, FAIL=2. UNRATED gets a
        # separate code so callers can distinguish "not calibrated" from a
        # failed board without parsing console text.
        if exit_code == 2:
            summary_path = _changed_summary(results_root, before)
            if summary_path is not None:
                try:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                except Exception:
                    summary = {}
                if str(summary.get("result") or "").upper() == "UNRATED":
                    return 3
        return exit_code

    test_runner_module.build_summary = build_summary_with_unrated
    test_runner_module.write_text_report = write_text_report_with_unrated
    test_runner_module.write_html_report = write_html_report_with_unrated
    test_runner_module.generate_results_index = generate_results_index_with_unrated
    test_orchestrator_module.main = orchestrator_main_with_unrated_exit
    test_runner_module._tri_state_result_installed = True
