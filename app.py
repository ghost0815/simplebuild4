"""Main application entry point."""

import sys

from core.paths import Paths


def main():
    """Launch SimpleBuild4."""
    try:
        from textual.app import App
    except ImportError:
        sys.exit("s4 requires textual. Install with: pip install textual pyyaml")

    from ui.app import S4App

    paths = Paths.detect()
    app = S4App(paths=paths)
    app.run()

    # Stop web service subprocess if it was running
    try:
        app._web_stop()
    except Exception:
        pass

    # Restore terminal state after Textual exits — prevents invisible
    # input if the app crashed or exited uncleanly from raw mode.
    try:
        import subprocess
        subprocess.run(["stty", "sane"], stderr=subprocess.DEVNULL)
    except Exception:
        pass


if __name__ == "__main__":
    main()
