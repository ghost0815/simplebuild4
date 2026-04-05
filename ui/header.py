"""Custom s4 header widget."""

from textual.widgets import Static
from textual.containers import Horizontal

VERSION = "2026.04 alpha"
DEVELOPER = "WXbet"
DEDICATION = "in memory of gorgone"
ICON = " s4 "


class s4HeaderIcon(Static):
    """Clickable icon on the left side of the header."""

    DEFAULT_CSS = """
    s4HeaderIcon {
        width: auto;
        height: 1;
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
    }
    s4HeaderIcon:hover {
        background: $primary-lighten-1;
    }
    """

    def render(self) -> str:
        return ICON

    def on_click(self) -> None:
        self.app.action_command_palette()


class s4HeaderBar(Static):
    """Center title and right-aligned version."""

    DEFAULT_CSS = """
    s4HeaderBar {
        width: 1fr;
        height: 1;
        background: $primary;
        color: $text;
        text-style: bold;
    }
    """

    def render(self) -> str:
        width = self.size.width
        center = f"SimpleBuild4 by {DEVELOPER} - {DEDICATION} -"
        version = f"v{VERSION} "
        total = len(center) + len(version)
        if width <= total + 2:
            return f" {center} {version}"
        padding = width - total
        left_pad = padding // 2
        right_pad = padding - left_pad
        return f"{' ' * left_pad}{center}{' ' * right_pad}{version}"


class s4Header(Horizontal):
    """Composite header with icon, title, and version."""

    DEFAULT_CSS = """
    s4Header {
        dock: top;
        width: 100%;
        height: 1;
        margin-bottom: 1;
    }
    """

    def compose(self):
        yield s4HeaderIcon()
        yield s4HeaderBar()
