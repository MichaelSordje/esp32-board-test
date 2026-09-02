from __future__ import annotations

import sys

import test_runner
from firmware_artifacts import install_runtime_artifact_mode

# Patch only the firmware build/flash boundary. The existing orchestrator,
# measurements, reports, board IDs and quality evaluation stay unchanged.
install_runtime_artifact_mode(test_runner)

import run_test


if __name__ == "__main__":
    try:
        raise SystemExit(run_test.main())
    except KeyboardInterrupt:
        print("\nTest aborted.")
        raise SystemExit(130)
    except Exception as exc:
        if sys.platform.startswith("linux"):
            print(f"\nERROR: {run_test._linux_error_text(exc)}")
        else:
            print(f"\nERROR: {exc}")
        raise SystemExit(1)
