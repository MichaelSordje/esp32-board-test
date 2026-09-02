#!/usr/bin/env bash
set -u
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
cd "$ROOT" || exit 1

echo
echo "=========================================="
echo " ESP32 Hardware / Quality Test"
echo " Linux artifact launcher"
echo "=========================================="
echo

find_python() {
    local candidate
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
                command -v "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON="$(find_python || true)"
if [[ -z "$PYTHON" ]]; then
    echo "ERROR: Python 3.10 or newer was not found."
    echo "Debian/Ubuntu example: sudo apt install python3 python3-venv"
    exit 1
fi

VENV_DIR="${ESP_TEST_VENV:-$ROOT/.venv-linux}"
VENV_PYTHON="$VENV_DIR/bin/python"
REQUIREMENTS="$ROOT/requirements.txt"
HASH_FILE="$VENV_DIR/.requirements.sha256"

if [[ ! -f "$REQUIREMENTS" ]]; then
    echo "ERROR: requirements.txt is missing: $REQUIREMENTS"
    exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Setting up the Linux Python environment on first launch ..."
    if ! "$PYTHON" -m venv "$VENV_DIR"; then
        echo
        echo "ERROR: The Python virtual environment could not be created."
        echo "Debian/Ubuntu usually requires: sudo apt install python3-venv"
        exit 1
    fi

    if ! "$VENV_PYTHON" -m pip install --disable-pip-version-check --quiet --upgrade pip; then
        echo "ERROR: pip could not be updated in the Linux virtual environment."
        exit 1
    fi
fi

if ! "$VENV_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    echo "ERROR: The existing Linux virtual environment uses Python older than 3.10."
    echo "Remove .venv-linux (or the directory selected by ESP_TEST_VENV) and run scripts/start-test.sh again."
    exit 1
fi

CURRENT_HASH="$("$PYTHON" - "$REQUIREMENTS" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
print(hashlib.sha256(path.read_bytes()).hexdigest().upper())
PY
)"
if [[ -z "$CURRENT_HASH" ]]; then
    echo "ERROR: Could not calculate requirements.txt SHA256."
    exit 1
fi

INSTALLED_HASH=""
if [[ -f "$HASH_FILE" ]]; then
    INSTALLED_HASH="$(tr -d '\r\n' < "$HASH_FILE")"
fi

if [[ "$CURRENT_HASH" != "$INSTALLED_HASH" ]]; then
    echo "Installing/updating required local test tools ..."
    if ! "$VENV_PYTHON" -m pip install --disable-pip-version-check --quiet -r "$REQUIREMENTS"; then
        echo "ERROR: The required test tools could not be installed."
        exit 1
    fi
    printf '%s\n' "$CURRENT_HASH" > "$HASH_FILE"
fi

# Fail before touching the DUT when firmware artifacts are missing, outdated,
# incomplete, or fail their size/SHA256 verification. firmware_artifacts.py
# currently uses the Windows command name in some status messages, so translate
# that status-only text for the Linux launcher.
STATUS_OUTPUT="$("$VENV_PYTHON" "$ROOT/tools/firmware_artifacts.py" status 2>&1)"
STATUS_RC=$?
printf '%s\n' "$STATUS_OUTPUT" | sed 's#scripts\\compile_all\.cmd#bash scripts/compile_all.sh#g'

if [[ "$STATUS_RC" -ne 0 ]]; then
    echo
    echo "The precompiled DUT firmware is not current."
    echo "Run: bash scripts/compile_all.sh"
    exit 1
fi

"$VENV_PYTHON" "$ROOT/tools/run_test_artifact.py" "$@"
RC=$?

if [[ "$RC" -eq 2 ]]; then
    echo
    echo "Board test completed: FAIL"
elif [[ "$RC" -ne 0 ]]; then
    echo
    echo "The test could not be completed due to a technical error."
fi

exit "$RC"
