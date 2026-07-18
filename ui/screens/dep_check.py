"""Dependency check modal — scan and install missing system packages."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, RichLog, Button, Input
from ui.widgets.artifact_richlog import ArtifactRichLog

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
        padding: 0 1;
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
            log = ArtifactRichLog(id="dep-log", highlight=False, markup=True, wrap=True)
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

    def _detect_wsl(self) -> str:
        """Return ``WSL 2``, ``WSL 1`` or ``""``.

        WSL 2 ships a real Linux kernel and tags itself in
        ``/proc/sys/kernel/osrelease`` (suffix ``-WSL2`` or
        ``microsoft-standard-WSL2``). WSL 1 only adds ``Microsoft``
        to the kernel string.
        """
        try:
            with open("/proc/sys/kernel/osrelease", "r", encoding="utf-8") as f:
                osrel = f.read().lower()
        except OSError:
            return ""
        if "wsl2" in osrel:
            return "WSL 2"
        if "microsoft" in osrel:
            return "WSL 1"
        return ""

    def _do_check(self) -> None:
        try:
            from core.dep_checker import DependencyChecker
            import sys as _sys
            checker = DependencyChecker(self._deps_yaml, log=self._log)
            pkg_mgr = checker.detect_distro()
            _pyver = ".".join(str(x) for x in _sys.version_info[:3])
            sys_label = checker._distro_name or checker._distro_id or "unknown"
            wsl = self._detect_wsl()
            if wsl:
                sys_label += f" ({wsl})"
            self._log(f"System: {sys_label}")
            self._log(f"Python: {_pyver} at {_sys.executable}")
            self._log(f"Package manager: {pkg_mgr or 'not detected'}")
            if wsl == "WSL 1":
                self._log(
                    "[yellow]Warning: WSL 1 detected — many cross-compile flows "
                    "(file watches, mount perms, networking) misbehave. Switch "
                    "to WSL 2:[/yellow]\n"
                    "  [dim]PowerShell (admin): [/dim]wsl --set-version <DistroName> 2\n"
                    "  [dim]Verify:            [/dim]wsl -l -v"
                )
            self._log("")

            result = checker.check_all()

            # Show installed
            if result["installed_bins"]:
                self._log(f"[green]Binaries OK ({len(result['installed_bins'])}):[/green] {', '.join(result['installed_bins'])}")
            if result["installed_headers"]:
                self._log(f"[green]Headers OK ({len(result['installed_headers'])}):[/green] {', '.join(result['installed_headers'])}")
            self._log("")

            # Python modules
            installed_py = result.get("installed_python", [])
            missing_py = result.get("missing_python", [])
            outdated_py = result.get("outdated_python", [])

            # Best-effort PyPI update check (batched in one pip call,
            # 20 s timeout). Annotates the OK list with "→ latest"
            # whenever a newer release is available so the user knows
            # whether clicking Install would actually upgrade something.
            self._log("[dim]Checking PyPI for updates ...[/dim]")
            updates = checker.check_python_updates()

            if installed_py:
                items = []
                for e in installed_py:
                    label = f"{e['name']} {e['installed_version']}".rstrip()
                    latest = updates.get((e['pip_name'] or e['name']).lower(), "")
                    if latest and latest != e['installed_version']:
                        label += f" [yellow]→ {latest}[/yellow]"
                    items.append(label)
                self._log(f"[green]Python OK ({len(installed_py)}):[/green] {', '.join(items)}")
            updates_available = sum(
                1 for e in installed_py
                if updates.get((e['pip_name'] or e['name']).lower(), "")
                and updates[(e['pip_name'] or e['name']).lower()] != e['installed_version']
            )
            if updates_available:
                self._log(f"[yellow]{updates_available} Python module(s) have updates available — click Install to upgrade.[/yellow]")

            # Show missing
            missing_bins = result["missing_bins"]
            missing_headers = result["missing_headers"]
            # Python: only count non-optional as 'missing' for the install gate
            need_py_install = [e for e in missing_py + outdated_py if not e.get("optional")]
            total_missing = len(missing_bins) + len(missing_headers) + len(need_py_install)

            if missing_bins:
                self._log(f"[red]Missing binaries ({len(missing_bins)}):[/red]")
                for dep in missing_bins:
                    self._log(f"  [red]✗[/red] {dep['name']} → {dep['package']}")
            if missing_headers:
                self._log(f"[red]Missing headers ({len(missing_headers)}):[/red]")
                for dep in missing_headers:
                    self._log(f"  [red]✗[/red] {dep['name']} → {dep['package']}")
            if need_py_install:
                self._log(f"[red]Missing/outdated Python modules ({len(need_py_install)}):[/red]")
                for e in need_py_install:
                    detail = e.get("reason") or "not installed"
                    self._log(f"  [red]✗[/red] {e['name']} ({e['pip_name']}) — {detail}")
            optional_missing = [e for e in missing_py if e.get("optional")]
            if optional_missing:
                self._log(f"[dim]Optional Python modules not installed:[/dim] "
                          + ", ".join(e['name'] for e in optional_missing))

            if total_missing == 0 and not updates_available:
                self._log(f"[bold green]{t('dep_all_ok')}[/bold green]")
                self._has_missing = False
            else:
                if total_missing > 0:
                    self._log(f"\n[bold yellow]{t('dep_missing').format(count=total_missing)}[/bold yellow]")
                    if missing_bins or missing_headers:
                        self._log(f"[dim]{t('dep_enter_root_pwd')}[/dim]")
                    if need_py_install:
                        self._log(f"[dim]Python modules install as your user (no root password needed).[/dim]")
                if updates_available:
                    self._log(f"[dim]Click Install to upgrade {updates_available} Python module(s).[/dim]")
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

            # Step 1: system packages (needs root only if there's anything to install)
            pre = checker.check_all()
            sys_targets = pre["missing_bins"] + pre["missing_headers"]
            sys_ok = True
            if sys_targets or pre.get("meta_needed"):
                sys_ok = checker.install(password=password)

            # Step 2: Python modules — always in user context, no
            # password needed. install_python() will pick up missing,
            # outdated, AND installed-but-newer-on-PyPI in one go;
            # if nothing needs doing, it logs "All up to date" and
            # returns immediately.
            self._log("")
            py_ok = checker.install_python()

            success = sys_ok and py_ok

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
