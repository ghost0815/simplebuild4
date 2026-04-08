"""CommitLogModal — pick a commit/revision from the history of a cloned repo."""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from translation import t


class CommitLogModal(ModalScreen[Optional[str]]):
    """Modal dialog showing the last N commits/revisions of a cloned repo.

    Dismisses with the picked SHA (git) or revision number (svn), or None
    on cancel.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    CommitLogModal {
        align: center middle;
    }
    #log-box {
        width: 120;
        height: 32;
        border: solid $accent;
        background: $surface;
        padding: 1;
    }
    #log-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #log-filter {
        height: 3;
        margin: 0 0 1 0;
    }
    #log-table {
        height: 1fr;
    }
    #log-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    #log-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self, repo_path: Path, vcs: str, branch: str = "", limit: int = 100) -> None:
        super().__init__()
        self._repo_path = repo_path
        self._vcs = vcs
        self._branch = branch
        self._limit = limit
        self._rows: list[tuple[str, str, str, str, str]] = []  # (rev, date, author, subject, refs)
        self._filtered_indices: list[int] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="log-box"):
            yield Static(t("log_title"), id="log-title")
            yield Input(placeholder=t("log_filter_ph"), id="log-filter")
            yield DataTable(id="log-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="log-buttons"):
                yield Button(t("cancel"), id="log-cancel")

    def on_mount(self) -> None:
        tbl = self.query_one("#log-table", DataTable)
        if self._vcs == "git":
            tbl.add_columns(t("log_col_sha"), t("log_col_refs"), t("log_col_subject"), t("log_col_date"), t("log_col_author"))
        else:
            tbl.add_columns(t("log_col_rev"), t("log_col_subject"), t("log_col_date"), t("log_col_author"))
        tbl.loading = True
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        try:
            if self._vcs == "git":
                self._rows = self._fetch_git()
            else:
                self._rows = self._fetch_svn()
        except Exception as e:
            self._rows = []
            self.app.call_from_thread(self.notify, f"Log failed: {e}", severity="error")
        self.app.call_from_thread(self._apply_rows)

    def _fetch_git(self) -> list[tuple[str, str, str, str, str]]:
        sep = "\x1f"
        fmt = f"%h{sep}%ci{sep}%an{sep}%d{sep}%s"
        cmd = ["git", "log", f"-n{self._limit}", f"--pretty=format:{fmt}"]
        if self._branch:
            cmd.append(self._branch)
        out = subprocess.check_output(
            cmd, cwd=self._repo_path, text=True, stderr=subprocess.DEVNULL, timeout=20,
        )
        rows = []
        for line in out.splitlines():
            parts = line.split(sep, 4)
            if len(parts) != 5:
                continue
            sha, date, author, refs, subject = parts
            # date: "2026-04-08 11:23:45 +0200" → keep yyyy-mm-dd
            date_short = date.split(" ", 1)[0] if date else ""
            refs_clean = refs.strip().lstrip("(").rstrip(")").strip()
            rows.append((sha, date_short, author, subject, refs_clean))
        return rows

    def _fetch_svn(self) -> list[tuple[str, str, str, str, str]]:
        out = subprocess.check_output(
            ["svn", "log", "-l", str(self._limit), "--non-interactive"],
            cwd=self._repo_path, text=True, stderr=subprocess.DEVNULL, timeout=30,
        )
        rows = []
        # Format:
        # ------------------------------------------------------------------------
        # rNNNNN | author | 2026-04-08 12:34:56 +0000 | N line(s)
        # <blank>
        # <subject lines...>
        # ------------------------------------------------------------------------
        entries = out.split("-" * 72)
        for entry in entries:
            lines = [ln for ln in entry.strip().splitlines() if ln.strip()]
            if len(lines) < 2:
                continue
            header = lines[0]
            parts = [p.strip() for p in header.split("|")]
            if len(parts) < 4 or not parts[0].startswith("r"):
                continue
            rev = parts[0]
            author = parts[1]
            date_short = parts[2].split(" ", 1)[0] if parts[2] else ""
            subject = " ".join(lines[1:]).strip()
            rows.append((rev, date_short, author, subject, ""))
        return rows

    def _apply_rows(self) -> None:
        tbl = self.query_one("#log-table", DataTable)
        tbl.loading = False
        self._rebuild_table(self._rows)

    def _rebuild_table(self, rows) -> None:
        tbl = self.query_one("#log-table", DataTable)
        tbl.clear()
        for rev, date, author, subject, refs in rows:
            if self._vcs == "git":
                tbl.add_row(rev, refs, subject, date, author, key=rev)
            else:
                tbl.add_row(rev, subject, date, author, key=rev)
        if tbl.row_count:
            tbl.move_cursor(row=0)
            tbl.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "log-filter":
            return
        term = event.value.strip().lower()
        if not term:
            self._rebuild_table(self._rows)
            return
        filtered = [
            r for r in self._rows
            if term in r[0].lower()
            or term in r[2].lower()
            or term in r[3].lower()
            or term in r[4].lower()
        ]
        self._rebuild_table(filtered)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            rev = event.row_key.value
            if rev:
                self.dismiss(str(rev))
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "log-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
