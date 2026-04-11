"""Dependency check modal — scan and install missing system packages."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, RichLog, Button, Input

from translation import t
from ui.icons import icon


class DependencyCheckModal(ModalScreen[None]):
    """Modal that checks system dependencies and offers to install missing ones."""

    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    DependencyCheckModal {
        align: center middle;
    }
    #dep-box {
        width: 110;
        height: 34;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }
    #dep-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #dep-log {
        height: 1fr;
        background: $boost;
        border: solid $accent;
        border-title-color: $text;
        border-title-style: bold;
        overflow-y: auto;
    }
    #dep-log:focus {
        border: heavy $accent;
    }
    #dep-pwd-row {
        height: 3;
        margin-top: 1;
    }
    #dep-pwd-row Input {
        width: 1fr;
    }
    #dep-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    #dep-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self, deps_yaml: Path) -> None:
        super().__init__()
        self._deps_yaml = deps_yaml
        self._check_running = False
        self._has_missing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dep-box"):
            yield Static(f"[bold]{t('dep_check_title')}[/bold]", id="dep-title")
            log = RichLog(id="dep-log", highlight=False, markup=True, wrap=True)
            log.border_title = t("output_title")
            yield log
            _eye_btn = Button(icon("eye"), id="dep-eye", classes="tc-reset-btn")
            _eye_btn.tooltip = t("tt_show_password")
            with Horizontal(id="dep-pwd-row"):
                yield Static(f" {t('dep_enter_root_pwd')}", classes="s-label-tall")
                yield Input(placeholder=t("dep_enter_root_pwd"), id="dep-pwd", password=True)
                yield _eye_btn
            with Horizontal(id="dep-buttons"):
                yield Button(t("dep_check_btn"), id="dep-check", variant="primary")
                yield Button(t("dep_install_btn"), id="dep-install", variant="warning", disabled=True)
                yield Button(t("cancel"), id="dep-close")

    def on_mount(self) -> None:
        self._log_widget = self.query_one("#dep-log", RichLog)
        self._log_widget.write(f"[dim]{t('dep_press_check')}[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dep-close":
            self.dismiss(None)
        elif event.button.id == "dep-check":
            self._run_check()
        elif event.button.id == "dep-install":
            self._run_install()
        elif event.button.id == "dep-eye":
            try:
                pw = self.query_one("#dep-pwd", Input)
                pw.password = not pw.password
            except Exception:
                pass

    def action_close(self) -> None:
        self.dismiss(None)

    def _log(self, msg: str) -> None:
        self.app.call_from_thread(self._log_widget.write, msg)

    def _run_check(self) -> None:
        if self._check_running:
            return
        self._check_running = True
        self._log_widget.clear()
        self._log_widget.write(f"[bold]{t('dep_checking')}[/bold]")
        threading.Thread(target=self._do_check, daemon=True).start()

    def _do_check(self) -> None:
        try:
            from core.dep_checker import DependencyChecker
            checker = DependencyChecker(self._deps_yaml, log=self._log)
            pkg_mgr = checker.detect_distro()
            self._log(f"System: {checker._distro_name or checker._distro_id or 'unknown'}")
            self._log(f"Package manager: {pkg_mgr or 'not detected'}")
            self._log("")

            result = checker.check_all()

            # Show installed
            if result["installed_bins"]:
                self._log(f"[green]Binaries OK ({len(result['installed_bins'])}):[/green] {', '.join(result['installed_bins'])}")
            if result["installed_headers"]:
                self._log(f"[green]Headers OK ({len(result['installed_headers'])}):[/green] {', '.join(result['installed_headers'])}")
            self._log("")

            # Show missing
            missing_bins = result["missing_bins"]
            missing_headers = result["missing_headers"]
            total_missing = len(missing_bins) + len(missing_headers)

            if missing_bins:
                self._log(f"[red]Missing binaries ({len(missing_bins)}):[/red]")
                for dep in missing_bins:
                    self._log(f"  [red]✗[/red] {dep['name']} → {dep['package']}")
            if missing_headers:
                self._log(f"[red]Missing headers ({len(missing_headers)}):[/red]")
                for dep in missing_headers:
                    self._log(f"  [red]✗[/red] {dep['name']} → {dep['package']}")

            if total_missing == 0:
                self._log(f"[bold green]{t('dep_all_ok')}[/bold green]")
                self._has_missing = False
            else:
                self._log(f"\n[bold yellow]{t('dep_missing').format(count=total_missing)}[/bold yellow]")
                self._log(f"[dim]{t('dep_enter_root_pwd')}[/dim]")
                self._has_missing = True

            # Enable/disable install button
            self.app.call_from_thread(self._update_install_btn, self._has_missing)

        except Exception:
            import traceback
            self._log(f"[red]{traceback.format_exc()}[/red]")
        finally:
            self._check_running = False

    def _update_install_btn(self, enable: bool) -> None:
        try:
            self.query_one("#dep-install", Button).disabled = not enable
        except Exception:
            pass

    def _run_install(self) -> None:
        if self._check_running:
            return
        self._check_running = True
        self._log_widget.write(f"\n[bold]{t('dep_installing')}[/bold]")
        threading.Thread(target=self._do_install, daemon=True).start()

    def _do_install(self) -> None:
        try:
            password = ""
            try:
                password = self.app.call_from_thread(
                    lambda: self.query_one("#dep-pwd", Input).value
                )
            except Exception:
                pass

            from core.dep_checker import DependencyChecker
            checker = DependencyChecker(self._deps_yaml, log=self._log)
            checker.detect_distro()
            success = checker.install(password=password)

            if success:
                self._log(f"\n[bold green]{t('dep_install_done')}[/bold green]")
                # Re-check to update status
                self._log(f"\n[bold]{t('dep_checking')}[/bold]")
                self._do_check()
            else:
                self._log(f"\n[bold red]{t('dep_install_failed')}[/bold red]")
        except Exception:
            import traceback
            self._log(f"[red]{traceback.format_exc()}[/red]")
        finally:
            self._check_running = False
