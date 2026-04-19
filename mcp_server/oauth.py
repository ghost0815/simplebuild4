"""Minimal OAuth 2.1 wrapper around the static bearer token.

Just enough OAuth for clients that insist on the MCP authorization
flow (claude.ai's custom-connector UI is the main target). The
implementation is deliberately narrow:

* No user database — anyone who knows ``settings.mcp_token`` can
  complete the flow. The bearer token IS the credential.
* No refresh tokens, no token revocation, no consent screen —
  the ``access_token`` returned by ``/token`` IS the bearer token.
* Dynamic Client Registration accepts anything; the ``client_id``
  is never checked again. Security comes from PKCE + the one-time
  authorization code + the bearer-token challenge at ``/authorize``.
* PKCE S256 is enforced when the client sends a ``code_challenge``.

Architecturally this is a "login proxy" over the existing bearer
token: claude.ai's browser flow ultimately delivers the same bearer
header that Claude Desktop / Code would send directly. Servers
behind a reverse proxy need no extra config — all routes live
under the same ``/s4-mcp/`` prefix.
"""

from __future__ import annotations

import base64
import hashlib
import html as _html
import json
import secrets
import time
import urllib.parse
from typing import Any

from mcp_server.context import McpContext


# Authorization codes expire quickly. claude.ai's browser round-trip
# from /authorize back to its own callback and on to /token takes
# seconds; 120s is conservative.
_CODE_TTL = 120


class CodeStore:
    """In-memory auth-code store. One-time-use with TTL."""

    def __init__(self) -> None:
        self._codes: dict[str, tuple[float, dict[str, Any]]] = {}

    def put(self, code: str, data: dict[str, Any]) -> None:
        self._codes[code] = (time.time(), data)

    def pop(self, code: str) -> dict[str, Any] | None:
        self._expire()
        entry = self._codes.pop(code, None)
        return entry[1] if entry else None

    def _expire(self) -> None:
        now = time.time()
        dead = [c for c, (t, _) in self._codes.items() if now - t > _CODE_TTL]
        for c in dead:
            del self._codes[c]


# ---------------------------------------------------------------- helpers


def _base_url_from_scope(scope: dict[str, Any]) -> str:
    """Compute the public base URL the client sees.

    Honors X-Forwarded-Proto (proxy TLS termination) and the ASGI
    ``root_path`` (populated by the ForwardedPrefixMiddleware from
    X-Forwarded-Prefix or an explicit --root-path).
    """
    headers = {
        k.decode("latin-1", errors="ignore").lower():
        v.decode("latin-1", errors="ignore")
        for k, v in scope.get("headers", [])
    }
    # Behind a reverse proxy the Host header carries the *backend*'s
    # address (e.g. 127.0.0.1:8181). The proxy tells us the public
    # host/proto via X-Forwarded-* — prefer those when present. Take
    # only the first comma-separated value in case the proxy chain
    # appended (RFC 7239 style).
    def _first(v: str) -> str:
        return v.split(",", 1)[0].strip()
    host = (
        _first(headers.get("x-forwarded-host", ""))
        or headers.get("host", "localhost")
    )
    proto = _first(headers.get("x-forwarded-proto", ""))
    if not proto:
        proto = "https" if scope.get("scheme") == "https" else "http"
    rp = scope.get("root_path", "") or ""
    if rp and not rp.startswith("/"):
        rp = "/" + rp
    rp = rp.rstrip("/")
    return f"{proto}://{host}{rp}"


async def _send_json(send, obj: Any, status: int = 200,
                     extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(obj).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _send_html(send, body_str: str, status: int = 200) -> None:
    body = body_str.encode("utf-8")
    headers = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive) -> bytes:
    parts: list[bytes] = []
    more = True
    while more:
        msg = await receive()
        if msg["type"] == "http.request":
            parts.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        else:
            break
    return b"".join(parts)


def _parse_form(body: bytes) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(body.decode("utf-8", errors="replace")))


# ---------------------------------------------------------------- metadata


async def handle_protected_resource(scope, receive, send, ctx: McpContext) -> None:
    """RFC 9728 — protected resource metadata."""
    base = _base_url_from_scope(scope)
    await _send_json(send, {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    })


async def handle_authorization_server(scope, receive, send, ctx: McpContext) -> None:
    """RFC 8414 — authorization server metadata."""
    base = _base_url_from_scope(scope)
    await _send_json(send, {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none", "client_secret_basic", "client_secret_post",
        ],
        "scopes_supported": ["mcp"],
    })


# ---------------------------------------------------------------- DCR


async def handle_register(scope, receive, send, ctx: McpContext) -> None:
    """RFC 7591 — Dynamic Client Registration.

    Accepts any request and returns a pseudo-client. Actual auth
    happens at /authorize (bearer-token challenge) + PKCE at /token.
    """
    raw = await _read_body(receive)
    try:
        req = json.loads(raw or b"{}")
    except Exception:
        req = {}
    client_id = f"s4mcp-{secrets.token_urlsafe(9)}"
    await _send_json(send, {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": req.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "web",
    }, status=201)


# ---------------------------------------------------------------- authorize


_AUTHORIZE_FORM = """<!doctype html>
<html><head><meta charset="utf-8">
<title>s4-mcp authorize</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 480px;
       margin: 4em auto; padding: 1em; color: #222; }}
h2 {{ margin-top: 0; }}
label {{ display: block; margin: .5em 0 .25em; }}
input[type=password] {{ width: 100%; padding: .5em;
                        font-size: 1em; box-sizing: border-box; }}
button {{ margin-top: 1em; padding: .5em 1em; font-size: 1em;
         background: #2a66c7; color: white; border: 0;
         border-radius: 4px; cursor: pointer; }}
button:hover {{ background: #1f4e9f; }}
.muted {{ color: #666; font-size: .9em; }}
.err {{ color: #b00; margin: .5em 0; }}
</style></head>
<body>
<h2>simplebuild4 MCP &mdash; authorize</h2>
<p>Grant <b>{client}</b> access to this s4-mcp server by pasting the
server&rsquo;s bearer token below. The token is the one configured
in F3 Settings &rarr; MCP Service &rarr; Bearer token.</p>
{error}
<form method="post" action="">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
  <input type="hidden" name="client_id" value="{client_id}">
  <label for="token">Bearer token</label>
  <input id="token" type="password" name="token" autofocus required
         autocomplete="off">
  <button type="submit">Authorize</button>
</form>
<p class="muted">Tip: the token never leaves this server. It is
  matched locally against the configured value; if it matches,
  a one-time authorization code is issued.</p>
</body></html>
"""


def _render_authorize_form(params: dict[str, str], error: str = "") -> str:
    def esc(v: str) -> str:
        return _html.escape(v or "", quote=True)
    err_block = (f'<p class="err">{_html.escape(error)}</p>'
                 if error else "")
    client = esc(params.get("client_id") or "client")
    return _AUTHORIZE_FORM.format(
        client=client,
        error=err_block,
        redirect_uri=esc(params.get("redirect_uri", "")),
        state=esc(params.get("state", "")),
        code_challenge=esc(params.get("code_challenge", "")),
        code_challenge_method=esc(params.get("code_challenge_method", "")),
        client_id=esc(params.get("client_id", "")),
    )


async def handle_authorize_get(scope, receive, send, ctx: McpContext) -> None:
    qs = dict(urllib.parse.parse_qsl(scope.get("query_string", b"").decode()))
    # response_type=code is the only thing we accept — if the client
    # asks for anything else, fail early.
    rt = qs.get("response_type", "code")
    if rt != "code":
        await _send_html(send,
            f"<h3>unsupported response_type: {_html.escape(rt)}</h3>",
            status=400)
        return
    cm = qs.get("code_challenge_method", "")
    if cm and cm != "S256":
        await _send_html(send,
            f"<h3>unsupported code_challenge_method: {_html.escape(cm)}</h3>"
            "<p>Only S256 is supported.</p>", status=400)
        return
    await _send_html(send, _render_authorize_form(qs))


async def handle_authorize_post(
    scope, receive, send, ctx: McpContext, code_store: CodeStore,
) -> None:
    form = _parse_form(await _read_body(receive))
    token = form.get("token", "").strip()
    redirect_uri = form.get("redirect_uri", "").strip()
    state = form.get("state", "")
    code_challenge = form.get("code_challenge", "")
    code_challenge_method = form.get("code_challenge_method", "")

    # Reject if the server wasn't configured with a token at all —
    # allowing free access via OAuth when the bearer mode is open
    # would be a privilege upgrade, not a fallback.
    if not ctx.http_token:
        await _send_html(send,
            "<h3>Server has no bearer token configured.</h3>"
            "<p>OAuth authorize requires a bearer token; set one in "
            "F3 Settings &rarr; MCP Service.</p>", status=400)
        return
    if not secrets.compare_digest(token, ctx.http_token):
        await _send_html(send, _render_authorize_form(form, error="Invalid token"),
                         status=401)
        return
    if not redirect_uri:
        await _send_html(send, "<h3>Missing redirect_uri</h3>", status=400)
        return
    if code_challenge_method and code_challenge_method != "S256":
        await _send_html(send, "<h3>Only S256 PKCE supported</h3>", status=400)
        return

    code = secrets.token_urlsafe(32)
    code_store.put(code, {
        "access_token": ctx.http_token,
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
    })
    # 302 back to the caller's redirect_uri with ?code=… (&state=…)
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={urllib.parse.quote(code)}"
    if state:
        location += f"&state={urllib.parse.quote(state)}"
    await send({
        "type": "http.response.start",
        "status": 302,
        "headers": [(b"location", location.encode("latin-1"))],
    })
    await send({"type": "http.response.body", "body": b""})


# ---------------------------------------------------------------- token


async def handle_token(
    scope, receive, send, ctx: McpContext, code_store: CodeStore,
) -> None:
    form = _parse_form(await _read_body(receive))
    grant_type = form.get("grant_type", "")
    code = form.get("code", "")
    code_verifier = form.get("code_verifier", "")
    redirect_uri = form.get("redirect_uri", "")

    if grant_type != "authorization_code":
        await _send_json(send,
            {"error": "unsupported_grant_type",
             "error_description": f"grant_type must be authorization_code"},
            status=400)
        return
    data = code_store.pop(code)
    if not data:
        await _send_json(send,
            {"error": "invalid_grant",
             "error_description": "unknown or expired code"}, status=400)
        return
    if redirect_uri and data["redirect_uri"] != redirect_uri:
        await _send_json(send,
            {"error": "invalid_grant",
             "error_description": "redirect_uri mismatch"}, status=400)
        return
    # PKCE verification — required when the authorize step carried a challenge
    if data["code_challenge"]:
        if not code_verifier:
            await _send_json(send,
                {"error": "invalid_request",
                 "error_description": "code_verifier required"},
                status=400)
            return
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if not secrets.compare_digest(expected, data["code_challenge"]):
            await _send_json(send,
                {"error": "invalid_grant",
                 "error_description": "PKCE verification failed"},
                status=400)
            return
    await _send_json(send, {
        "access_token": data["access_token"],
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "mcp",
    })


# -------------------------------------------------------- 401 helper


def www_authenticate_header(scope) -> tuple[bytes, bytes]:
    """WWW-Authenticate value for unauthorized /mcp requests.

    Points the client at the resource-metadata endpoint so it can
    discover our authorization server and start an OAuth flow.

    Format mirrors the RFC 9728 example exactly — no realm, no
    ``error`` param — some MCP clients (incl. claude.ai's custom-
    connector backend) parse this strictly.
    """
    base = _base_url_from_scope(scope)
    value = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
    return (b"www-authenticate", value.encode("latin-1"))


def protected_resource_url(scope) -> str:
    """Absolute URL of the protected-resource metadata endpoint."""
    return f"{_base_url_from_scope(scope)}/.well-known/oauth-protected-resource"
