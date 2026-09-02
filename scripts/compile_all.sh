#!/usr/bin/env bash
set -u
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
cd "$ROOT" || exit 1

COMMAND="${1:-compile-all}"
case "$COMMAND" in
    compile-all|compile-reference-all|prepare-all) ;;
    *)
        echo "ERROR: Unsupported firmware command: $COMMAND"
        exit 1
        ;;
esac

echo
echo "=========================================="
echo " Compile all ESP32 test firmware"
echo " Linux artifact builder"
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
    echo "Setting up the Linux Python/build environment on first launch ..."
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
    echo "Remove .venv-linux (or the directory selected by ESP_TEST_VENV) and run this command again."
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
    echo "Installing/updating required local build tools ..."
    if ! "$VENV_PYTHON" -m pip install --disable-pip-version-check --quiet -r "$REQUIREMENTS"; then
        echo "ERROR: The required build tools could not be installed."
        exit 1
    fi
    printf '%s\n' "$CURRENT_HASH" > "$HASH_FILE"
fi

"$VENV_PYTHON" "$ROOT/tools/firmware_artifacts.py" "$COMMAND"
RC=$?

if [[ "$RC" -ne 0 ]]; then
    echo
    echo "Firmware command failed. Existing valid firmware artifacts were not marked as current."
    exit "$RC"
fi

echo
if [[ "$COMMAND" == "compile-all" ]]; then
    echo "All DUT firmware variants compiled successfully."
    echo "Board tests can now run without PlatformIO/compiler access."
elif [[ "$COMMAND" == "compile-reference-all" ]]; then
    echo "All reference firmware variants compiled successfully."
else
    echo "PlatformIO package preparation completed."
fi
exit 0
