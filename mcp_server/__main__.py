"""Entry point: `python -m mcp_server` runs the MCP stdio server.

Use `python -m mcp_server.cli` for helper subcommands (list-ports, monitor, test).
"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("list-ports", "monitor", "test", "--help", "-h"):
        from .cli import main as cli_main

        sys.exit(cli_main(sys.argv[1:]))
    from .server import main as server_main

    server_main()


if __name__ == "__main__":
    main()
