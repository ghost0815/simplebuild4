"""crosstool-NG customize dialog for web sessions (textual-serve).

In a web session, App.suspend() is not supported so the classic F6 path
(``_sp.run(['ct-ng', 'menuconfig'])`` under ``with app.suspend():``)
fails. Instead we open menuconfig in a separate browser tab, streamed
by ``/term/*`` → ttyd. This modal orchestrates that:

  1. On mount → open the /term/open?cmd=ctng-menuconfig URL in a new
     tab and start a polling timer.
  2. Every 2s: hash the working ``.config`` on disk. As soon as it
     differs from ``hash_before``, dismiss(True) — the caller then
     runs the same fork-to-local + save flow as the TTY path.
  3. If nothing changes for 5 minutes, dismiss(False) with a "timed
     out" notification — the user probably wandered off.
  4. Manual controls: [Reopen tab] respawns the menuconfig tab (useful
     if the user closed it), [Discard] closes without applying.
"""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Callable

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from translation import t

# Kconfig's ncurses menuconfig binary. Present in ctng's kconfig/mconf
# during the interactive session, gone once the user quits. Match by
# exact process name so we don't accidentally hit the ttyd parent
# (which has "ct-ng menuconfig" in its own argv and would falsely read
# as "running" the entire container lifetime).
_PROC_NAME = "mconf"

# ttyd binds loopback-only in the docker entrypoint. In other deployments
# (native s4-web on a laptop without ttyd wired up) this port is refused;
# we detect that upfront so the user doesn't get a confusingly hanging
# tab and a 30 s timeout for nothing.
_TTYD_HOST = "127.0.0.1"
_TTYD_PORT = 7681


def _ttyd_reachable() -> bool:
    try:
        with socket.create_connection((_TTYD_HOST, _TTYD_PORT), timeout=0.5):
            return True
    except OSError:
        return False


class CtngWebCustomizeModal(ModalScreen[bool]):
    """Modal shown when F6 runs inside a textual-serve web session.

    Dismisses True when a .config change was detected (caller should
    apply), False on Discard, ESC or timeout (caller should do nothing).
    """

    DEFAULT_CSS = """
    CtngWebCustomizeModal {
        align: center middle;
    }
    CtngWebCustomizeModal > Vertical {
        width: 70;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }
    #ctng-web-title { text-style: bold; padding-bottom: 1; }
    #ctng-web-body { padding-bottom: 1; color: $text-muted; }
    #ctng-web-btns { height: 3; align-horizontal: right; }
    #ctng-web-btns Button { margin-left: 1; }
    """

    BINDINGS = [("escape", "discard", "Discard")]

    # Relative URL — resolves against the textual-serve page URL. This
    # keeps the reverse-proxy prefix (e.g. /s4/web/) transparent: when
    # the app is served at https://host/s4/web/, browser opens
    # https://host/s4/web/term/open?...; the textual app itself has no
    # knowledge of the prefix.
    URL_PATH = "term/open?cmd=ctng-menuconfig"

    # Poll pgrep every _POLL_INTERVAL s; wait up to _START_TIMEOUT for
    # the process to appear (browser tab not opened yet, ttyd handshake
    # in progress); after we have seen it running, allow up to _TIMEOUT
    # of total dialog lifetime for the user to finish.
    _POLL_INTERVAL = 2.0
    _START_TIMEOUT = 30.0
    _TIMEOUT = 300.0  # 5 minutes total

    _STATE_WAITING = "waiting_for_start"
    _STATE_RUNNING = "running"

    def __init__(
        self,
        working_cfg: Path,
        hash_before: str,
        hash_fn: Callable[[Path], str],
    ) -> None:
        super().__init__()
        self._working_cfg = working_cfg
        self._hash_before = hash_before
        self._hash_fn = hash_fn
        self._timer = None
        self._elapsed = 0.0
        self._state = self._STATE_WAITING

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(t("customize_web_title"), id="ctng-web-title")
            yield Static(t("customize_web_body"), id="ctng-web-body")
            with Horizontal(id="ctng-web-btns"):
                yield Button(t("customize_web_reopen"), id="reopen", variant="default")
                yield Button(t("customize_web_discard"), id="discard", variant="warning")

    def on_mount(self) -> None:
        # ttyd is required — bail out with a clear notification if the
        # loopback endpoint isn't there. Prevents a hanging tab + 30 s
        # start-timeout on deployments that ship s4-web without ttyd.
        if not _ttyd_reachable():
            self.notify(t("customize_web_unavailable"), severity="warning", timeout=6)
            self.dismiss(False)
            return
        # Open the menuconfig tab immediately.
        self._open_tab()
        # Start polling; each tick advances _elapsed and checks the hash.
        self._timer = self.set_interval(self._POLL_INTERVAL, self._tick)

    def _open_tab(self) -> None:
        try:
            self.app.open_url(self.URL_PATH, new_tab=True)
        except Exception as e:
            self.notify(f"Could not open browser tab: {e}", severity="error")

    def _process_alive(self) -> bool:
        """True iff an mconf (menuconfig) process is currently running."""
        try:
            r = subprocess.run(
                ["pgrep", "-x", _PROC_NAME],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _tick(self) -> None:
        self._elapsed += self._POLL_INTERVAL
        alive = self._process_alive()

        if self._state == self._STATE_WAITING:
            if alive:
                self._state = self._STATE_RUNNING
                return
            # menuconfig failed to start within the grace period → give up
            if self._elapsed >= self._START_TIMEOUT:
                self._stop_timer()
                self.notify(t("customize_web_timeout"), severity="warning", timeout=8)
                self.dismiss(False)
            return

        # _STATE_RUNNING
        if not alive:
            # Process ended — user exited menuconfig. Hash-check to decide
            # whether they actually saved something.
            self._stop_timer()
            try:
                current = self._hash_fn(self._working_cfg)
            except Exception:
                current = self._hash_before
            self.dismiss(current != self._hash_before)
            return
        if self._elapsed >= self._TIMEOUT:
            self._stop_timer()
            self.notify(t("customize_web_timeout"), severity="warning", timeout=8)
            self.dismiss(False)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reopen":
            # Reset state + timeout window when the user manually respawns
            # the tab — a new ttyd subshell needs to appear.
            self._elapsed = 0.0
            self._state = self._STATE_WAITING
            self._open_tab()
        elif event.button.id == "discard":
            self._stop_timer()
            self.dismiss(False)

    def action_discard(self) -> None:
        self._stop_timer()
        self.dismiss(False)
