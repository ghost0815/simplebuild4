"""HTTP transport for the s4 MCP server.

The MCP Python SDK ships an ASGI-compatible SSE transport
(``mcp.server.sse.SseServerTransport``). We wrap it in a minimal
Starlette application so we can enforce bearer-token auth before
any MCP traffic reaches the server and add a health-check endpoint.

Usage from the command line::

    ./s4 mcp --http --port 8765 --token SECRET

This spawns Starlette + uvicorn in-process. The server binds
``127.0.0.1`` by default — put it behind your existing Apache/Caddy
reverse proxy for TLS termination and public exposure.

Apache snippet::

    <Location /s4-mcp/>
        ProxyPass        http://127.0.0.1:8765/
        ProxyPassReverse http://127.0.0.1:8765/
    </Location>

Clients connect to ``https://your.host/s4-mcp/sse`` and POST
``https://your.host/s4-mcp/messages/?session_id=...`` with
``Authorization: Bearer SECRET`` on every request.
"""

from __future__ import annotations

import sys
from typing import Optional

from mcp_server.context import McpContext


def serve(ctx: McpContext, host: str, port: int) -> int:
    """Block serving HTTP+SSE until interrupted. Returns exit code."""
    try:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Mount, Route
        from mcp.server.sse import SseServerTransport
    except ImportError as exc:
        sys.stderr.write(
            "HTTP transport needs 'starlette', 'uvicorn' and 'mcp[sse]'.\n"
            "Install with: pip install --user starlette uvicorn mcp\n"
            f"(import error: {exc})\n"
        )
        return 2

    # Local import to avoid pulling MCP into stdio-only runs.
    from mcp_server.server import _make_server

    server = _make_server(ctx)
    sse = SseServerTransport("/messages/")

    def _unauthorized() -> Response:
        return Response(
            status_code=401,
            content="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )

    def _authed(request: Request) -> bool:
        if not ctx.http_token:
            return True
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return False
        return auth.split(" ", 1)[1].strip() == ctx.http_token

    async def handle_sse(request: Request):
        if not _authed(request):
            return _unauthorized()
        async with sse.connect_sse(
            request.scope, request.receive, request._send,
        ) as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options(),
            )

    async def handle_health(request: Request):
        return JSONResponse({
            "status": "ok",
            "server": "simplebuild4",
            "version": "0.1.0",
            "allow_actions": ctx.allow_actions,
            "auth": bool(ctx.http_token),
        })

    # Auth-gate the POST /messages/ endpoint too. SseServerTransport
    # exposes it as an ASGI app — wrap it.
    async def guarded_messages(scope, receive, send):
        if scope["type"] != "http":
            return await sse.handle_post_message(scope, receive, send)
        # Manual header check since we can't build a Request cheaply here
        token_ok = not ctx.http_token
        if ctx.http_token:
            for name, value in scope.get("headers", []):
                if name == b"authorization":
                    v = value.decode("latin-1", errors="ignore").strip()
                    if v.lower().startswith("bearer "):
                        token_ok = v.split(" ", 1)[1].strip() == ctx.http_token
                    break
        if not token_ok:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await sse.handle_post_message(scope, receive, send)

    app = Starlette(routes=[
        Route("/health", endpoint=handle_health, methods=["GET"]),
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=guarded_messages),
    ])

    use_tls = bool(ctx.ssl_certfile and ctx.ssl_keyfile)
    proto = "https" if use_tls else "http"
    sys.stderr.write(
        f"s4-mcp {proto.upper()} server listening on {host}:{port} "
        f"(auth: {'on' if ctx.http_token else 'OFF \u2014 LOCALHOST ONLY!'}, "
        f"tls: {'on' if use_tls else 'off'})\n"
    )

    kwargs = dict(host=host, port=port, log_level="warning", access_log=False)
    if use_tls:
        kwargs["ssl_certfile"] = ctx.ssl_certfile
        kwargs["ssl_keyfile"] = ctx.ssl_keyfile

    uvicorn.run(app, **kwargs)
    return 0
