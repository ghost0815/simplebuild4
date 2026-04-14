"""S4Server — textual-serve with SSL and cookie-based auth support."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import signal
import ssl
import time

import aiohttp_jinja2
from aiohttp import web
from textual_serve.server import Server

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

    def _make_ssl_context(self) -> ssl.SSLContext | None:
        if not self._ssl_certfile or not self._ssl_keyfile:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self._ssl_certfile, self._ssl_keyfile)
        return ctx

    async def _handle_login_get(self, request: web.Request) -> web.Response:
        """Show login form."""
        return aiohttp_jinja2.render_template(
            "login.html", request, {"error": "", **self._theme_colors},
        )

    async def _handle_login_post(self, request: web.Request) -> web.Response:
        """Validate credentials and set session cookie."""
        data = await request.post()
        user = data.get("username", "")
        password = data.get("password", "")
        if user == self._auth_user and password == self._auth_password:
            # Redirect to index with session cookie
            target = request.query.get("next", "/")
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
            {"error": "Invalid username or password", **self._theme_colors},
        )
        response.set_status(401)
        return response

    async def _handle_logout(self, request: web.Request) -> web.Response:
        """Clear session cookie and redirect to login."""
        response = web.HTTPFound("/login")
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

        # Redirect to login form
        return web.HTTPFound(f"/login?next={path}")

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
        if host_header:
            http_proto = "https" if self._ssl_certfile else "http"
            ws_proto = "wss" if self._ssl_certfile else "ws"
            base_url = f"{http_proto}://{host_header}"
        else:
            base_url = self.public_url
            ws_proto = "wss" if self.public_url.startswith("https") else "ws"

        ws_path = router["websocket"].url_for()
        static_path = str(router["static"].url_for(filename="/")).rstrip("/") + "/"

        return {
            "font_size": font_size,
            "app_websocket_url": f"{ws_proto}://{host_header or self.host}{ws_path}",
            "config": {
                "static": {"url": f"{base_url}{static_path}"},
            },
            "application": {"name": self.title},
        }

    async def _make_app(self) -> web.Application:
        """Override to inject auth middleware and login routes."""
        app = await super()._make_app()
        has_auth = bool(self._auth_user and self._auth_password)
        if has_auth:
            app.router.add_get("/login", self._handle_login_get)
            app.router.add_post("/login", self._handle_login_post)
            app.router.add_get("/logout", self._handle_logout)
            app.middlewares.insert(0, self._auth_middleware)
        # Make has_auth available to all jinja2 templates
        env = aiohttp_jinja2.get_env(app)
        env.globals["has_auth"] = has_auth
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

        web.run_app(
            self._make_app(),
            host=self.host,
            port=self.port,
            handle_signals=False,
            loop=loop,
            ssl_context=ssl_ctx,
            print=lambda *args: None,
        )
