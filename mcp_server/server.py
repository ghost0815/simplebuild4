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
``--transport {stdio,sse,http}``
    Select the transport protocol:
      * ``stdio`` — default, for local LLM clients.
      * ``sse``  — legacy HTTP+SSE (two endpoints /sse + /messages).
      * ``http`` — Streamable HTTP (single /mcp endpoint, spec 2025-03-26).
``--http``
    Legacy alias for ``--transport sse``. Kept for backward compat.
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
    try:
        from mcp.types import Icon as _McpIcon
    except ImportError:  # older SDKs (< 1.27) don't expose Icon yet
        _McpIcon = None
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
    # Icon advertised in the initialize response (MCP spec 2025-06
    # Implementation.icons). claude.ai's connector card uses this —
    # it does NOT probe /favicon.ico on its own, and many client UIs
    # reject ``data:`` URLs for icons (CSP / same-origin rules).
    #
    # The icon's absolute URL is patched per-request by
    # streamable_http_transport's ``REQUEST_BASE_URL`` contextvar so
    # whatever hostname / scheme / root-path the client reached us at
    # is echoed back. Without a request context (stdio, initial setup),
    # ``None`` means the SDK omits the icons field entirely.
    server = Server(
        "simplebuild4",
        website_url="https://git.streamboard.tv/common/simplebuild4",
        icons=None,
    )

    # Replace the SDK's create_initialization_options so the icons list
    # is built fresh every time a session initialises — with the base
    # URL that applies to *this* request. Stdio runs have no request
    # context and the contextvar stays empty; in that case the SDK
    # omits the icons field.
    _REQUEST_BASE_URL = None
    if _McpIcon is not None:
        try:
            from mcp_server.streamable_http_transport import REQUEST_BASE_URL as _REQUEST_BASE_URL
        except Exception:
            _REQUEST_BASE_URL = None
    if _McpIcon is not None and _REQUEST_BASE_URL is not None:
        _orig_create = server.create_initialization_options

        def _create_init_options(
            notification_options=None, experimental_capabilities=None,
        ):
            opts = _orig_create(notification_options, experimental_capabilities)
            base = _REQUEST_BASE_URL.get("") if _REQUEST_BASE_URL else ""
            if base:
                try:
                    opts = opts.model_copy(update={
                        "icons": [
                            _McpIcon(
                                src=f"{base}/favicon.svg",
                                mimeType="image/svg+xml",
                                sizes=["any"],
                            ),
                        ],
                    })
                except Exception:
                    pass
            return opts

        server.create_initialization_options = _create_init_options  # type: ignore[method-assign]

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


def _serve_streamable_http(
    ctx: McpContext, host: str, port: int, root_path: str = "",
) -> int:
    """Streamable HTTP transport (delegated to streamable_http_transport)."""
    from mcp_server.streamable_http_transport import serve as serve_streamable
    return serve_streamable(ctx, host, port, root_path=root_path)


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
    p.add_argument("--transport", choices=("stdio", "sse", "http"), default=None,
                   help="Transport protocol: stdio (default), sse (legacy "
                        "HTTP+SSE), or http (Streamable HTTP, spec 2025-03-26)")
    p.add_argument("--http", action="store_true",
                   help="Legacy alias for --transport sse")
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

    # Resolve transport selection. --transport wins; --http is a legacy
    # alias for --transport sse. If both are set and disagree, error out.
    transport = args.transport
    if args.http:
        if transport and transport != "sse":
            sys.stderr.write(
                "ERROR: --http is an alias for '--transport sse' and conflicts "
                f"with --transport {transport}\n"
            )
            return 2
        transport = "sse"
    if not transport:
        transport = "stdio"

    try:
        if transport == "sse":
            return _serve_http(ctx, args.host, args.port, root_path=root_path)
        if transport == "http":
            return _serve_streamable_http(
                ctx, args.host, args.port, root_path=root_path,
            )
        asyncio.run(_serve_stdio(ctx))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
