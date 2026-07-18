"""MCP Service setup modal — install/remove systemd unit for ``s4-mcp``.

Structural clone of :class:`WebServiceSetupModal` — same UX: shows
current status, asks for sudo password, has Install / Remove buttons
with double-press confirmation on Remove.
"""

from __future__ import annotations

import threading
import time

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, RichLog, Button, Input, Select
from ui.widgets.artifact_richlog import ArtifactRichLog

from translation import t
from ui.icons import icon


class McpServiceSetupModal(ModalScreen[None]):
    """Modal for installing/removing the s4-mcp systemd unit."""

    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    McpServiceSetupModal {
        align: center middle;
    }
    #mcp-box {
        width: 110;
        height: 38;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }
    #mcp-transport-row {
        height: 3;
        margin-top: 1;
    }
    #mcp-transport-row Static.s-label-tall {
        width: 26;
    }
    #mcp-transport-row Select {
        width: 1fr;
    }
    #mcp-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #mcp-log {
        height: 1fr;
        background: $boost;
        border: solid $accent;
        border-title-color: $text;
        border-title-style: bold;
        padding: 0 1;
        overflow-y: auto;
    }
    #mcp-log:focus {
        border: heavy $accent;
    }
    #mcp-pwd-row {
        height: 3;
        margin-top: 1;
    }
    #mcp-pwd-row Input {
        width: 1fr;
    }
    #mcp-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    #mcp-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._busy = False
        self._remove_confirm_time: float = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp-box"):
            yield Static(f"[bold]{t('mcp_svc_setup_title')}[/bold]", id="mcp-title")
            log = ArtifactRichLog(id="mcp-log", highlight=False, markup=True, wrap=True)
            log.border_title = t("output_title")
            yield log
            # Transport selector: SSE (legacy) vs. Streamable HTTP (current)
            s = self.app.settings
            _tsel = Select(
                options=[
                    (t("mcp_transport_sse"),  "sse"),
                    (t("mcp_transport_http"), "http"),
                ],
                value=(s.mcp_transport if s.mcp_transport in ("sse", "http") else "sse"),
                id="mcp-transport",
                allow_blank=False,
            )
            _tsel.tooltip = t("tt_mcp_transport")
            with Horizontal(id="mcp-transport-row"):
                yield Static(f" {t('mcp_transport')}", classes="s-label-tall")
                yield _tsel
            _eye_btn = Button(icon("eye"), id="mcp-eye", classes="tc-reset-btn")
            _eye_btn.tooltip = t("tt_show_password")
            with Horizontal(id="mcp-pwd-row"):
                yield Static(f" {t('dep_enter_root_pwd')}", classes="s-label-tall")
                yield Input(placeholder=t("dep_enter_root_pwd"), id="mcp-pwd", password=True)
                yield _eye_btn
            with Horizontal(id="mcp-buttons"):
                yield Button(t("mcp_svc_install_btn"), id="mcp-install", variant="primary")
                yield Button(t("mcp_svc_remove_btn"), id="mcp-remove", variant="error", disabled=True)
                yield Button(t("close"), id="mcp-close")

    def on_mount(self) -> None:
        self._log_widget = self.query_one("#mcp-log", RichLog)
        self._show_status()

    def _show_status(self) -> None:
        from core.mcp_service import McpServiceManager
        mgr = McpServiceManager(self.app.paths.root)

        container = mgr.container_mode()
        available = mgr.systemd_available()
        installed = mgr.is_installed()

        self._log_widget.write(
            f"[bold]{t('mcp_svc_current_status')}:[/bold] {t(mgr.status_text())}"
        )
        if installed and not container:
            self._log_widget.write(f"  Service: {mgr.service}")
            self._log_widget.write(f"  Unit: {mgr._unit_path}")
            self._log_widget.write(f"  Env:  {mgr._env_file}")

        if container:
            self._log_widget.write(f"\n[yellow]{t('mcp_svc_container_hint')}[/yellow]")
        elif not available:
            self._log_widget.write("\n[yellow]systemd is not running as PID 1[/yellow]")

        s = self.app.settings
        self._log_widget.write("")
        self._log_widget.write(f"[bold]Config:[/bold]")
        self._log_widget.write(f"  host:          {s.mcp_host}")
        self._log_widget.write(f"  port:          {s.mcp_port}")
        if s.mcp_dns_name:
            self._log_widget.write(f"  dns:           {s.mcp_dns_name}")
        if s.mcp_ssl:
            cert = s.mcp_ssl_cert or "(auto-generate)"
            self._log_widget.write(f"  ssl:           [green]on[/green] \u2014 {cert}")
        else:
            self._log_widget.write(f"  ssl:           off (plain HTTP)")
        if s.mcp_token:
            self._log_widget.write(f"  token:         [green]set[/green] ({len(s.mcp_token)} chars)")
        else:
            self._log_widget.write(
                f"  token:         [red]empty[/red] "
                f"(server will accept any caller \u2014 localhost only!)"
            )
        self._log_widget.write(
            f"  allow actions: {'yes' if s.mcp_allow_actions else 'no (read-only)'}"
        )
        _t_label = {
            "sse":  "sse — HTTP+SSE (legacy, /sse + /messages/)",
            "http": "http — Streamable HTTP (/mcp, spec 2025-03-26)",
        }.get(s.mcp_transport, "sse (default)")
        self._log_widget.write(f"  transport:     {_t_label}")

        try:
            self.query_one("#mcp-install", Button).disabled = not (available or container)
            self.query_one("#mcp-remove", Button).disabled = not installed
        except Exception:
            pass

    def _log(self, msg: str) -> None:
        self.app.call_from_thread(self._log_widget.write, msg)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "mcp-close":
            self.dismiss(None)
        elif event.button.id == "mcp-install":
            self._run_action("install")
        elif event.button.id == "mcp-remove":
            now = time.time()
            if now - self._remove_confirm_time < 3:
                self._remove_confirm_time = 0
                self._run_action("remove")
            else:
                self._remove_confirm_time = now
                self.notify(t("mcp_svc_confirm_remove"), severity="warning", timeout=3)
        elif event.button.id == "mcp-eye":
            try:
                pw = self.query_one("#mcp-pwd", Input)
                pw.password = not pw.password
            except Exception:
                pass

    def action_close(self) -> None:
        self.dismiss(None)

    def _run_action(self, action: str) -> None:
        if self._busy:
            return
        self._busy = True
        self._log_widget.clear()
        threading.Thread(target=self._do_action, args=(action,), daemon=True).start()

    def _do_action(self, action: str) -> None:
        try:
            password = ""
            try:
                password = self.app.call_from_thread(
                    lambda: self.query_one("#mcp-pwd", Input).value
                )
            except Exception:
                pass

            from core.mcp_service import McpServiceManager, validate_port
            mgr = McpServiceManager(self.app.paths.root)
            s = self.app.settings

            if action == "install":
                err = validate_port(s.mcp_port or 8181)
                if err:
                    self._log(f"[red]{err}[/red]")
                    return
                # Persist the transport choice selected in the modal so
                # the main settings panel + future Restarts pick it up.
                # We also mark it as explicit so load() stops treating a
                # stored "sse" as a stale implicit default.
                try:
                    _tval = self.app.call_from_thread(
                        lambda: self.query_one("#mcp-transport", Select).value
                    )
                    if _tval in ("sse", "http"):
                        _changed = (
                            _tval != s.mcp_transport
                            or not s.mcp_transport_explicit
                        )
                        if _changed:
                            s.mcp_transport = _tval
                            s.mcp_transport_explicit = True
                            s.save(self.app.settings_path)
                            self._log(f"  transport: set to [b]{_tval}[/b]")
                except Exception:
                    pass
                self._log("[bold]Installing MCP service...[/bold]")
                success = mgr.install(
                    host=s.mcp_host or "0.0.0.0",
                    port=s.mcp_port or 8181,
                    dns_name=s.mcp_dns_name,
                    token=s.mcp_token,
                    allow_actions=s.mcp_allow_actions,
                    ssl=s.mcp_ssl,
                    ssl_cert=s.mcp_ssl_cert,
                    ssl_key=s.mcp_ssl_key,
                    transport=s.mcp_transport,
                    root_path=s.mcp_root_path,
                    logs_dir=self.app.paths.logs_dir,
                    organize_logs=s.organize_logs,
                    password=password,
                    log=self._log,
                )
                if success:
                    mgr.start(log=self._log)
            elif action == "remove":
                self._log("[bold]Removing MCP service...[/bold]")
                mgr.uninstall(password=password, log=self._log)

            self._log("")
            self.app.call_from_thread(self._show_status)

        except Exception:
            import traceback
            self._log(f"[red]{traceback.format_exc()}[/red]")
        finally:
            self._busy = False
