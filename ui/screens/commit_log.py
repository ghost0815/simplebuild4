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
        width: 140;
        height: 34;
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
        overflow-x: hidden;
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
        self._current_rows: list[tuple[str, str, str, str, str]] = []
        self._sort_col: int | None = None
        self._sort_reverse: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="log-box"):
            yield Static(t("log_title"), id="log-title")
            yield Input(placeholder=t("log_filter_ph"), id="log-filter")
            yield DataTable(id="log-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="log-buttons"):
                yield Button(t("cancel"), id="log-cancel")

    # Max width per column — long values are ellipsised and the full
    # text is shown as a hover tooltip on the highlighted row.
    _MAX_REFS = 12
    _MAX_SUBJECT = 78
    _MAX_AUTHOR = 12

    def on_mount(self) -> None:
        tbl = self.query_one("#log-table", DataTable)
        if self._vcs == "git":
            tbl.add_columns(t("log_col_sha"), t("log_col_refs"), t("log_col_subject"), t("log_col_date"), t("log_col_author"))
        else:
            tbl.add_columns(t("log_col_rev"), t("log_col_subject"), t("log_col_date"), t("log_col_author"))
        tbl.loading = True
        threading.Thread(target=self._fetch, daemon=True).start()

    @staticmethod
    def _ellipsise(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        if limit <= 1:
            return value[:limit]
        return value[: limit - 1] + "…"

    def _fetch(self) -> None:
        try:
            if self._vcs == "git":
                self._rows = self._fetch_git()
            else:
                self._rows = self._fetch_svn()
        except Exception as e:
            self._rows = []
            self.app.call_from_thread(self.notify, f"Log failed: {e}", severity="error", markup=False)
        self.app.call_from_thread(self._apply_rows)

    def _fetch_git(self) -> list[tuple[str, str, str, str, str]]:
        sep = "\x1f"
        fmt = f"%h{sep}%ci{sep}%an{sep}%d{sep}%s"
        cmd = ["git", "log", f"-n{self._limit}", f"--pretty=format:{fmt}"]
        if self._branch:
            cmd += [self._branch, "--"]
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
        self._rebuild_table(self._rows, initial=True)

    # Map visible column index → index in the row tuple
    # (rev, date, author, subject, refs)
    def _col_key_index(self, col_index: int) -> int:
        if self._vcs == "git":
            # columns: sha, refs, subject, date, author
            return (0, 4, 3, 1, 2)[col_index]
        # columns: rev, subject, date, author
        return (0, 3, 1, 2)[col_index]

    def _rebuild_table(self, rows, initial: bool = False) -> None:
        tbl = self.query_one("#log-table", DataTable)
        tbl.clear()
        self._current_rows = list(rows)
        for rev, date, author, subject, refs in self._current_rows:
            refs_c = self._ellipsise(refs, self._MAX_REFS)
            subj_c = self._ellipsise(subject, self._MAX_SUBJECT)
            auth_c = self._ellipsise(author, self._MAX_AUTHOR)
            if self._vcs == "git":
                tbl.add_row(rev, refs_c, subj_c, date, auth_c, key=rev)
            else:
                tbl.add_row(rev, subj_c, date, auth_c, key=rev)
        if tbl.row_count:
            tbl.move_cursor(row=0)
            # Only move focus on the first render — don't steal it from
            # the filter input while the user is still typing.
            if initial:
                tbl.focus()
            self._refresh_row_tooltip(0)
        else:
            tbl.tooltip = None

    def _refresh_row_tooltip(self, row_index: int) -> None:
        tbl = self.query_one("#log-table", DataTable)
        if not (0 <= row_index < len(self._current_rows)):
            tbl.tooltip = None
            return
        rev, date, author, subject, refs = self._current_rows[row_index]
        lines = [f"{rev}  {date}  {author}"]
        if refs:
            lines.append(f"refs: {refs}")
        lines.append(subject)
        tbl.tooltip = "\n".join(lines)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._refresh_row_tooltip(event.cursor_row)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        col_index = event.column_index
        key_idx = self._col_key_index(col_index)
        if self._sort_col == col_index:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col_index
            self._sort_reverse = False
        # Remember selected row key before re-sort
        tbl = self.query_one("#log-table", DataTable)
        prev_key = None
        try:
            prev_key = tbl.get_row_at(tbl.cursor_row)
            # get_row_at returns cells; use the row key from ordered_rows
            prev_key = tbl.ordered_rows[tbl.cursor_row].key.value
        except Exception:
            pass
        self._current_rows.sort(
            key=lambda r: (r[key_idx] or "").lower(),
            reverse=self._sort_reverse,
        )
        self._rebuild_table(self._current_rows)
        # Restore cursor to the previously selected row
        if prev_key:
            for idx, (rev, *_) in enumerate(self._current_rows):
                if rev == prev_key:
                    tbl.move_cursor(row=idx)
                    break

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
