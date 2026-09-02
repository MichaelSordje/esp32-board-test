#!/usr/bin/env bash
set -u
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
cd "$ROOT" || exit 1

VENV_DIR="${ESP_TEST_VENV:-$ROOT/.venv-linux}"
VENV_PYTHON="$VENV_DIR/bin/python"

if [[ -x "$VENV_PYTHON" ]]; then
    PYTHON="$VENV_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
else
    echo "ERROR: Python 3.10 or newer was not found."
    exit 1
fi

"$PYTHON" "$ROOT/tools/linux_label.py" "$@"
exit $?
