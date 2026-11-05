#!/usr/bin/env bash
# Register this project's MCP server with Claude Code (project .mcp.json).
# Merges into an existing .mcp.json so other project MCP servers are preserved.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$REPO_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Error: $PYTHON_BIN not found. Run scripts/setup_venv.sh first." >&2
    exit 1
fi

"$PYTHON_BIN" - "$REPO_DIR/.mcp.json" "$PYTHON_BIN" <<'EOF'
import json
import sys

path, python_bin = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("not an object")
except FileNotFoundError:
    cfg = {}
except Exception:
    print(f"Warning: {path} is not valid JSON; it will be overwritten.", file=sys.stderr)
    cfg = {}

cfg.setdefault("mcpServers", {})
cfg["mcpServers"]["cubase"] = {
    "command": python_bin,
    "args": ["-m", "mcp_server"],
    "env": {},
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
others = sorted(k for k in cfg["mcpServers"] if k != "cubase")
print(f"Updated {path} (existing servers preserved: {others or 'none'})")
EOF

echo "Restart Claude Code in this directory (or run 'claude mcp list') to load it."
echo
echo "For a global (user-scope) registration instead:"
echo "  claude mcp add --scope user cubase -- $PYTHON_BIN -m mcp_server"
