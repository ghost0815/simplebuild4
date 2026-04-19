"""Streamable HTTP transport for the s4 MCP server.

The MCP spec defines two HTTP-based transports:

  * **HTTP+SSE** (legacy, spec rev 2024-11-05) — two endpoints:
    ``GET /sse`` opens an event stream and announces the POST-endpoint
    ``POST /messages/?session_id=…`` that the client uses to push
    JSON-RPC requests. Implemented in ``http_transport.py``.

  * **Streamable HTTP** (current, spec rev 2025-03-26) — a single
    endpoint ``POST /mcp`` that either returns a JSON response directly
    or upgrades to ``text/event-stream`` for streaming. ``GET /mcp``
    can optionally open a server-initiated stream. Session state is
    tracked via the ``Mcp-Session-Id`` header. Implemented here.

Usage from the command line::

    ./s4 mcp --transport http --port 8765 --token SECRET

Clients connect to ``https://your.host/s4-mcp/mcp`` with
``Authorization: Bearer SECRET``. The Apache reverse-proxy snippet
from ``http_transport.py`` works unchanged — just point the client at
``/mcp`` instead of ``/sse``.
"""

from __future__ import annotations

import sys

from mcp_server.context import McpContext


def serve(ctx: McpContext, host: str, port: int, root_path: str = "") -> int:
    """Block serving Streamable HTTP until interrupted. Returns exit code.

    ``root_path`` is the ASGI root-path prefix (see http_transport.serve
    for details; behavior is identical here).
    """
    try:
        import json as _json
        import uvicorn
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp_server import oauth as _oauth
    except ImportError as exc:
        sys.stderr.write(
            "Streamable HTTP transport needs 'starlette', 'uvicorn' and a\n"
            "recent 'mcp' package (>= 1.1 with streamable_http support).\n"
            "Install with: pip install --user 'starlette' 'uvicorn' 'mcp'\n"
            f"(import error: {exc})\n"
        )
        return 2

    # Local import to avoid pulling MCP into stdio-only runs.
    from mcp_server.server import _make_server

    server = _make_server(ctx)

    # Stateful session manager. ``json_response=False`` lets the manager
    # pick SSE vs. plain JSON per request based on the client's Accept
    # header — the default and most compatible mode.
    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,      # no resumability; set an EventStore for that
        json_response=False,
        stateless=False,
    )

    def _token_ok_from_headers(headers) -> bool:
        if not ctx.http_token:
            return True
        for name, value in headers or ():
            if name == b"authorization":
                v = value.decode("latin-1", errors="ignore").strip()
                if v.lower().startswith("bearer "):
                    return v.split(" ", 1)[1].strip() == ctx.http_token
                return False
        return False

    async def handle_mcp(scope, receive, send):
        # Auth gate. Streamable HTTP uses GET, POST and DELETE on the
        # same endpoint — check them all.
        if scope["type"] == "http" and ctx.http_token:
            if not _token_ok_from_headers(scope.get("headers", [])):
                # Pointed WWW-Authenticate so MCP clients (claude.ai's
                # custom-connector, mcp-remote, …) can discover our
                # OAuth metadata (resource + AS).
                wa = _oauth.www_authenticate_header(scope)
                import json as __json
                body_json = __json.dumps({
                    "error": "invalid_token",
                    "error_description": "Missing or invalid bearer token.",
                    "resource_metadata": _oauth.protected_resource_url(scope),
                }).encode()
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body_json)).encode()),
                        wa,
                        # Expose WWW-Authenticate to browser JS so the
                        # claude.ai custom-connector flow can read the
                        # resource_metadata hint.
                        (b"access-control-expose-headers",
                         b"WWW-Authenticate, Mcp-Session-Id"),
                    ],
                })
                await send({"type": "http.response.body", "body": body_json})
                return
        await session_manager.handle_request(scope, receive, send)

    async def handle_health(scope, receive, send):
        body = _json.dumps({
            "status": "ok",
            "server": "simplebuild4",
            "version": "0.1.0",
            "transport": "streamable-http",
            "allow_actions": ctx.allow_actions,
            "auth": bool(ctx.http_token),
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def handle_not_found(scope, receive, send):
        body = b"not found"
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": body})

    # --- ASGI middleware: CORS -------------------------------------------
    # claude.ai is a browser-side app and its custom-connector flow runs
    # cross-origin against this server. Without CORS the preflight on
    # /mcp, /authorize, /register, /token fails before any real request
    # is made — the UI surfaces this as "Couldn't reach the MCP server".
    #
    # We reflect the Origin header back (credentials-compatible) and
    # allow the MCP-related headers. Requests without Origin (server-
    # side curl, Claude Desktop via mcp-remote, …) are unaffected.
    _CORS_ALLOW_HEADERS = (
        b"authorization, content-type, mcp-session-id, mcp-protocol-version, "
        b"last-event-id, x-requested-with"
    )
    _CORS_EXPOSE_HEADERS = b"mcp-session-id, www-authenticate"

    def _origin_from_scope(scope) -> bytes | None:
        for name, value in scope.get("headers", ()):
            if name == b"origin":
                return value
        return None

    class CorsMiddleware:
        def __init__(self, inner):
            self._inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self._inner(scope, receive, send)
            origin = _origin_from_scope(scope)
            # CORS preflight — answer directly, don't dispatch.
            if scope.get("method") == "OPTIONS" and origin is not None:
                req_headers = b""
                for name, value in scope.get("headers", ()):
                    if name == b"access-control-request-headers":
                        req_headers = value
                        break
                allow_headers = req_headers or _CORS_ALLOW_HEADERS
                await send({
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        (b"access-control-allow-origin", origin),
                        (b"access-control-allow-methods",
                         b"GET, POST, DELETE, OPTIONS"),
                        (b"access-control-allow-headers", allow_headers),
                        (b"access-control-allow-credentials", b"true"),
                        (b"access-control-max-age", b"86400"),
                        (b"vary", b"Origin"),
                        (b"content-length", b"0"),
                    ],
                })
                await send({"type": "http.response.body", "body": b""})
                return
            if origin is None:
                return await self._inner(scope, receive, send)
            # Wrap ``send`` so every response gains CORS headers.
            async def _send(event):
                if event["type"] == "http.response.start":
                    headers = list(event.get("headers") or [])
                    # Avoid duplicating headers the handler already set.
                    have = {h[0] for h in headers}
                    if b"access-control-allow-origin" not in have:
                        headers.append(
                            (b"access-control-allow-origin", origin))
                    if b"access-control-allow-credentials" not in have:
                        headers.append(
                            (b"access-control-allow-credentials", b"true"))
                    if b"access-control-expose-headers" not in have:
                        headers.append(
                            (b"access-control-expose-headers",
                             _CORS_EXPOSE_HEADERS))
                    if b"vary" not in have:
                        headers.append((b"vary", b"Origin"))
                    event = {**event, "headers": headers}
                await send(event)
            await self._inner(scope, receive, _send)

    # --- ASGI middleware: honor X-Forwarded-Prefix -----------------------
    # Mirrors http_transport.py — see there for rationale.
    explicit_root_path = (root_path or "").strip()

    class ForwardedPrefixMiddleware:
        def __init__(self, inner):
            self._inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and not explicit_root_path:
                for name, value in scope.get("headers", ()):
                    if name == b"x-forwarded-prefix":
                        prefix = value.decode("latin-1", errors="ignore").strip()
                        if prefix and prefix != "/":
                            prefix = "/" + prefix.strip("/")
                            scope = dict(scope)
                            scope["root_path"] = prefix
                        break
            return await self._inner(scope, receive, send)

    # One-time-use auth-code store shared across OAuth endpoints.
    _code_store = _oauth.CodeStore()

    # Minimal ASGI router. We *intentionally* don't use Starlette's Mount
    # here because Mount always 307-redirects bare "/mcp" to "/mcp/" —
    # most HTTP clients then retry the redirect as GET, losing the
    # original POST body and breaking MCP's ``initialize``. Writing a
    # 30-line dispatcher is simpler than disabling the redirect.
    async def router(scope, receive, send):
        if scope["type"] == "lifespan":
            # Drive the session manager's task group via lifespan.
            async with session_manager.run():
                while True:
                    msg = await receive()
                    if msg["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif msg["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            return
        if scope["type"] != "http":
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        # Normalize trailing slash so /mcp and /mcp/ both route to the
        # streamable handler; strip one trailing '/' for comparison only.
        norm = path.rstrip("/") or "/"
        if norm == "/health" and method == "GET":
            await handle_health(scope, receive, send)
            return
        if norm == "/mcp":
            await handle_mcp(scope, receive, send)
            return
        # --- OAuth 2.1 wrapper over the static bearer token (for
        # clients that insist on the MCP authorize flow, e.g.
        # claude.ai's custom connector UI) ---------------------------
        # RFC 9728 allows the "path-suffixed" form where the resource's
        # path is appended to the well-known URI (e.g.
        # /.well-known/oauth-protected-resource/s4-mcp/mcp for resource
        # /s4-mcp/mcp). Clients also probe the bare form. We accept
        # any path starting with either well-known prefix so both
        # reach the same metadata handler.
        if method == "GET" and norm.startswith("/.well-known/oauth-protected-resource"):
            await _oauth.handle_protected_resource(scope, receive, send, ctx)
            return
        if method == "GET" and norm.startswith("/.well-known/oauth-authorization-server"):
            await _oauth.handle_authorization_server(scope, receive, send, ctx)
            return
        if norm == "/register" and method == "POST":
            await _oauth.handle_register(scope, receive, send, ctx)
            return
        if norm == "/authorize":
            if method == "GET":
                await _oauth.handle_authorize_get(scope, receive, send, ctx)
                return
            if method == "POST":
                await _oauth.handle_authorize_post(scope, receive, send, ctx, _code_store)
                return
        if norm == "/token" and method == "POST":
            await _oauth.handle_token(scope, receive, send, ctx, _code_store)
            return
        await handle_not_found(scope, receive, send)

    app = ForwardedPrefixMiddleware(CorsMiddleware(router))

    use_tls = bool(ctx.ssl_certfile and ctx.ssl_keyfile)
    proto = "https" if use_tls else "http"
    root_info = f", root_path: {root_path}" if root_path else ""
    sys.stderr.write(
        f"s4-mcp Streamable HTTP server listening on {host}:{port} "
        f"(endpoint: {proto}://{host}:{port}{root_path}/mcp, "
        f"auth: {'on' if ctx.http_token else 'OFF \u2014 LOCALHOST ONLY!'}, "
        f"tls: {'on' if use_tls else 'off'}{root_info})\n"
    )

    kwargs = dict(host=host, port=port, log_level="warning", access_log=False)
    if use_tls:
        kwargs["ssl_certfile"] = ctx.ssl_certfile
        kwargs["ssl_keyfile"] = ctx.ssl_keyfile
    if root_path:
        kwargs["root_path"] = root_path

    uvicorn.run(app, **kwargs)
    return 0
