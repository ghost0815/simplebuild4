"""Main application entry point."""

import sys

from core.paths import Paths


def main():
    """Launch SimpleBuild4."""
    try:
        from textual.app import App
    except ImportError:
        sys.exit("s4 requires textual. Install with: pip install textual pyyaml")

    from ui.app import s4App

    paths = Paths.detect()
    app = s4App(paths=paths)
    app.run()
