"""Main Textual application."""

from textual.app import App, ComposeResult
from textual.binding import Binding

from core.paths import Paths
from core.settings import Settings
from ui.commands import S4Commands


class S4App(App):
    """SimpleBuild4 TUI application."""

    TITLE = "SimpleBuild4"
    CSS_PATH = "theme.tcss"

    ENABLE_COMMAND_PALETTE = True
    COMMANDS = App.COMMANDS | {S4Commands}

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
        Binding("f10", "add", show=False),
        Binding("f11", "remove", show=False),
    ]

    def __init__(self, paths: Paths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self.settings = Settings.load(paths.root / "data" / "settings.yaml")

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
            result = subprocess.run(
                ["git", "rev-list", "HEAD..origin/" + subprocess.run(
                    ["git", "branch", "--show-current"], cwd=str(root),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip(), "--count"],
                cwd=str(root), capture_output=True, text=True, timeout=5,
            )
            behind = int(result.stdout.strip() or "0")
            if behind > 0:
                self.call_from_thread(
                    self.notify,
                    f"s4 update available ({behind} new commit{'s' if behind > 1 else ''}) — press F1 → s4 update",
                    severity="information",
                    timeout=10,
                )
            else:
                self.call_from_thread(
                    self.notify,
                    "s4 is up to date",
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

    def action_select_all(self) -> None:
        """Select all text in focused widget via Ctrl+A."""
        widget = self.focused
        if widget and hasattr(widget, 'text_select_all'):
            widget.text_select_all()

    def action_s4_settings(self) -> None:
        from ui.screens.settings import SettingsScreen
        if not isinstance(self.screen, SettingsScreen):
            self.push_screen(SettingsScreen())

    def action_save_action(self) -> None:
        """Dispatch F12 Save to the current screen."""
        screen = self.screen
        if hasattr(screen, 'do_save'):
            msg = screen.do_save()
            self.notify(msg or "Saved")

    def action_build(self) -> None:
        """Dispatch F5 Build to main screen."""
        screen = self.screen
        if hasattr(screen, 'action_build'):
            screen.action_build()

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

    def action_add(self) -> None:
        """Dispatch F10 Add to the current screen."""
        screen = self.screen
        if hasattr(screen, 'do_add'):
            screen.do_add()

    def action_remove(self) -> None:
        """Dispatch F11 Remove to the current screen."""
        screen = self.screen
        if hasattr(screen, 'do_remove'):
            screen.do_remove()

    def action_s4_update(self) -> None:
        """Self-update s4 via git pull (handles force pushes). Output to main screen RichLog."""
        import subprocess
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent

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
                    self.notify("s4 is already up to date")
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
                    self.notify(f"s4 updated ({behind} new commits) — please restart", severity="warning")
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
                self.notify("s4 updated — please restart", severity="warning")
                self.notify(f"Update failed: {result.stderr.strip()}", severity="error")
        except Exception as e:
            _log(f"[red]Update failed: {e}[/red]")
            self.notify(f"Update failed: {e}", severity="error")
        finally:
            _log(f"Log: {log_path}")
            log_file.close()
