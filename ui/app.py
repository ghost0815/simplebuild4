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
        Binding("f1", "command_palette", show=False),
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

    def on_mount(self) -> None:
        self.paths.ensure_dirs()
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
                    f"s4 update available ({behind} new commit{'s' if behind > 1 else ''}) — run ./s4 update",
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
        """Self-update s4 via git pull (handles force pushes)."""
        import subprocess
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        if not (root / ".git").exists():
            self.notify("Not a git repository — use './s4 update' from shell", severity="warning")
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

            ahead = int(subprocess.run(
                ["git", "rev-list", f"origin/{branch}..HEAD", "--count"], cwd=str(root),
                capture_output=True, text=True, timeout=5,
            ).stdout.strip() or "0")

            behind = int(subprocess.run(
                ["git", "rev-list", f"HEAD..origin/{branch}", "--count"], cwd=str(root),
                capture_output=True, text=True, timeout=5,
            ).stdout.strip() or "0")

            if behind == 0 and ahead == 0:
                self.notify("s4 is already up to date")
                return

            if ahead > 0:
                # Force push detected — reset to remote
                result = subprocess.run(
                    ["git", "reset", f"origin/{branch}", "--hard"], cwd=str(root),
                    capture_output=True, text=True, timeout=30,
                )
            else:
                result = subprocess.run(
                    ["git", "pull"], cwd=str(root),
                    capture_output=True, text=True, timeout=30,
                )

            if result.returncode == 0:
                self.notify(f"s4 updated ({behind} new commits) — please restart", severity="warning")
            else:
                self.notify(f"Update failed: {result.stderr.strip()}", severity="error")
        except Exception as e:
            self.notify(f"Update failed: {e}", severity="error")
