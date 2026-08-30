#!/usr/bin/env python
"""Run the MCP supervisor server or a local JSON smoke-check tool command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server import build_supervisor, build_supervisor_registry, create_mcp_app
from src.supervisor import SupervisorMCPServer


def main() -> int:
    parser = argparse.ArgumentParser(description="AlgoSphere MCP supervisor runner")
    parser.add_argument("tool", nargs="?", default="serve", help="Tool name, list_tools, or serve")
    parser.add_argument("--config", default=None)
    parser.add_argument("--users", default=None)
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--no-broker", action="store_true")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio", "sse", "streamable-http"),
        help="FastMCP transport when tool=serve",
    )
    args = parser.parse_args()

    if args.tool == "serve":
        registry = build_supervisor_registry(
            config_path=args.config,
            users_path=args.users,
            user_id=args.user_id,
            connect_broker=not args.no_broker,
        )
        app = create_mcp_app(registry=registry)
        app.run(transport=args.transport)
        return 0

    supervisor = build_supervisor(
        config_path=args.config,
        users_path=args.users,
        user_id=args.user_id or "default",
        connect_broker=not args.no_broker,
    )
    server = SupervisorMCPServer(supervisor)
    if args.tool == "list_tools":
        payload = server.list_tools()
    else:
        payload = server.call_tool(args.tool)
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
