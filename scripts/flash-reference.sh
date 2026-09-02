#!/usr/bin/env bash
set -u
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
cd "$ROOT" || exit 1

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo "ERROR: Python 3.10 or newer was not found."
    exit 1
fi

VENV_DIR="${ESP_TEST_VENV:-$ROOT/.venv-linux}"
VENV_PYTHON="$VENV_DIR/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
    "$PYTHON" -m venv "$VENV_DIR" || exit 1
fi
"$VENV_PYTHON" -m pip install --disable-pip-version-check --quiet -r "$ROOT/requirements.txt" || exit 1
exec "$VENV_PYTHON" "$ROOT/tools/flash_reference.py" "$@"
