"""Main Textual application."""

from textual.app import App, ComposeResult
from textual.binding import Binding

from core.paths import Paths
from ui.screens.main import MainScreen


class S4App(App):
    """Simplebuild4 TUI application."""

    TITLE = "Simplebuild4"
    CSS_PATH = "theme.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+p", "command_palette", "Commands"),
    ]

    def __init__(self, paths: Paths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths

    def on_mount(self) -> None:
        self.paths.ensure_dirs()
        self.push_screen(MainScreen())
