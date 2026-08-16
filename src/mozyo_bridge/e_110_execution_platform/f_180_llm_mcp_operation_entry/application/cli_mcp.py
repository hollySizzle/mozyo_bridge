"""CLI surface for the local MCP server (Redmine #15161).

``mozyo-bridge mcp serve`` is how an MCP client launches this server from the
installed package: the client spawns it as a subprocess and speaks JSON-RPC over
its stdin/stdout. ``mozyo-bridge mcp tools`` prints the published catalog for an
operator who wants to see the surface without running a session.

Note the direction of the subprocess here. The *client* spawns the server — that
is what the stdio transport is. What the boundary forbids is the reverse: the
server shelling out to the CLI to answer a tool call. It does not; every handler
calls shared application processing in-process.

This module is a thin adapter, as the shared-boundary design requires: it parses
flags and terminates the Namespace, then calls
:func:`...application.mcp_server.serve_stdio`. No tool logic lives here.
"""

from __future__ import annotations

import argparse
import json as _json
import sys
from pathlib import Path


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    """Run a non-interactive stdio MCP session until stdin reaches EOF."""
    from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mcp_server import (  # noqa: E501
        CatalogSurfaceError,
        serve_stdio,
    )

    repo_raw = (getattr(args, "repo", None) or "").strip()
    repo_root = Path(repo_raw) if repo_raw else Path.cwd()
    try:
        return serve_stdio(repo_root=repo_root)
    except CatalogSurfaceError as exc:
        # Startup refusal. Reported on stderr so a client that spawned the server
        # sees why it exited without ever receiving a non-MCP byte on stdout.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


def cmd_mcp_tools(args: argparse.Namespace) -> int:
    """Print the published tool catalog (operator / debug view)."""
    from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_catalog import (  # noqa: E501
        catalog_surface_violations,
        list_tools_payload,
    )

    violations = catalog_surface_violations()
    if violations:
        for violation in violations:
            print(f"error: catalog surface violation: {violation}", file=sys.stderr)
        return 2
    tools = list_tools_payload()
    if getattr(args, "as_json", False):
        print(_json.dumps({"tools": tools}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    for tool in tools:
        annotations = tool.get("annotations", {})
        mode = "read-only" if annotations.get("readOnlyHint") else "MUTATING"
        print(f"{tool['name']}  [{mode}]")
        print(f"  {tool['title']}")
        required = tool.get("inputSchema", {}).get("required", [])
        print(f"  required arguments: {', '.join(required) if required else '(none)'}")
    return 0


def register(sub) -> None:
    """Register the ``mcp`` family on the top-level subparsers action."""
    mcp = sub.add_parser(
        "mcp",
        help="Local MCP server exposing the read/plan tools to an LLM client.",
        description=(
            "Local Model Context Protocol server. `serve` runs a non-interactive "
            "stdio session (the transport an MCP client spawns); `tools` prints "
            "the published read-only tool catalog. No tool published here mutates "
            "anything, delivers a handoff, or runs a command."
        ),
    )
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)

    serve = mcp_sub.add_parser(
        "serve",
        help="Run a stdio MCP session (reads JSON-RPC on stdin, writes on stdout).",
    )
    serve.add_argument(
        "--repo",
        default=None,
        help="Repo root the session reads. Defaults to the current directory.",
    )
    serve.set_defaults(func=cmd_mcp_serve)

    tools = mcp_sub.add_parser("tools", help="Print the published tool catalog.")
    tools.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=False,
        help="Emit the catalog as JSON.",
    )
    tools.set_defaults(func=cmd_mcp_tools)


__all__ = ("cmd_mcp_serve", "cmd_mcp_tools", "register")
