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
        width: 95%;
        max-width: 140;
        height: 80%;
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
        max-height: 80vh;
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

    def __init__(self, repo_path: Path, vcs: str, branch: str = "", limit: int = 100,
                 auth_env: dict | None = None) -> None:
        super().__init__()
        self._repo_path = repo_path
        self._vcs = vcs
        self._branch = branch
        self._limit = limit
        self._auth_env = auth_env
        self._rows: list[tuple[str, str, str, str, str]] = []  # (rev, date, author, subject, refs)
        self._current_rows: list[tuple[str, str, str, str, str]] = []
        self._current_rev: str = ""  # current HEAD sha/rev for highlighting
        self._sort_col: int | None = None
        self._sort_reverse: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="log-box"):
            yield Static(t("log_title"), id="log-title")
            yield Input(placeholder=t("log_filter_ph"), id="log-filter")
            yield DataTable(id="log-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="log-buttons"):
                yield Button(t("re_switch_to"), id="log-switch", variant="primary")
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
            # Detect current HEAD/rev for highlighting
            if self._vcs == "git":
                try:
                    self._current_rev = subprocess.check_output(
                        ["git", "rev-parse", "--short", "HEAD"],
                        cwd=self._repo_path, text=True, timeout=5,
                    ).strip()
                except Exception:
                    pass
            else:
                try:
                    out = subprocess.check_output(
                        ["svnversion", "."],
                        cwd=self._repo_path, text=True, timeout=5,
                    ).strip().rstrip("MSP")
                    self._current_rev = f"r{out}"
                except Exception:
                    pass
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
        # No branch specified → resolve the remote's default branch so the
        # log doesn't just walk HEAD (which is the pinned commit on a
        # detached repo, hiding everything newer).
        branch = self._branch
        if not branch:
            for candidate in ("origin/HEAD", "origin/master", "origin/main"):
                try:
                    resolved = subprocess.check_output(
                        ["git", "rev-parse", "--abbrev-ref", candidate],
                        cwd=self._repo_path, text=True, stderr=subprocess.DEVNULL, timeout=5,
                    ).strip()
                    if resolved and resolved != candidate:
                        branch = resolved  # e.g. "origin/master"
                        break
                except subprocess.CalledProcessError:
                    continue
        if branch:
            cmd += [branch, "--"]
        else:
            # Last resort — walk every local ref so at least something shows.
            cmd += ["--all"]
        out = subprocess.check_output(
            cmd, cwd=self._repo_path, text=True, stderr=subprocess.DEVNULL, timeout=20,
            env=self._auth_env,
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
            is_current = self._current_rev and rev == self._current_rev
            if is_current:
                _c = "bold green"
                if self._vcs == "git":
                    tbl.add_row(f"[{_c}]{rev}[/{_c}]", f"[{_c}]{refs_c}[/{_c}]", f"[{_c}]{subj_c}[/{_c}]", f"[{_c}]{date}[/{_c}]", f"[{_c}]{auth_c}[/{_c}]", key=rev)
                else:
                    tbl.add_row(f"[{_c}]{rev}[/{_c}]", f"[{_c}]{subj_c}[/{_c}]", f"[{_c}]{date}[/{_c}]", f"[{_c}]{auth_c}[/{_c}]", key=rev)
            else:
                if self._vcs == "git":
                    tbl.add_row(rev, refs_c, subj_c, date, auth_c, key=rev)
                else:
                    tbl.add_row(rev, subj_c, date, auth_c, key=rev)
        if tbl.row_count:
            # Land the cursor on the currently-checked-out commit so a
            # pinned repo opens right at its pin — otherwise row 0.
            cursor_row = 0
            if initial and self._current_rev:
                for idx, row in enumerate(self._current_rows):
                    if row[0] == self._current_rev:
                        cursor_row = idx
                        break
            tbl.move_cursor(row=cursor_row)
            # Only move focus on the first render — don't steal it from
            # the filter input while the user is still typing.
            if initial:
                tbl.focus()
            self._refresh_row_tooltip(cursor_row)
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
        if event.button.id == "log-switch":
            try:
                tbl = self.query_one("#log-table", DataTable)
                rev = tbl.ordered_rows[tbl.cursor_row].key.value
                if rev:
                    self.dismiss(str(rev))
            except Exception:
                pass
        elif event.button.id == "log-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
