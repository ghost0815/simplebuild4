"""MCP Inspector modal — launches ``@modelcontextprotocol/inspector``.

Two modes:

* **Web UI** — starts the inspector in web-server mode bound to
  ``0.0.0.0`` so it is reachable from the LAN. The inspector prints
  a URL like ``http://localhost:6274/?MCP_PROXY_AUTH_TOKEN=…`` on
  stdout. We parse that URL out of the stream, highlight it in a
  header bar, and offer *Open in browser* / *Copy URL* buttons.

* **CLI tools/list** — runs the inspector with ``--cli --method
  tools/list``; it prints a JSON tool list and exits.

All stdout/stderr is streamed live into a RichLog. On modal close the
subprocess is terminated so ports 6274 / 6277 are freed.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, RichLog, Button
from ui.widgets.artifact_richlog import ArtifactRichLog

from translation import t
from core.env import is_running_in_container
from core.node_bootstrap import (
    find_npx, node_bin_dir, install_node as _install_node,
)


# Match any http(s)://…MCP_PROXY_AUTH_TOKEN=… URL the inspector prints.
_URL_RE = re.compile(r"https?://[^\s\033]+MCP_PROXY_AUTH_TOKEN=[^\s\033]+")


class McpInspectorModal(ModalScreen[None]):
    """Start the MCP inspector against the configured local service."""

    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    McpInspectorModal {
        align: center middle;
    }
    #insp-box {
        width: 120;
        height: 36;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }
    #insp-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #insp-pick-row {
        height: 3;
        margin-bottom: 1;
    }
    #insp-pick-row Button {
        margin: 0 1 0 0;
    }
    #insp-pick-row Button.hidden {
        display: none;
    }
    #insp-url-bar {
        height: auto;
        background: $boost;
        border: solid $success;
        padding: 0 1;
        margin-bottom: 1;
        display: none;
    }
    #insp-url-bar.visible {
        display: block;
    }
    #insp-url-text {
        color: $success;
        text-style: bold;
    }
    #insp-url-btns {
        height: 3;
        margin-top: 1;
    }
    #insp-url-btns Button {
        margin: 0 1 0 0;
    }
    #insp-log {
        height: 1fr;
        background: $boost;
        border: solid $accent;
        border-title-color: $text;
        border-title-style: bold;
        overflow-y: auto;
    }
    #insp-log:focus {
        border: heavy $accent;
    }
    #insp-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    #insp-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self, server_url: str, token: str, transport: str = "sse") -> None:
        super().__init__()
        self._server_url = server_url
        self._token = token or ""
        # Inspector CLI transport values: "sse" or "streamable-http".
        # We accept s4's "http" shorthand and translate it here.
        _t = (transport or "sse").lower()
        self._transport = "streamable-http" if _t == "http" else "sse"
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._url_found: str = ""
        self._installing: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="insp-box"):
            yield Static(f"[bold]{t('mcp_inspector_title')}[/bold]", id="insp-title")
            with Horizontal(id="insp-pick-row"):
                yield Static(f" {t('mcp_inspector_pick')} ", classes="s-label-tall")
                yield Button(t("mcp_inspector_web"), id="insp-web", variant="primary")
                yield Button(t("mcp_inspector_cli"), id="insp-cli", variant="default")
                yield Button(t("mcp_inspector_install_node"), id="insp-install-node",
                             variant="warning", classes="hidden")
            with Vertical(id="insp-url-bar"):
                yield Static("", id="insp-url-text")
                with Horizontal(id="insp-url-btns"):
                    yield Button(t("mcp_inspector_open"), id="insp-open", variant="success")
                    yield Button(t("mcp_inspector_copy"), id="insp-copy", variant="primary")
            log = ArtifactRichLog(id="insp-log", highlight=False, markup=True, wrap=True)
            log.border_title = t("output_title")
            yield log
            with Horizontal(id="insp-buttons"):
                yield Button(t("mcp_inspector_stop"), id="insp-stop",
                             variant="warning", disabled=True)
                yield Button(t("close"), id="insp-close")

    def on_mount(self) -> None:
        self._log_widget = self.query_one("#insp-log", RichLog)
        self._log_widget.write(
            f"[dim]{t('mcp_inspector_first_run')}[/dim]"
        )
        self._log_widget.write("")
        self._log_widget.write(f"[bold]Server URL:[/bold] {self._server_url}")
        if self._token:
            self._log_widget.write(f"[bold]Auth:[/bold]       Bearer ({len(self._token)} chars)")
        else:
            self._log_widget.write(f"[bold]Auth:[/bold]       [yellow]no token[/yellow]")
        self._log_widget.write("")
        self._refresh_npx_state()

    def _refresh_npx_state(self) -> None:
        """Show/hide Web/CLI vs. Install-Node depending on npx availability."""
        npx = find_npx()
        web = self.query_one("#insp-web", Button)
        cli = self.query_one("#insp-cli", Button)
        ins = self.query_one("#insp-install-node", Button)
        if npx is None:
            web.disabled = True
            cli.disabled = True
            ins.remove_class("hidden")
            ins.disabled = False
            self._log_widget.write(
                f"[yellow]{t('mcp_inspector_need_node')}[/yellow]"
            )
        else:
            web.disabled = False
            cli.disabled = False
            ins.add_class("hidden")
            self._log_widget.write(f"[dim]npx: {npx}[/dim]")

    # ------------------------------------------------------------------ UI

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "insp-close":
            self.action_close()
        elif bid == "insp-stop":
            self._terminate_proc()
        elif bid == "insp-web":
            self._start("web")
        elif bid == "insp-cli":
            self._start("cli")
        elif bid == "insp-install-node":
            self._install_node_clicked()
        elif bid == "insp-open":
            self._open_url()
        elif bid == "insp-copy":
            if self._url_found:
                try:
                    self.app.copy_to_clipboard(self._url_found)
                    self.notify(t("copied"), timeout=2)
                except Exception:
                    pass

    def action_close(self) -> None:
        self._terminate_proc()
        self.dismiss(None)

    # ------------------------------------------------------------------ subprocess

    def _start(self, mode: str) -> None:
        if self._proc is not None:
            return
        npx = find_npx()
        if npx is None:
            self._log_widget.write(f"[red]{t('mcp_inspector_no_npx')}[/red]")
            self._refresh_npx_state()
            return

        # Disable mode pick buttons once a run has started.
        try:
            self.query_one("#insp-web", Button).disabled = True
            self.query_one("#insp-cli", Button).disabled = True
            self.query_one("#insp-stop", Button).disabled = False
        except Exception:
            pass

        # Two CLI shapes:
        #   Web UI :  npx … @modelcontextprotocol/inspector \
        #               --transport sse --server-url <URL> \
        #               --header "Authorization: Bearer …"
        #   CLI    :  npx … @modelcontextprotocol/inspector --cli \
        #               <URL> --transport sse \
        #               --header "Authorization: Bearer …" \
        #               --method tools/list
        # In CLI mode the server URL is passed POSITIONALLY; the
        # ``--server-url`` flag only exists for the Web UI launcher.
        cmd = [str(npx), "-y", "@modelcontextprotocol/inspector"]
        if mode == "cli":
            cmd += ["--cli", self._server_url,
                    "--transport", self._transport]
            if self._token:
                cmd += ["--header", f"Authorization: Bearer {self._token}"]
            cmd += ["--method", "tools/list"]
        else:
            cmd += ["--transport", self._transport,
                    "--server-url", self._server_url]
            if self._token:
                cmd += ["--header", f"Authorization: Bearer {self._token}"]

        env = os.environ.copy()
        # If we are using the user-local Node install, node must be on
        # PATH too (npx's shebang resolves ``env node``).
        bd = node_bin_dir()
        if bd:
            env["PATH"] = f"{bd}:{env.get('PATH', '')}"
        # Bind host + origin policy: on a native install we default to
        # localhost + the built-in DNS-rebinding protection (which pins
        # accepted Origins to the bind host); remote access is via SSH
        # port forwarding, secure and simple.
        #
        # In the container everything is different: the inspector process
        # sits inside the container's netns, so binding to 127.0.0.1
        # means it is only reachable from *inside* the container —
        # `docker run -p 6274:6274` can't forward traffic to a socket
        # that isn't on the container's bridge interface. So container
        # mode forces HOST=0.0.0.0.
        #
        # That in turn breaks the rebinding guard, which defaults to
        # "only Origin == bind host" — 0.0.0.0 is never a real Origin.
        # ALLOWED_ORIGINS is documented as a comma-separated list of
        # exact URLs, NOT a wildcard-capable pattern ("*" is treated
        # as the literal string "*" and matches nothing). So we ship
        # sensible defaults for the typical local case and let the
        # user extend via S4_INSPECTOR_ORIGINS for LAN/DNS access.
        if is_running_in_container():
            env["HOST"] = "0.0.0.0"
            origins = ["http://localhost:6274", "http://127.0.0.1:6274"]
            extra = os.environ.get("S4_INSPECTOR_ORIGINS", "").strip()
            if extra:
                origins.extend(o.strip() for o in extra.split(",") if o.strip())
            env["ALLOWED_ORIGINS"] = ",".join(origins)
        if mode == "web":
            # Suppress the inspector's own "auto-open browser" — we
            # expose an explicit [Open in browser] button in the modal.
            env["MCP_AUTO_OPEN_ENABLED"] = "false"
            self._log_widget.write("")
            hint_key = (
                "mcp_inspector_container_hint"
                if is_running_in_container()
                else "mcp_inspector_ssh_hint"
            )
            self._log_widget.write(f"[dim]{t(hint_key)}[/dim]")

        self._log_widget.write(f"[dim]$ {' '.join(cmd)}[/dim]")
        self._log_widget.write("")

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                env=env,
                # New process group so SIGTERM reaches children (npm/node)
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
        except Exception as e:
            self._log_widget.write(f"[red]Failed to launch npx: {e}[/red]")
            self._proc = None
            return

        self._reader_thread = threading.Thread(
            target=self._stream_output, daemon=True,
        )
        self._reader_thread.start()

    def _stream_output(self) -> None:
        assert self._proc is not None
        proc = self._proc
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                self.app.call_from_thread(self._log_widget.write, line)
                if not self._url_found:
                    m = _URL_RE.search(line)
                    if m:
                        url = m.group(0)
                        # Inspector prints its literal bind host in the
                        # URL; in container mode that's "0.0.0.0", which
                        # no browser can meaningfully open (and its
                        # Origin header wouldn't match anything). Swap
                        # for "localhost", which is in our ALLOWED_ORIGINS
                        # default. The user still needs to substitute
                        # the server hostname manually for remote access.
                        url = url.replace("//0.0.0.0:", "//localhost:")
                        self._url_found = url
                        self.app.call_from_thread(self._show_url, url)
        except Exception:
            pass
        rc = proc.wait()
        self.app.call_from_thread(
            self._log_widget.write,
            f"\n[dim]-- process exited with code {rc} --[/dim]",
        )
        self.app.call_from_thread(self._on_proc_end)

    def _show_url(self, url: str) -> None:
        try:
            bar = self.query_one("#insp-url-bar")
            bar.add_class("visible")
            self.query_one("#insp-url-text", Static).update(
                f"{t('mcp_inspector_url')} {url}"
            )
        except Exception:
            pass

    def _open_url(self) -> None:
        if not self._url_found:
            return
        import shutil
        import subprocess
        # Try openers in order; first one that exists wins.
        #   wslview     — WSL (wslu package) → Windows default browser
        #   xdg-open    — Linux desktop
        #   open        — macOS
        # Python's ``webbrowser`` module guesses wrong on WSL (it tries
        # firefox/google-chrome which aren't installed) so we override
        # with an explicit command chain before falling back to it.
        candidates: list[list[str]] = []
        for opener in ("wslview", "xdg-open", "open"):
            if shutil.which(opener):
                candidates.append([opener, self._url_found])
        # WSL without wslu: reach out to the Windows side via cmd.exe,
        # which is on PATH whenever WSL interop is enabled (the default).
        # ``start`` eats a quoted empty-string as its "window title" arg,
        # which prevents odd parsing of URLs containing ``&``.
        if shutil.which("cmd.exe"):
            candidates.append(["cmd.exe", "/c", "start", "", self._url_found])
        for cmd in candidates:
            try:
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self.notify(t("mcp_inspector_opened"), timeout=2)
                return
            except Exception:
                continue
        # Last resort — Python's webbrowser; may fail silently on WSL
        # or headless boxes, hence the explicit chain above.
        try:
            import webbrowser
            if webbrowser.open(self._url_found):
                self.notify(t("mcp_inspector_opened"), timeout=2)
                return
        except Exception:
            pass
        self._log_widget.write(
            f"[red]{t('mcp_inspector_open_failed')}[/red]"
        )

    def _terminate_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _on_proc_end(self) -> None:
        self._proc = None
        try:
            self.query_one("#insp-stop", Button).disabled = True
            # Re-enable mode pick so user can run again.
            self.query_one("#insp-web", Button).disabled = False
            self.query_one("#insp-cli", Button).disabled = False
        except Exception:
            pass

    # ------------------------------------------------------------------ Node bootstrap

    def _install_node_clicked(self) -> None:
        if self._installing:
            return
        self._installing = True
        try:
            btn = self.query_one("#insp-install-node", Button)
            btn.disabled = True
            btn.label = t("mcp_inspector_installing")
        except Exception:
            pass
        self._log_widget.write("")
        self._log_widget.write(f"[bold]{t('mcp_inspector_installing')}[/bold]")
        threading.Thread(target=self._do_install_node, daemon=True).start()

    def _do_install_node(self) -> None:
        def log(msg: str) -> None:
            self.app.call_from_thread(self._log_widget.write, msg)
        try:
            ok = _install_node(log=log)
        except Exception as e:
            ok = False
            log(f"[red]Install crashed: {e}[/red]")
        self.app.call_from_thread(self._after_install_node, ok)

    def _after_install_node(self, ok: bool) -> None:
        self._installing = False
        try:
            btn = self.query_one("#insp-install-node", Button)
            btn.label = t("mcp_inspector_install_node")
            btn.disabled = False
        except Exception:
            pass
        self._log_widget.write("")
        if ok:
            self._refresh_npx_state()
            self._log_widget.write(
                f"[green]{t('mcp_inspector_node_ready')}[/green]"
            )
        else:
            self._log_widget.write(
                f"[red]{t('mcp_inspector_install_failed')}[/red]"
            )
