"""Toolchains screen — browse, install, remove toolchains."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import re
import tarfile
import threading
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.screen import Screen, ModalScreen
from textual.widgets import Static, Input, Button, Collapsible, DataTable, Select, RichLog, DirectoryTree
from textual.containers import Horizontal, Vertical, VerticalScroll

from core.toolchain import Toolchain, ToolchainRegistry, scan_libraries, load_library_recipes, check_library_updates, KNOWN_LIBS
from core.lib_builder import build_library, sort_by_dependencies
from translation import t
from ui.header import S4Header


class S4DataTable(DataTable):
    """DataTable with custom cursor colors."""

    DEFAULT_CSS = """
    S4DataTable {
        background: $surface;
        color: $foreground;
        height: auto;
        max-height: 100%;

        &:focus {
            background-tint: $foreground 5%;
            & > .datatable--cursor {
                background: $primary;
                color: $text;
                text-style: bold;
            }
            & > .datatable--header {
                background-tint: $foreground 5%;
            }
        }

        &:dark {
            & > .datatable--even-row {
                background: $surface-darken-1 40%;
            }
        }

        & > .datatable--header {
            text-style: bold;
            background: #444444;
            color: $text;
        }

        & > .datatable--cursor {
            background: $primary;
            color: $text;
        }

        & > .datatable--even-row {
            background: $surface-lighten-1 50%;
        }

        & > .datatable--hover {
            background: $block-hover-background;
        }
    }
    """
from ui.footer import S4Footer

_BASE_CATEGORIES = [
    ("local", "Local"),
    ("common", "Common"),
    ("stb", "STB"),
    ("android", "Android"),
    ("openwrt", "OpenWrt"),
    ("router", "Router"),
    ("nas", "NAS"),
    ("minipc", "Mini PC"),
    ("tv", "TV"),
    ("legacy", "Legacy"),
]


def _build_categories(reg) -> list[tuple[str, str]]:
    """Build category list: base categories + custom ones inserted after 'Local'."""
    known_keys = {k for k, _ in _BASE_CATEGORIES}
    custom = []
    for tc in reg.catalog_toolchains() + reg.local_toolchains():
        cat = tc.category
        if cat and cat not in known_keys and cat != "native":
            known_keys.add(cat)
            custom.append((cat, cat.capitalize()))
    categories = []
    for entry in _BASE_CATEGORIES:
        categories.append(entry)
        if entry[0] == "local":
            categories.extend(sorted(custom))
    return categories


class FilePickerScreen(ModalScreen[tuple]):
    """Modal file picker for selecting .tar.xz files."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    FilePickerScreen {
        align: center middle;
    }
    #file-picker-box {
        width: 80;
        height: 30;
        border: solid $accent;
        background: $surface;
        padding: 1;
    }
    #file-picker-box #fp-title-row {
        height: 3;
    }
    #file-picker-box #fp-title {
        width: auto;
        padding: 1 0;
        text-style: bold;
    }
    #file-picker-box .fp-nav-btn {
        min-width: 10;
        margin: 0 0 0 1;
    }
    #file-picker-box DirectoryTree {
        height: 1fr;
        border: solid #444444;
    }
    #file-picker-box #fp-path {
        height: 3;
        margin: 1 0 0 0;
    }
    #file-picker-box #fp-name-row {
        height: 3;
        margin: 0;
    }
    #file-picker-box #fp-name-label {
        width: 8;
        padding: 1 0;
    }
    #file-picker-box #fp-name {
        width: 1fr;
    }
    #file-picker-box #fp-buttons {
        height: 3;
        align: right middle;
    }
    #file-picker-box #fp-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self, start_path: str = "/") -> None:
        super().__init__()
        self._start_path = start_path

    def compose(self) -> ComposeResult:
        with Vertical(id="file-picker-box"):
            with Horizontal(id="fp-title-row"):
                yield Static("Restore toolchain from backup", id="fp-title")
                yield Static("", classes="spacer")
                yield Button("⌂ Home", id="fp-home", classes="fp-nav-btn")
                yield Button("/ Root", id="fp-root", classes="fp-nav-btn")
            yield DirectoryTree(self._start_path, id="fp-tree")
            yield Input(placeholder=t("ph_enter_path"), id="fp-path")
            with Horizontal(id="fp-name-row"):
                yield Static(" Name: ", id="fp-name-label")
                yield Input(placeholder=t("ph_tc_name"), id="fp-name")
            with Horizontal(id="fp-buttons"):
                yield Button("Cancel", id="fp-cancel")
                yield Button("Import", id="fp-select", variant="primary")

    @staticmethod
    def _name_from_filename(filename: str) -> str:
        """Derive toolchain name from archive filename."""
        name = filename
        for ext in (".tar.xz", ".tar.bz2", ".tar.gz"):
            if name.endswith(ext):
                name = name[:-len(ext)]
                break
        if name.startswith("Toolchain-"):
            name = name[len("Toolchain-"):]
        return name

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """When a file is clicked in the tree."""
        self.query_one("#fp-path", Input).value = str(event.path)
        self.query_one("#fp-name", Input).value = self._name_from_filename(event.path.name)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "fp-path":
            path = event.value.strip()
            if path:
                self.query_one("#fp-name", Input).value = self._name_from_filename(Path(path).name)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "fp-cancel":
            self.dismiss(("", ""))
        elif event.button.id == "fp-select":
            path = self.query_one("#fp-path", Input).value.strip()
            name = self.query_one("#fp-name", Input).value.strip()
            if path and name:
                self.dismiss((path, name))
            elif path:
                self.notify("Enter a toolchain name", severity="warning")
        elif event.button.id == "fp-home":
            self._change_root(Path.home())
        elif event.button.id == "fp-root":
            self._change_root(Path("/"))

    def _change_root(self, new_path: Path) -> None:
        """Replace the directory tree with a new root."""
        tree = self.query_one("#fp-tree", DirectoryTree)
        tree.path = new_path
        tree.reload()

    def action_cancel(self) -> None:
        self.dismiss(("", ""))


class ToolchainsScreen(Screen):
    BINDINGS = [
        ("d", "switch_right_tab('details')", "Details"),
        ("l", "switch_right_tab('libs')", "Libraries"),
        ("o", "switch_right_tab('log')", "Output"),
        ("p", "switch_left_tab('prebuild')", "PreBuild"),
        ("s", "switch_left_tab('selfbuild')", "SelfBuild"),
    ]

    _log_history: list[str] = []
    _selected_tc: str = ""

    def __init__(self) -> None:
        super().__init__()
        self._registry: ToolchainRegistry | None = None
        self._mounted = False
        self._building = False
        self._cancel_requested = False

    def _get_registry(self) -> ToolchainRegistry:
        if self._registry is None:
            self._registry = ToolchainRegistry(
                self.app.paths.toolchains_defaults,
                self.app.paths.toolchains_local,
                self.app.paths.toolchains_dir,
            )
            self._registry.load()
        return self._registry

    def compose(self) -> ComposeResult:
        reg = self._get_registry()

        yield S4Header()

        with Horizontal(id="tc-main"):
            # Left: Toolchain browser (half screen)
            with Vertical(id="tc-browser-wrap"):
                # Filter + Actions
                with Horizontal(id="tc-actions"):
                    yield Input(placeholder=t("ph_filter"), id="tc-filter")
                    yield Static("", classes="spacer")
                    install_btn = Button(t("install"), id="btn-install", variant="primary")
                    install_btn.tooltip = "Download and install the selected toolchain"
                    yield install_btn
                    check_btn = Button(t("check"), id="btn-check")
                    check_btn.tooltip = "Verify the selected toolchain is working"
                    yield check_btn
                    backup_btn = Button(t("backup"), id="btn-backup")
                    backup_btn.tooltip = "Backup the selected installed toolchain to .tar.xz"
                    yield backup_btn
                    restore_btn = Button(t("restore"), id="btn-restore")
                    restore_btn.tooltip = "Restore a toolchain from a .tar.xz backup"
                    yield restore_btn
                    remove_btn = Button(t("remove"), id="btn-remove", variant="error")
                    remove_btn.tooltip = "Delete the selected toolchain from disk"
                    yield remove_btn

                # Installed toolchains (always visible)
                yield Static(" [bold]Installed[/bold]", id="tc-installed-label")
                yield S4DataTable(id="tbl-installed", cursor_type="row", zebra_stripes=True)

                # PreBuild / SelfBuild tabs
                with Horizontal(id="tc-left-tab-bar"):
                    yield Static(" [b]P[/b]reBuild ", id="btn-ltab-prebuild", classes="tab-active")
                    yield Static(" [b]S[/b]elfBuild ", id="btn-ltab-selfbuild", classes="tab-inactive")

                # PreBuild panel — catalog categories + local
                self._categories = _build_categories(reg)
                all_catalog = reg.catalog_toolchains()
                all_local = reg.local_toolchains()
                with VerticalScroll(id="tc-browser"):
                    for cat_key, cat_label in self._categories:
                        if cat_key == "local":
                            tcs = [tc for tc in all_local if tc.category in ("local", "", "native")]
                        else:
                            tcs = [tc for tc in all_catalog + all_local if tc.category == cat_key]
                        with Collapsible(title=f"{cat_label} ({len(tcs)})", collapsed=True, id=f"cat-{cat_key}"):
                            table = S4DataTable(id=f"tbl-{cat_key}", cursor_type="row", zebra_stripes=True)
                            yield table

                # SelfBuild panel (hidden by default)
                with VerticalScroll(id="tc-selfbuild"):
                    with Collapsible(title="OE-SDK (0)", collapsed=True, id="cat-sb-oesdk"):
                        yield S4DataTable(id="tbl-sb-oesdk", cursor_type="row", zebra_stripes=True)
                    with Collapsible(title="CrosstoolNG (0)", collapsed=True, id="cat-sb-ctng"):
                        yield S4DataTable(id="tbl-sb-ctng", cursor_type="row", zebra_stripes=True)
                    with Collapsible(title="FreetzNG (0)", collapsed=True, id="cat-sb-freetz"):
                        yield S4DataTable(id="tbl-sb-freetz", cursor_type="row", zebra_stripes=True)
                    with Collapsible(title="Android-SDK (0)", collapsed=True, id="cat-sb-android"):
                        yield S4DataTable(id="tbl-sb-android", cursor_type="row", zebra_stripes=True)

            # Right: Details + Output
            with Vertical(id="tc-right"):
                # Detail/Libraries/Log tab buttons
                with Horizontal(id="tc-tab-bar"):
                    yield Static(" [b]D[/b]etails ", id="btn-tab-details", classes="tab-active")
                    yield Static(" [b]L[/b]ibraries ", id="btn-tab-libs", classes="tab-inactive")
                    yield Static(" [b]O[/b]utput ", id="btn-tab-log", classes="tab-inactive")

                # Details panel
                detail = Static("", id="tc-detail")
                detail.auto_links = False
                yield detail

                # Libraries panel (hidden by default)
                with VerticalScroll(id="tc-libs"):
                    yield Static("", id="tc-libs-header")
                    with Horizontal(classes="lib-action-row", id="lib-header-row"):
                        yield Static(f"  [dim]{t('lib_col_library'):<15s} {t('lib_col_installed'):<12s}[/dim]", classes="lib-status")
                        yield Static(f" [dim]{t('lib_col_action')}[/dim] ([dim]{t('lib_skip_all')}[/dim])", id="btn-skip-all", classes="lib-skip-all")
                    # Pre-create rows for all known libraries
                    for lib_name, _, _ in KNOWN_LIBS:
                        safe_id = lib_name.replace(".", "-").replace(" ", "-")
                        with Horizontal(classes="lib-action-row", id=f"lib-row-{safe_id}"):
                            yield Static(f"  {lib_name:<15s} {'—':<12s}", id=f"lib-status-{safe_id}", classes="lib-status")
                            yield Select(
                                options=[(t("lib_skip"), "skip")],
                                value="skip",
                                id=f"lib-action-{safe_id}",
                                classes="lib-select",
                                prompt=t("select_prompt"),
                            )

                # Output
                yield RichLog(highlight=False, markup=True, id="tc-output", wrap=True)

        yield S4Footer(active_screen="toolchains")

    def on_mount(self) -> None:
        reg = self._get_registry()

        # Populate installed table (from local yaml)
        tbl = self.query_one("#tbl-installed", DataTable)
        tbl.add_columns("Name", "Arch", "Description")
        for tc in reg.installed_toolchains():
            if tc.native:
                tbl.add_row(
                    Text(tc.name, style="bold"),
                    Text(tc.arch, style="bold"),
                    Text(tc.description, style="bold"),
                    key=tc.name,
                )
            else:
                tbl.add_row(tc.name, tc.arch, tc.description, key=tc.name)

        # Populate category tables
        all_catalog = reg.catalog_toolchains()
        all_local = reg.local_toolchains()
        for cat_key, _ in self._categories:
            if cat_key == "local":
                tcs = [tc for tc in all_local if tc.category in ("local", "", "native")]
            else:
                tcs = [tc for tc in all_catalog + all_local if tc.category == cat_key]
            if not tcs:
                continue
            try:
                tbl = self.query_one(f"#tbl-{cat_key}", DataTable)
                tbl.add_columns("●", "Name", "Arch", "Description")
                for tc in tcs:
                    status = "●" if tc.installed else ""
                    tbl.add_row(status, tc.name, tc.arch, tc.description, key=tc.name)
            except Exception:
                pass

        # SelfBuild tables (empty placeholders)
        for sb_id in ("tbl-sb-oesdk", "tbl-sb-ctng", "tbl-sb-freetz", "tbl-sb-android"):
            try:
                tbl_sb = self.query_one(f"#{sb_id}", DataTable)
                tbl_sb.add_columns("Name", "Arch", "Description")
            except Exception:
                pass

        # Border titles
        tc_output = self.query_one("#tc-output", RichLog)
        tc_output.border_title = "Output"
        tc_output.write("")  # Prevent IndexError on empty RichLog click

        # Restore log
        if ToolchainsScreen._log_history:
            log = self.query_one("#tc-output", RichLog)
            for msg in ToolchainsScreen._log_history:
                log.write(msg)

        # Set selection — either restore previous or pick first installed
        tbl = self.query_one("#tbl-installed", DataTable)
        if ToolchainsScreen._selected_tc:
            # Try to select the saved toolchain in installed table
            try:
                for i, key in enumerate(tbl.rows):
                    if key.value == ToolchainsScreen._selected_tc:
                        tbl.move_cursor(row=i)
                        break
            except Exception:
                pass
        elif tbl.row_count > 0:
            # First open — select first installed toolchain
            first_key = list(tbl.rows.keys())[0]
            ToolchainsScreen._selected_tc = first_key.value

        self._update_detail()
        self._update_f9_state()
        # Focus the installed table, not the filter field
        tbl.focus()
        # Delay enabling event handling — tables fire RowHighlighted during render
        self.set_timer(0.5, self._enable_events)

    def _enable_events(self) -> None:
        self._mounted = True

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tc-filter":
            self._apply_filter(event.value)

    def _tc_matches(self, tc, term_lower: str) -> bool:
        """Check if toolchain matches filter term."""
        if not term_lower:
            return True
        return (term_lower in tc.name.lower()
                or term_lower in tc.description.lower()
                or term_lower in tc.arch.lower()
                or term_lower in tc.machines.lower())

    def _apply_filter(self, term: str) -> None:
        """Filter toolchain tables by search term."""
        term_lower = term.lower().strip()
        reg = self._get_registry()

        # Rebuild installed table
        try:
            tbl = self.query_one("#tbl-installed", DataTable)
            tbl.clear()
            count = 0
            for tc in reg.installed_toolchains():
                if not self._tc_matches(tc, term_lower):
                    continue
                if tc.native:
                    tbl.add_row(
                        Text(tc.name, style="bold"),
                        Text(tc.arch, style="bold"),
                        Text(tc.description, style="bold"),
                        key=tc.name,
                    )
                else:
                    tbl.add_row(tc.name, tc.arch, tc.description, key=tc.name)
                count += 1
            self.query_one("#tc-installed-label", Static).update(f" [bold]Installed ({count})[/bold]")
        except Exception:
            pass

        # Rebuild category tables
        all_catalog = reg.catalog_toolchains()
        all_local = reg.local_toolchains()
        for cat_key, cat_label in self._categories:
            if cat_key == "local":
                tcs = [tc for tc in all_local if tc.category in ("local", "", "native")]
            else:
                tcs = [tc for tc in all_catalog + all_local if tc.category == cat_key]
            if not tcs:
                continue
            try:
                tbl = self.query_one(f"#tbl-{cat_key}", DataTable)
                tbl.clear()
                count = 0
                for tc in tcs:
                    if not self._tc_matches(tc, term_lower):
                        continue
                    status = "●" if tc.installed else ""
                    tbl.add_row(status, tc.name, tc.arch, tc.description, key=tc.name)
                    count += 1
                col = self.query_one(f"#cat-{cat_key}", Collapsible)
                col.title = f"{cat_label} ({count})"
            except Exception:
                pass

    def on_click(self, event) -> None:
        """Handle clicks on tab labels."""
        widget = event.widget if hasattr(event, 'widget') else None
        if widget:
            if widget.id == "btn-tab-details":
                self._show_tab("details")
            elif widget.id == "btn-tab-libs":
                self._show_tab("libs")
            elif widget.id == "btn-tab-log":
                self._show_tab("log")
            elif widget.id == "btn-skip-all":
                self._skip_all_libs()
            elif widget.id == "btn-ltab-prebuild":
                self._show_left_tab("prebuild")
            elif widget.id == "btn-ltab-selfbuild":
                self._show_left_tab("selfbuild")

    def _show_left_tab(self, tab: str) -> None:
        """Switch between PreBuild and SelfBuild views."""
        browser = self.query_one("#tc-browser", VerticalScroll)
        selfbuild = self.query_one("#tc-selfbuild", VerticalScroll)
        tab_pre = self.query_one("#btn-ltab-prebuild", Static)
        tab_self = self.query_one("#btn-ltab-selfbuild", Static)

        tab_pre.set_classes("tab-inactive")
        tab_self.set_classes("tab-inactive")

        if tab == "prebuild":
            browser.display = True
            selfbuild.display = False
            tab_pre.set_classes("tab-active")
        elif tab == "selfbuild":
            browser.display = False
            selfbuild.display = True
            tab_self.set_classes("tab-active")

    def _set_tc_status(self, tc_name: str, status: str) -> None:
        """Update the status and row color of a toolchain in its category DataTable."""
        from rich.text import Text
        reg = self._get_registry()
        tc = reg.get(tc_name)
        if not tc:
            return
        for cat_key, _ in self._categories:
            try:
                tbl = self.query_one(f"#tbl-{cat_key}", DataTable)
                row_keys = {k.value: k for k in tbl.rows}
                if tc_name not in row_keys:
                    continue
                rk = row_keys[tc_name]
                cols = list(tbl.columns.keys())
                if "installing" in status:
                    tbl.update_cell(rk, cols[0], Text("⟳", style="yellow"))
                    tbl.update_cell(rk, cols[1], Text(tc.name, style="yellow"))
                    tbl.update_cell(rk, cols[2], Text(tc.arch, style="yellow"))
                    tbl.update_cell(rk, cols[3], Text(tc.description, style="yellow"))
                elif "done" in status:
                    tbl.update_cell(rk, cols[0], Text("●", style="green"))
                    tbl.update_cell(rk, cols[1], Text(tc.name, style="green"))
                    tbl.update_cell(rk, cols[2], Text(tc.arch, style="green"))
                    tbl.update_cell(rk, cols[3], Text(tc.description, style="green"))
                elif "failed" in status:
                    tbl.update_cell(rk, cols[0], Text("✗", style="red"))
                    tbl.update_cell(rk, cols[1], Text(tc.name, style="red"))
                    tbl.update_cell(rk, cols[2], Text(tc.arch, style="red"))
                    tbl.update_cell(rk, cols[3], Text(tc.description, style="red"))
                else:
                    tbl.update_cell(rk, cols[0], "●" if tc.installed else "")
                    tbl.update_cell(rk, cols[1], tc.name)
                    tbl.update_cell(rk, cols[2], tc.arch)
                    tbl.update_cell(rk, cols[3], tc.description)
                return
            except Exception:
                continue

    def _set_installed_status(self, tc, status: str) -> None:
        """Show install progress as a temporary row in the installed table."""
        from rich.text import Text
        try:
            tbl = self.query_one("#tbl-installed", DataTable)
            temp_key = f"_inst_{tc.name}"
            # Remove previous temp row if exists
            try:
                tbl.remove_row(temp_key)
            except Exception:
                pass
            if status == "installing":
                tbl.add_row(
                    Text(tc.name, style="yellow"),
                    Text(tc.arch, style="yellow"),
                    Text("installing...", style="yellow"),
                    key=temp_key,
                )
            elif status == "done":
                # Remove temp row, add permanent row
                tbl.add_row(tc.name, tc.arch, tc.description, key=tc.name)
                # Update ● in category table
                self._set_tc_status(tc.name, "done")
            elif status == "failed":
                tbl.add_row(
                    Text(tc.name, style="red"),
                    Text(tc.arch, style="red"),
                    Text("failed", style="red"),
                    key=temp_key,
                )
        except Exception:
            pass

    def _set_installed_row_desc(self, tc_name: str, desc: str) -> None:
        """Update the description column of an installed toolchain row."""
        try:
            tbl = self.query_one("#tbl-installed", DataTable)
            row_keys = {k.value: k for k in tbl.rows}
            if tc_name not in row_keys:
                return
            rk = row_keys[tc_name]
            cols = list(tbl.columns.keys())
            tbl.update_cell(rk, cols[2], desc)  # 3rd column = Description
        except Exception:
            pass

    def _set_installed_row_style(self, tc_name: str, style: str) -> None:
        """Update the style of an existing row in the installed table."""
        try:
            tbl = self.query_one("#tbl-installed", DataTable)
            row_keys = {k.value: k for k in tbl.rows}
            if tc_name not in row_keys:
                return
            rk = row_keys[tc_name]
            cols = list(tbl.columns.keys())
            # Read current cell values as plain text
            for ci, col in enumerate(cols):
                val = tbl.get_cell(rk, col)
                plain = str(val) if not isinstance(val, Text) else val.plain
                if style:
                    tbl.update_cell(rk, col, Text(plain, style=style))
                else:
                    tbl.update_cell(rk, col, plain)
        except Exception:
            pass

    def _skip_all_libs(self) -> None:
        """Set all library action dropdowns to skip."""
        for lib_name, _, _ in KNOWN_LIBS:
            safe_id = lib_name.replace(".", "-").replace(" ", "-")
            try:
                sel = self.query_one(f"#lib-action-{safe_id}", Select)
                sel.value = "skip"
            except Exception:
                pass

    def _is_libs_tab_active(self) -> bool:
        try:
            return self.query_one("#tc-libs", VerticalScroll).display
        except Exception:
            return False

    def _update_active_tab(self) -> None:
        if self._is_libs_tab_active():
            self._update_libraries()
        else:
            self._update_detail()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not self._mounted or self._building:
            return
        if event.row_key and event.row_key.value:
            ToolchainsScreen._selected_tc = event.row_key.value
            self._update_active_tab()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._building:
            return
        if event.row_key and event.row_key.value:
            ToolchainsScreen._selected_tc = event.row_key.value
            self._update_active_tab()

    def _update_detail(self) -> None:
        try:
            name = ToolchainsScreen._selected_tc
            if not name:
                return
            reg = self._get_registry()
            tc = reg.get(name)
            if not tc:
                return
            status = "[green]installed[/green]" if tc.installed else "[red]not installed[/red]"
            lines = [
                f"  [bold]{tc.name}[/bold] — {tc.description}",
                f"  Status:    {status}",
                f"  Category:  {tc.category}     Arch: {tc.arch}",
            ]
            if tc.machines:
                lines.append(f"  Machines:  {tc.machines}")
            lines.append(f"  Prefix:    {tc.prefix}")
            if tc.sysroot:
                lines.append(f"  Sysroot:   {tc.sysroot}")
            if tc.cflags:
                lines.append(f"  C flags:   {tc.cflags}")
            if tc.ldflags:
                lines.append(f"  LD flags:  {tc.ldflags}")
            if tc.make_vars:
                lines.append(f"  Make vars: {tc.make_vars}")
            if tc.stapi_lib:
                lines.append(f"  STAPI:     {tc.stapi_lib}")
            if tc.url:
                lines.append(f"  URL:       [dim]{tc.url}[/dim]")
            self.query_one("#tc-detail", Static).update("\n".join(lines))
        except Exception:
            pass

    def _update_libraries(self) -> None:
        """Scan libraries and update pre-created rows with options."""
        if self._building:
            return  # Don't refresh while building — preserves status display
        try:
            name = ToolchainsScreen._selected_tc
            if not name:
                return
            reg = self._get_registry()
            tc = reg.get(name)
            if not tc:
                return

            header = self.query_one("#tc-libs-header", Static)

            if not tc.installed:
                header.update(f"  [dim]Not installed — install toolchain first[/dim]")
                return
                return

            install_dir = self.app.paths.toolchains_dir / tc.name
            libs = scan_libraries(install_dir, tc.sysroot, native=tc.native)

            recipes = load_library_recipes(self.app.paths.defaults_dir / "libraries.yaml", self.app.paths.libraries_local)
            has_c11 = self._check_c11(install_dir, tc.prefix)
            libs = check_library_updates(libs, recipes, has_c11=has_c11)

            recipe_map = {r["name"]: r for r in recipes}

            # Header just shows Library + Installed, Action aligns with dropdowns
            header.update("")

            for lib in libs:
                safe_id = lib.name.replace(".", "-").replace(" ", "-")
                if lib.found:
                    source = "[dim](sys)[/dim]" if lib.pc_file == "system" else "[dim](local)[/dim]" if tc.native else ""
                    installed = f"{lib.version} {source}" if source else lib.version
                else:
                    installed = "—"

                # Update status label
                try:
                    status_widget = self.query_one(f"#lib-status-{safe_id}", Static)
                    if lib.found and lib.up_to_date:
                        status_widget.update(f"  [green]{lib.name:<15s}[/green] {installed}")
                    elif lib.found:
                        status_widget.update(f"  [yellow]{lib.name:<15s}[/yellow] {installed}")
                    else:
                        status_widget.update(f"  [dim]{lib.name:<15s}[/dim] {'—':<12s}")
                except Exception:
                    continue

                # Build dropdown options
                recipe = recipe_map.get(lib.name, {})
                variants = recipe.get("variants", [])

                options: list[tuple[str, str]] = []

                if lib.found and lib.up_to_date:
                    options.append((t("lib_skip_uptodate"), "skip"))
                    options.append((f"{t('lib_reinstall')} {lib.version}", f"reinstall:{lib.version}"))
                elif lib.found and lib.update_version:
                    options.append((t("lib_skip"), "skip"))
                    options.append((f"→ {lib.update_version} ({t('lib_recommended')})", f"update:{lib.update_version}"))
                elif not lib.found and lib.update_version:
                    options.append((t("lib_skip"), "skip"))
                    options.append((f"{t('lib_install')} {lib.update_version} ({t('lib_recommended')})", f"install:{lib.update_version}"))
                else:
                    options.append((t("lib_skip"), "skip"))

                for v in variants:
                    ver = v.get("version", "")
                    label = v.get("label", ver)
                    needs_c11 = v.get("requires") == "c11"
                    if needs_c11 and not has_c11:
                        req = f" ({t('lib_needs_c11')})"
                    elif needs_c11:
                        req = " [c11]"
                    else:
                        req = ""
                    action = "install" if not lib.found else "update"
                    opt_value = f"{action}:{ver}"
                    if opt_value not in [o[1] for o in options]:
                        action_label = t("lib_install") if action == "install" else t("lib_update")
                        options.append((f"{action_label} {ver} — {label}{req}", opt_value))

                # Default
                if lib.found and lib.up_to_date:
                    default = "skip"
                elif lib.update_version:
                    action = "install" if not lib.found else "update"
                    default = f"{action}:{lib.update_version}"
                else:
                    default = "skip"

                # Update select options
                try:
                    sel = self.query_one(f"#lib-action-{safe_id}", Select)
                    sel.set_options(options)
                    sel.value = default
                except Exception:
                    pass

        except Exception:
            pass

    def _update_f9_state(self) -> None:
        """Update F10 button: Update/Cancel based on build state."""
        try:
            for item in self.query("FooterItem"):
                if item._key == "F9":
                    if self._building:
                        item._label = t("cancel")
                        item._disabled = False
                        item._active = True
                        item.remove_class("disabled")
                    else:
                        item._label = t("update")
                        item._active = False
                        libs_active = self._is_libs_tab_active()
                        item._disabled = not libs_active
                        if libs_active:
                            item.remove_class("disabled")
                        else:
                            item.add_class("disabled")
                    item.refresh()
                    break
        except Exception:
            pass

    def do_tc_update(self) -> None:
        """F9 — execute library actions or cancel running build."""
        if self._building:
            # Cancel running build
            self._cancel_requested = True
            self._log_sync("[yellow]Cancelling build...[/yellow]")
            return

        if not self._is_libs_tab_active():
            return

        name = ToolchainsScreen._selected_tc
        if not name:
            self.notify(t("no_toolchain_selected"), severity="warning")
            return

        reg = self._get_registry()
        tc = reg.get(name)
        if not tc or not tc.installed:
            self.notify("Toolchain not installed", severity="warning")
            return

        # Collect actions from dropdowns
        actions = []
        for lib_name, _, _ in KNOWN_LIBS:
            safe_id = lib_name.replace(".", "-").replace(" ", "-")
            try:
                sel = self.query_one(f"#lib-action-{safe_id}", Select)
                if sel.value and isinstance(sel.value, str) and sel.value != "skip":
                    actions.append((lib_name, sel.value))
            except Exception:
                pass

        if not actions:
            self.notify("No library actions selected", severity="warning")
            return

        # Sort by dependencies
        libraries_yaml = self.app.paths.defaults_dir / "libraries.yaml"
        actions = sort_by_dependencies(actions, libraries_yaml)

        self._building = True
        self._cancel_requested = False
        self._update_f9_state()
        order = ", ".join(name for name, _ in actions)
        self._log_sync(f"[bold]Building {len(actions)} libraries for {tc.name}:[/bold] {order}")

        def cancel_check() -> bool:
            return self._cancel_requested

        def _set_lib_status(lib_name: str, status: str) -> None:
            """Update the status label of a library during build."""
            safe_id = lib_name.replace(".", "-").replace(" ", "-")
            try:
                self.app.call_from_thread(
                    self.query_one(f"#lib-status-{safe_id}", Static).update,
                    f"  {status}"
                )
            except Exception:
                pass

        def do_build():
            try:
                for lib_name, action_str in actions:
                    if self._cancel_requested:
                        self._log("[yellow]Build cancelled[/yellow]")
                        break
                    parts = action_str.split(":", 1)
                    if len(parts) != 2:
                        continue
                    action_type, version = parts

                    # Show building indicator
                    _set_lib_status(lib_name, f"[bold yellow]{lib_name:<15s}[/bold yellow] [yellow]building...[/yellow]")
                    self._log(f"\n[bold]{action_type}: {lib_name} {version}[/bold]")

                    success = build_library(
                        lib_name=lib_name,
                        version=version,
                        toolchain_dir=self.app.paths.toolchains_dir / tc.name,
                        toolchain_prefix=tc.prefix,
                        toolchain_sysroot=tc.sysroot,
                        toolchain_cflags=tc.cflags,
                        toolchain_ldflags=tc.ldflags,
                        libraries_yaml=self.app.paths.defaults_dir / "libraries.yaml",
                        libraries_local=self.app.paths.libraries_local,
                        downloads_dir=self.app.paths.downloads_dir,
                        logs_dir=self.app.paths.logs_dir,
                        max_cpus=self.app.settings.max_cpus,
                        native=tc.native,
                        cancel_check=cancel_check,
                        log=self._log,
                    )
                    if success:
                        _set_lib_status(lib_name, f"[bold green]{lib_name:<15s}[/bold green] [green]done[/green]")
                    else:
                        if self._cancel_requested:
                            _set_lib_status(lib_name, f"[bold yellow]{lib_name:<15s}[/bold yellow] [yellow]cancelled[/yellow]")
                            break
                        else:
                            _set_lib_status(lib_name, f"[bold red]{lib_name:<15s}[/bold red] [red]failed[/red]")

                self._log(f"\n[bold]Done[/bold]")
            finally:
                self._building = False
                self._cancel_requested = False
                self.app.call_from_thread(self._update_f9_state)
                # Refresh library info (stay on Libraries tab)
                self.app.call_from_thread(self._update_libraries)

        thread = threading.Thread(target=do_build, daemon=True)
        thread.start()

    def _refresh(self) -> None:
        self._registry = None
        self.app.pop_screen()
        self.app.push_screen(ToolchainsScreen())

    def _log(self, msg: str) -> None:
        ToolchainsScreen._log_history.append(msg)
        try:
            self.app.call_from_thread(self.query_one("#tc-output", RichLog).write, msg)
        except Exception:
            pass

    def _log_sync(self, msg: str) -> None:
        ToolchainsScreen._log_history.append(msg)
        try:
            self.query_one("#tc-output", RichLog).write(msg)
        except Exception:
            pass

    def _run_in_thread(self, func) -> None:
        def wrapper():
            func()
            self.app.call_from_thread(self._refresh)
        thread = threading.Thread(target=wrapper, daemon=True)
        thread.start()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id
        if btn == "btn-install":
            self._handle_install()
        elif btn == "btn-backup":
            self._handle_backup()
        elif btn == "btn-restore":
            self._handle_restore()
        elif btn == "btn-check":
            self._handle_check()
        elif btn == "btn-remove":
            self._handle_remove()

    def _show_tab(self, tab: str) -> None:
        """Switch between Details, Libraries and Log views."""
        detail = self.query_one("#tc-detail", Static)
        libs = self.query_one("#tc-libs", VerticalScroll)
        output = self.query_one("#tc-output", RichLog)
        tab_details = self.query_one("#btn-tab-details", Static)
        tab_libs = self.query_one("#btn-tab-libs", Static)
        tab_log = self.query_one("#btn-tab-log", Static)

        # Reset all tabs to inactive
        tab_details.set_classes("tab-inactive")
        tab_libs.set_classes("tab-inactive")
        tab_log.set_classes("tab-inactive")

        # Hide all panels
        detail.display = False
        libs.display = False

        if tab == "details":
            detail.display = True
            tab_details.set_classes("tab-active")
            output.styles.height = "1fr"
            self._update_detail()
        elif tab == "libs":
            libs.display = True
            tab_libs.set_classes("tab-active")
            output.styles.height = "1fr"
            self._update_libraries()
        elif tab == "log":
            tab_log.set_classes("tab-active")
            output.styles.height = "1fr"

        # Update F10 button state in footer
        self._update_f9_state()

    def _check_c11(self, install_dir: Path, prefix: str) -> bool:
        """Check if toolchain's gcc supports C11 (>= GCC 4.9)."""
        gcc = install_dir / "bin" / f"{prefix}gcc"
        if not gcc.exists():
            found = list(install_dir.glob(f"**/bin/{prefix}gcc"))
            gcc = found[0] if found else gcc
        if not gcc.exists():
            return True  # assume modern if we can't check
        try:
            version_out = subprocess.check_output(
                [str(gcc), "-dumpversion"], text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
            parts = version_out.split(".")
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
            return (major, minor) >= (4, 9)
        except Exception:
            return True

    def _handle_install(self) -> None:
        name = ToolchainsScreen._selected_tc
        if not name:
            self.notify(t("no_toolchain_selected"), severity="warning")
            return
        reg = self._get_registry()
        tc = reg.get(name)
        if not tc:
            return
        if tc.installed:
            self.notify(t("already_installed", name=tc.name), severity="warning")
            return
        if not tc.url:
            self.notify(t("no_download_url", name=tc.name), severity="error")
            return

        self._log_sync(f"[bold]Installing {tc.name}...[/bold]")

        # Show installing status in installed table (unfocused = colors visible)
        self._set_installed_status(tc, "installing")

        def do_install():
            install_dir = self.app.paths.toolchains_dir / tc.name
            downloads_dir = self.app.paths.downloads_dir
            downloads_dir.mkdir(parents=True, exist_ok=True)

            url = tc.url
            filename = url.split("/")[-1]
            filepath = downloads_dir / filename

            # Handle local file:// URLs — resolve relative to downloads dir
            if url.startswith("file://"):
                file_ref = url.replace("file://", "")
                local_path = Path(file_ref) if file_ref.startswith("/") else downloads_dir / file_ref
                if not local_path.exists():
                    self._log(f"[red]Archive not found: {local_path}[/red]")
                    self.app.call_from_thread(self._set_installed_status, tc, "failed")
                    return
                filepath = local_path
                self._log(f"Using local archive: {filepath.name}")
            else:
                # Skip download if file exists and checksum matches
                if filepath.exists() and tc.checksum and self._verify_checksum(filepath, tc.checksum):
                    self._log(f"Using cached {filename} (checksum OK)")
                else:
                    self._log(f"Downloading {filename}...")
                    try:
                        result = subprocess.run(
                            ["wget", "-q", "-O", str(filepath), url],
                            capture_output=True, text=True, timeout=600,
                        )
                        if result.returncode != 0:
                            result = subprocess.run(
                                ["curl", "-sL", "-o", str(filepath), url],
                                capture_output=True, text=True, timeout=600,
                            )
                        if result.returncode != 0:
                            self._log("[red]Download failed[/red]")
                            self.app.call_from_thread(self._set_installed_status, tc, "failed")
                            return
                    except Exception as e:
                        self._log(f"[red]Download failed: {e}[/red]")
                        self.app.call_from_thread(self._set_installed_status, tc, "failed")
                        return

            if tc.checksum:
                self._log("Verifying checksum...")
                if not self._verify_checksum(filepath, tc.checksum):
                    self._log("[red]Checksum mismatch![/red]")
                    filepath.unlink(missing_ok=True)
                    self.app.call_from_thread(self._set_installed_status, tc, "failed")
                    return
                self._log("[green]Checksum OK[/green]")

            self._log(f"Extracting to {install_dir.name}...")
            try:
                install_dir.mkdir(parents=True, exist_ok=True)
                result = subprocess.run(
                    ["tar", "-xf", str(filepath), f"--strip-components={tc.strip_components}", "-C", str(install_dir)],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode != 0:
                    self._log(f"[red]Extract failed: {result.stderr.strip()}[/red]")
                    shutil.rmtree(install_dir, ignore_errors=True)
                    self.app.call_from_thread(self._set_installed_status, tc, "failed")
                    return
            except Exception as e:
                self._log(f"[red]Extract failed: {e}[/red]")
                shutil.rmtree(install_dir, ignore_errors=True)
                self.app.call_from_thread(self._set_installed_status, tc, "failed")
                return

            self._relocate_toolchain(install_dir, tc)
            self._fix_pc_files(install_dir)
            self._fix_la_files(install_dir)
            # Add entry to local yaml only if not already there (defaults → local copy)
            reg = self._get_registry()
            local_tcs = reg._load_file(reg.local_path)
            local_names = {t.name for t in local_tcs}
            if tc.name not in local_names:
                local_tcs.append(tc)
                reg.save_local(local_tcs)
            self._log(f"[green]{tc.name} installed successfully[/green]")
            self.app.call_from_thread(self._set_installed_status, tc, "done")

        self._run_in_thread(do_install)

    def _handle_backup(self) -> None:
        """Backup the selected installed toolchain to a .tar.xz archive."""
        name = ToolchainsScreen._selected_tc
        if not name:
            self.notify(t("no_toolchain_selected"), severity="warning")
            return
        reg = self._get_registry()
        tc = reg.get(name)
        if not tc:
            return
        if tc.native:
            self.notify("Native toolchain cannot be backed up", severity="warning")
            return
        if not tc.installed:
            self.notify(t("not_installed", name=tc.name), severity="warning")
            return

        install_dir = self.app.paths.toolchains_dir / tc.name
        backup_dir = self.app.paths.downloads_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = backup_dir / f"Toolchain-{tc.name}.tar.xz"

        self._log_sync(f"[bold]Backing up {tc.name}...[/bold]")
        self._set_installed_row_desc(tc.name, "backing up...")

        def do_backup():
            from datetime import datetime
            from rich.markup import escape as rich_escape

            # Setup log file
            logs_dir = self.app.paths.logs_dir
            logs_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
            log_path = logs_dir / f"{timestamp}.{tc.name}.backup.log"
            log_file = open(log_path, "w", encoding="utf-8")

            def blog(msg):
                import re
                plain = re.sub(r'\[/?[a-z_ ]+\]', '', msg)
                log_file.write(plain + "\n")
                log_file.flush()
                self._log(msg)

            try:
                blog(f"Source: {install_dir}")
                blog(f"Target: {backup_file}")
                blog(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                blog("")

                # Create backup with verbose output
                proc = subprocess.Popen(
                    ["tar", "-cJvf", str(backup_file), "-C", str(self.app.paths.toolchains_dir), tc.name],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                for line in proc.stdout:
                    blog(f"  {rich_escape(line.rstrip())}")
                proc.wait()

                if proc.returncode != 0:
                    blog(f"[red]Backup failed (exit {proc.returncode})[/red]")
                    self.app.call_from_thread(self._set_installed_row_desc, tc.name, "[red]backup failed[/red]")
                    return

                size_mb = backup_file.stat().st_size / (1024 * 1024)

                # Update checksum + url in local yaml only for local (file://) toolchains
                if not tc.url or tc.url.startswith("file://"):
                    import hashlib as _hashlib
                    h = _hashlib.sha256()
                    with open(backup_file, "rb") as bf:
                        for chunk in iter(lambda: bf.read(8192), b""):
                            h.update(chunk)
                    new_checksum = f"sha256:{h.hexdigest()}"

                    reg = self._get_registry()
                    local_tcs = reg._load_file(reg.local_path)
                    for ltc in local_tcs:
                        if ltc.name == tc.name:
                            ltc.url = f"file://{backup_file.name}"
                            ltc.checksum = new_checksum
                            break
                    reg.save_local(local_tcs)

                blog(f"")
                blog(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                blog(f"Size: {size_mb:.1f} MB")
                blog(f"Checksum: {new_checksum}")
                blog(f"Log: {log_path}")
                blog(f"[green]{tc.name} backed up to {backup_file.name}[/green]")
                self.app.call_from_thread(self._set_installed_row_desc, tc.name, f"{tc.description} [green]✓ backed up[/green]")
            except Exception as e:
                blog(f"[red]Backup failed: {e}[/red]")
                self.app.call_from_thread(self._set_installed_row_desc, tc.name, "[red]backup failed[/red]")
            finally:
                log_file.close()

        thread = threading.Thread(target=do_backup, daemon=True)
        thread.start()

    def _handle_restore(self) -> None:
        """Restore a toolchain from backup or import from file."""
        name = ToolchainsScreen._selected_tc
        reg = self._get_registry()
        tc = reg.get(name) if name else None

        # Check for pending confirmation (second press)
        if hasattr(self, '_pending_restore') and self._pending_restore:
            pending_tc, pending_file = self._pending_restore
            self._pending_restore = None
            if pending_tc.name == (tc.name if tc else ""):
                self._do_restore_from_backup(pending_tc, pending_file)
                return

        # Check if selected toolchain has a local backup
        if tc and tc.installed:
            backup_file = self.app.paths.downloads_dir / f"Toolchain-{tc.name}.tar.xz"
            if not backup_file.exists():
                for ext in (".tar.bz2", ".tar.gz"):
                    alt = self.app.paths.downloads_dir / f"Toolchain-{tc.name}{ext}"
                    if alt.exists():
                        backup_file = alt
                        break
            if backup_file.exists():
                size_mb = backup_file.stat().st_size / (1024 * 1024)
                self.notify(
                    f"Restore {tc.name} from {backup_file.name} ({size_mb:.1f} MB)? Press Restore again to confirm.",
                    severity="information",
                )
                self._pending_restore = (tc, backup_file)
                return

        # No backup found — open file picker
        start = str(self.app.paths.downloads_dir) if self.app.paths.downloads_dir.exists() else "/"
        self.app.push_screen(FilePickerScreen(start), callback=self._do_restore)

    def _do_restore_from_backup(self, tc, backup_file: Path) -> None:
        """Restore an installed toolchain from its local backup."""
        install_dir = self.app.paths.toolchains_dir / tc.name

        self._log_sync(f"[bold]Restoring {tc.name} from {backup_file.name}...[/bold]")
        self._set_installed_row_desc(tc.name, "restoring...")

        def do_restore_backup():
            # Remove existing
            if install_dir.exists():
                self._log("Removing current installation...")
                shutil.rmtree(install_dir)

            # Extract without checksum verification
            self._log(f"Extracting {backup_file.name}...")
            try:
                install_dir.mkdir(parents=True, exist_ok=True)
                result = subprocess.run(
                    ["tar", "-xf", str(backup_file), "-C", str(install_dir)],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode != 0:
                    self._log(f"[red]Extract failed: {result.stderr.strip()}[/red]")
                    shutil.rmtree(install_dir, ignore_errors=True)
                    self.app.call_from_thread(self._set_installed_row_desc, tc.name, "[red]restore failed[/red]")
                    return
            except Exception as e:
                self._log(f"[red]Extract failed: {e}[/red]")
                shutil.rmtree(install_dir, ignore_errors=True)
                self.app.call_from_thread(self._set_installed_row_desc, tc.name, "[red]restore failed[/red]")
                return

            # Flatten if needed
            subdirs = [d for d in install_dir.iterdir() if d.is_dir()]
            if len(subdirs) == 1 and not (install_dir / "bin").exists():
                subdir = subdirs[0]
                for item in subdir.iterdir():
                    item.rename(install_dir / item.name)
                subdir.rmdir()

            self._fix_pc_files(install_dir)
            self._fix_la_files(install_dir)
            self._relocate_toolchain(install_dir, tc)
            self._log(f"[green]{tc.name} restored from backup[/green]")
            self.app.call_from_thread(self._set_installed_row_desc, tc.name, tc.description)

        self._run_in_thread(do_restore_backup)

    def _do_restore(self, result: tuple) -> None:
        """Process the selected file from the file picker."""
        filepath, name = result if result else ("", "")
        if not filepath or not name:
            return

        path = Path(filepath)
        if not path.exists():
            self.notify(f"File not found: {filepath}", severity="error")
            return
        if not path.name.endswith((".tar.xz", ".tar.bz2", ".tar.gz")):
            self.notify("Only .tar.xz, .tar.bz2 or .tar.gz files supported", severity="error")
            return

        install_dir = self.app.paths.toolchains_dir / name
        if install_dir.exists():
            self.notify(f"'{name}' already installed — remove first or choose a different name", severity="warning")
            return

        self._log_sync(f"[bold]Restoring {name}...[/bold]")
        temp_tc = Toolchain(name=name, arch="...", description="restoring...")
        self._set_installed_status(temp_tc, "installing")

        def do_restore():
            import hashlib

            # Copy archive to downloads dir
            downloads_dir = self.app.paths.downloads_dir
            downloads_dir.mkdir(parents=True, exist_ok=True)
            # Always name archive after the toolchain name
            ext = ".tar.xz"
            for e in (".tar.xz", ".tar.bz2", ".tar.gz"):
                if path.name.endswith(e):
                    ext = e
                    break
            local_archive = downloads_dir / f"Toolchain-{name}{ext}"

            if path.resolve() != local_archive.resolve():
                self._log(f"Copying archive to {local_archive.name}...")
                shutil.copy2(str(path), str(local_archive))

            # Calculate checksum
            self._log("Calculating checksum...")
            h = hashlib.sha256()
            with open(local_archive, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            checksum = f"sha256:{h.hexdigest()}"

            # Extract from local copy
            self._log(f"Extracting {local_archive.name}...")
            try:
                install_dir.mkdir(parents=True, exist_ok=True)
                result = subprocess.run(
                    ["tar", "-xf", str(local_archive), "-C", str(install_dir)],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode != 0:
                    self._log(f"[red]Extract failed: {result.stderr.strip()}[/red]")
                    shutil.rmtree(install_dir, ignore_errors=True)
                    self.app.call_from_thread(self._set_installed_status, temp_tc, "failed")
                    return
            except Exception as e:
                self._log(f"[red]Extract failed: {e}[/red]")
                shutil.rmtree(install_dir, ignore_errors=True)
                self.app.call_from_thread(self._set_installed_status, temp_tc, "failed")
                return

            # Flatten if extracted into single subdirectory
            subdirs = [d for d in install_dir.iterdir() if d.is_dir()]
            if len(subdirs) == 1 and not (install_dir / "bin").exists():
                subdir = subdirs[0]
                for item in subdir.iterdir():
                    item.rename(install_dir / item.name)
                subdir.rmdir()

            # Fix .pc files with wrong prefix paths from backup
            self._fix_pc_files(install_dir)
            self._fix_la_files(install_dir)

            # Check if this toolchain exists in defaults catalog (use its config)
            reg = self._get_registry()
            default_tc = reg.get_from_defaults(name)
            if default_tc:
                tc = default_tc
                self._log(f"  Matched catalog: {tc.description}")
            else:
                # Auto-detect toolchain properties
                self._log("Detecting toolchain properties...")
                tc = self._detect_toolchain(name, install_dir)
                if not tc:
                    self._log("[red]Could not detect compiler in restored toolchain[/red]")
                    shutil.rmtree(install_dir, ignore_errors=True)
                    self.app.call_from_thread(self._set_installed_status, temp_tc, "failed")
                    return

            # Set url and checksum from local archive
            tc.url = f"file://{local_archive.name}"
            tc.checksum = checksum

            # Save to local yaml
            local_tcs = [t for t in reg._load_file(reg.local_path) if t.name != tc.name]
            local_tcs.append(tc)
            reg.save_local(local_tcs)

            self._log(f"  Name:     {tc.name}")
            self._log(f"  Prefix:   {tc.prefix}")
            self._log(f"  Arch:     {tc.arch}")
            self._log(f"  Sysroot:  {tc.sysroot or '(none)'}")
            self._log(f"  Archive:  {local_archive.name}")
            self._log(f"  Checksum: {checksum}")
            self._log(f"[green]{name} restored successfully[/green]")

        self._run_in_thread(do_restore)

    def _fix_pc_files(self, install_dir: Path) -> None:
        """Fix prefix= paths in .pc files after import/install.

        .pc files from backups or OE-SDK installs contain stale absolute paths
        from the original build/install location. We replace the old prefix with
        the correct current absolute path.

        We keep absolute paths (not ${pcfiledir} relative) because:
        - OE-SDK itself uses absolute paths + PKG_CONFIG_SYSROOT_DIR
        - configure scripts do string comparisons on include paths
        - Relative ../../ paths cause issues with some build systems

        Typical .pc layout:  <sysroot>/usr/lib/pkgconfig/foo.pc
        prefix should be:    <sysroot>/usr  (= pkgconfig dir / ../../)
        """
        pc_files = list(install_dir.glob("**/pkgconfig/*.pc"))
        self._log(f"  Checking {len(pc_files)} .pc files...")
        for pc_file in pc_files:
            try:
                content = pc_file.read_text(encoding="utf-8", errors="replace")
                old_prefix = ""
                for line in content.splitlines():
                    if line.startswith("prefix="):
                        old_prefix = line.split("=", 1)[1].strip()
                        break
                if not old_prefix:
                    continue

                # New prefix = two levels up from pkgconfig dir
                # e.g. .../sysroot/usr/lib/pkgconfig → .../sysroot/usr
                new_prefix = str(pc_file.parent.parent.parent)

                if old_prefix != new_prefix:
                    content = content.replace(old_prefix, new_prefix)
                    pc_file.write_text(content, encoding="utf-8")
                    self._log(f"  Fixed {pc_file.name}")
            except Exception:
                pass

    def _fix_la_files(self, install_dir: Path) -> None:
        """Fix .la files: replace '=' sysroot prefix in dependency_libs with actual path.

        OE-SDK .la files use '=/usr/lib/...' as sysroot-relative paths, but
        libtool on the host doesn't understand the '=' prefix. We replace it
        with the actual sysroot path.
        """
        la_files = list(install_dir.glob("**/*.la"))
        if not la_files:
            return
        # Find sysroot — look for usr/lib pattern
        sysroot = ""
        for la in la_files:
            # Walk up from .la to find the sysroot root (parent of usr/lib or lib)
            p = la.parent
            while p != install_dir and p != p.parent:
                if (p / "usr" / "lib").is_dir() or (p / "lib").is_dir():
                    sysroot = str(p)
                    break
                p = p.parent
            if sysroot:
                break
        if not sysroot:
            return

        fixed = 0
        for la_file in la_files:
            try:
                content = la_file.read_text(encoding="utf-8", errors="replace")
                if "dependency_libs" not in content or "=/" not in content:
                    continue
                # Replace =/usr/lib/... with <sysroot>/usr/lib/...
                new_content = content.replace(" =/", f" {sysroot}/")
                if new_content != content:
                    la_file.write_text(new_content, encoding="utf-8")
                    fixed += 1
            except Exception:
                pass
        if fixed:
            self._log(f"  Fixed {fixed} .la files (sysroot={sysroot})")

    def _detect_toolchain(self, name: str, install_dir: Path) -> "Toolchain | None":
        """Auto-detect toolchain properties from an extracted directory."""
        from core.toolchain import Toolchain

        # Find *-gcc in bin/
        gcc_bins = list(install_dir.glob("bin/*-gcc"))
        if not gcc_bins:
            # Try deeper paths (OE-SDK style)
            gcc_bins = list(install_dir.glob("**/bin/*-gcc"))
            gcc_bins = [g for g in gcc_bins if "++" not in g.name and g.name.endswith("-gcc")]
        if not gcc_bins:
            return None

        gcc = gcc_bins[0]
        gcc_name = gcc.name  # e.g. "arm-linux-gnueabihf-gcc"
        prefix = gcc_name[:-3]  # strip "gcc" → "arm-linux-gnueabihf-"

        # Get arch from dumpmachine
        arch = ""
        try:
            dumpmachine = subprocess.check_output(
                [str(gcc), "-dumpmachine"], text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
            # Derive arch: arm-linux-gnueabihf → armv7, aarch64-... → aarch64, mipsel-... → mipsel
            if dumpmachine.startswith("aarch64"):
                arch = "aarch64"
            elif dumpmachine.startswith("arm"):
                arch = "armv7"
            elif dumpmachine.startswith("mipsel"):
                arch = "mipsel"
            elif dumpmachine.startswith("mips"):
                arch = "mips"
            elif dumpmachine.startswith("sh4"):
                arch = "sh4"
            elif dumpmachine.startswith("x86_64"):
                arch = "x86_64"
            elif dumpmachine.startswith("i686") or dumpmachine.startswith("i386"):
                arch = "x86"
            elif dumpmachine.startswith("powerpc"):
                arch = "ppc"
            else:
                arch = dumpmachine.split("-")[0]
        except Exception:
            arch = "unknown"

        # Detect sysroot
        sysroot = ""
        # Check common sysroot patterns relative to install_dir
        for pattern in ("sysroots/*/", f"{prefix.rstrip('-')}/sysroot", f"{prefix.rstrip('-')}/sys-root",
                        "sysroot", "sys-root"):
            matches = list(install_dir.glob(pattern))
            if matches:
                sr = matches[0]
                # If sysroot contains usr/, include it (needed for _find_prefix)
                if (sr / "usr").is_dir():
                    sr = sr / "usr"
                sysroot = str(sr.relative_to(install_dir))
                break
        # Try gcc -print-sysroot
        if not sysroot:
            try:
                gs = subprocess.check_output(
                    [str(gcc), "-print-sysroot"], text=True, stderr=subprocess.DEVNULL, timeout=5,
                ).strip()
                if gs and Path(gs).is_dir():
                    try:
                        sysroot = str(Path(gs).relative_to(install_dir))
                    except ValueError:
                        pass
            except Exception:
                pass

        # Get description from gcc version
        description = name
        try:
            ver_out = subprocess.check_output(
                [str(gcc), "--version"], text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).splitlines()[0]
            description = f"{name} ({ver_out.split(')')[-1].strip() if ')' in ver_out else ver_out})"
        except Exception:
            pass

        return Toolchain(
            name=name,
            description=description,
            category="local",
            arch=arch,
            prefix=prefix,
            sysroot=sysroot,
        )

    def _handle_check(self) -> None:
        """Comprehensive toolchain check like s3: compiler, sysroot, headers, libraries."""
        name = ToolchainsScreen._selected_tc
        if not name:
            self.notify(t("no_toolchain_selected"), severity="warning")
            return
        reg = self._get_registry()
        tc = reg.get(name)
        if not tc:
            return
        if not tc.installed:
            self.notify(t("not_installed", name=tc.name), severity="warning")
            return

        install_dir = self.app.paths.toolchains_dir / tc.name
        ok = True
        log = self._log_sync
        fmt_ok = lambda label, val, extra="": f"  [green]✓[/green] {label:<16s} {val}" + (f"  [dim]{extra}[/dim]" if extra else "")
        fmt_fail = lambda label, val: f"  [red]✗[/red] {label:<16s} {val}"

        # --- Compiler Info ---
        log(f"\n[bold]Compiler info -----> {tc.name}[/bold]")
        log(f"  {'=' * 40}")

        gcc = install_dir / "bin" / f"{tc.prefix}gcc"
        if not gcc.exists():
            found = list(install_dir.glob(f"**/bin/{tc.prefix}gcc"))
            if found:
                gcc = found[0]

        if not gcc.exists() and not tc.native:
            log(fmt_fail("GCC Binary", f"{tc.prefix}gcc not found"))
            return
        if tc.native:
            gcc_path = shutil.which("gcc")
            if not gcc_path:
                log(fmt_fail("GCC Binary", "gcc not found"))
                return
            gcc = Path(gcc_path)

        # GCC version
        try:
            version = subprocess.check_output(
                [str(gcc), "--version"], text=True, stderr=subprocess.DEVNULL, timeout=10,
            ).splitlines()[0]
            log(fmt_ok("GCC Version", version))
        except Exception as e:
            log(fmt_fail("GCC Version", str(e)))
            ok = False

        # GCC binary path (relative to install dir)
        try:
            rel_gcc = gcc.resolve().relative_to(install_dir.resolve())
            log(fmt_ok("GCC Binary", str(rel_gcc)))
        except ValueError:
            log(fmt_ok("GCC Binary", str(gcc)))

        # Sysroot
        if tc.sysroot:
            sysroot_path = install_dir / tc.sysroot
            if sysroot_path.is_dir():
                log(fmt_ok("GCC Sysroot", tc.sysroot))
            else:
                log(fmt_fail("GCC Sysroot", f"{tc.sysroot} NOT FOUND"))
                ok = False
        elif not tc.native:
            # Try gcc -print-sysroot
            try:
                sr = subprocess.check_output(
                    [str(gcc), "-print-sysroot"], text=True, stderr=subprocess.DEVNULL, timeout=5,
                ).strip()
                if sr and Path(sr).is_dir():
                    log(fmt_ok("GCC Sysroot", sr))
                else:
                    log(fmt_fail("GCC Sysroot", sr or "(empty)"))
            except Exception:
                pass

        # GDB version
        gdb = str(gcc).replace("gcc", "gdb")
        try:
            gdb_ver = subprocess.check_output(
                [gdb, "--version"], text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).splitlines()[0].split()[-1]
            log(fmt_ok("GDB Version", gdb_ver))
        except Exception:
            pass

        # LD version
        ld = str(gcc).replace("gcc", "ld")
        try:
            ld_ver = subprocess.check_output(
                [ld, "--version"], text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).splitlines()[0].split()[-1]
            log(fmt_ok("LD Version", ld_ver))
        except Exception:
            pass

        # Host + Target arch
        try:
            target_arch = subprocess.check_output(
                [str(gcc), "-dumpmachine"], text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
            log(fmt_ok("TARGET arch", target_arch))
        except Exception:
            target_arch = ""

        import platform as _platform
        log(fmt_ok("HOST arch", f"{_platform.machine()}-{_platform.system().lower()}"))

        # C11 support
        has_c11 = self._check_c11(install_dir, tc.prefix)
        if has_c11:
            log(fmt_ok("C11 Support", "Yes"))
        else:
            log(fmt_ok("C11 Support", "[yellow]No[/yellow]"))

        # --- Sysroot Config ---
        sysroot_path = install_dir / tc.sysroot if tc.sysroot else install_dir
        if tc.native:
            sysroot_path = Path("/")

        log(f"\n[bold]Sysroot config ----> {tc.sysroot or '(system)'}[/bold]")
        log(f"  {'=' * 40}")

        # Linux headers
        linux_h = None
        for candidate in [
            sysroot_path / "usr" / "include" / "linux" / "version.h",
            sysroot_path / "include" / "linux" / "version.h",
        ]:
            if candidate.exists():
                linux_h = candidate
                break
        if linux_h:
            try:
                content = linux_h.read_text(errors="replace")
                linux_ver = ""
                for line in content.splitlines():
                    if "LINUX_VERSION_CODE" in line and "#define" in line:
                        try:
                            code = int(line.split()[-1])
                            major = code >> 16
                            minor = (code >> 8) & 0xFF
                            patch = code & 0xFF
                            linux_ver = f"{major}.{minor}.{patch}"
                        except (ValueError, IndexError):
                            linux_ver = line.strip()
                        break
                    elif "UTS_RELEASE" in line:
                        linux_ver = line.strip()
                        break
                log(fmt_ok("Linux", "version.h", linux_ver or "found"))
            except Exception:
                log(fmt_ok("Linux", "version.h"))
        else:
            log(fmt_fail("Linux", "version.h not found"))

        # C-Library detection (like s3: ldd --version for native, file search for cross)
        libc_type = ""
        libc_ver = ""
        if tc.native:
            try:
                ldd_out = subprocess.check_output(
                    ["ldd", "--version"], text=True, stderr=subprocess.STDOUT, timeout=5,
                ).splitlines()[0].lower()
                if "musl" in ldd_out:
                    libc_type = "musl"
                elif "glibc" in ldd_out or "gnu" in ldd_out:
                    libc_type = "glibc"
                # Extract version number
                import re
                ver_match = re.search(r'(\d+\.\d+)', ldd_out)
                if ver_match:
                    libc_ver = ver_match.group(1)
            except Exception:
                pass
        else:
            lib_search = [sysroot_path / "lib", sysroot_path / "usr" / "lib",
                          sysroot_path / "lib64", sysroot_path / "usr" / "lib64"]
            libc_so = None
            for candidate in ["libc.so.6", "libc.so", "libc.so.0"]:
                for lib_dir in lib_search:
                    p = lib_dir / candidate
                    if p.exists():
                        libc_so = p
                        break
                if libc_so:
                    break
            if libc_so:
                resolved = str(libc_so.resolve()) if libc_so.is_symlink() else str(libc_so)
                if "musl" in resolved:
                    libc_type = "musl"
                elif "uclibc" in resolved:
                    libc_type = "uclibc"
                else:
                    libc_type = "glibc"
                try:
                    name = libc_so.resolve().name
                    ver_match = re.search(r'(\d+\.\d+)', name)
                    if ver_match:
                        libc_ver = ver_match.group(1)
                except Exception:
                    pass
        if libc_type:
            log(fmt_ok("C-Library", libc_type, libc_ver))
        else:
            log(fmt_fail("C-Library", "not found"))

        # Important header files
        check_headers = [
            ("openssl/opensslconf.h", "openssl/configuration.h"),  # opensslconf.h → configuration.h in OpenSSL 3.x
            "openssl/crypto.h",
            "libusb-1.0/libusb.h", "PCSC/pcsclite.h",
            "pthread.h", "dvbcsa/dvbcsa.h",
        ]
        for header_entry in check_headers:
            # Support alternatives: ("openssl/opensslconf.h", "openssl/configuration.h")
            alternatives = header_entry if isinstance(header_entry, tuple) else (header_entry,)
            short = alternatives[0].split("/")[-1]
            found = False
            found_name = ""
            inc_dirs = [sysroot_path / "usr" / "include", sysroot_path / "include"]
            # Native: add multiarch include path (e.g. /usr/include/x86_64-linux-gnu)
            if tc.native and target_arch:
                inc_dirs.append(Path(f"/usr/include/{target_arch}"))
            for header in alternatives:
                for inc_dir in inc_dirs:
                    if (inc_dir / header).exists():
                        found = True
                        found_name = header.split("/")[-1]
                        break
                if found:
                    break
            if found:
                log(fmt_ok("Header", found_name))
            else:
                log(fmt_fail("Header", f"{short} [dim]({alternatives[0]})[/dim]"))

        # --- Library Config (pkgconfig) ---
        if tc.native:
            # Native: search known system pkgconfig dirs
            pkg_dirs = [p for p in [
                Path("/usr/lib/pkgconfig"),
                Path("/usr/lib64/pkgconfig"),
                Path("/usr/local/lib/pkgconfig"),
                Path("/usr/share/pkgconfig"),
                Path(f"/usr/lib/{target_arch}/pkgconfig") if target_arch else None,
            ] if p and p.is_dir()]
        else:
            pkg_dirs = list(sysroot_path.glob("**/pkgconfig"))
        if pkg_dirs:
            for pkg_dir in pkg_dirs:
                try:
                    rel_pkg = pkg_dir.resolve().relative_to(install_dir.resolve())
                except ValueError:
                    rel_pkg = pkg_dir
                log(f"\n[bold]Library config ----> {rel_pkg}[/bold]")
                log(f"  {'=' * 40}")

                for pc_file in sorted(pkg_dir.glob("*.pc")):
                    try:
                        content = pc_file.read_text(encoding="utf-8", errors="replace")
                        pc_name = ""
                        pc_ver = ""
                        for line in content.splitlines():
                            if line.startswith("Name:"):
                                pc_name = line.split(":", 1)[1].strip()
                            elif line.startswith("Version:"):
                                pc_ver = line.split(":", 1)[1].strip()

                        # Check for static (.a) and dynamic (.so)
                        lib_dir = pc_file.parent.parent
                        base = pc_file.stem  # e.g. "libcrypto" from "libcrypto.pc"
                        # Map common pc names to lib names
                        lib_base = base.replace("openssl", "libcrypto").replace("zlib", "libz")
                        has_static = (lib_dir / f"{lib_base}.a").exists()
                        has_dynamic = bool(list(lib_dir.glob(f"{lib_base}.so*")))
                        link_type = ""
                        if has_static and has_dynamic:
                            link_type = "static+dynamic"
                        elif has_static:
                            link_type = "static"
                        elif has_dynamic:
                            link_type = "dynamic"

                        log(fmt_ok(f"{pc_file.name:<20s}", f"{pc_name:<20s} {pc_ver}", link_type))
                    except Exception:
                        log(fmt_fail(pc_file.name, "unreadable"))
        else:
            log(f"\n[bold]Library config[/bold]")
            log(f"  [yellow]No pkgconfig directories found[/yellow]")

        # --- Relocate check (OE-SDK) ---
        nss = install_dir / "sysroots" / "x86_64-oesdk-linux"
        if nss.is_dir():
            log(f"\n[bold]OE-SDK Relocate[/bold]")
            log(f"  {'=' * 40}")
            ranlib_bins = list(nss.glob("usr/bin/*/**/*-ranlib"))
            if ranlib_bins:
                real_bin = ranlib_bins[0].resolve()
                try:
                    output = subprocess.check_output(
                        ["readelf", "-l", str(real_bin)], text=True, stderr=subprocess.DEVNULL,
                    )
                    for line in output.splitlines():
                        if "interpreter" in line:
                            interp = line.split(":")[-1].strip().rstrip("]").strip()
                            if Path(interp).exists():
                                log(fmt_ok("Relocate", "OK"))
                            else:
                                log(fmt_fail("Relocate", f"FAILED — {interp}"))
                                ok = False
                            break
                except Exception:
                    pass

        # --- Summary ---
        log("")
        if ok:
            log(f"[green]{tc.name} — all checks passed[/green]")
        else:
            log(f"[red]{tc.name} — some checks failed[/red]")

    _remove_confirm_time: float = 0
    _remove_confirm_name: str = ""

    def _handle_remove(self) -> None:
        """Remove toolchain (double-press to confirm)."""
        import time
        name = ToolchainsScreen._selected_tc
        if not name:
            self.notify(t("no_toolchain_selected"), severity="warning")
            return
        reg = self._get_registry()
        tc = reg.get(name)
        if not tc:
            return
        if not tc.installed and not tc.native:
            self.notify(t("not_installed", name=tc.name), severity="warning")
            return

        now = time.time()
        if now - self._remove_confirm_time < 3 and self._remove_confirm_name == name:
            self._remove_confirm_time = 0
            self._remove_confirm_name = ""

            if tc.native:
                install_dir = self.app.paths.toolchains_dir / tc.name
                if install_dir.exists():
                    shutil.rmtree(install_dir)
                reg = self._get_registry()
                local_tcs = [t for t in reg._load_file(reg.local_path) if not t.native]
                reg.save_local(local_tcs)
                self._log_sync("[yellow]Native toolchain regenerated[/yellow]")
                self._refresh()
                return

            install_dir = self.app.paths.toolchains_dir / tc.name
            if install_dir.exists():
                shutil.rmtree(install_dir)
            self._log_sync(f"[yellow]Removed {tc.name}[/yellow]")
            self._refresh()
            return

        self._remove_confirm_time = now
        self._remove_confirm_name = name
        self.notify(t("confirm_remove_tc", name=tc.name), severity="warning", timeout=3)

    def _relocate_toolchain(self, install_dir: Path, tc: Toolchain) -> None:
        relocate_sh = install_dir / "relocate-sdk.sh"
        if relocate_sh.exists():
            self._log("Running relocate-sdk.sh...")
            try:
                subprocess.run(
                    [str(relocate_sh)], cwd=install_dir,
                    capture_output=True, text=True, timeout=120,
                )
            except Exception:
                pass
            return

        nss = install_dir / "sysroots" / "x86_64-oesdk-linux"
        if not nss.is_dir():
            return

        gcc_scripts = list(nss.glob("usr/bin/*/**/*-gcc"))
        gcc_scripts = [g for g in gcc_scripts if "++" not in g.name]
        if not gcc_scripts:
            return

        gcc_dir = gcc_scripts[0].parent
        ranlib_bins = list(gcc_dir.glob("*-ranlib"))
        if not ranlib_bins:
            return

        real_bin = ranlib_bins[0].resolve()

        interp = ""
        try:
            output = subprocess.check_output(
                ["readelf", "-l", str(real_bin)], text=True, stderr=subprocess.DEVNULL,
            )
            for line in output.splitlines():
                if "interpreter" in line:
                    interp = line.split(":")[-1].strip().rstrip("]").strip()
                    break
        except Exception:
            return

        if not interp:
            return
        if Path(interp).exists():
            return

        rpy = install_dir / "relocate_sdk.py"
        if not rpy.exists():
            return

        old_prefix = interp.split("/sysroots/")[0] if "/sysroots/" in interp else ""
        if not old_prefix:
            return

        sdk_loader = nss / "lib" / Path(interp).name
        if not sdk_loader.exists():
            return

        self._log(f"Relocating OE-SDK to {install_dir.name}...")

        rpy_text = rpy.read_text(encoding="utf-8", errors="replace")
        rpy_text = re.sub(
            r'old_prefix = .*',
            f'old_prefix = re.compile(b("{old_prefix}"))',
            rpy_text,
        )
        rpy.write_text(rpy_text, encoding="utf-8")

        try:
            exec_files = subprocess.check_output(
                ["find", str(nss), "-type", "f", "(", "-perm", "/0111", ")"],
                text=True, stderr=subprocess.DEVNULL, timeout=60,
            ).strip().splitlines()
        except Exception:
            exec_files = []

        if exec_files:
            try:
                cmd = ["python3", str(rpy), str(install_dir), str(sdk_loader)] + exec_files
                subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                self._log("[green]OE-SDK relocated[/green]")
            except Exception as e:
                self._log(f"[yellow]Relocate warning: {e}[/yellow]")

    def _verify_checksum(self, filepath: Path, checksum: str) -> bool:
        if ":" not in checksum:
            return True
        algo, expected = checksum.split(":", 1)
        if algo == "sha256":
            h = hashlib.sha256()
        elif algo == "md5":
            h = hashlib.md5()
        else:
            return True
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest() == expected

    def action_switch_right_tab(self, tab: str) -> None:
        self._show_tab(tab)

    def action_switch_left_tab(self, tab: str) -> None:
        self._show_left_tab(tab)

    def action_go_back(self) -> None:
        if self._building:
            self.notify("Build in progress — cancel first with F9", severity="warning")
            return
        ToolchainsScreen._log_history.clear()
        ToolchainsScreen._selected_tc = ""
        self.app.pop_screen()
