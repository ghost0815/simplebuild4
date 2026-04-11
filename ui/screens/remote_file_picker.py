"""Remote file picker — browse directories on an SSH host."""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import PurePosixPath
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, Input, Button, OptionList
from textual.widgets.option_list import Option

from core.uploader import SSH_OPTS
from translation import t
from ui.icons import icon, is_ascii


class RemoteFilePickerScreen(ModalScreen[str]):
    """Modal that lists remote directories via SSH and lets the user pick a path."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    RemoteFilePickerScreen {
        align: center middle;
    }
    #rfp-box {
        width: 80;
        height: 30;
        border: solid $accent;
        background: $surface;
        padding: 1;
    }
    #rfp-title-row {
        height: 3;
    }
    #rfp-title {
        width: 1fr;
        padding: 1 0;
        text-style: bold;
    }
    .rfp-nav-btn {
        min-width: 10;
        margin: 0 0 0 1;
    }
    #rfp-list {
        height: 1fr;
        border: solid #444444;
    }
    #rfp-path {
        height: 3;
        margin: 1 0 0 0;
    }
    #rfp-buttons {
        height: 3;
        align: right middle;
    }
    #rfp-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self, host: str, user: str = "root", password: str = "",
                 port: int = 22, key: str = "",
                 start_path: str = "/usr/bin",
                 current_value: str = "") -> None:
        super().__init__()
        self._host = host
        self._user = user
        self._password = password
        self._port = port
        self._key = key
        self._start_path = start_path
        self._current_value = current_value
        self._current_dir = start_path
        self._entries: list[tuple[str, bool]] = []  # (name, is_dir)
        self._check_running = False

        # Build SSH base command (shorter timeout for browsing)
        ssh_opts = [o if o != "ConnectTimeout=10" else "ConnectTimeout=5" for o in SSH_OPTS]
        self._ssh_base = ["ssh", "-p", str(port)] + ssh_opts
        if key:
            self._ssh_base += ["-i", key]
        self._ssh_target = f"{user}@{host}"
        self._sshpass: list[str] = []
        if key and password:
            # SSH key with passphrase
            if shutil.which("sshpass"):
                self._sshpass = ["sshpass", "-P", "Enter passphrase", "-p", password]
        elif not key and password:
            # Password auth (no key)
            if shutil.which("sshpass"):
                self._sshpass = ["sshpass", "-p", password]

    def compose(self) -> ComposeResult:
        with Vertical(id="rfp-box"):
            with Horizontal(id="rfp-title-row"):
                yield Static(f"Remote: {self._host}", id="rfp-title")
                _up_icon = "^" if is_ascii() else "▲"
                yield Button(f"{_up_icon} {t('rfp_up')}", id="rfp-up", classes="rfp-nav-btn")
            yield OptionList(id="rfp-list")
            yield Input(value=self._current_value or self._start_path, id="rfp-path")
            with Horizontal(id="rfp-buttons"):
                yield Button(t("cancel"), id="rfp-cancel")
                yield Button(t("rfp_select"), id="rfp-select", variant="primary")

    def on_mount(self) -> None:
        self._list_widget = self.query_one("#rfp-list", OptionList)
        # If current_value has a directory, start there
        if self._current_value:
            parent = str(PurePosixPath(self._current_value).parent)
            if parent and parent != self._current_value:
                self._current_dir = parent
        self._load_dir(self._current_dir)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rfp-cancel":
            self.dismiss("")
        elif event.button.id == "rfp-select":
            path = self.query_one("#rfp-path", Input).value.strip()
            if path:
                self.dismiss(path)
        elif event.button.id == "rfp-up":
            parent = str(PurePosixPath(self._current_dir).parent)
            if parent != self._current_dir:
                self._current_dir = parent
                self._load_dir(parent)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """User double-clicked or pressed enter on an entry."""
        idx = event.option_index
        if idx < 0 or idx >= len(self._entries):
            return
        name, is_dir = self._entries[idx]
        if name == "..":
            parent = str(PurePosixPath(self._current_dir).parent)
            if parent != self._current_dir:
                self._current_dir = parent
                self._load_dir(parent)
            return
        full = str(PurePosixPath(self._current_dir) / name)
        if is_dir:
            self._current_dir = full
            self._load_dir(full)
        else:
            self.query_one("#rfp-path", Input).value = full

    def _load_dir(self, path: str) -> None:
        """Fetch directory listing from remote host in a background thread."""
        if self._check_running:
            return
        self._check_running = True
        self._list_widget.clear_options()
        self._list_widget.add_option(Option(t("rfp_loading"), disabled=True))
        threading.Thread(target=self._fetch_listing, args=(path,), daemon=True).start()

    def _fetch_listing(self, path: str) -> None:
        """Run ls via SSH and update the option list."""
        try:
            cmd = self._sshpass + self._ssh_base + [self._ssh_target, f"ls -1pa '{path}'"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                err = result.stderr.strip().split("\n")[-1] if result.stderr else "unknown error"
                self.app.call_from_thread(self._show_error, err)
                return
            lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
            # Parse entries
            dirs = []
            files = []
            for line in lines:
                if line in ("./", "../"):
                    continue
                if line.endswith("/"):
                    dirs.append(line.rstrip("/"))
                else:
                    files.append(line)
            dirs.sort(key=str.lower)
            files.sort(key=str.lower)
            self.app.call_from_thread(self._populate, path, dirs, files)
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(self._show_error, "timeout")
        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))
        finally:
            self._check_running = False

    def _populate(self, path: str, dirs: list[str], files: list[str]) -> None:
        """Update option list with directory contents (main thread)."""
        self._list_widget.clear_options()
        self._entries = []
        _dir_icon = icon("browse")
        # Parent dir entry
        if path != "/":
            self._entries.append(("..", True))
            self._list_widget.add_option(Option(f"{_dir_icon} .."))
        # Directories
        for d in dirs:
            self._entries.append((d, True))
            self._list_widget.add_option(Option(f"{_dir_icon} {d}/"))
        # Files
        for f in files:
            self._entries.append((f, False))
            self._list_widget.add_option(Option(f"   {f}"))
        if not dirs and not files:
            self._list_widget.add_option(Option(t("rfp_empty"), disabled=True))
            # Keep entries empty so index mapping stays correct
        # Update path input
        self.query_one("#rfp-path", Input).value = path

    def _show_error(self, error: str) -> None:
        """Show connection error in the list (main thread)."""
        self._list_widget.clear_options()
        self._entries = []
        self._list_widget.add_option(
            Option(f"[red]{t('rfp_connect_failed').format(error=error)}[/red]", disabled=True)
        )

    def action_cancel(self) -> None:
        self.dismiss("")
