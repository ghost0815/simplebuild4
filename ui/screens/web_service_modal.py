"""Web Service setup modal — install/remove systemd service."""

from __future__ import annotations

import threading
import time

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, RichLog, Button, Input
from ui.widgets.artifact_richlog import ArtifactRichLog

from translation import t
from ui.icons import icon


class WebServiceSetupModal(ModalScreen[None]):
    """Modal for installing/removing the s4 web service as a systemd unit."""

    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    WebServiceSetupModal {
        align: center middle;
    }
    #ws-box {
        width: 110;
        height: 34;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }
    #ws-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #ws-log {
        height: 1fr;
        background: $boost;
        border: solid $accent;
        border-title-color: $text;
        border-title-style: bold;
        padding: 0 1;
        overflow-y: auto;
    }
    #ws-log:focus {
        border: heavy $accent;
    }
    #ws-pwd-row {
        height: 3;
        margin-top: 1;
    }
    #ws-pwd-row Input {
        width: 1fr;
    }
    #ws-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    #ws-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._busy = False
        self._remove_confirm_time: float = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="ws-box"):
            yield Static(f"[bold]{t('web_svc_setup_title')}[/bold]", id="ws-title")
            log = ArtifactRichLog(id="ws-log", highlight=False, markup=True, wrap=True)
            log.border_title = t("output_title")
            yield log
            _eye_btn = Button(icon("eye"), id="ws-eye", classes="tc-reset-btn")
            _eye_btn.tooltip = t("tt_show_password")
            with Horizontal(id="ws-pwd-row"):
                yield Static(f" {t('dep_enter_root_pwd')}", classes="s-label-tall")
                yield Input(placeholder=t("dep_enter_root_pwd"), id="ws-pwd", password=True)
                yield _eye_btn
            with Horizontal(id="ws-buttons"):
                yield Button(t("web_svc_install_btn"), id="ws-install", variant="primary")
                yield Button(t("web_svc_remove_btn"), id="ws-remove", variant="error", disabled=True)
                yield Button(t("close"), id="ws-close")

    def on_mount(self) -> None:
        self._log_widget = self.query_one("#ws-log", RichLog)
        self._show_status()

    def _show_status(self) -> None:
        """Display current service status and update button states."""
        from core.web_service import WebServiceManager
        mgr = WebServiceManager(self.app.paths.root)

        container = mgr.container_mode()
        available = mgr.systemd_available()
        installed = mgr.is_installed()
        running = mgr.is_running() if installed else False

        self._log_widget.write(f"[bold]{t('web_svc_current_status')}:[/bold] {t(mgr.status_text())}")
        if installed and not container:
            self._log_widget.write(f"  Service: {mgr.service}")
            self._log_widget.write(f"  Unit: {mgr._unit_path}")

        wsl_pre_systemd = not available and not container and mgr._is_wsl()
        if container:
            self._log_widget.write(f"\n[yellow]{t('web_svc_container_hint')}[/yellow]")
        elif not available:
            self._log_widget.write(f"\n[yellow]systemd is not running as PID 1[/yellow]")
            if wsl_pre_systemd:
                self._log_widget.write(
                    "[yellow]WSL detected — systemd will be enabled on install[/yellow]"
                )

        # For WSL without systemd: allow install (it will configure wsl.conf
        # and guide the user to restart WSL before the full install).
        # In container: install/remove act on settings.yaml.
        install_enabled = available or wsl_pre_systemd or container
        try:
            self.query_one("#ws-install", Button).disabled = not install_enabled
            self.query_one("#ws-remove", Button).disabled = not installed
        except Exception:
            pass

    def _log(self, msg: str) -> None:
        self.app.call_from_thread(self._log_widget.write, msg)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ws-close":
            self.dismiss(None)
        elif event.button.id == "ws-install":
            self._run_action("install")
        elif event.button.id == "ws-remove":
            # Double-press confirmation
            now = time.time()
            if now - self._remove_confirm_time < 3:
                self._remove_confirm_time = 0
                self._run_action("remove")
            else:
                self._remove_confirm_time = now
                self.notify(t("web_svc_confirm_remove"), severity="warning", timeout=3)
        elif event.button.id == "ws-eye":
            try:
                pw = self.query_one("#ws-pwd", Input)
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
                    lambda: self.query_one("#ws-pwd", Input).value
                )
            except Exception:
                pass

            from core.web_service import WebServiceManager
            mgr = WebServiceManager(self.app.paths.root)
            s = self.app.settings

            if action == "install":
                from core.web_service import validate_port
                port = s.web_port or 8080
                # WSL without systemd: only configure wsl.conf, then guide restart
                if mgr._is_wsl() and not mgr.systemd_available():
                    self._log("[bold]Configuring WSL for systemd...[/bold]")
                    mgr.install_wsl_systemd_only(password=password, log=self._log)
                    self.app.call_from_thread(self._show_status)
                    return
                err = validate_port(port)
                if err:
                    self._log(f"[red]{err}[/red]")
                    return
                self._log(f"[bold]Installing service...[/bold]")
                success = mgr.install(
                    host=s.web_host or "0.0.0.0",
                    port=s.web_port or 8080,
                    dns_name=s.web_dns_name,
                    ssl=s.web_ssl,
                    ssl_cert=s.web_ssl_cert,
                    ssl_key=s.web_ssl_key,
                    auth_user=s.web_auth_user,
                    auth_password=s.web_auth_password,
                    logs_dir=self.app.paths.logs_dir,
                    organize_logs=s.organize_logs,
                    password=password,
                    theme=s.theme,
                    language=s.language,
                    log=self._log,
                )
                if success:
                    # Auto-start after install
                    mgr.start(log=self._log)
            elif action == "remove":
                self._log(f"[bold]Removing service...[/bold]")
                mgr.uninstall(password=password, log=self._log)

            # Refresh status display
            self._log("")
            self.app.call_from_thread(self._show_status)

        except Exception:
            import traceback
            self._log(f"[red]{traceback.format_exc()}[/red]")
        finally:
            self._busy = False
