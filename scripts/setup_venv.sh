#!/usr/bin/env bash
# Create the Python venv and install dependencies.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    # This project requires Python 3.10+ (mcp/pydantic).
    for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
                PYTHON="$candidate"
                break
            fi
        fi
    done
fi

if [[ -z "$PYTHON" ]]; then
    echo "Error: no Python 3.10+ interpreter found." >&2
    echo "Install one (e.g. 'brew install python@3.12') or set PYTHON=/path/to/python and re-run." >&2
    exit 1
fi

echo "Using python: $PYTHON ($($PYTHON --version 2>&1))"
"$PYTHON" -m venv .venv
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/pip install -r requirements.txt
echo
echo "venv ready: $REPO_DIR/.venv"
./.venv/bin/python -m mcp_server.cli list-ports || true
