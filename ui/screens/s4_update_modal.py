"""S4 Update modal — browse commit history and select version to update/downgrade."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, DataTable, Input, Button

from translation import t


class S4UpdateModal(ModalScreen[Optional[str]]):
    """Modal that shows s4 commit history and lets the user pick a version."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    S4UpdateModal {
        align: center middle;
    }
    #s4u-box {
        width: 95%;
        max-width: 140;
        height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1;
    }
    #s4u-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #s4u-filter {
        height: 3;
        margin: 0 0 1 0;
    }
    #s4u-table {
        height: 1fr;
        max-height: 80vh;
    }
    #s4u-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    #s4u-buttons Button {
        margin: 0 0 0 1;
    }
    """

    _MAX_REFS = 12
    _MAX_SUBJECT = 78
    _MAX_AUTHOR = 12

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root
        self._branch = ""
        self._current_sha = ""
        self._current_index = -1
        self._rows: list[tuple[str, str, str, str, str, str]] = []  # (sha, date_short, author, subject, refs, date_full)
        self._current_rows: list[tuple[str, str, str, str, str, str]] = []
        self._selected_sha = ""
        self._is_downgrade = False
        self._downgrade_confirm_time = 0.0
        self._sort_col: int | None = None
        self._sort_reverse: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="s4u-box"):
            yield Static(t("s4u_title"), id="s4u-title")
            yield Input(placeholder=t("log_filter_ph"), id="s4u-filter")
            yield DataTable(id="s4u-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="s4u-buttons"):
                yield Button(t("s4u_update"), id="s4u-action", variant="primary")
                yield Button(t("cancel"), id="s4u-cancel")

    def on_mount(self) -> None:
        tbl = self.query_one("#s4u-table", DataTable)
        tbl.add_columns(t("log_col_sha"), t("log_col_refs"), t("log_col_subject"), t("log_col_date"), t("log_col_author"))
        tbl.loading = True
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        try:
            root = str(self._root)
            self._branch = subprocess.run(
                ["git", "branch", "--show-current"], cwd=root,
                capture_output=True, text=True, timeout=5,
            ).stdout.strip() or "master"

            subprocess.run(
                ["git", "fetch", "--prune"], cwd=root,
                capture_output=True, text=True, timeout=15,
            )

            self._current_sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"], cwd=root,
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()

            sep = "\x1f"
            fmt = f"%h{sep}%ci{sep}%an{sep}%d{sep}%s"
            out = subprocess.check_output(
                ["git", "log", "-n200", f"--pretty=format:{fmt}", f"origin/{self._branch}", "--"],
                cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=20,
            )
            rows = []
            for line in out.splitlines():
                parts = line.split(sep, 4)
                if len(parts) != 5:
                    continue
                sha, date, author, refs, subject = parts
                date_short = date.split(" ", 1)[0] if date else ""
                date_full = date.strip()  # "2026-04-12 14:51:48 +0200" for sorting
                refs_clean = refs.strip().lstrip("(").rstrip(")").strip()
                rows.append((sha, date_short, author, subject, refs_clean, date_full))
            self._rows = rows
        except Exception as e:
            self._rows = []
            self.app.call_from_thread(self.notify, f"Fetch failed: {e}", severity="error", markup=False)
        self.app.call_from_thread(self._apply_rows)

    def _apply_rows(self) -> None:
        try:
            self.query_one("#s4u-title", Static).update(
                f"[bold]{t('s4u_title')} ({t('s4u_branch')}: {self._branch})[/bold]"
            )
        except Exception:
            pass
        tbl = self.query_one("#s4u-table", DataTable)
        tbl.loading = False
        self._rebuild_table(self._rows, initial=True)

    @staticmethod
    def _ellipsise(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        if limit <= 1:
            return value[:limit]
        return value[: limit - 1] + "…"

    def _rebuild_table(self, rows, initial: bool = False) -> None:
        tbl = self.query_one("#s4u-table", DataTable)
        tbl.clear()
        self._current_rows = list(rows)
        self._current_index = -1

        for i, (sha, date, author, subject, refs, _date_full) in enumerate(self._current_rows):
            refs_c = self._ellipsise(refs, self._MAX_REFS)
            subj_c = self._ellipsise(subject, self._MAX_SUBJECT)
            auth_c = self._ellipsise(author, self._MAX_AUTHOR)
            is_current = (sha == self._current_sha)
            if is_current:
                self._current_index = i
                _c = "bold green"
                tbl.add_row(
                    f"[{_c}]{sha}[/{_c}]",
                    f"[{_c}]{refs_c}[/{_c}]",
                    f"[{_c}]{subj_c}[/{_c}]",
                    f"[{_c}]{date}[/{_c}]",
                    f"[{_c}]{auth_c}[/{_c}]",
                    key=sha,
                )
            else:
                tbl.add_row(sha, refs_c, subj_c, date, auth_c, key=sha)

        if tbl.row_count:
            tbl.move_cursor(row=0)
            if initial:
                tbl.focus()
            self._update_button_label(0)
            self._refresh_row_tooltip(0)

    def _refresh_row_tooltip(self, row_index: int) -> None:
        tbl = self.query_one("#s4u-table", DataTable)
        if not (0 <= row_index < len(self._current_rows)):
            tbl.tooltip = None
            return
        sha, date, author, subject, refs, _date_full = self._current_rows[row_index]
        lines = [f"{sha}  {date}  {author}"]
        if refs:
            lines.append(f"refs: {refs}")
        lines.append(subject)

        tbl.tooltip = "\n".join(lines)

    # Column index → row tuple index mapping for sorting
    # Columns: SHA(0), Refs(4), Subject(3), Date→date_full(5), Author(2)
    def _col_key_index(self, col_index: int) -> int:
        return (0, 4, 3, 5, 2)[col_index]

    def _update_button_label(self, row_index: int) -> None:
        try:
            btn = self.query_one("#s4u-action", Button)
            # Use original row order (chronological) to determine up/downgrade
            if not (0 <= row_index < len(self._current_rows)):
                return
            selected_sha = self._current_rows[row_index][0]
            # Find positions in original (chronological) order
            orig_shas = [r[0] for r in self._rows]
            try:
                orig_current = orig_shas.index(self._current_sha)
            except ValueError:
                orig_current = -1
            try:
                orig_selected = orig_shas.index(selected_sha)
            except ValueError:
                orig_selected = -1
            if orig_current < 0:
                self._is_downgrade = False
                btn.label = t("s4u_update")
                btn.variant = "primary"
            elif orig_selected > orig_current:
                # Selected is older (further down in chronological list)
                self._is_downgrade = True
                btn.label = t("s4u_downgrade")
                btn.variant = "warning"
            else:
                self._is_downgrade = False
                btn.label = t("s4u_update")
                btn.variant = "primary"
        except Exception:
            pass

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._update_button_label(event.cursor_row)
        self._refresh_row_tooltip(event.cursor_row)
        if 0 <= event.cursor_row < len(self._current_rows):
            self._selected_sha = self._current_rows[event.cursor_row][0]
        self._downgrade_confirm_time = 0

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        col_index = event.column_index
        key_idx = self._col_key_index(col_index)
        if self._sort_col == col_index:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col_index
            self._sort_reverse = False
        tbl = self.query_one("#s4u-table", DataTable)
        prev_key = None
        try:
            prev_key = tbl.ordered_rows[tbl.cursor_row].key.value
        except Exception:
            pass
        self._current_rows.sort(
            key=lambda r: (r[key_idx] or "").lower(),
            reverse=self._sort_reverse,
        )
        self._rebuild_table(self._current_rows)
        if prev_key:
            for idx, (sha, *_) in enumerate(self._current_rows):
                if sha == prev_key:
                    tbl.move_cursor(row=idx)
                    break

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "s4u-filter":
            return
        term = event.value.strip().lower()
        if not term:
            self._rebuild_table(self._rows)
            return
        filtered = [
            r for r in self._rows
            if term in r[0].lower() or term in r[2].lower()
            or term in r[3].lower() or term in r[4].lower()
        ]
        self._rebuild_table(filtered)

    def _confirm_selection(self) -> None:
        """Confirm the selected SHA — downgrades require double confirmation."""
        if not self._selected_sha:
            return
        if self._is_downgrade:
            now = time.time()
            if now - self._downgrade_confirm_time < 3:
                self._downgrade_confirm_time = 0
                self.dismiss(self._selected_sha)
            else:
                self._downgrade_confirm_time = now
                self.notify(t("s4u_downgrade_warn"), severity="warning", timeout=3)
        else:
            self.dismiss(self._selected_sha)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self._selected_sha = event.row_key.value
            self._update_button_label(event.cursor_row)
            self._confirm_selection()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "s4u-cancel":
            self.dismiss(None)
        elif event.button.id == "s4u-action":
            self._confirm_selection()

    def action_cancel(self) -> None:
        self.dismiss(None)
