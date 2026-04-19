"""MCP server entry point.

Usage::

    python -m mcp_server [--allow-actions] [--allow-dangerous]
    python -m mcp_server --http --host 127.0.0.1 --port 8765 --token SECRET

Flags
-----
``--allow-actions``
    Enable tools that start background jobs (build_target, run_automation_job).
``--allow-dangerous``
    Reserved for future destructive tools (update s4, write settings).
``--http``
    Switch transport from stdio to HTTP+SSE.
``--host`` / ``--port``
    Bind address for HTTP. Defaults to 127.0.0.1:8765 (bind local only,
    use a reverse proxy for external exposure).
``--token``
    Bearer token required on the ``Authorization`` header. Omitting it
    on public HTTP is a bad idea.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# Make sure ``core.*`` imports work when this module is launched as
# ``python -m mcp_server`` from the project root.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from mcp.server import Server
    from mcp.types import TextContent, Tool
    from mcp.server.stdio import stdio_server
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "The 'mcp' Python package is required.\n"
        "Install with: pip install --user mcp\n"
        f"(import error: {exc})\n"
    )
    sys.exit(2)

from mcp_server.context import McpContext
from mcp_server.tools import (
    targets as tools_targets,
    toolchains as tools_toolchains,
    libraries as tools_libraries,
    logs as tools_logs,
    settings as tools_settings,
    automation as tools_automation,
    repositories as tools_repositories,
    wiki as tools_wiki,
)


# ------------------------------------------------------------------ routing


_MODULES = (
    tools_targets,
    tools_repositories,
    tools_toolchains,
    tools_libraries,
    tools_logs,
    tools_settings,
    tools_automation,
    tools_wiki,
)


def _build_router(ctx: McpContext) -> tuple[list[Tool], dict[str, Any]]:
    """Collect all tool definitions and build the name → module dispatch map."""
    all_tools: list[Tool] = []
    dispatch_map: dict[str, Any] = {}
    for mod in _MODULES:
        mod_tools = mod.register(server=None, ctx=ctx)  # type: ignore[arg-type]
        all_tools.extend(mod_tools)
        for tool in mod_tools:
            dispatch_map[tool.name] = mod.dispatch
    return all_tools, dispatch_map


def _make_server(ctx: McpContext) -> Server:
    """Create and configure the MCP Server with all s4 tools wired up."""
    tools, dispatch_map = _build_router(ctx)
    server = Server("simplebuild4")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            handler = dispatch_map.get(name)
            if not handler:
                raise KeyError(f"Unknown tool: {name}")
            # Run the (synchronous) tool in a thread so we don't block
            # the asyncio event loop on disk I/O or long YAML parses.
            result = await asyncio.to_thread(handler, ctx, name, arguments or {})
            text = json.dumps(result, indent=2, default=str, ensure_ascii=False)
            return [TextContent(type="text", text=text)]
        except PermissionError as exc:
            return [TextContent(type="text", text=f"Permission denied: {exc}")]
        except (ValueError, KeyError) as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]
        except Exception as exc:  # pragma: no cover
            return [TextContent(
                type="text",
                text=f"Internal error calling {name}: {type(exc).__name__}: {exc}",
            )]

    return server


# ------------------------------------------------------------------ transports


async def _serve_stdio(ctx: McpContext) -> None:
    server = _make_server(ctx)
    init_options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def _serve_http(ctx: McpContext, host: str, port: int, root_path: str = "") -> int:
    """HTTP+SSE transport (delegated to mcp_server.http_transport)."""
    from mcp_server.http_transport import serve as serve_http
    return serve_http(ctx, host, port, root_path=root_path)


# ------------------------------------------------------------------ CLI


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="s4-mcp",
        description="SimpleBuild4 Model Context Protocol server",
    )
    p.add_argument("--allow-actions", action="store_true",
                   help="Enable tools that start background jobs (build_target, ...)")
    p.add_argument("--allow-dangerous", action="store_true",
                   help="Reserved for future destructive tools (writes settings, updates s4)")
    p.add_argument("--http", action="store_true",
                   help="Serve HTTP+SSE instead of stdio")
    p.add_argument("--host", default="127.0.0.1",
                   help="HTTP bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8181,
                   help="HTTP port (default: 8765)")
    p.add_argument("--token", default="",
                   help="Bearer token required on HTTP Authorization header")
    p.add_argument("--ssl-cert", default="",
                   help="Path to SSL certificate (HTTPS)")
    p.add_argument("--ssl-key", default="",
                   help="Path to SSL private key (HTTPS)")
    p.add_argument("--root-path", default="",
                   help=(
                       "ASGI root_path prefix when behind a reverse proxy "
                       "(e.g. '/s4-mcp' if Apache terminates at "
                       "https://host/s4-mcp/). The SSE transport uses this "
                       "to tell the client the correct POST endpoint."
                   ))
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    token = (args.token or os.environ.get("S4_MCP_TOKEN", "")).strip() or None
    ssl_cert = (args.ssl_cert or os.environ.get("S4_MCP_SSL_CERT", "")).strip() or None
    ssl_key = (args.ssl_key or os.environ.get("S4_MCP_SSL_KEY", "")).strip() or None
    root_path = (args.root_path or os.environ.get("S4_MCP_ROOT_PATH", "")).strip()
    # Normalise: must start with '/', must not end with '/'
    if root_path and not root_path.startswith("/"):
        root_path = "/" + root_path
    if root_path.endswith("/"):
        root_path = root_path.rstrip("/")
    # Both must be set to enable TLS; a half-config is rejected early
    if bool(ssl_cert) != bool(ssl_key):
        sys.stderr.write(
            "ERROR: --ssl-cert and --ssl-key must both be set (or both omitted)\n"
        )
        return 2

    ctx = McpContext.build(
        allow_actions=args.allow_actions,
        allow_dangerous=args.allow_dangerous,
        http_token=token,
        ssl_certfile=ssl_cert,
        ssl_keyfile=ssl_key,
    )

    try:
        if args.http:
            return _serve_http(ctx, args.host, args.port, root_path=root_path)
        asyncio.run(_serve_stdio(ctx))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
