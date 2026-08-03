"""S4Server — textual-serve with SSL and cookie-based auth support."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import signal
import ssl
import time

import aiohttp
import aiohttp_jinja2
from aiohttp import web
from textual_serve.server import Server

# Term-token (F6 menuconfig-in-browser) — short-lived signed URL
_TERM_TTL = 60                        # seconds a term URL is valid
_TERM_CMD_WHITELIST = ("ctng-menuconfig",)
_TTYD_INTERNAL = "http://127.0.0.1:7681"

# Session cookie valid for 24 hours
_SESSION_MAX_AGE = 86400
_COOKIE_NAME = "s4_session"


def _sign_token(user: str, secret: bytes) -> str:
    """Create a signed session token: timestamp:user:signature."""
    ts = str(int(time.time()))
    msg = f"{ts}:{user}".encode()
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return f"{ts}:{user}:{sig}"


def _verify_token(token: str, secret: bytes) -> bool:
    """Verify a signed session token is valid and not expired."""
    try:
        ts_str, user, sig = token.rsplit(":", 2)
        ts = int(ts_str)
        if time.time() - ts > _SESSION_MAX_AGE:
            return False
        expected = hmac.new(secret, f"{ts_str}:{user}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


def _resolve_theme_colors(theme_name: str) -> dict[str, str]:
    """Resolve a Textual theme name to CSS color values."""
    try:
        from textual.theme import BUILTIN_THEMES
        t = BUILTIN_THEMES.get(theme_name)
        if not t:
            t = BUILTIN_THEMES.get("textual-dark")
    except Exception:
        t = None
    if t is None:
        return dict(bg="#121212", surface="#1e1e1e", panel="#252525",
                    fg="#e0e0e0", fg_dim="rgba(255,255,255,0.4)",
                    primary="#0178D4", accent="#ffa62b", error_color="#ba3c5b")
    dark = t.dark
    return dict(
        bg=t.background or ("#121212" if dark else "#f5f5f5"),
        surface=t.surface or ("#1e1e1e" if dark else "#e0e0e0"),
        panel=t.panel or ("#252525" if dark else "#d0d0d0"),
        fg=t.foreground or ("#e0e0e0" if dark else "#1e1e1e"),
        fg_dim="rgba(255,255,255,0.4)" if dark else "rgba(0,0,0,0.4)",
        primary=t.primary or "#0178D4",
        accent=t.accent or "#ffa62b",
        error_color=t.error or "#ba3c5b",
    )


class S4Server(Server):
    """Extends textual-serve Server with HTTPS and form-based auth."""

    def __init__(
        self,
        command: str,
        host: str = "localhost",
        port: int = 8000,
        title: str | None = None,
        public_url: str | None = None,
        statics_path: str = "./static",
        templates_path: str = "./templates",
        ssl_certfile: str = "",
        ssl_keyfile: str = "",
        auth_user: str = "",
        auth_password: str = "",
        theme: str = "textual-dark",
        language: str = "en",
    ) -> None:
        super().__init__(
            command=command, host=host, port=port, title=title,
            public_url=public_url, statics_path=statics_path,
            templates_path=templates_path,
        )
        self._ssl_certfile = ssl_certfile
        self._ssl_keyfile = ssl_keyfile
        self._auth_user = auth_user
        self._auth_password = auth_password
        self._cookie_secret = os.urandom(32)
        self._theme_colors = _resolve_theme_colors(theme)
        # Load translations for template rendering — F6 web-modal + term.html
        # both use {{ t(...) }}. Without an explicit load(), t() would return
        # keys verbatim (no active translation dict).
        try:
            from translation import load as _load_translations
            _load_translations(language)
        except Exception:
            pass
        # Kill-first policy for menuconfig-in-browser: newest F6 revokes
        # the user's previous term-session. Key = user, value = sid.
        self._active_term_sids: dict[str, str] = {}

    def _make_ssl_context(self) -> ssl.SSLContext | None:
        if not self._ssl_certfile or not self._ssl_keyfile:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self._ssl_certfile, self._ssl_keyfile)
        return ctx


    @staticmethod
    def _fwd_prefix(request):
        """Public URL prefix set by a reverse proxy (X-Forwarded-Prefix).
        Returns "" when absent so string-concat stays a no-op."""
        p = request.headers.get("X-Forwarded-Prefix", "").rstrip("/")
        if p and not p.startswith("/"):
            p = "/" + p
        return p

    async def _handle_login_get(self, request: web.Request) -> web.Response:
        """Show login form."""
        return aiohttp_jinja2.render_template(
            "login.html", request,
            {"error": "", "prefix": self._fwd_prefix(request), **self._theme_colors},
        )

    async def _handle_login_post(self, request: web.Request) -> web.Response:
        """Validate credentials and set session cookie."""
        data = await request.post()
        user = data.get("username", "")
        password = data.get("password", "")
        if user == self._auth_user and password == self._auth_password:
            # Redirect to index with session cookie
            prefix = self._fwd_prefix(request)
            target = request.query.get("next", "/")
            if prefix and not target.startswith(prefix):
                target = f"{prefix}{target}" if target.startswith("/") else f"{prefix}/{target}"
            response = web.HTTPFound(target)
            token = _sign_token(user, self._cookie_secret)
            response.set_cookie(
                _COOKIE_NAME, token,
                max_age=_SESSION_MAX_AGE,
                httponly=True,
                secure=bool(self._ssl_certfile),
                samesite="Lax",
            )
            return response
        response = aiohttp_jinja2.render_template(
            "login.html", request,
            {"error": "Invalid username or password",
             "prefix": self._fwd_prefix(request), **self._theme_colors},
        )
        response.set_status(401)
        return response

    async def _handle_logout(self, request: web.Request) -> web.Response:
        """Clear session cookie and redirect to login."""
        prefix = self._fwd_prefix(request)
        response = web.HTTPFound(f"{prefix}/login")
        response.del_cookie(_COOKIE_NAME)
        return response

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        """Cookie-based session auth with login form redirect."""
        path = request.path
        # Login/logout and static assets always pass through
        if path in ("/login", "/logout") or path.startswith("/static"):
            return await handler(request)

        # Check session cookie
        cookie = request.cookies.get(_COOKIE_NAME, "")
        if cookie and _verify_token(cookie, self._cookie_secret):
            return await handler(request)

        # Redirect to login form (proxy-aware)
        prefix = self._fwd_prefix(request)
        return web.HTTPFound(f"{prefix}/login?next={prefix}{path}")

    @aiohttp_jinja2.template("app_index.html")
    async def handle_index(self, request: web.Request) -> dict:
        """Override to derive URLs from the request's Host header.

        textual-serve hardcodes public_url into WebSocket and static URLs.
        This fails with port forwarding (WSL), reverse proxies, or when
        accessing via different hostnames. We use the browser's Host header
        so URLs always match how the page was accessed.
        """
        from textual_serve.server import to_int
        router = request.app.router
        font_size = to_int(request.query.get("fontsize", "16"), 16)

        # Use Host header from the actual browser request
        host_header = (
            request.headers.get("Host", "")
            or request.headers.get("X-Forwarded-Host", "")
        )
        # Trust X-Forwarded-Proto / -Prefix from a reverse proxy so
        # generated URLs match how the browser reached us.
        fwd_proto = request.headers.get("X-Forwarded-Proto", "").lower()
        prefix = self._fwd_prefix(request)
        if host_header:
            http_proto = fwd_proto or ("https" if self._ssl_certfile else "http")
            ws_proto = "wss" if http_proto == "https" else "ws"
            base_url = f"{http_proto}://{host_header}{prefix}"
        else:
            base_url = self.public_url
            ws_proto = "wss" if self.public_url.startswith("https") else "ws"

        ws_path = router["websocket"].url_for()
        static_path = str(router["static"].url_for(filename="/")).rstrip("/") + "/"

        return {
            "font_size": font_size,
            "prefix": prefix,
            "app_websocket_url": f"{ws_proto}://{host_header or self.host}{prefix}{ws_path}",
            "config": {
                "static": {"url": f"{base_url}{static_path}"},
            },
            "application": {"name": self.title},
        }

    # ------------------------------------------------------------------
    # menuconfig-in-browser: F6 opens a signed URL that streams a ttyd
    # subshell (running `ct-ng menuconfig`) through a WS-proxy in this
    # very server. All gating lives here — ttyd itself listens on
    # loopback only, unauthenticated. See _start_ttyd() in
    # docker/entrypoint.py for the ttyd side.
    # ------------------------------------------------------------------

    def _user_from_cookie(self, cookie: str) -> str:
        """Return the user field from a signed session cookie, "" if malformed."""
        try:
            _ts, user, _sig = cookie.rsplit(":", 2)
            return user
        except Exception:
            return ""

    def _sign_term(self, user: str, sid: str, exp: int) -> str:
        msg = f"term:{user}:{sid}:{exp}".encode()
        return hmac.new(self._cookie_secret, msg, hashlib.sha256).hexdigest()[:16]

    def _verify_term(self, user: str, sid: str, exp: int, sig: str) -> bool:
        if time.time() > exp:
            return False
        return hmac.compare_digest(sig, self._sign_term(user, sid, exp))

    def _authed_user(self, request: web.Request) -> tuple[str, web.Response | None]:
        """Resolve the effective user for a term-* request.

        - auth on  + valid cookie  → (user, None)
        - auth on  + missing/bad   → ("", HTTPFound to /login) if login
                                     is wired, else 401 response
        - auth off                 → ("anonymous", None) — single-tenant
                                     containers without a password still
                                     get to use F6.
        """
        has_auth = bool(self._auth_user and self._auth_password)
        if not has_auth:
            return "anonymous", None
        cookie = request.cookies.get(_COOKIE_NAME, "")
        if cookie and _verify_token(cookie, self._cookie_secret):
            user = self._user_from_cookie(cookie)
            if user:
                return user, None
        prefix = self._fwd_prefix(request)
        return "", web.HTTPFound(f"{prefix}/login?next={prefix}{request.path_qs}")

    async def _handle_term_open(self, request: web.Request) -> web.Response:
        """GET /term/open?cmd=… — mint a session, redirect browser to /term/.

        Called via ``self.app.open_url()`` from the textual TUI on F6.
        The redirect keeps the flow entirely browser-driven: the login
        cookie is presented naturally, no cross-process token passing
        needed. Revokes any previous term session for the same user
        (kill-first policy). Only whitelisted ``cmd`` values are minted.
        """
        user, err = self._authed_user(request)
        if err is not None:
            return err
        cmd = request.query.get("cmd", "")
        if cmd not in _TERM_CMD_WHITELIST:
            return web.Response(status=400, text=f"unknown cmd: {cmd!r}")
        sid = secrets.token_hex(16)
        exp = int(time.time()) + _TERM_TTL
        sig = self._sign_term(user, sid, exp)
        self._active_term_sids[user] = sid
        prefix = self._fwd_prefix(request)
        return web.HTTPFound(f"{prefix}/term/?sid={sid}&exp={exp}&sig={sig}")

    async def _handle_term_shell(self, request: web.Request) -> web.Response:
        """GET /term/ — serves the minimal xterm.js shell page."""
        prefix = self._fwd_prefix(request)
        sid = request.query.get("sid", "")
        exp = request.query.get("exp", "")
        sig = request.query.get("sig", "")
        # WS URL is same-origin, protocol chosen by browser.
        return aiohttp_jinja2.render_template(
            "term.html", request,
            {
                "prefix": prefix,
                "ws_path": f"{prefix}/term/ws?sid={sid}&exp={exp}&sig={sig}",
                "static_prefix": f"{prefix}/static",
                **self._theme_colors,
            },
        )

    async def _handle_term_ws(self, request: web.Request) -> web.StreamResponse:
        """WebSocket proxy /term/ws → ttyd 127.0.0.1:7681/ws.

        Bidirectional forwarding via two asyncio tasks. Enforces:
          - valid session cookie
          - HMAC on (user, sid, exp) matches
          - exp not passed
          - sid == user's currently-active sid (kill-first)
        """
        user, err = self._authed_user(request)
        if err is not None:
            # WS: HTTPFound is not helpful; just fail explicitly.
            return web.Response(status=401, text="unauthorized")
        sid = request.query.get("sid", "")
        try:
            exp = int(request.query.get("exp", "0"))
        except ValueError:
            exp = 0
        sig = request.query.get("sig", "")
        if not (user and sid and self._verify_term(user, sid, exp, sig)):
            return web.Response(status=403, text="term token invalid or expired")
        if self._active_term_sids.get(user) != sid:
            return web.Response(status=409, text="term session revoked by newer session")

        ws_client = web.WebSocketResponse(protocols=("tty",))
        await ws_client.prepare(request)

        session = aiohttp.ClientSession()
        try:
            try:
                ws_ttyd = await session.ws_connect(
                    f"{_TTYD_INTERNAL}/ws",
                    protocols=("tty",),
                    origin=_TTYD_INTERNAL,
                    max_msg_size=0,
                )
            except Exception as e:
                await ws_client.close(message=f"ttyd connect failed: {e}".encode())
                return ws_client

            async def _fwd_msgs(src, dst):
                try:
                    async for msg in src:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await dst.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await dst.send_bytes(msg.data)
                        else:  # CLOSE/CLOSING/CLOSED/ERROR
                            break
                except Exception:
                    pass

            # Wait for ONE side to close, then tear down the other. If we
            # await both (asyncio.gather), a browser tab-close only
            # completes client→ttyd; the ttyd→client task blocks forever
            # waiting for ttyd traffic, ws_ttyd.close() in `finally`
            # never fires, ttyd never sees its client leave, no SIGHUP
            # gets sent — and mconf/make/bash orphan under dumb-init.
            t_c2t = asyncio.create_task(_fwd_msgs(ws_client, ws_ttyd))
            t_t2c = asyncio.create_task(_fwd_msgs(ws_ttyd, ws_client))
            try:
                done, pending = await asyncio.wait(
                    {t_c2t, t_t2c}, return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                for t in pending:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            finally:
                if not ws_ttyd.closed:
                    await ws_ttyd.close()
        finally:
            if not ws_client.closed:
                await ws_client.close()
            await session.close()
            # If this session is still the "active" one, clear it — user
            # closed the tab. A stale entry (already revoked by a newer
            # F6) is left alone so we don't overwrite the newer sid.
            if self._active_term_sids.get(user) == sid:
                del self._active_term_sids[user]

        return ws_client

    async def _make_app(self) -> web.Application:
        """Override to inject auth middleware and login routes."""
        app = await super()._make_app()
        has_auth = bool(self._auth_user and self._auth_password)
        if has_auth:
            app.router.add_get("/login", self._handle_login_get)
            app.router.add_post("/login", self._handle_login_post)
            app.router.add_get("/logout", self._handle_logout)
            app.middlewares.insert(0, self._auth_middleware)
        # menuconfig-in-browser: always available; the whole flow is
        # gated by the auth middleware above (when auth is on) and by
        # HMAC-signed short-lived tokens (always).
        app.router.add_get("/term/open", self._handle_term_open)
        app.router.add_get("/term/", self._handle_term_shell)
        app.router.add_get("/term/ws", self._handle_term_ws)
        # xterm.js + fit-addon vendored under ui/web/static/vendor/.
        # web_server.py lives at core/web_server.py, so parent.parent is
        # the repo root — same location whether we run from a fresh
        # checkout, /app inside docker, or a system-install.
        from pathlib import Path as _Path
        _vendor_dir = _Path(__file__).resolve().parent.parent / "ui" / "web" / "static" / "vendor"
        if _vendor_dir.is_dir():
            app.router.add_static("/term/vendor/", str(_vendor_dir))
        # Make has_auth + t() available to all jinja2 templates
        env = aiohttp_jinja2.get_env(app)
        env.globals["has_auth"] = has_auth
        try:
            from translation import t as _t
            env.globals["t"] = _t
        except Exception:
            # Fallback: identity — templates using {{ t(...) }} still render
            # something sensible even if the translation module is absent.
            env.globals["t"] = lambda key, **kwargs: key
        return app

    def serve(self, debug: bool = False) -> None:
        """Override to add SSL context to aiohttp.web.run_app."""
        self.debug = debug
        self.initialize_logging()

        try:
            loop = asyncio.get_event_loop()
        except Exception:
            loop = asyncio.new_event_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, self.request_exit)
            loop.add_signal_handler(signal.SIGTERM, self.request_exit)
        except NotImplementedError:
            pass

        ssl_ctx = self._make_ssl_context()

        try:
            web.run_app(
                self._make_app(),
                host=self.host,
                port=self.port,
                handle_signals=False,
                loop=loop,
                ssl_context=ssl_ctx,
                print=lambda *args: None,
            )
        except OSError as e:
            print(
                f"\n[s4-web] ERROR: Cannot bind to {self.host}:{self.port} — {e}\n"
                f"Check that the address exists on this machine and the port is free.\n"
                f"Update the bind address in s4 Settings → Web Service.",
                flush=True,
            )
            raise SystemExit(1) from None
