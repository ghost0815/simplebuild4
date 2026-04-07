"""Main Textual application."""

from textual.app import App, ComposeResult
from textual.binding import Binding

from core.paths import Paths
from core.settings import Settings
from translation import t
from ui.commands import S4Commands


class S4App(App):
    """SimpleBuild4 TUI application."""

    TITLE = "SimpleBuild4"
    CSS_PATH = "theme.tcss"

    ENABLE_COMMAND_PALETTE = True
    # Single provider with deterministic ordering (s4 commands first, then system)
    COMMANDS = {S4Commands}

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False),
        Binding("escape", "esc", show=False),
        Binding("ctrl+c", "copy_text", show=False, priority=True),
        Binding("ctrl+a", "select_all", show=False),
        Binding("ctrl+p", "command_palette", show=False),
        Binding("f12", "save_action", show=False),
        Binding("f1", "s4_settings", show=False),
        Binding("f2", "open_targets", show=False),
        Binding("f3", "open_repository", show=False),
        Binding("f4", "open_toolchains", show=False),
        Binding("f5", "build", show=False),
        Binding("f6", "customize", show=False),
        Binding("f9", "tc_update", show=False),
        Binding("f10", "add", show=False),
        Binding("f11", "remove", show=False),
    ]

    def __init__(self, paths: Paths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self.settings = Settings.load(paths.root / "data" / "settings.yaml")
        # Build/library-update timer state (read by ui.footer.BuildTimerWidget)
        self._timer_screen: str | None = None
        self._timer_start: float | None = None
        self._timer_stopped_at: float | None = None

    # ---------------------------------------------------------------- timer
    def build_timer_start(self, screen: str) -> None:
        """Start (or restart) the build timer for the given screen."""
        import time
        self._timer_screen = screen
        self._timer_start = time.monotonic()
        self._timer_stopped_at = None
        self._refresh_build_timer()

    def build_timer_stop(self) -> None:
        """Freeze the timer at the current elapsed time."""
        import time
        if self._timer_start is not None and self._timer_stopped_at is None:
            self._timer_stopped_at = time.monotonic()
        self._refresh_build_timer()

    def build_timer_reset(self) -> None:
        """Clear the timer entirely."""
        self._timer_screen = None
        self._timer_start = None
        self._timer_stopped_at = None
        self._refresh_build_timer()

    def build_timer_elapsed(self) -> float | None:
        """Return current elapsed seconds, or None if no timer."""
        if self._timer_start is None:
            return None
        import time
        end = self._timer_stopped_at if self._timer_stopped_at is not None else time.monotonic()
        return max(0.0, end - self._timer_start)

    def build_timer_format(self) -> str:
        """Format current elapsed time as HH:MM:SS / MM:SS, or '' if no timer."""
        e = self.build_timer_elapsed()
        if e is None:
            return ""
        h = int(e // 3600)
        m = int((e % 3600) // 60)
        s = int(e % 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _refresh_build_timer(self) -> None:
        try:
            from ui.footer import BuildTimerWidget
            for w in self.query(BuildTimerWidget):
                w.refresh_display()
        except Exception:
            pass

    @property
    def settings_path(self) -> "Path":
        from pathlib import Path
        return self.paths.root / "data" / "settings.yaml"

    def watch_theme(self, theme: str) -> None:
        """Save theme to settings when changed via command palette."""
        if hasattr(self, 'settings') and self.settings.theme != theme:
            self.settings.theme = theme
            self.settings.save(self.settings_path)

    def on_mount(self) -> None:
        self.paths.ensure_dirs()
        # Load translations
        from translation import load as load_translations
        load_translations(self.settings.language)
        # Restore saved theme
        if self.settings.theme:
            self.theme = self.settings.theme
        from ui.screens.main import MainScreen
        self.push_screen(MainScreen())

        # Check for updates in background
        if self.settings.update_check:
            import threading
            threading.Thread(target=self._check_for_updates, daemon=True).start()

    def _check_for_updates(self) -> None:
        """Background check: git fetch and compare local vs remote."""
        import subprocess
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        if not (root / ".git").exists():
            return
        try:
            subprocess.run(
                ["git", "fetch", "--prune"], cwd=str(root),
                capture_output=True, text=True, timeout=15,
            )
            branch = subprocess.run(
                ["git", "branch", "--show-current"], cwd=str(root),
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            result = subprocess.run(
                ["git", "rev-list", f"HEAD..origin/{branch}", "--count"],
                cwd=str(root), capture_output=True, text=True, timeout=5,
            )
            behind = int(result.stdout.strip() or "0")
            if behind > 0:
                # Collect the actual commit subjects so the user sees what
                # would be applied. Cap to keep the toast readable.
                commits = subprocess.run(
                    ["git", "log", f"HEAD..origin/{branch}",
                     "--pretty=format:%h %s", "--no-merges"],
                    cwd=str(root), capture_output=True, text=True, timeout=5,
                ).stdout.strip().splitlines()
                max_show = 10
                shown = commits[:max_show]
                lines = [f"  • {c}" for c in shown]
                if len(commits) > max_show:
                    lines.append(f"  … +{len(commits) - max_show} more")
                header = t("s4_update_available", count=behind, s="s" if behind > 1 else "")
                msg = f"[bold]{header}[/bold]\n\n" + "\n".join(lines)
                self.call_from_thread(
                    self.notify,
                    msg,
                    severity="information",
                    timeout=10,
                )
            else:
                self.call_from_thread(
                    self.notify,
                    t("s4_up_to_date"),
                    timeout=3,
                )
        except Exception:
            pass

    def action_copy_text(self) -> None:
        """Copy selected text to clipboard via Ctrl+C."""
        selection = self.screen.get_selected_text()
        if selection:
            self.copy_to_clipboard(selection)
            self.notify("Copied to clipboard")

    def action_show_help_panel(self) -> None:
        """Override Textual's default Keys to show our keyboard reference."""
        self.action_show_keys()

    def action_show_license(self) -> None:
        """Show COPYING license file in a modal."""
        from textual.screen import ModalScreen
        from textual.widgets import Static
        from textual.containers import VerticalScroll
        from textual.binding import Binding
        from pathlib import Path

        copying = Path(__file__).resolve().parent.parent / "COPYING"
        try:
            content = copying.read_text(encoding="utf-8", errors="replace")
        except Exception:
            self.notify("COPYING file not found", severity="error")
            return

        class LicenseScreen(ModalScreen):
            BINDINGS = [Binding("escape", "dismiss", show=False)]
            DEFAULT_CSS = """
            LicenseScreen {
                align: center middle;
            }
            LicenseScreen > VerticalScroll {
                width: 80;
                height: 85%;
                border: solid $primary;
                background: $surface;
                padding: 1 2;
            }
            """
            def compose(self):
                with VerticalScroll():
                    yield Static(f"[bold]License[/bold]  [dim](Esc to close)[/dim]\n\n{content}")

        self.push_screen(LicenseScreen())

    def action_show_keys(self) -> None:
        """Show keyboard shortcuts help."""
        from textual.screen import ModalScreen
        from textual.widgets import Static
        from textual.containers import VerticalScroll
        from textual.binding import Binding

        class KeysScreen(ModalScreen):
            BINDINGS = [
                Binding("escape", "dismiss", show=False),
                Binding("ctrl+p", "dismiss", show=False),
            ]
            DEFAULT_CSS = """
            KeysScreen {
                align: center middle;
            }
            KeysScreen > VerticalScroll {
                width: 80;
                height: 85%;
                border: solid $primary;
                background: $surface;
                padding: 1 2;
            }
            """
            def compose(self):
                with VerticalScroll():
                    yield Static(HELP_TEXT)

        from translation import t as _t
        K = 20  # key column width
        D = 40  # description column width
        def _r(key, desc):
            return f"  {key:<{K}}{desc}"

        HELP_TEXT = f"""\
[bold]{_t('keys_title')}[/bold]  [dim]({_t('keys_esc_close')})[/dim]

[bold]{_t('keys_navigation')}[/bold]
{_r('Tab / Shift+Tab', _t('keys_next_prev'))}
{_r('Arrow Up/Down', _t('keys_nav_lists'))}
{_r('Enter', _t('keys_activate'))}
{_r('Space', _t('keys_toggle_open'))}
{_r('Escape', _t('keys_close_back'))}

[bold]{_t('keys_text_input')}[/bold]
{_r('Backspace', _t('keys_delete_char'))}
{_r('Ctrl+A', _t('keys_select_all'))}
{_r('Ctrl+C', _t('keys_copy'))}
{_r('Home / End', _t('keys_jump_field'))}

[bold]{_t('keys_dropdowns')}[/bold]
{_r('Space / Enter', _t('keys_open_dropdown'))}
{_r('Arrow Up/Down', _t('keys_nav_options'))}
{_r('Enter', _t('keys_select_option'))}
{_r('Escape', _t('keys_close_no_select'))}

[bold]{_t('keys_switches')}[/bold]
{_r('Space / Enter', _t('keys_toggle'))}

[bold]{_t('keys_tables')}[/bold]
{_r('Arrow Up/Down', _t('keys_select_row'))}
{_r('Home / End', _t('keys_first_last'))}
{_r('Page Up/Down', _t('keys_scroll_page'))}

[bold]{_t('keys_collapsible')}[/bold]
{_r('Enter / Space', _t('keys_expand_collapse'))}

[bold]{_t('keys_scrolling')}[/bold]
{_r('Arrow Up/Down', _t('keys_scroll_content'))}
{_r('Page Up/Down', _t('keys_scroll_page'))}
{_r('Mouse wheel', _t('keys_scroll'))}

[bold]{_t('keys_screens')}[/bold]
{_r('F1', _t('settings_nav'))}
{_r('F2', _t('targets_nav') + ' (Main)')}
{_r('F3', _t('repositories_nav'))}
{_r('F4', _t('toolchains_nav'))}
{_r('Esc', _t('keys_back_quit'))}

[bold]{_t('keys_tabs')}[/bold]
  Main                [bold]C[/bold]ompact  [bold]O[/bold]utput
  Target              [bold]G[/bold]eneral  [bold]R[/bold]epo/Toolchain  [bold]O[/bold]ptions  [bold]U[/bold]pload
  Toolchain left      [bold]P[/bold]reBuild  [bold]S[/bold]elfBuild
  Toolchain right     [bold]D[/bold]etails  [bold]L[/bold]ibraries  [bold]O[/bold]utput

[bold]{_t('keys_actions')}[/bold]
{_r('F5', _t('keys_build_cancel'))}
{_r('F9', _t('keys_update_cancel'))}
{_r('F10', _t('keys_add'))}
{_r('F11', _t('keys_remove'))}
{_r('F12', _t('keys_save'))}
{_r('Ctrl+P', _t('keys_cmd_palette'))}

[bold]{_t('keys_cli')}[/bold]
  ./s4 build <target> [--make|--cmake] [OPTION_FLAG OPTION_FLAG_off ...]
{_r('./s4 targets', _t('keys_list_targets'))}
{_r('./s4 version', _t('keys_show_version'))}
{_r('./s4 update', _t('keys_self_update'))}"""

        self.push_screen(KeysScreen())

    def action_select_all(self) -> None:
        """Select all text in focused widget via Ctrl+A."""
        widget = self.focused
        if widget and hasattr(widget, 'text_select_all'):
            widget.text_select_all()

    def action_s4_settings(self) -> None:
        from ui.screens.settings import SettingsScreen
        if not isinstance(self.screen, SettingsScreen):
            self.push_screen(SettingsScreen())

    def _footer_disabled(self, key: str) -> bool:
        """Return True if the footer item for the given F-key is currently disabled.

        Only the current screen's footer is consulted — other screens on the
        stack would otherwise pollute the result.
        """
        try:
            from ui.footer import FooterItem
            for item in self.screen.query(FooterItem):
                if item._key == key:
                    return bool(item._disabled)
        except Exception:
            pass
        return False

    def action_save_action(self) -> None:
        """Dispatch F12 Save to the current screen."""
        if self._footer_disabled("F12"):
            return
        screen = self.screen
        if hasattr(screen, 'do_save'):
            msg = screen.do_save()
            self.notify(msg or "Saved")

    def action_build(self) -> None:
        """Dispatch F5 Build to main screen."""
        if self._footer_disabled("F5"):
            return
        screen = self.screen
        if hasattr(screen, 'action_build'):
            screen.action_build()

    def action_customize(self) -> None:
        """Dispatch F6 Customize to the current screen (Toolchains SelfBuild)."""
        if self._footer_disabled("F6"):
            return
        screen = self.screen
        if hasattr(screen, 'action_customize'):
            screen.action_customize()

    def action_esc(self) -> None:
        """Esc: quit on main, back on sub-screens."""
        from ui.screens.main import MainScreen
        if isinstance(self.screen, MainScreen):
            self.exit()
        else:
            screen = self.screen
            if hasattr(screen, 'action_go_back'):
                screen.action_go_back()
            else:
                self.pop_screen()

    def action_open_targets(self) -> None:
        from ui.screens.main import MainScreen
        if not isinstance(self.screen, MainScreen):
            # Pop back to main screen
            while not isinstance(self.screen, MainScreen):
                self.pop_screen()

    def action_open_repository(self) -> None:
        from ui.screens.repository import RepositoryScreen
        if not isinstance(self.screen, RepositoryScreen):
            self.push_screen(RepositoryScreen())

    def action_open_toolchains(self) -> None:
        from ui.screens.toolchains import ToolchainsScreen
        if not isinstance(self.screen, ToolchainsScreen):
            self.push_screen(ToolchainsScreen())

    def action_tc_update(self) -> None:
        """Dispatch F9 Update to toolchains screen."""
        if self._footer_disabled("F9"):
            return
        screen = self.screen
        if hasattr(screen, 'do_tc_update'):
            screen.do_tc_update()

    def action_add(self) -> None:
        """Dispatch F10 Add to the current screen."""
        if self._footer_disabled("F10"):
            return
        screen = self.screen
        if hasattr(screen, 'do_add'):
            screen.do_add()

    def action_remove(self) -> None:
        """Dispatch F11 Remove to the current screen."""
        if self._footer_disabled("F11"):
            return
        screen = self.screen
        if hasattr(screen, 'do_remove'):
            screen.do_remove()

    def _check_busy_or_dirty(self) -> tuple:
        """Check if any screen has a running operation or actual unsaved changes.

        Returns (busy_screen_name, dirty_screen_name). Either may be empty.

        A screen is only considered dirty if it explicitly implements an
        is_dirty() method that returns True. The previous heuristic
        (any screen with do_save() = dirty) produced too many false
        positives — Settings/Repository/Toolchains/TargetEdit always have
        do_save() but are only actually dirty when the user changed
        something.
        """
        busy = ""
        dirty = ""
        try:
            for screen in self.screen_stack:
                if getattr(screen, "_building", False):
                    busy = type(screen).__name__
                    break
            for screen in self.screen_stack:
                is_dirty = getattr(screen, "is_dirty", None)
                if callable(is_dirty):
                    try:
                        if is_dirty():
                            dirty = type(screen).__name__
                            break
                    except Exception:
                        pass
        except Exception:
            pass
        return (busy, dirty)

    def action_s4_update(self) -> None:
        """Self-update s4 via git pull (handles force pushes). Output to main screen RichLog."""
        import subprocess
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent

        # Check for running operations — block update entirely
        busy, dirty = self._check_busy_or_dirty()
        if busy:
            self.notify(t("update_busy"), severity="warning", timeout=10)
            return
        if dirty:
            self.notify(t("update_dirty_warning"), severity="warning", timeout=15)

        # Track success for auto-restart at the end
        updated_ok = False

        # Setup logging: RichLog + file
        import re
        from datetime import datetime
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.paths.logs_dir / f"{datetime.now().strftime('%Y-%m-%d.%H-%M-%S')}.s4-update.log"
        log_file = open(log_path, "w", encoding="utf-8")

        def _log(msg: str) -> None:
            # Write plain to file
            plain = re.sub(r'\[/?[a-z_ #0-9]+\]', '', msg)
            log_file.write(plain + "\n")
            log_file.flush()
            # Write markup to RichLog
            try:
                from ui.screens.main import MainScreen
                from textual.widgets import RichLog
                screen = self.screen
                if isinstance(screen, MainScreen):
                    screen.query_one("#output", RichLog).write(msg)
            except Exception:
                pass

        has_git = (root / ".git").exists()

        try:
            _log("[bold]s4 self-update[/bold]")
            _log("")

            if has_git:
                # --- Git repo: fetch + pull ---
                _log("Fetching remote...")
                subprocess.run(
                    ["git", "fetch", "--prune"], cwd=str(root),
                    capture_output=True, text=True, timeout=15,
                )
                branch = subprocess.run(
                    ["git", "branch", "--show-current"], cwd=str(root),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()

                ahead = int(subprocess.run(
                    ["git", "rev-list", f"origin/{branch}..HEAD", "--count"], cwd=str(root),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip() or "0")

                behind = int(subprocess.run(
                    ["git", "rev-list", f"HEAD..origin/{branch}", "--count"], cwd=str(root),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip() or "0")

                local_rev = subprocess.run(
                    ["git", "rev-list", "HEAD", "--count"], cwd=str(root),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                local_commit = subprocess.run(
                    ["git", "log", "HEAD", "-n1", "--oneline"], cwd=str(root),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()

                _log(f"Local:  r{local_rev} {local_commit}")

                if behind == 0 and ahead == 0:
                    _log("[green]s4 is up to date[/green]")
                    self.notify(t("s4_up_to_date"))
                    return

                online_rev = subprocess.run(
                    ["git", "rev-list", f"origin/{branch}", "--count"], cwd=str(root),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                online_commit = subprocess.run(
                    ["git", "log", f"origin/{branch}", "-n1", "--oneline"], cwd=str(root),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                _log(f"Online: r{online_rev} {online_commit}")
                _log("")

                changelog = subprocess.run(
                    ["git", "log", f"HEAD..origin/{branch}", "--oneline"], cwd=str(root),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                if changelog:
                    for line in changelog.splitlines():
                        _log(f"  [cyan]{line}[/cyan]")
                    _log("")

                subprocess.run(["git", "checkout", "."], cwd=str(root), capture_output=True, timeout=10)
                subprocess.run(["git", "clean", "-fd"], cwd=str(root), capture_output=True, timeout=10)

                if ahead > 0:
                    _log("Resetting to remote (forced push detected)...")
                    result = subprocess.run(
                        ["git", "reset", f"origin/{branch}", "--hard"], cwd=str(root),
                        capture_output=True, text=True, timeout=30,
                    )
                else:
                    _log("Pulling updates...")
                    result = subprocess.run(
                        ["git", "pull"], cwd=str(root),
                        capture_output=True, text=True, timeout=30,
                    )

                if result.returncode == 0:
                    new_rev = subprocess.run(
                        ["git", "rev-list", "HEAD", "--count"], cwd=str(root),
                        capture_output=True, text=True, timeout=5,
                    ).stdout.strip()
                    new_commit = subprocess.run(
                        ["git", "log", "HEAD", "-n1", "--oneline"], cwd=str(root),
                        capture_output=True, text=True, timeout=5,
                    ).stdout.strip()
                    _log(f"[green]Updated to r{new_rev}: {new_commit}[/green]")
                    updated_ok = True
                else:
                    _log(f"[red]Update failed: {result.stderr.strip()}[/red]")

            else:
                # --- No git repo: clone to temp, copy files over ---
                import shutil
                s4_url = self.settings.s4_url
                _log(f"No git repo — updating from {s4_url}")

                tmp_dir = Path("/tmp/s4_update")
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir)

                _log("Cloning...")
                result = subprocess.run(
                    ["git", "clone", "--depth", "1", s4_url, str(tmp_dir)],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    _log(f"[red]Clone failed: {result.stderr.strip()}[/red]")
                    self.notify("Update failed", severity="error")
                    return

                _log("Updating files...")
                for item in tmp_dir.iterdir():
                    if item.name in ("data", ".git"):
                        continue
                    dest = root / item.name
                    if dest.exists():
                        if dest.is_dir():
                            shutil.rmtree(dest)
                        else:
                            dest.unlink()
                    if item.is_dir():
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)
                    _log(f"  {item.name}")

                shutil.rmtree(tmp_dir)
                _log(f"[green]Updated from {s4_url}[/green]")
                updated_ok = True
        except Exception as e:
            _log(f"[red]Update failed: {e}[/red]")
            self.notify(f"Update failed: {e}", severity="error")
        finally:
            _log(f"Log: {log_path}")
            log_file.close()

        # Auto-restart if update succeeded and no unsaved changes
        if updated_ok:
            if dirty:
                self.notify(t("update_done_manual_restart"), severity="warning", timeout=15)
            else:
                self.notify(t("update_done_restarting"), severity="information", timeout=3)
                self.set_timer(1.5, self._restart_s4)

    def _restart_s4(self) -> None:
        """Restart s4 in-place via os.execv()."""
        import os
        import sys
        try:
            self.exit()
        except Exception:
            pass
        # Replace current process with a fresh s4 invocation
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception:
            pass
