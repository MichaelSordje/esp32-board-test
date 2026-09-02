from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import test_orchestrator
import test_runner
from native_usb_deep_sleep import install as install_native_usb_deep_sleep
from report_identity import install as install_report_identity
from result_status import install as install_result_status


ROOT = Path(__file__).resolve().parents[1]
BASE_SETTINGS_PATH = ROOT / "config" / "test-settings.json"
LOCAL_SETTINGS_PATH = ROOT / "config" / "test-settings.local.json"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)

    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)

    return result


def _load_effective_settings() -> dict[str, Any]:
    base = test_runner.load_json(BASE_SETTINGS_PATH)
    if not isinstance(base, dict):
        raise RuntimeError("config/test-settings.json must contain a JSON object.")

    if not LOCAL_SETTINGS_PATH.exists():
        return base

    local = test_runner.load_json(LOCAL_SETTINGS_PATH)
    if not isinstance(local, dict):
        raise RuntimeError(
            "config/test-settings.local.json must contain a JSON object."
        )

    print("Local settings override: config/test-settings.local.json")
    return _deep_merge(base, local)


def _linux_error_text(exc: Exception) -> str:
    message = str(exc)

    message = message.replace(
        "cannot be opened or configured by Windows. Disconnect the board from USB "
        "briefly, reconnect it, and try again.",
        "cannot be opened on Linux. Check the USB connection and serial-device "
        "permissions, then try again.",
    )
    message = message.replace(
        "A suitable COM port was found",
        "A suitable serial port was found",
    )

    lower = message.lower()
    hints: list[str] = []

    if (
        "permissionerror(13" in lower
        or "permission denied" in lower
        or "cannot be opened on linux" in lower
    ):
        hints.append(
            'Linux serial permission hint: on Debian/Ubuntu add your user to '
            'the "dialout" group with `sudo usermod -aG dialout "$USER"`, then '
            "log out and back in."
        )

    if "host 'ping' command is required" in lower:
        hints.append(
            "Linux ping hint: on Debian/Ubuntu install it with "
            "`sudo apt install iputils-ping`."
        )

    if hints:
        message += "\n\n" + "\n".join(hints)

    return message




def _capture_linux_result_state() -> dict[str, int]:
    if not sys.platform.startswith("linux"):
        return {}

    try:
        from linux_postprocess import capture_result_state

        return capture_result_state()
    except Exception:
        return {}


def _run_linux_postprocess(before_state: dict[str, int]) -> None:
    if not sys.platform.startswith("linux"):
        return

    try:
        from linux_postprocess import postprocess

        postprocess(before_state)
    except Exception as exc:
        print(
            "\nNote: Linux report/label post-processing failed. "
            "The hardware test result remains unchanged."
        )
        print(f"Detail: {exc}")


def main() -> int:
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        return test_orchestrator.main()

    effective_settings = _load_effective_settings()
    original_load_json = test_runner.load_json
    settings_path = BASE_SETTINGS_PATH.resolve()

    def load_json_with_local_override(path: Path) -> dict[str, Any]:
        candidate = Path(path).resolve()
        if candidate == settings_path:
            return copy.deepcopy(effective_settings)
        return original_load_json(path)

    test_runner.load_json = load_json_with_local_override
    install_native_usb_deep_sleep(test_runner)
    install_result_status(test_runner, test_orchestrator)
    install_report_identity(test_orchestrator)
    linux_result_state = _capture_linux_result_state()

    result = test_orchestrator.main()

    _run_linux_postprocess(linux_result_state)
    return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nTest aborted.")
        raise SystemExit(130)
    except Exception as exc:
        if sys.platform.startswith("linux"):
            print(f"\nERROR: {_linux_error_text(exc)}")
        else:
            print(f"\nERROR: {exc}")
        raise SystemExit(1)
