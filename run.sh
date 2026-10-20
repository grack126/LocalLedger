#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# LocalLedger OCR - one-click launcher for macOS / Linux
#   First run: creates .venv, installs dependencies, generates sample data.
#   Later runs: starts straight away. Extra arguments go to launcher.py
#   (e.g.  ./run.sh --port 8600   or   ./run.sh --reinstall).
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

VPY=".venv/bin/python"

if [ ! -x "$VPY" ]; then
    echo
    echo "  LocalLedger OCR - first-time setup"
    echo "  ----------------------------------"
    PY=""
    for candidate in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    done

    if [ -n "$PY" ]; then
        echo "  Using Python: $PY ($("$PY" --version 2>&1))"
        if ! "$PY" -m venv .venv; then
            echo
            echo "  Could not create the virtual environment."
            echo "  On Debian/Ubuntu install it with:  sudo apt install python3-venv"
            rm -rf .venv
            exit 1
        fi
    elif command -v uv >/dev/null 2>&1; then
        echo "  No Python 3.11+ on PATH - setting one up with uv ..."
        uv venv --seed --python ">=3.11" .venv
    else
        echo
        echo "  Python 3.11 or newer is required."
        echo "    macOS:  brew install python@3.12   (or https://www.python.org/downloads/)"
        echo "    Linux:  sudo apt install python3.12 python3.12-venv   (or your distro's equivalent)"
        exit 1
    fi
fi

exec "$VPY" launcher.py "$@"
