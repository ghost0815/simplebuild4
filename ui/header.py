"""Custom s4 header widget."""

import subprocess
from textual.widgets import Static
from textual.containers import Horizontal

VERSION = "2026.04 alpha"


def _get_version() -> str:
    """Build version string: sha@branch 2026.04.337 alpha."""
    base, _, suffix = VERSION.partition(" ")
    try:
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if count and sha:
            return f"{sha}@{branch} {base}.{count} {suffix}".strip()
    except Exception:
        pass
    # Fallback like s3: sum of ASCII digits from version string
    non_git = sum(int(c) for c in base if c.isdigit())
    return f"{base}.{non_git}_non-git {suffix}".strip()
DEVELOPER = "WXbet"
DEDICATION = "in memory of gorgone"
ICON = " s4 "


class S4HeaderIcon(Static):
    """Clickable icon on the left side of the header."""

    DEFAULT_CSS = """
    S4HeaderIcon {
        width: auto;
        height: 1;
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
    }
    S4HeaderIcon:hover {
        background: $primary-lighten-1;
    }
    """

    def render(self) -> str:
        return ICON

    def on_click(self) -> None:
        self.app.action_command_palette()


class S4HeaderBar(Static):
    """Center title."""

    DEFAULT_CSS = """
    S4HeaderBar {
        width: 1fr;
        height: 1;
        background: $primary;
        color: $text;
        text-style: bold;
        content-align: center middle;
    }
    """

    def render(self) -> str:
        return f"SimpleBuild4 by {DEVELOPER} \u2014 {DEDICATION} \u2014"


class S4HeaderVersion(Static):
    """Right-aligned clickable version — opens update modal."""

    DEFAULT_CSS = """
    S4HeaderVersion {
        width: auto;
        height: 1;
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
        padding: 0 1;
    }
    S4HeaderVersion:hover {
        background: $primary-lighten-1;
    }
    """

    def render(self) -> str:
        return _get_version()

    def on_click(self) -> None:
        self.app.action_s4_update_modal()


class S4Header(Horizontal):
    """Composite header with icon, title, and version."""

    DEFAULT_CSS = """
    S4Header {
        dock: top;
        width: 100%;
        height: 1;
        margin-bottom: 1;
    }
    """

    def compose(self):
        yield S4HeaderIcon()
        yield S4HeaderBar()
        yield S4HeaderVersion()
