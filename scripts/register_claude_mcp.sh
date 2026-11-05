#!/usr/bin/env bash
# Register this project's MCP server with Claude Code (project .mcp.json).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$REPO_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Error: $PYTHON_BIN not found. Run scripts/setup_venv.sh first." >&2
    exit 1
fi

cat > "$REPO_DIR/.mcp.json" <<EOF
{
  "mcpServers": {
    "cubase": {
      "command": "$PYTHON_BIN",
      "args": ["-m", "mcp_server"],
      "env": {}
    }
  }
}
EOF

echo "Wrote $REPO_DIR/.mcp.json"
echo "Restart Claude Code in this directory (or run 'claude mcp list') to load it."
echo
echo "For a global (user-scope) registration instead:"
echo "  claude mcp add --scope user cubase -- $PYTHON_BIN -m mcp_server"
