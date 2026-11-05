#!/usr/bin/env bash
# Create the Python venv and install dependencies.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    done
fi

echo "Using python: $PYTHON ($($PYTHON --version 2>&1))"
"$PYTHON" -m venv .venv
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/pip install -r requirements.txt
echo
echo "venv ready: $REPO_DIR/.venv"
./.venv/bin/python -m mcp_server.cli list-ports || true
