"""Upload check modal — test SSH connectivity and optionally upload a binary."""

from __future__ import annotations

import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, RichLog, Button, Select

from core.target import UploadConfig
from core.uploader import Uploader
from translation import t
from ui.icons import is_ascii


class UploadCheckModal(ModalScreen[None]):
    """Modal that tests SSH connectivity and performs upload check or real upload."""

    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    UploadCheckModal {
        align: center middle;
    }
    #check-box {
        width: 120;
        height: 34;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }
    #check-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #check-select-row {
        height: 3;
        margin: 0 0 1 0;
    }
    #check-select-row Select {
        width: 1fr;
    }
    #check-log {
        height: 1fr;
        background: $boost;
        border: solid $accent;
        border-title-color: $text;
        border-title-style: bold;
        overflow-y: auto;
    }
    #check-log:focus {
        border: heavy $accent;
    }
    #check-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    #check-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self, uploads: list[UploadConfig], target_name: str,
                 binaries_dir: Optional[Path] = None,
                 logs_dir: Optional[Path] = None,
                 toolchain_filter: str = "") -> None:
        super().__init__()
        self._uploads = [u for u in uploads if u.host]
        self._target_name = target_name
        self._binaries_dir = binaries_dir
        self._logs_dir = logs_dir
        self._toolchain_filter = toolchain_filter or (self._uploads[0].toolchain if self._uploads else "")
        self._check_running = False
        self._binary_path: Optional[Path] = None

    def compose(self) -> ComposeResult:
        bin_options = [(t("uc_dry_run"), "dry-run")]
        if self._binaries_dir and self._binaries_dir.is_dir():
            tc = self._toolchain_filter
            _hostname = socket.gethostname()
            for f in sorted(self._binaries_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if not f.is_file():
                    continue
                if f.name.endswith((".debug", ".zip", ".log")):
                    continue
                if tc and tc != "native" and tc not in f.name:
                    continue
                if tc == "native" and _hostname not in f.name and "native" not in f.name:
                    continue
                size_kb = f.stat().st_size // 1024
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                label = f"{f.name} ({size_kb} KB, {mtime})"
                bin_options.append((label, str(f)))

        with Vertical(id="check-box"):
            yield Static(f"[bold]Upload — {self._target_name}[/bold]", id="check-title")
            with Horizontal(id="check-select-row"):
                yield Static(f" {t('uc_binary')}", classes="s-label-tall")
                yield Select(options=bin_options, value="dry-run", id="check-binary", allow_blank=False)
            log = RichLog(id="check-log", highlight=False, markup=True, wrap=True)
            log.border_title = t("output_title")
            yield log
            with Horizontal(id="check-buttons"):
                yield Button(t("uc_start"), id="check-start", variant="primary")
                yield Button(t("cancel"), id="check-close")

    def on_mount(self) -> None:
        self._log_widget = self.query_one("#check-log", RichLog)
        self._log_widget.write(f"[dim]{t('uc_press_start')}[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "check-close":
            self.dismiss(None)
        elif event.button.id == "check-start":
            self._start_check()

    def _start_check(self) -> None:
        if self._check_running:
            return
        self._check_running = True
        self._log_widget.write(f"[bold]{t('uc_starting')}[/bold]")
        try:
            binary_val = self.query_one("#check-binary", Select).value
            self._binary_path = None if binary_val == "dry-run" else Path(str(binary_val))
        except Exception:
            self._binary_path = None
        threading.Thread(target=self._run_thread, daemon=True).start()

    def _run_thread(self) -> None:
        try:
            self._run_checks(self._binary_path)
        except Exception:
            import traceback
            msg = traceback.format_exc()
            try:
                self.app.call_from_thread(self._log_widget.write, f"[red]{msg}[/red]")
            except Exception:
                pass
        finally:
            self._check_running = False

    def action_close(self) -> None:
        self.dismiss(None)

    def _log(self, msg: str) -> None:
        self.app.call_from_thread(self._log_widget.write, msg)

    def _run_checks(self, binary_path: Optional[Path] = None) -> None:
        log_lines: list[str] = []
        is_dry_run = binary_path is None
        _arrow = "->" if is_ascii() else "→"
        _sep = "---" if is_ascii() else "━━━"

        def log(msg: str) -> None:
            log_lines.append(msg)
            self._log(msg)

        mode = t("uc_mode_dry") if is_dry_run else t("uc_mode_upload").format(name=binary_path.name)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log(f"[bold]{t('uc_upload_started').format(time=now)}[/bold]")
        log(t("uc_target").format(name=self._target_name))
        log(t("uc_mode").format(mode=mode))
        log(t("uc_destinations").format(count=len(self._uploads)))
        log("")

        for idx, u in enumerate(self._uploads):
            name = u.name or f"#{idx + 1}"
            is_local = u.host in ("localhost", "127.0.0.1", "::1")

            if is_local:
                header = t("uc_separator_local").format(name=name, path=u.target_path).replace("→", _arrow)
            else:
                header = t("uc_separator_remote").format(
                    name=name, user=u.user, host=u.host, path=u.target_path)
            log(f"[bold]{_sep} {header} {_sep}[/bold]")
            log("")

            uploader = Uploader(
                config=u, log=log,
                binary_path=binary_path,
                dry_run=is_dry_run,
            )
            uploader.run()
            log("")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log(f"[bold]{t('uc_complete').format(time=now)}[/bold]")

        # Write log file
        if self._logs_dir:
            try:
                import re
                self._logs_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
                upload_names = "-".join(
                    (u.name or u.host).replace(" ", "_").replace("/", "_")
                    for u in self._uploads
                )
                log_path = self._logs_dir / f"{timestamp}.{self._target_name}.{upload_names}.upload-check.log"
                plain_lines = [re.sub(r'\[/?[a-z_ #0-9]+\]', '', line) for line in log_lines]
                log_path.write_text("\n".join(plain_lines) + "\n", encoding="utf-8")
                log(f"Log: {log_path}")
            except Exception:
                pass

        try:
            from core.log_cleanup import run_retention
            run_retention(self._logs_dir, self.app.settings)
        except Exception:
            pass
