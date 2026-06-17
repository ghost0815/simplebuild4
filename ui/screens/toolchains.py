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
from textual.widgets import Static, Input, Button, Collapsible, DataTable, Select, RichLog, DirectoryTree, TextArea
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate

from core.sysroot import read_linux_version
from core.toolchain import Toolchain, ToolchainRegistry, scan_libraries, load_library_recipes, check_library_updates, KNOWN_LIBS, rmtree_force, ensure_writable_dirs
from core.lib_builder import build_library, sort_by_dependencies
from translation import t
from ui.header import S4Header


def _ensure_excl_auto(yaml_text: str) -> str:
    """Inject 'exclude_from_automation: false' after the description line if missing."""
    if "exclude_from_automation" in yaml_text:
        return yaml_text
    lines = yaml_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("description:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(i + 1, f"{indent}exclude_from_automation: false\n")
            return "".join(lines)
    # Fallback: append at end
    return yaml_text.rstrip("\n") + "\nexclude_from_automation: false\n"


class S4DataTable(DataTable):
    """DataTable with a leading marker column and focus-aware cursor.

    A hidden marker column is prepended automatically (via overrides of
    add_columns / add_row). The currently selected row carries a small
    glyph in that column, so the selection stays visible even when the
    table is not focused — no background colour required.
    """

    @staticmethod
    def _get_marker():
        from ui.icons import icon
        return icon("marker")

    MARKER = "▶"  # class default; mark_row uses _get_marker()

    DEFAULT_CSS = """
    S4DataTable {
        background: $surface;
        color: $foreground;
        height: auto;
        max-height: 100%;

        /* Cursor row: invisible when not focused — the marker glyph
           in column 0 indicates the selection instead. */
        & > .datatable--cursor {
            background: $surface;
            color: $foreground;
            text-style: none;
        }

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

        & > .datatable--even-row {
            background: $surface-lighten-1 50%;
        }

        & > .datatable--hover {
            background: $block-hover-background;
        }
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._marker_row: int = -1
        self._sort_col: int | None = None
        self._sort_reverse: bool = False

    def add_columns(self, *labels):
        """Prepend a 1-char marker column before the user-supplied ones."""
        return super().add_columns(" ", *labels)

    def add_row(self, *cells, height=1, key=None, label=None):
        return super().add_row(" ", *cells, height=height, key=key, label=label)

    def clear(self, columns: bool = False):
        self._marker_row = -1
        self._sort_col = None
        self._sort_reverse = False
        return super().clear(columns=columns)

    def sort_by_column(self, col_index: int) -> None:
        """Sort all rows by the given column index, toggling direction."""
        if col_index == 0:
            return  # skip the marker column
        if self.row_count == 0:
            return
        if self._sort_col == col_index:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col_index
            self._sort_reverse = False
        num_cols = len(self.columns)
        # Collect rows: list of (row_key, [cell_values])
        row_keys = [row.key for row in self.ordered_rows]
        rows = []
        for row_idx, rk in enumerate(row_keys):
            cells = []
            for col_idx in range(num_cols):
                try:
                    cells.append(str(self.get_cell_at(Coordinate(row_idx, col_idx))))
                except Exception:
                    cells.append("")
            rows.append((rk, cells))
        # Sort by the selected column text
        rows.sort(
            key=lambda r: r[1][col_index].lower() if col_index < len(r[1]) else "",
            reverse=self._sort_reverse,
        )
        # Remember which row key had the marker / cursor
        prev_marker_key = None
        if 0 <= self._marker_row < len(row_keys):
            prev_marker_key = row_keys[self._marker_row].value
        # Also remember cursor row key
        prev_cursor_key = None
        try:
            prev_cursor_key = self.get_row_at(self.cursor_row)
            # get_row_at returns cells; we need the key
            if 0 <= self.cursor_row < len(row_keys):
                prev_cursor_key = row_keys[self.cursor_row].value
        except Exception:
            pass

        # Rebuild table
        self._marker_row = -1
        super().clear()
        new_marker_row = -1
        new_cursor_row = 0
        for idx, (rk, cells) in enumerate(rows):
            cells[0] = " "  # reset marker
            super().add_row(*cells, key=rk.value)
            if rk.value == prev_marker_key:
                new_marker_row = idx
            if rk.value == prev_cursor_key:
                new_cursor_row = idx

        # Restore marker and cursor position
        if new_marker_row >= 0:
            self.mark_row(new_marker_row)
        if self.row_count:
            self.move_cursor(row=new_cursor_row)

    def mark_row(self, row: int) -> None:
        """Paint the selection marker on `row`, clearing the previous one.

        Called explicitly by the screen on real selection events — never
        as a side-effect of populating the table, so the first row of an
        idle table stays unmarked until the user picks something.
        """
        try:
            if self._marker_row == row:
                return
            if 0 <= self._marker_row < self.row_count:
                try:
                    self.update_cell_at(Coordinate(self._marker_row, 0), " ", update_width=False)
                except Exception:
                    pass
            if 0 <= row < self.row_count:
                try:
                    self.update_cell_at(Coordinate(row, 0), self._get_marker(), update_width=False)
                except Exception:
                    pass
            self._marker_row = row
        except Exception:
            pass

    def clear_marker(self) -> None:
        """Erase the current marker without painting a new one."""
        try:
            if 0 <= self._marker_row < self.row_count:
                self.update_cell_at(Coordinate(self._marker_row, 0), " ", update_width=False)
            self._marker_row = -1
        except Exception:
            pass
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

    def __init__(self, start_path: str = "/", title: str = "", simple: bool = False,
                 select_label: str = "Import", directories_only: bool = False) -> None:
        super().__init__()
        self._start_path = start_path
        self._title = title or "Restore toolchain from backup"
        self._simple = simple or directories_only
        self._directories_only = directories_only
        self._select_label = select_label
        self._directories_only = directories_only

    def compose(self) -> ComposeResult:
        with Vertical(id="file-picker-box"):
            with Horizontal(id="fp-title-row"):
                yield Static(self._title, id="fp-title")
                yield Static("", classes="spacer")
                yield Button("▲ Up", id="fp-up", classes="fp-nav-btn")
                yield Button("⌂ Home", id="fp-home", classes="fp-nav-btn")
                yield Button("/ Root", id="fp-root", classes="fp-nav-btn")
            yield DirectoryTree(self._start_path, id="fp-tree")
            yield Input(placeholder=t("ph_enter_path"), id="fp-path")
            if not self._simple:
                with Horizontal(id="fp-name-row"):
                    yield Static(" Name: ", id="fp-name-label")
                    yield Input(placeholder=t("ph_tc_name"), id="fp-name")
            with Horizontal(id="fp-buttons"):
                yield Button(self._select_label, id="fp-select", variant="primary")
                yield Button("Cancel", id="fp-cancel")

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
        if self._directories_only:
            return
        self.query_one("#fp-path", Input).value = str(event.path)
        if not self._simple:
            self.query_one("#fp-name", Input).value = self._name_from_filename(event.path.name)

    def on_directory_tree_directory_selected(self, event) -> None:
        """When a directory is clicked in the tree (only used in directories_only mode)."""
        if not self._directories_only:
            return
        try:
            self.query_one("#fp-path", Input).value = str(event.path)
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "fp-path" and not self._simple:
            path = event.value.strip()
            if path:
                self.query_one("#fp-name", Input).value = self._name_from_filename(Path(path).name)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "fp-cancel":
            self.dismiss(("", ""))
        elif event.button.id == "fp-select":
            path = self.query_one("#fp-path", Input).value.strip()
            if self._simple:
                if path:
                    self.dismiss((path, ""))
                return
            name = self.query_one("#fp-name", Input).value.strip()
            if path and name:
                self.dismiss((path, name))
            elif path:
                self.notify("Enter a toolchain name", severity="warning")
        elif event.button.id == "fp-up":
            tree = self.query_one("#fp-tree", DirectoryTree)
            parent = tree.path.parent
            if parent != tree.path:
                self._change_root(parent)
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
        ("e", "switch_right_tab('editor')", "Editor"),
        ("o", "switch_right_tab('log')", "Output"),
        ("p", "switch_left_tab('prebuild')", "PreBuild"),
        ("t", "switch_left_tab('selfbuild')", "ToolchainBuilder"),
    ]

    _log_history: list[str] = []
    _selected_tc: str = ""
    _selected_tpl_source: str = ""  # "default" / "local" / "" — for SelfBuild templates
    _pending_sb_overwrite: str = ""  # name of template awaiting confirm-to-overwrite
    _active_left_tab: str = "prebuild"   # "prebuild" | "selfbuild"
    _active_right_tab: str = "details"   # "details" | "libs" | "editor" | "log"

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
                with Vertical(id="tc-installed-box"):
                    yield S4DataTable(id="tbl-installed", cursor_type="row", zebra_stripes=True)

                # PreBuild / SelfBuild tabs
                with Horizontal(id="tc-left-tab-bar"):
                    yield Static(" [b]P[/b]reBuild ", id="btn-ltab-prebuild", classes="tab-active")
                    yield Static(" [b]T[/b]oolchainBuilder ", id="btn-ltab-selfbuild", classes="tab-inactive")

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

                # SelfBuild panel (hidden by default) — populated in on_mount
                with VerticalScroll(id="tc-selfbuild"):
                    with Collapsible(title="Local (0)", collapsed=True, id="cat-sb-local"):
                        yield S4DataTable(id="tbl-sb-local", cursor_type="row", zebra_stripes=True)
                    with Collapsible(title="OE-SDK (0)", collapsed=True, id="cat-sb-oesdk"):
                        yield S4DataTable(id="tbl-sb-oesdk", cursor_type="row", zebra_stripes=True)
                    with Collapsible(title="Crosstool-NG (0)", collapsed=True, id="cat-sb-ctng"):
                        yield S4DataTable(id="tbl-sb-ctng", cursor_type="row", zebra_stripes=True)
                    with Collapsible(title="Freetz-NG (0)", collapsed=True, id="cat-sb-freetz"):
                        yield S4DataTable(id="tbl-sb-freetz", cursor_type="row", zebra_stripes=True)
                    with Collapsible(title="Android-NDK (0)", collapsed=True, id="cat-sb-android"):
                        yield S4DataTable(id="tbl-sb-android", cursor_type="row", zebra_stripes=True)

            # Right: Details + Output
            with Vertical(id="tc-right"):
                # Detail/Libraries/Log tab buttons
                with Horizontal(id="tc-tab-bar"):
                    yield Static(" [b]D[/b]etails ", id="btn-tab-details", classes="tab-active")
                    yield Static(" [b]E[/b]ditor ", id="btn-tab-editor", classes="tab-inactive")
                    yield Static(" [b]L[/b]ibraries ", id="btn-tab-libs", classes="tab-inactive")
                    yield Static(" [b]O[/b]utput ", id="btn-tab-log", classes="tab-inactive")

                # Details panel
                detail = Static("", id="tc-detail")
                detail.auto_links = False
                detail.can_focus = True
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

                # Yaml editor panel (hidden by default)
                yield TextArea("", id="tc-editor", language="yaml", show_line_numbers=True)

                # Output
                yield RichLog(highlight=False, markup=True, id="tc-output", wrap=True)

        yield S4Footer(active_screen="toolchains")

    def on_mount(self) -> None:
        reg = self._get_registry()

        # Show YAML parse errors as notification
        if reg.yaml_error:
            self.notify(f"YAML error: {reg.yaml_error}", severity="error", timeout=15)

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
        tbl.sort_by_column(1)  # sort by Name

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
                tbl.sort_by_column(2)  # sort by Name
            except Exception:
                pass

        # SelfBuild tables — load templates and group by builder + local
        from core.template import load_templates
        templates = load_templates(
            self.app.paths.templates_defaults,
            self.app.paths.templates_local,
        )
        # Builder name → table id
        _builder_table = {
            "crosstool-ng": "tbl-sb-ctng",
            "freetz-ng": "tbl-sb-freetz",
            "oe-sdk": "tbl-sb-oesdk",
            "android-ndk": "tbl-sb-android",
        }
        _builder_collapsible = {
            "oe-sdk": ("cat-sb-oesdk", "OE-SDK"),
            "crosstool-ng": ("cat-sb-ctng", "Crosstool-NG"),
            "freetz-ng": ("cat-sb-freetz", "Freetz-NG"),
            "android-ndk": ("cat-sb-android", "Android-NDK"),
        }
        # Init columns
        for sb_id in ("tbl-sb-local", "tbl-sb-ctng", "tbl-sb-freetz", "tbl-sb-oesdk", "tbl-sb-android"):
            try:
                tbl_sb = self.query_one(f"#{sb_id}", DataTable)
                tbl_sb.add_columns("●", "Name", "Arch", "Builder", "Description")
            except Exception:
                pass
        # Populate
        local_count = 0
        builder_counts: dict[str, int] = {}
        # If a name exists as both default + local template, only the local
        # one is treated as installed — it shadows the default.
        _local_names = {t.name for t in templates if t.is_local}
        for tpl in templates:
            _reg_entry = self._get_registry().get(tpl.name)
            installed = bool(_reg_entry and _reg_entry.installed)
            if installed and not tpl.is_local and tpl.name in _local_names:
                installed = False
            status = "●" if installed else ""
            if tpl.is_local:
                try:
                    tbl_sb = self.query_one("#tbl-sb-local", DataTable)
                    tbl_sb.add_row(status, tpl.name, tpl.arch, tpl.builder, tpl.description, key=tpl.name)
                    local_count += 1
                except Exception:
                    pass
            else:
                tbl_id = _builder_table.get(tpl.builder)
                if tbl_id:
                    try:
                        tbl_sb = self.query_one(f"#{tbl_id}", DataTable)
                        tbl_sb.add_row(status, tpl.name, tpl.arch, tpl.builder, tpl.description, key=tpl.name)
                        builder_counts[tpl.builder] = builder_counts.get(tpl.builder, 0) + 1
                    except Exception:
                        pass
        # Sort selfbuild tables by Name (col 2)
        for sb_id in ("tbl-sb-local", "tbl-sb-ctng", "tbl-sb-freetz", "tbl-sb-oesdk", "tbl-sb-android"):
            try:
                self.query_one(f"#{sb_id}", DataTable).sort_by_column(2)
            except Exception:
                pass

        # Update collapsible titles with counts
        try:
            self.query_one("#cat-sb-local", Collapsible).title = f"Local ({local_count})"
            for builder, (col_id, label) in _builder_collapsible.items():
                count = builder_counts.get(builder, 0)
                self.query_one(f"#{col_id}", Collapsible).title = f"{label} ({count})"
        except Exception:
            pass

        # Border titles
        self.query_one("#tc-installed-box", Vertical).border_title = t("installed")
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
        # Restore previous left tab (PreBuild / SelfBuild)
        if ToolchainsScreen._active_left_tab and ToolchainsScreen._active_left_tab != "prebuild":
            try:
                self._show_left_tab(ToolchainsScreen._active_left_tab)
            except Exception:
                pass

        # Restore selection — either a SelfBuild template or an installed toolchain
        focused_tbl = tbl  # default focus target
        if ToolchainsScreen._selected_tpl_source and ToolchainsScreen._selected_tc:
            # SelfBuild template — find the row in the right table
            tpl_tbl_id = "tbl-sb-local" if ToolchainsScreen._selected_tpl_source == "local" else None
            _table_to_collapsible = {
                "tbl-sb-local": "cat-sb-local",
                "tbl-sb-ctng": "cat-sb-ctng",
                "tbl-sb-freetz": "cat-sb-freetz",
                "tbl-sb-oesdk": "cat-sb-oesdk",
                "tbl-sb-android": "cat-sb-android",
            }
            try:
                if tpl_tbl_id:
                    candidates = [tpl_tbl_id]
                else:
                    candidates = ["tbl-sb-ctng", "tbl-sb-freetz", "tbl-sb-oesdk", "tbl-sb-android"]
                for cand_id in candidates:
                    try:
                        sb_tbl = self.query_one(f"#{cand_id}", DataTable)
                    except Exception:
                        continue
                    found = False
                    for i, key in enumerate(sb_tbl.rows):
                        if key.value == ToolchainsScreen._selected_tc:
                            # Expand the collapsible so the row is visible
                            col_id = _table_to_collapsible.get(cand_id)
                            if col_id:
                                try:
                                    self.query_one(f"#{col_id}", Collapsible).collapsed = False
                                except Exception:
                                    pass
                            sb_tbl.move_cursor(row=i)
                            sb_tbl.mark_row(i)
                            focused_tbl = sb_tbl
                            found = True
                            break
                    if found:
                        break
            except Exception:
                pass
        elif ToolchainsScreen._selected_tc or self.app.settings.last_toolchain:
            # Installed toolchain — restore cursor in installed table
            restore_tc = ToolchainsScreen._selected_tc or self.app.settings.last_toolchain
            try:
                for i, key in enumerate(tbl.rows):
                    if key.value == restore_tc:
                        ToolchainsScreen._selected_tc = restore_tc
                        tbl.move_cursor(row=i)
                        tbl.mark_row(i)
                        break
            except Exception:
                pass
        elif tbl.row_count > 0:
            # First open — select first installed toolchain
            first_key = list(tbl.rows.keys())[0]
            ToolchainsScreen._selected_tc = first_key.value
            tbl.move_cursor(row=0)
            tbl.mark_row(0)

        # Restore previous right tab (Details / Editor / Libraries / Output)
        if ToolchainsScreen._active_right_tab and ToolchainsScreen._active_right_tab != "details":
            try:
                self._show_tab(ToolchainsScreen._active_right_tab)
            except Exception:
                self._update_detail()
        else:
            self._update_detail()
        self._update_footer_state()
        # Focus the previously-active table
        focused_tbl.focus()
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
        """Filter toolchain tables by search term.

        Applies to both the PreBuild catalog tables AND the SelfBuild
        template tables so the filter input is universal.
        """
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
            self.query_one("#tc-installed-label", Static).update(f" [bold]{t('installed')} ({count})[/bold]")
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

        # Rebuild SelfBuild template tables
        self._apply_selfbuild_filter(term_lower)

    def _apply_selfbuild_filter(self, term_lower: str) -> None:
        """Filter the SelfBuild template tables by the same search term."""
        from core.template import load_templates
        templates = load_templates(
            self.app.paths.templates_defaults,
            self.app.paths.templates_local,
        )
        _builder_table = {
            "crosstool-ng": "tbl-sb-ctng",
            "freetz-ng": "tbl-sb-freetz",
            "oe-sdk": "tbl-sb-oesdk",
            "android-ndk": "tbl-sb-android",
        }
        _builder_collapsible = {
            "oe-sdk": ("cat-sb-oesdk", "OE-SDK"),
            "crosstool-ng": ("cat-sb-ctng", "Crosstool-NG"),
            "freetz-ng": ("cat-sb-freetz", "Freetz-NG"),
            "android-ndk": ("cat-sb-android", "Android-NDK"),
        }
        for sb_id in ("tbl-sb-local", "tbl-sb-ctng", "tbl-sb-freetz", "tbl-sb-oesdk", "tbl-sb-android"):
            try:
                self.query_one(f"#{sb_id}", DataTable).clear()
            except Exception:
                pass
        local_count = 0
        builder_counts: dict[str, int] = {}
        for tpl in templates:
            # Reuse the same match logic — Template has name, description,
            # arch, machines just like Toolchain.
            if not self._tc_matches(tpl, term_lower):
                continue
            _reg_entry = self._get_registry().get(tpl.name)
            installed = bool(_reg_entry and _reg_entry.installed)
            status = "●" if installed else ""
            if tpl.is_local:
                try:
                    self.query_one("#tbl-sb-local", DataTable).add_row(
                        status, tpl.name, tpl.arch, tpl.builder, tpl.description, key=tpl.name,
                    )
                    local_count += 1
                except Exception:
                    pass
            else:
                tbl_id = _builder_table.get(tpl.builder)
                if tbl_id:
                    try:
                        self.query_one(f"#{tbl_id}", DataTable).add_row(
                            status, tpl.name, tpl.arch, tpl.builder, tpl.description, key=tpl.name,
                        )
                        builder_counts[tpl.builder] = builder_counts.get(tpl.builder, 0) + 1
                    except Exception:
                        pass
        try:
            self.query_one("#cat-sb-local", Collapsible).title = f"Local ({local_count})"
            for builder, (col_id, label) in _builder_collapsible.items():
                count = builder_counts.get(builder, 0)
                self.query_one(f"#{col_id}", Collapsible).title = f"{label} ({count})"
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
            elif widget.id == "btn-tab-editor":
                self._show_tab("editor")
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

        ToolchainsScreen._active_left_tab = tab
        # Refresh F5/F9 button states for the new tab
        self._update_footer_state()
        # Focus first focusable widget in the new panel
        panel_id = "#tc-browser" if tab == "prebuild" else "#tc-selfbuild"
        try:
            panel = self.query_one(panel_id)
            for child in panel.query("*"):
                if child.focusable:
                    child.focus()
                    break
        except Exception:
            pass

    def _set_tc_status(self, tc_name: str, status: str) -> None:
        """Update the status and row color of a toolchain in its category DataTable.

        S4DataTable prepends a hidden marker column to whatever
        ``add_columns`` was called with. So a category table created
        as ``add_columns("●", "Name", "Arch", "Description")`` actually
        ends up with 5 columns: ``[marker, ●, Name, Arch, Description]``.
        We skip ``cols[0]`` (the marker) and write into ``cols[1..4]``.
        """
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
                # cols[0] is S4DataTable's hidden marker — skip it.
                c_status, c_name, c_arch, c_desc = cols[1], cols[2], cols[3], cols[4]
                if "installing" in status:
                    tbl.update_cell(rk, c_status, Text("⟳", style="yellow"))
                    tbl.update_cell(rk, c_name,   Text(tc.name, style="yellow"))
                    tbl.update_cell(rk, c_arch,   Text(tc.arch, style="yellow"))
                    tbl.update_cell(rk, c_desc,   Text(tc.description, style="yellow"))
                elif "done" in status:
                    tbl.update_cell(rk, c_status, Text("●", style="green"))
                    tbl.update_cell(rk, c_name,   Text(tc.name, style="green"))
                    tbl.update_cell(rk, c_arch,   Text(tc.arch, style="green"))
                    tbl.update_cell(rk, c_desc,   Text(tc.description, style="green"))
                elif "failed" in status:
                    tbl.update_cell(rk, c_status, Text("✗", style="red"))
                    tbl.update_cell(rk, c_name,   Text(tc.name, style="red"))
                    tbl.update_cell(rk, c_arch,   Text(tc.arch, style="red"))
                    tbl.update_cell(rk, c_desc,   Text(tc.description, style="red"))
                else:
                    tbl.update_cell(rk, c_status, "●" if tc.installed else "")
                    tbl.update_cell(rk, c_name,   tc.name)
                    tbl.update_cell(rk, c_arch,   tc.arch)
                    tbl.update_cell(rk, c_desc,   tc.description)
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

    def _is_editor_tab_active(self) -> bool:
        try:
            return self.query_one("#tc-editor", TextArea).display
        except Exception:
            return False

    def _is_selfbuild_tab_active(self) -> bool:
        try:
            return self.query_one("#tc-selfbuild", VerticalScroll).display
        except Exception:
            return False

    def _is_template_selected(self) -> bool:
        """True if the currently selected row is a SelfBuild template (not a toolchain)."""
        return bool(ToolchainsScreen._selected_tpl_source)

    def _is_template_buildable(self) -> bool:
        """True if the selected template is ready to build.

        For crosstool-ng: always True (config is self-contained).
        For oe-sdk: True only if branch + machine + repo_url are set
        in the .config (i.e. F6 Customize was completed).
        """
        tpl = self._get_selected_template()
        if not tpl:
            return False
        if tpl.builder == "oe-sdk":
            from core.template import load_oesdk_config
            cfg_path = tpl.config_path
            if not cfg_path or not cfg_path.exists():
                return False
            cfg = load_oesdk_config(cfg_path)
            return bool(cfg.repo_branch and cfg.machine and cfg.repo_url)
        if tpl.builder == "android-ndk":
            from core.template import load_andk_config
            cfg_path = tpl.config_path
            if not cfg_path or not cfg_path.exists():
                return False
            cfg = load_andk_config(cfg_path)
            return bool(cfg.api and cfg.ndk_version)
        return True

    def _update_active_tab(self) -> None:
        if self._is_libs_tab_active():
            self._update_libraries()
        elif self._is_editor_tab_active():
            self._update_editor()
        else:
            self._update_detail()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        tbl = event.data_table
        if isinstance(tbl, S4DataTable):
            tbl.sort_by_column(event.column_index)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not self._mounted or self._building:
            return
        if event.row_key and event.row_key.value:
            ToolchainsScreen._selected_tc = event.row_key.value
            self._track_template_source(event)
            self._mark_selected_row(event)
            self._update_active_tab()
            self._update_footer_state()
            # Persist installed toolchain selection across restarts
            if event.data_table.id == "tbl-installed":
                self.app.settings.last_toolchain = event.row_key.value
                self.app.settings.save(self.app.settings_path)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._building:
            return
        if event.row_key and event.row_key.value:
            ToolchainsScreen._selected_tc = event.row_key.value
            self._track_template_source(event)
            self._mark_selected_row(event)
            self._update_active_tab()
            self._update_footer_state()

    def _mark_selected_row(self, event) -> None:
        """Paint the marker on the row that the user just picked.

        Also clears the marker on every other S4DataTable on the screen
        so only one row across all tables is marked at a time.
        """
        try:
            picked_table = getattr(event, "data_table", None)
            picked_row = getattr(event, "cursor_row", None)
            if picked_row is None:
                picked_row = getattr(event, "row_index", None)
            for tbl in self.query(S4DataTable):
                if tbl is picked_table and picked_row is not None:
                    tbl.mark_row(int(picked_row))
                else:
                    tbl.clear_marker()
        except Exception:
            pass

    def _track_template_source(self, event) -> None:
        """When the user picks a row in a SelfBuild table, remember its source.

        sb-local → user-edited copy in data/templates/
        anything else → shipped template in defaults/templates/
        """
        try:
            tbl_id = getattr(event.data_table, "id", "") or ""
        except Exception:
            tbl_id = ""
        if tbl_id == "tbl-sb-local":
            ToolchainsScreen._selected_tpl_source = "local"
        elif tbl_id.startswith("tbl-sb-"):
            ToolchainsScreen._selected_tpl_source = "default"
        else:
            ToolchainsScreen._selected_tpl_source = ""
        # Changing selection cancels any pending overwrite confirmation
        ToolchainsScreen._pending_sb_overwrite = ""

    def _update_detail(self) -> None:
        try:
            name = ToolchainsScreen._selected_tc
            if not name:
                return

            # SelfBuild template selected → show template metadata
            if self._is_template_selected():
                tpl = self._get_selected_template()
                if not tpl:
                    self.query_one("#tc-detail", Static).update(
                        f"  [dim]Template '{name}' not found[/dim]"
                    )
                    return
                source_label = "[yellow]local[/yellow]" if tpl.is_local else "[cyan]default[/cyan]"
                try:
                    rel_yaml = tpl.yaml_path.relative_to(self.app.paths.root) if tpl.yaml_path else ""
                except Exception:
                    rel_yaml = str(tpl.yaml_path or "")
                lines = [
                    f"  [bold]{tpl.name}[/bold] — {tpl.description}",
                    f"  Source:    {source_label}    File: [dim]{rel_yaml}[/dim]",
                    f"  Builder:   {tpl.builder}",
                    f"  Arch:      {tpl.arch}",
                ]
                if tpl.machines:
                    lines.append(f"  Machines:  {tpl.machines}")
                if tpl.prefix:
                    lines.append(f"  Prefix:    {tpl.prefix}")
                if tpl.sysroot:
                    lines.append(f"  Sysroot:   {tpl.sysroot}")
                if tpl.cflags:
                    lines.append(f"  Cflags:    {tpl.cflags}")
                if tpl.ldflags:
                    lines.append(f"  Ldflags:   {tpl.ldflags}")
                if tpl.config:
                    lines.append(f"  Config:    [dim]{tpl.config}[/dim]")
                self.query_one("#tc-detail", Static).update("\n".join(lines))
                return

            reg = self._get_registry()
            tc = reg.get(name)
            if not tc:
                return
            # Status with automation hint
            if tc.installed:
                if tc.exclude_from_automation:
                    status = "[green]installed[/green] [dim](excluded from automation)[/dim]"
                else:
                    status = "[green]installed[/green] [dim](available for automation)[/dim]"
            else:
                status = "[red]not installed[/red]"
            # Location
            install_dir = self.app.paths.toolchains_dir / tc.name
            try:
                location = str(install_dir.relative_to(self.app.paths.root))
            except Exception:
                location = str(install_dir)
            lines = [
                f"  [bold]{tc.name}[/bold] — {tc.description}",
                f"  Location:  {location}",
                f"  Status:    {status}",
                f"  Category:  {tc.category}",
                f"  Arch:      {tc.arch}",
                f"  Selfbuild: {'yes' if tc.selfbuild else 'no'}",
            ]
            if tc.machines:
                lines.append(f"  Machines:  {tc.machines}")
            lines.append(f"  Prefix:    {tc.prefix}")
            if tc.sysroot:
                lines.append(f"  Sysroot:   {tc.sysroot}")
            if tc.cflags:
                lines.append(f"  Cflags:    {tc.cflags}")
            if tc.ldflags:
                lines.append(f"  Ldflags:   {tc.ldflags}")
            if tc.make_vars:
                lines.append(f"  Make vars: {tc.make_vars}")
            if tc.stapi_lib:
                lines.append(f"  STAPI:     {tc.stapi_lib}")
            # Libraries (comma-separated on one line)
            if tc.libraries:
                lib_list = ", ".join(f"{k} {v}" for k, v in tc.libraries.items())
                lines.append(f"  Libraries: {lib_list}")
            if tc.url:
                lines.append(f"  URL:       [dim]{tc.url}[/dim]")
            self.query_one("#tc-detail", Static).update("\n".join(lines))
        except Exception:
            pass

    def _get_selected_template(self):
        """Return the Template currently selected in SelfBuild, respecting source."""
        from core.template import load_templates
        name = ToolchainsScreen._selected_tc
        source = ToolchainsScreen._selected_tpl_source
        if not name:
            return None
        templates = load_templates(
            self.app.paths.templates_defaults,
            self.app.paths.templates_local,
        )
        # Filter by source if known
        if source == "local":
            cands = [t_ for t_ in templates if t_.name == name and t_.is_local]
        elif source == "default":
            cands = [t_ for t_ in templates if t_.name == name and not t_.is_local]
        else:
            cands = [t_ for t_ in templates if t_.name == name]
        return cands[0] if cands else None

    def _update_libraries(self, built_libs: "set[str] | None" = None,
                          removed_libs: "set[str] | None" = None) -> None:
        """Scan libraries and update pre-created rows with options.

        When built_libs is None, the dropdowns are reset to their fresh
        defaults (toolchain switch). When a set is supplied, only the libs
        in that set are reset; everything else preserves its current
        Select.value (so an F9 update doesn't blow away unrelated picks
        the user just made). removed_libs forces just-removed entries to
        "skip" instead of auto-suggesting a reinstall of what was just
        deleted.
        """
        if self._building:
            return  # Don't refresh while building — preserves status display
        try:
            name = ToolchainsScreen._selected_tc
            if not name:
                return

            header = self.query_one("#tc-libs-header", Static)

            # Template selected → libraries are managed by the builder, not s4
            if self._is_template_selected():
                header.update(f"  [dim]{t('libs_not_for_templates')}[/dim]")
                # Clear all library rows + blank the action dropdowns
                for lib_name, _, _ in KNOWN_LIBS:
                    safe_id = lib_name.replace(".", "-").replace(" ", "-")
                    try:
                        sw = self.query_one(f"#lib-status-{safe_id}", Static)
                        sw.update(f"  [dim]{lib_name:<15s} —[/dim]")
                    except Exception:
                        pass
                    try:
                        sel = self.query_one(f"#lib-action-{safe_id}", Select)
                        sel.set_options([(t("lib_skip"), "skip")])
                        sel.value = Select.BLANK
                    except Exception:
                        pass
                return

            reg = self._get_registry()
            tc = reg.get(name)
            if not tc:
                return

            if not tc.installed:
                header.update(f"  [dim]Not installed — install toolchain first[/dim]")
                # Same idle state as for templates: status dashes + blank dropdowns
                for lib_name, _, _ in KNOWN_LIBS:
                    safe_id = lib_name.replace(".", "-").replace(" ", "-")
                    try:
                        sw = self.query_one(f"#lib-status-{safe_id}", Static)
                        sw.update(f"  [dim]{lib_name:<15s} —[/dim]")
                    except Exception:
                        pass
                    try:
                        sel = self.query_one(f"#lib-action-{safe_id}", Select)
                        sel.set_options([(t("lib_skip"), "skip")])
                        sel.value = Select.BLANK
                    except Exception:
                        pass
                return

            # Snapshot current dropdown selections — used to preserve picks
            # for libs that weren't part of the just-finished build run.
            prev_values: dict[str, str] = {}
            if built_libs is not None:
                for _ln, _, _ in KNOWN_LIBS:
                    _sid = _ln.replace(".", "-").replace(" ", "-")
                    try:
                        _sel = self.query_one(f"#lib-action-{_sid}", Select)
                        if _sel.value and isinstance(_sel.value, str):
                            prev_values[_ln] = _sel.value
                    except Exception:
                        pass

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

                # Remove is only available for locally built libs (not system-provided).
                # ``pc_file == "system"`` marks a native-host lib detected via
                # the host pkg-config — never remove those. Empty pc_file on a
                # cross toolchain just means the .pc was lost (broken /
                # quarantined / stripped by the vendor) but the actual .so is
                # still in the sysroot and ought to be removable.
                _is_local_lib = lib.found and lib.pc_file != "system"

                if lib.found and lib.up_to_date:
                    options.append((t("lib_skip_uptodate"), "skip"))
                    options.append((f"{t('lib_reinstall')} {lib.version}", f"reinstall:{lib.version}"))
                    if _is_local_lib:
                        from core.toolchain import check_remove_allowed
                        dependents = check_remove_allowed(
                            lib.name, libs,
                            self.app.paths.defaults_dir / "libraries.yaml",
                            self.app.paths.libraries_local,
                        )
                        if not dependents:
                            options.append((f"{t('lib_remove')} {lib.version}", f"remove:{lib.version}"))
                elif lib.found and lib.update_version:
                    options.append((t("lib_skip"), "skip"))
                    options.append((f"→ {lib.update_version} ({t('lib_recommended')})", f"update:{lib.update_version}"))
                    if _is_local_lib:
                        from core.toolchain import check_remove_allowed
                        dependents = check_remove_allowed(
                            lib.name, libs,
                            self.app.paths.defaults_dir / "libraries.yaml",
                            self.app.paths.libraries_local,
                        )
                        if not dependents:
                            options.append((f"{t('lib_remove')} {lib.version}", f"remove:{lib.version}"))
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

                # Default: only suggest an update for libraries that already
                # exist in the toolchain. Missing libraries default to skip
                # so we don't push users to integrate libs they never asked
                # for. Just-removed libs always go to skip — otherwise the
                # row immediately suggests reinstalling what was deleted.
                if removed_libs and lib.name in removed_libs:
                    fresh_default = "skip"
                elif lib.found and not lib.up_to_date and lib.update_version:
                    fresh_default = f"update:{lib.update_version}"
                else:
                    fresh_default = "skip"

                # Preserve user selection for libs that weren't part of the
                # finished build run. Libs in built_libs (successfully built
                # this round) get the fresh default.
                if (
                    built_libs is not None
                    and lib.name not in built_libs
                    and lib.name in prev_values
                ):
                    candidate = prev_values[lib.name]
                    valid_values = {o[1] for o in options}
                    default = candidate if candidate in valid_values else fresh_default
                else:
                    default = fresh_default

                # Update select options
                try:
                    sel = self.query_one(f"#lib-action-{safe_id}", Select)
                    sel.set_options(options)
                    sel.value = default
                except Exception:
                    pass

        except Exception:
            pass

    def _update_footer_state(self) -> None:
        """Update F5 (Build/Cancel), F9 (Update/Cancel) and F12 (Save) buttons.

        F5 Cancel is shown only when a SelfBuild is in progress (started from
        SelfBuild tab). F9 Cancel only for library builds (PreBuild + Libs tab).
        """
        tpl_selected = self._is_template_selected()
        libs_active = self._is_libs_tab_active()
        sb_building = self._building and getattr(self, "_build_kind", "") == "selfbuild"
        lib_building = self._building and getattr(self, "_build_kind", "") == "libs"
        try:
            for item in self.query("FooterItem"):
                if item._key == "F9":
                    if lib_building:
                        item._label = t("cancel")
                        item._disabled = False
                        item._active = True
                        item.remove_class("disabled")
                    else:
                        item._label = t("update")
                        item._active = False
                        # F9 only for installed toolchain library actions, never
                        # for templates or uninstalled entries
                        f9_enabled = libs_active and not tpl_selected and self._selected_is_installed()
                        item._disabled = not f9_enabled
                        if f9_enabled:
                            item.remove_class("disabled")
                        else:
                            item.add_class("disabled")
                    item.refresh()
                elif item._key == "F5":
                    if sb_building:
                        item._label = t("cancel")
                        item._disabled = False
                        item._active = True
                        item.remove_class("disabled")
                    else:
                        item._label = t("build")
                        item._active = False
                        f5_enabled = tpl_selected and self._is_template_buildable()
                        item._disabled = not f5_enabled
                        if f5_enabled:
                            item.remove_class("disabled")
                        else:
                            item.add_class("disabled")
                    item.refresh()
                elif item._key == "F6":
                    # Enable F6 Customize only when a template is selected
                    item._disabled = not tpl_selected or sb_building
                    if item._disabled:
                        item.add_class("disabled")
                    else:
                        item.remove_class("disabled")
                    item.refresh()
                elif item._key == "F12":
                    yaml_active = self._is_editor_tab_active()
                    item._disabled = not yaml_active
                    if yaml_active:
                        item.remove_class("disabled")
                    else:
                        item.add_class("disabled")
                    item.refresh()
                elif item._key == "F11":
                    # Enabled only when the selection is a removable local
                    # entry/template that isn't installed.
                    f11_enabled = self._is_local_removable() and not self._building
                    item._disabled = not f11_enabled
                    if f11_enabled:
                        item.remove_class("disabled")
                    else:
                        item.add_class("disabled")
                    item.refresh()
        except Exception:
            pass

    def _selected_is_installed(self) -> bool:
        """True when the selected PreBuild toolchain is actually installed."""
        try:
            if self._is_template_selected():
                return False
            name = ToolchainsScreen._selected_tc
            if not name:
                return False
            tc = self._get_registry().get(name)
            return bool(tc and tc.installed)
        except Exception:
            return False

    def _is_local_removable(self) -> bool:
        """True when the current selection is a removable local entry.

        Removable means: from data/templates/ (SelfBuild local) OR from
        toolchains.local.yaml (PreBuild local), AND not currently
        installed (no data/toolchains/<name>/ directory).
        """
        name = ToolchainsScreen._selected_tc
        if not name:
            return False
        # Template case (SelfBuild local)
        if self._is_template_selected():
            if ToolchainsScreen._selected_tpl_source != "local":
                return False
            install_dir = self.app.paths.toolchains_dir / name
            return not install_dir.exists()
        # PreBuild case — check if entry exists in toolchains.local.yaml
        try:
            reg = self._get_registry()
            local_tcs = reg._load_file(reg.local_path)
            in_local = any(tc.name == name for tc in local_tcs)
            if not in_local:
                return False
            tc = reg.get(name)
            if tc and tc.installed:
                return False
            return True
        except Exception:
            return False

    _pending_local_remove: str = ""

    def do_remove(self) -> None:
        """F11 Remove — delete a local catalog entry or local template.

        Only acts when the current selection is a removable local item
        (see _is_local_removable). Two-press confirmation: the first
        press shows a warning, a second press within 5 s actually
        removes the entry.
        """
        if self._building:
            return
        if not self._is_local_removable():
            return
        name = ToolchainsScreen._selected_tc
        if ToolchainsScreen._pending_local_remove != name:
            ToolchainsScreen._pending_local_remove = name
            self.notify(t("local_remove_confirm", name=name), severity="warning", timeout=5)
            self.set_timer(5.0, self._clear_pending_local_remove)
            return
        ToolchainsScreen._pending_local_remove = ""

        if self._is_template_selected():
            self._remove_local_template(name)
        else:
            self._remove_local_toolchain_entry(name)

    def _clear_pending_local_remove(self) -> None:
        ToolchainsScreen._pending_local_remove = ""

    def _remove_local_template(self, name: str) -> None:
        """Delete data/templates/<name>.yaml + corresponding .config."""
        local_dir = self.app.paths.templates_local
        yaml_path = local_dir / f"{name}.yaml"
        if not yaml_path.exists():
            self.notify(t("template_not_found", name=name), severity="error")
            return
        # Determine config filename from the yaml before unlinking
        cfg_name = ""
        try:
            from core.template import load_template
            tpl = load_template(yaml_path)
            if tpl:
                cfg_name = tpl.config or ""
        except Exception:
            pass
        try:
            yaml_path.unlink()
            self._log_sync(f"  [green]Deleted {yaml_path.name}[/green]")
            if cfg_name:
                cfg_path = local_dir / cfg_name
                if cfg_path.exists():
                    cfg_path.unlink()
                    self._log_sync(f"  [green]Deleted {cfg_name}[/green]")
        except Exception as e:
            self.notify(t("template_remove_failed", err=str(e)), severity="error")
            return
        self.notify(t("local_remove_done", name=name))
        # Clear selection and refresh
        ToolchainsScreen._selected_tc = ""
        ToolchainsScreen._selected_tpl_source = ""
        try:
            self._mounted = False
            self._refresh_selfbuild_tables()
            self._update_detail()
            self._update_editor()
        finally:
            self._mounted = True
        self._update_footer_state()

    def _remove_local_toolchain_entry(self, name: str) -> None:
        """Delete a single entry from toolchains.local.yaml by name."""
        reg = self._get_registry()
        try:
            local_tcs = reg._load_file(reg.local_path)
            new_tcs = [tc for tc in local_tcs if tc.name != name]
            if len(new_tcs) == len(local_tcs):
                self.notify(t("local_remove_not_found", name=name), severity="error")
                return
            reg.save_local(new_tcs)
            self._log_sync(f"  [green]Removed {name} from toolchains.local.yaml[/green]")
        except Exception as e:
            self.notify(t("local_remove_failed", err=str(e)), severity="error")
            return
        self.notify(t("local_remove_done", name=name))
        # Force a registry reload + screen refresh
        self._registry = None
        self.app.pop_screen()
        self.app.push_screen(ToolchainsScreen())

    def do_tc_update(self) -> None:
        """F9 — execute library actions or cancel running build."""
        if self._building:
            # Cancel running build
            self._cancel_requested = True
            self._log_sync("[yellow]Cancelling build...[/yellow]")
            return

        # Clear output before starting
        try:
            self.query_one("#tc-output", RichLog).clear()
            ToolchainsScreen._log_history.clear()
        except Exception:
            pass

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
        self._build_kind = "libs"
        self._cancel_requested = False
        self.app.build_timer_start("toolchains")
        self._update_footer_state()
        order = ", ".join(name for name, _ in actions)
        has_remove = any(a.startswith("remove:") for _, a in actions)
        has_build = any(not a.startswith("remove:") for _, a in actions)
        if has_remove and not has_build:
            verb = "Removing"
        elif has_remove and has_build:
            verb = "Processing"
        else:
            verb = "Building"
        self._log_sync(f"[bold]{verb} {len(actions)} libraries for {tc.name}:[/bold] {order}")

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

        built_libs: set[str] = set()
        removed_libs: set[str] = set()

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

                    # Handle remove action
                    if action_type == "remove":
                        _set_lib_status(lib_name, f"[bold yellow]{lib_name:<15s}[/bold yellow] [yellow]removing...[/yellow]")
                        self._log(f"\n[bold]remove: {lib_name} {version}[/bold]")
                        from core.toolchain import remove_library
                        success = remove_library(
                            install_dir=self.app.paths.toolchains_dir / tc.name,
                            sysroot=tc.sysroot,
                            lib_name=lib_name,
                            libraries_yaml=self.app.paths.defaults_dir / "libraries.yaml",
                            libraries_local=self.app.paths.libraries_local,
                            log=self._log,
                        )
                        if success:
                            built_libs.add(lib_name)
                            removed_libs.add(lib_name)
                            _set_lib_status(lib_name, f"[bold green]{lib_name:<15s}[/bold green] [green]removed[/green]")
                            # Keep the persistent libraries record in sync
                            try:
                                self._get_registry().forget_library(tc.name, lib_name)
                            except Exception:
                                pass
                        else:
                            _set_lib_status(lib_name, f"[bold red]{lib_name:<15s}[/bold red] [red]failed[/red]")
                        continue

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
                        organize_logs=self.app.settings.organize_logs,
                    )
                    if success:
                        built_libs.add(lib_name)
                        _set_lib_status(lib_name, f"[bold green]{lib_name:<15s}[/bold green] [green]done[/green]")
                        # Persist "this toolchain has <lib> <version>" so
                        # the update-libraries automation action can re-
                        # install after a full toolchain rebuild. Re-scan
                        # the sysroot to capture the actual version (the
                        # passed `version` may be a recipe selector like
                        # libdvbcsa's "auto", not a real version).
                        try:
                            actual = version
                            for lib in scan_libraries(
                                self.app.paths.toolchains_dir / tc.name,
                                tc.sysroot, native=tc.native,
                            ):
                                if lib.name == lib_name and lib.found and lib.version:
                                    actual = lib.version
                                    break
                            self._get_registry().record_library(tc.name, lib_name, actual)
                        except Exception:
                            pass
                        # Export shared libs to zip if enabled
                        if self.app.settings.export_libs_zip:
                            try:
                                from core.lib_export import export_lib_to_zip
                                export_lib_to_zip(
                                    lib_name=lib_name,
                                    toolchain_dir=self.app.paths.toolchains_dir / tc.name,
                                    toolchain_sysroot=tc.sysroot,
                                    binaries_dir=self.app.paths.binaries_dir,
                                    tc_name=tc.name,
                                    libraries_yaml=self.app.paths.defaults_dir / "libraries.yaml",
                                    libraries_local=self.app.paths.libraries_local,
                                    log=self._log,
                                )
                            except Exception:
                                pass
                    else:
                        if self._cancel_requested:
                            _set_lib_status(lib_name, f"[bold yellow]{lib_name:<15s}[/bold yellow] [yellow]cancelled[/yellow]")
                            break
                        else:
                            _set_lib_status(lib_name, f"[bold red]{lib_name:<15s}[/bold red] [red]failed[/red]")

                self.app.call_from_thread(self.app.build_timer_stop)
                elapsed = self.app.build_timer_format()
                if elapsed:
                    self._log(f"[bold]Elapsed:[/bold] {elapsed}")
                self._log(f"\n[bold]Done[/bold]")
                try:
                    from core.log_cleanup import run_retention
                    run_retention(self.app.paths.logs_dir, self.app.settings, log=self._log)
                except Exception:
                    pass
            finally:
                self._building = False
                self._cancel_requested = False
                self.app.call_from_thread(self._update_footer_state)
                # Refresh library info (stay on Libraries tab)
                # Pass built_libs so unrelated picks the user made
                # before pressing F9 are preserved. removed_libs forces
                # those rows back to "skip" rather than auto-suggesting
                # a fresh install of the lib the user just removed.
                self.app.call_from_thread(self._update_libraries, built_libs, removed_libs)

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
        thread = threading.Thread(target=func, daemon=True)
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
        """Switch between Details, Editor, Libraries and Log views."""
        detail = self.query_one("#tc-detail", Static)
        libs = self.query_one("#tc-libs", VerticalScroll)
        editor = self.query_one("#tc-editor", TextArea)
        output = self.query_one("#tc-output", RichLog)
        tab_details = self.query_one("#btn-tab-details", Static)
        tab_libs = self.query_one("#btn-tab-libs", Static)
        tab_editor = self.query_one("#btn-tab-editor", Static)
        tab_log = self.query_one("#btn-tab-log", Static)

        # Reset all tabs to inactive
        tab_details.set_classes("tab-inactive")
        tab_libs.set_classes("tab-inactive")
        tab_editor.set_classes("tab-inactive")
        tab_log.set_classes("tab-inactive")

        # Hide all panels
        detail.display = False
        libs.display = False
        editor.display = False

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
        elif tab == "editor":
            editor.display = True
            tab_editor.set_classes("tab-active")
            output.styles.height = "1fr"
            self._update_editor()
        elif tab == "log":
            tab_log.set_classes("tab-active")
            output.styles.height = "1fr"

        ToolchainsScreen._active_right_tab = tab
        # Update F10 button state in footer
        self._update_footer_state()
        # Focus the first interactive widget on the new tab.
        first_focus = {
            "details": "#tc-detail",
            "editor": "#tc-editor",
            "libs": "#tc-libs",
            "log": "#tc-output",
        }
        fid = first_focus.get(tab)
        if fid:
            try:
                self.query_one(fid).focus()
            except Exception:
                pass

    # -- YAML editor (Yaml tab) ---------------------------------------------

    def is_dirty(self) -> bool:
        """True only if the editor has unsaved changes vs. its loaded source."""
        try:
            editor = self.query_one("#tc-editor", TextArea)
            if not editor.display or editor.disabled:
                return False
            return editor.text != getattr(self, "_editor_loaded_text", editor.text)
        except Exception:
            return False

    def _update_editor(self) -> None:
        """Load editor content based on the active left tab.

        - PreBuild: YAML block of the selected installed toolchain
        - SelfBuild: full YAML of the selected template
        """
        editor = self.query_one("#tc-editor", TextArea)

        name = ToolchainsScreen._selected_tc

        # Template selected → load template YAML
        if self._is_template_selected():
            tpl = self._get_selected_template()
            if not tpl or not tpl.yaml_path:
                editor.text = f"# {t('template_not_found', name=name)}"
                editor.disabled = True
                self._editor_loaded_text = editor.text
                return
            try:
                editor.text = _ensure_excl_auto(tpl.yaml_path.read_text(encoding="utf-8"))
                editor.disabled = False
            except Exception as e:
                editor.text = f"# Error: {e}"
                editor.disabled = True
            self._editor_loaded_text = editor.text
            return

        reg = self._get_registry()
        tc = reg.get(name) if name else None

        if not tc or not tc.installed:
            editor.text = f"# {t('yaml_only_installed')}"
            editor.disabled = True
            self._editor_loaded_text = editor.text
            return

        block = self._extract_yaml_block(name)
        if block is None:
            editor.text = f"# {t('yaml_block_not_found', name=name)}"
            editor.disabled = True
            self._editor_loaded_text = editor.text
            return

        editor.disabled = False
        editor.text = _ensure_excl_auto(block)
        self._editor_loaded_text = editor.text

    def _extract_yaml_block(self, tc_name: str) -> "str | None":
        """Extract the YAML block of a single toolchain from toolchains.local.yaml.

        Returns the raw text (without the leading '- ' prefix removed — keeps
        the original formatting). Returns None if not found.
        """
        path = self.app.paths.toolchains_local
        if not path.exists():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return None

        # Find the line "- name: <tc_name>" and collect until next "- name:" or non-indented line
        start = -1
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("- name:"):
                # Extract name value
                val = stripped[len("- name:"):].strip().strip('"').strip("'")
                if val == tc_name:
                    start = i
                    break
        if start < 0:
            return None

        # Collect lines until next "- name:" at same indent or end / non-list line
        end = len(lines)
        for j in range(start + 1, len(lines)):
            stripped = lines[j].lstrip()
            if stripped.startswith("- name:"):
                end = j
                break
            # Stop if we hit a top-level key (no indent) or empty section break
            if lines[j] and not lines[j].startswith(" ") and not lines[j].startswith("-"):
                end = j
                break

        return "\n".join(lines[start:end]).rstrip() + "\n"

    def _replace_yaml_block(self, tc_name: str, new_block: str) -> bool:
        """Replace the toolchain block in toolchains.local.yaml."""
        path = self.app.paths.toolchains_local
        if not path.exists():
            return False
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return False

        start = -1
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("- name:"):
                val = stripped[len("- name:"):].strip().strip('"').strip("'")
                if val == tc_name:
                    start = i
                    break
        if start < 0:
            return False

        end = len(lines)
        for j in range(start + 1, len(lines)):
            stripped = lines[j].lstrip()
            if stripped.startswith("- name:"):
                end = j
                break
            if lines[j] and not lines[j].startswith(" ") and not lines[j].startswith("-"):
                end = j
                break

        new_lines = new_block.rstrip().splitlines()
        # Ensure block starts with "- name:" (preserve list item)
        if not new_lines or not new_lines[0].lstrip().startswith("- name:"):
            return False

        result = lines[:start] + new_lines + lines[end:]
        try:
            path.write_text("\n".join(result) + "\n", encoding="utf-8")
            return True
        except Exception:
            return False

    def _save_template(self, new_text: str, name: str, saved_cursor, editor_was_focused: bool) -> str:
        """Save a SelfBuild template YAML.

        Fork-on-write: if the user is editing a default template (shipped in
        defaults/templates/), we copy both the .yaml and the .config to
        data/templates/ before applying the changes. If a local copy with the
        same name already exists, the edit is rejected — the user has to
        delete the local copy first.
        """
        import shutil as _shutil
        import yaml as _yaml

        self._log_sync(f"[bold]Validating template YAML for {name}...[/bold]")

        # Step 1: YAML syntax check
        try:
            parsed = _yaml.safe_load(new_text)
        except _yaml.YAMLError as e:
            err = str(e).splitlines()[0]
            msg = t("yaml_syntax_error", err=err)
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return ""
        self._log_sync("  [green]✓ Syntax OK[/green]")

        if not isinstance(parsed, dict):
            msg = t("template_invalid_yaml")
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return ""

        # Step 2: Field validation against Template dataclass
        from dataclasses import fields as _fields
        from core.template import Template as _Tpl
        allowed = {f.name for f in _fields(_Tpl) if f.name not in ("yaml_path", "is_local")}
        unknown = [k for k in parsed.keys() if k not in allowed]
        if unknown:
            msg = t("yaml_unknown_fields", fields=", ".join(unknown))
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return ""
        self._log_sync("  [green]✓ Fields OK[/green]")

        # Step 3: handle rename (name in YAML differs from selection)
        new_name = (parsed.get("name") or "").strip()
        if not new_name:
            msg = t("template_name_required")
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return ""

        is_rename = (new_name != name)
        if is_rename:
            import re as _re
            if not _re.fullmatch(r"[a-zA-Z0-9_.-]+", new_name):
                msg = t("template_rename_invalid_name", name=new_name)
                self._log_sync(f"  [red]✗ {msg}[/red]")
                self.notify(msg, severity="error", timeout=10)
                return ""
            # Collision check against ALL templates (defaults + local)
            from core.template import load_templates as _load_tpls
            _all = _load_tpls(self.app.paths.templates_defaults, self.app.paths.templates_local)
            if any(t.name == new_name for t in _all):
                msg = t("template_rename_name_exists", name=new_name)
                self._log_sync(f"  [red]✗ {msg}[/red]")
                self.notify(msg, severity="error", timeout=10)
                return ""

        # Step 4: Find current template (incl. source) to know if we need to fork
        tpl = self._get_selected_template()
        if not tpl or not tpl.yaml_path:
            msg = t("template_not_found", name=name)
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return ""

        local_dir = self.app.paths.templates_local
        # Final on-disk yaml/config names depend on rename
        final_name = new_name if is_rename else name
        local_yaml = local_dir / f"{final_name}.yaml"

        # Adjust the `config:` field inside the new_text if it still references
        # the old config filename — keeps yaml + on-disk file name in sync.
        if is_rename and tpl.config:
            old_cfg = tpl.config
            old_stem = Path(old_cfg).stem
            if old_stem == name:
                new_cfg = f"{new_name}{Path(old_cfg).suffix or '.config'}"
                # Replace inside YAML text so the dump stays user-formatted
                import re as _re2
                new_text = _re2.sub(
                    r'(?m)^(\s*config\s*:\s*)["\']?' + _re2.escape(old_cfg) + r'["\']?\s*$',
                    rf'\1{new_cfg}',
                    new_text,
                )
                parsed["config"] = new_cfg

        if not tpl.is_local:
            # Editing a default template → fork to data/templates/
            if local_yaml.exists():
                msg = t("template_local_exists", name=final_name)
                self._log_sync(f"  [red]✗ {msg}[/red]")
                self.notify(msg, severity="error", timeout=10)
                return ""
            try:
                local_dir.mkdir(parents=True, exist_ok=True)
                # Copy the .config (under new filename if renamed)
                if tpl.config and tpl.config_path and tpl.config_path.exists():
                    target_cfg_name = parsed.get("config") or tpl.config
                    _shutil.copy2(tpl.config_path, local_dir / target_cfg_name)
                    self._log_sync(f"  [green]✓ Forked {tpl.config} → data/templates/{target_cfg_name}[/green]")
                local_yaml.write_text(new_text, encoding="utf-8")
                self._log_sync(f"  [green]✓ Forked {name}.yaml → data/templates/{final_name}.yaml[/green]")
            except Exception as e:
                msg = t("template_fork_failed", err=str(e))
                self._log_sync(f"  [red]✗ {msg}[/red]")
                self.notify(msg, severity="error", timeout=10)
                return ""
            ToolchainsScreen._selected_tpl_source = "local"
        else:
            # Already local → overwrite (and rename files if requested)
            try:
                if is_rename:
                    # Rename .config first
                    if tpl.config and tpl.config_path and tpl.config_path.exists():
                        target_cfg_name = parsed.get("config") or tpl.config
                        new_cfg_path = local_dir / target_cfg_name
                        if new_cfg_path.exists() and new_cfg_path != tpl.config_path:
                            msg = t("template_rename_dir_exists", name=target_cfg_name)
                            self._log_sync(f"  [red]✗ {msg}[/red]")
                            self.notify(msg, severity="error", timeout=10)
                            return ""
                        tpl.config_path.rename(new_cfg_path)
                        self._log_sync(f"  [green]✓ Renamed {tpl.config} → {target_cfg_name}[/green]")
                    # Then rename .yaml (write new content + remove old file)
                    local_yaml.write_text(new_text, encoding="utf-8")
                    if tpl.yaml_path.exists() and tpl.yaml_path != local_yaml:
                        tpl.yaml_path.unlink()
                    self._log_sync(f"  [green]✓ Renamed {name}.yaml → {final_name}.yaml[/green]")
                else:
                    tpl.yaml_path.write_text(new_text, encoding="utf-8")
                    self._log_sync(f"  [green]✓ Saved {tpl.yaml_path.name}[/green]")
            except Exception as e:
                msg = t("template_save_failed", err=str(e))
                self._log_sync(f"  [red]✗ {msg}[/red]")
                self.notify(msg, severity="error", timeout=10)
                return ""

        if is_rename:
            ToolchainsScreen._selected_tc = final_name
            name = final_name

        # Step 5: Refresh SelfBuild tables and detail/editor
        try:
            # Suppress row_highlighted handlers while tables rebuild — otherwise
            # RowHighlighted from e.g. tbl-sb-ctng row 0 would clobber our
            # _selected_tc with whatever sits at the top of those tables.
            self._mounted = False
            self._refresh_selfbuild_tables()
            # After a rename, move the cursor in the local table to the new entry
            if is_rename:
                try:
                    self.query_one("#cat-sb-local", Collapsible).collapsed = False
                except Exception:
                    pass
                try:
                    sb_tbl = self.query_one("#tbl-sb-local", DataTable)
                    for i, key in enumerate(sb_tbl.rows):
                        if key.value == final_name:
                            sb_tbl.move_cursor(row=i)
                            if hasattr(sb_tbl, "mark_row"):
                                sb_tbl.mark_row(i)
                            sb_tbl.focus()
                            break
                except Exception as e:
                    self._log_sync(f"  [yellow]select_renamed: {e}[/yellow]")
                # Pin the selection again after the deferred RowHighlighted
                # events from the table rebuild have flushed.
                def _repin():
                    ToolchainsScreen._selected_tc = final_name
                    ToolchainsScreen._selected_tpl_source = "local"
                    self._mounted = True
                    self._update_detail()
                    self._update_editor()
                self.set_timer(0.1, _repin)
            else:
                self._mounted = True
            self._update_detail()
            self._update_editor()
            # Skip editor focus restore on rename — focus stays on the SB table
            # so the highlight on the renamed entry is visible.
            if saved_cursor and editor_was_focused and not is_rename:
                self.set_timer(0.05, lambda: self._restore_editor_cursor(saved_cursor))
        except Exception:
            pass

        return t("template_saved", name=name)

    def _refresh_selfbuild_tables(self) -> None:
        """Reload SelfBuild templates and rebuild all tables."""
        from core.template import load_templates
        templates = load_templates(
            self.app.paths.templates_defaults,
            self.app.paths.templates_local,
        )
        _builder_table = {
            "crosstool-ng": "tbl-sb-ctng",
            "freetz-ng": "tbl-sb-freetz",
            "oe-sdk": "tbl-sb-oesdk",
            "android-ndk": "tbl-sb-android",
        }
        _builder_collapsible = {
            "oe-sdk": ("cat-sb-oesdk", "OE-SDK"),
            "crosstool-ng": ("cat-sb-ctng", "Crosstool-NG"),
            "freetz-ng": ("cat-sb-freetz", "Freetz-NG"),
            "android-ndk": ("cat-sb-android", "Android-NDK"),
        }
        # Clear all
        for sb_id in ("tbl-sb-local", "tbl-sb-ctng", "tbl-sb-freetz", "tbl-sb-oesdk", "tbl-sb-android"):
            try:
                self.query_one(f"#{sb_id}", DataTable).clear()
            except Exception:
                pass
        local_count = 0
        builder_counts: dict[str, int] = {}
        for tpl in templates:
            _reg_entry = self._get_registry().get(tpl.name)
            installed = bool(_reg_entry and _reg_entry.installed)
            status = "●" if installed else ""
            if tpl.is_local:
                try:
                    tbl_sb = self.query_one("#tbl-sb-local", DataTable)
                    tbl_sb.add_row(status, tpl.name, tpl.arch, tpl.builder, tpl.description, key=tpl.name)
                    local_count += 1
                except Exception:
                    pass
            else:
                tbl_id = _builder_table.get(tpl.builder)
                if tbl_id:
                    try:
                        tbl_sb = self.query_one(f"#{tbl_id}", DataTable)
                        tbl_sb.add_row(status, tpl.name, tpl.arch, tpl.builder, tpl.description, key=tpl.name)
                        builder_counts[tpl.builder] = builder_counts.get(tpl.builder, 0) + 1
                    except Exception:
                        pass
        try:
            self.query_one("#cat-sb-local", Collapsible).title = f"Local ({local_count})"
            for builder, (col_id, label) in _builder_collapsible.items():
                count = builder_counts.get(builder, 0)
                self.query_one(f"#{col_id}", Collapsible).title = f"{label} ({count})"
        except Exception:
            pass

    def do_save(self) -> str:
        """F12 Save handler. Only saves the YAML editor if the Yaml tab is active."""
        try:
            editor = self.query_one("#tc-editor", TextArea)
            if not editor.display:
                return ""  # Not on editor tab → nothing to save
        except Exception:
            return ""
        if editor.disabled:
            return ""

        name = ToolchainsScreen._selected_tc
        if not name:
            return ""

        new_text = editor.text
        # Save cursor position to restore after reload
        saved_cursor = editor.cursor_location
        editor_was_focused = editor.has_focus

        # Template selected → save template (with fork-on-write logic)
        if self._is_template_selected():
            return self._save_template(new_text, name, saved_cursor, editor_was_focused)

        self._log_sync(f"[bold]Validating YAML for {name}...[/bold]")

        # Step 1: YAML syntax check
        # Wrap the block in a "toolchains:" mapping for parsing
        try:
            import yaml as _yaml
            wrapped = "toolchains:\n" + "\n".join("  " + ln if ln else ln for ln in new_text.splitlines())
            parsed = _yaml.safe_load(wrapped)
        except _yaml.YAMLError as e:
            err = str(e).splitlines()[0]
            msg = t("yaml_syntax_error", err=err)
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return ""
        self._log_sync("  [green]✓ Syntax OK[/green]")

        if not parsed or "toolchains" not in parsed or not parsed["toolchains"]:
            msg = t("yaml_no_entry")
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return ""

        entries = parsed["toolchains"]
        if len(entries) != 1:
            msg = t("yaml_multi_entries")
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return ""

        entry = entries[0]
        if not isinstance(entry, dict):
            msg = t("yaml_no_entry")
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return ""
        self._log_sync(f"  [green]✓ Single entry found ({len(entry)} fields)[/green]")

        # Step 2: Field validation against Toolchain dataclass
        from dataclasses import fields as _fields
        allowed = {f.name for f in _fields(Toolchain) if f.name != "installed"}
        unknown = [k for k in entry.keys() if k not in allowed]
        if unknown:
            msg = t("yaml_unknown_fields", fields=", ".join(unknown))
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return ""
        self._log_sync("  [green]✓ All fields known[/green]")

        # Type check (simple)
        type_map = {f.name: f.type for f in _fields(Toolchain)}
        for k, v in entry.items():
            expected = type_map.get(k)
            if expected is bool and not isinstance(v, bool):
                msg = t("yaml_wrong_type", field=k, expected="bool")
                self._log_sync(f"  [red]✗ {msg}[/red]")
                self.notify(msg, severity="error", timeout=10)
                return ""
            if expected is int and not isinstance(v, int):
                msg = t("yaml_wrong_type", field=k, expected="int")
                self._log_sync(f"  [red]✗ {msg}[/red]")
                self.notify(msg, severity="error", timeout=10)
                return ""
            if expected is str and not isinstance(v, (str, type(None))):
                msg = t("yaml_wrong_type", field=k, expected="str")
                self._log_sync(f"  [red]✗ {msg}[/red]")
                self.notify(msg, severity="error", timeout=10)
                return ""
            # libraries: dict[str, str] — allow empty/missing; if given,
            # must be a mapping of strings to strings.
            if k == "libraries":
                if v is None:
                    continue
                if not isinstance(v, dict) or not all(
                    isinstance(lk, str) and isinstance(lv, (str, int, float))
                    for lk, lv in v.items()
                ):
                    msg = t("yaml_wrong_type", field=k, expected="dict[str, str]")
                    self._log_sync(f"  [red]✗ {msg}[/red]")
                    self.notify(msg, severity="error", timeout=10)
                    return ""
        self._log_sync("  [green]✓ Field types OK[/green]")

        # Detect rename: name field changed
        new_name = entry.get("name", "")
        is_rename = (new_name != name)

        if is_rename:
            ok, msg = self._rename_toolchain(name, new_name, new_text)
            if not ok:
                return ""
            final_name = new_name
            result_msg = msg
        else:
            # Step 3: Save (no rename)
            if not self._replace_yaml_block(name, new_text):
                msg = t("yaml_save_failed")
                self._log_sync(f"  [red]✗ {msg}[/red]")
                self.notify(msg, severity="error", timeout=10)
                return ""
            self._log_sync(f"  [green]✓ Written to toolchains.local.yaml[/green]")
            if entry.get("exclude_from_automation", False):
                from core.automation import remove_from_jobs
                remove_from_jobs(self.app.paths.automation_yaml, toolchain=name)
            final_name = name
            result_msg = t("yaml_saved", name=name)

        # Step 4: Reload registry and refresh tables
        self._registry = None
        reg = self._get_registry()
        try:
            tbl = self.query_one("#tbl-installed", DataTable)
            tbl.clear()
            row_index = -1
            for i, tc in enumerate(reg.installed_toolchains()):
                if tc.native:
                    tbl.add_row(
                        Text(tc.name, style="bold"),
                        Text(tc.arch, style="bold"),
                        Text(tc.description, style="bold"),
                        key=tc.name,
                    )
                else:
                    tbl.add_row(tc.name, tc.arch, tc.description, key=tc.name)
                if tc.name == final_name:
                    row_index = i
            # Restore selection on the just-edited (or renamed) toolchain
            if row_index >= 0:
                tbl.move_cursor(row=row_index)
                if hasattr(tbl, "mark_row"):
                    tbl.mark_row(row_index)
                ToolchainsScreen._selected_tc = final_name
                # Refresh yaml editor with new content
                self._update_editor()
                # Defer cursor restore: row_highlighted events trigger another
                # _update_editor() that would reset (0,0). Run after events.
                if saved_cursor and editor_was_focused:
                    self.set_timer(0.05, lambda: self._restore_editor_cursor(saved_cursor))
        except Exception:
            pass

        return result_msg

    def _restore_editor_cursor(self, saved_cursor) -> None:
        """Restore cursor position in the YAML editor after a save+reload."""
        try:
            editor = self.query_one("#tc-editor", TextArea)
            if editor.disabled:
                return
            line_count = editor.document.line_count
            row, col = saved_cursor
            if row >= line_count:
                row = max(0, line_count - 1)
            try:
                line_len = len(editor.document.get_line(row))
            except Exception:
                line_len = 0
            if col > line_len:
                col = line_len
            editor.cursor_location = (row, col)
            editor.focus()
        except Exception:
            pass

    def _rename_toolchain(self, old_name: str, new_name: str, new_yaml_text: str) -> tuple:
        """Rename an installed toolchain. Returns (success, message).

        Performs all rename steps with rollback on failure.
        """
        import re as _re
        import shutil as _shutil

        self._log_sync(f"[bold]Renaming {old_name} → {new_name}...[/bold]")

        reg = self._get_registry()
        tc = reg.get(old_name)
        if not tc:
            self.notify(t("yaml_rename_failed", err=f"toolchain {old_name} not found"),
                        severity="error", timeout=10)
            return (False, "")

        # --- Pre-validation ---
        # 1. Block native
        if tc.native:
            msg = t("yaml_rename_native_blocked")
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return (False, "")

        # 2. Name format
        if not new_name or not _re.match(r"^[a-zA-Z0-9_.\-]+$", new_name):
            msg = t("yaml_rename_invalid_name", name=new_name)
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return (False, "")

        # 3. Name collision in toolchains.local.yaml
        local_tcs = reg._load_file(reg.local_path)
        if any(t_.name == new_name for t_ in local_tcs):
            msg = t("yaml_rename_name_exists", name=new_name)
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return (False, "")

        # 4. Directory collision
        old_dir = self.app.paths.toolchains_dir / old_name
        new_dir = self.app.paths.toolchains_dir / new_name
        if new_dir.exists():
            msg = t("yaml_rename_dir_exists", name=new_name)
            self._log_sync(f"  [red]✗ {msg}[/red]")
            self.notify(msg, severity="error", timeout=10)
            return (False, "")

        self._log_sync("  [green]✓ Pre-validation OK[/green]")

        # --- Perform rename ---
        done_steps = []  # for rollback

        try:
            # Step 1: Move directory
            if old_dir.exists():
                _shutil.move(str(old_dir), str(new_dir))
                done_steps.append("move_dir")
                self._log_sync(f"  [green]✓ Moved {old_name} → {new_name}[/green]")

            # Step 2: Relocate
            from core.toolchain import Toolchain as _TC
            new_tc = _TC(name=new_name, prefix=tc.prefix, sysroot=tc.sysroot, arch=tc.arch)
            if new_dir.exists():
                pc_total, pc_fixed = self._fix_pc_files(new_dir)
                self._log_sync(f"  [green]✓ .pc files: {pc_fixed}/{pc_total} updated[/green]")
                la_total, la_fixed = self._fix_la_files(new_dir)
                if la_total:
                    self._log_sync(f"  [green]✓ .la files: {la_fixed}/{la_total} updated[/green]")
                self._relocate_toolchain(new_dir, new_tc)
                done_steps.append("relocate")
                self._log_sync("  [green]✓ ELF interpreters relocated[/green]")

            # Step 3: Update target references
            from core.target import Target as _Target
            updated_targets = []
            targets_dir = self.app.paths.targets_dir
            if targets_dir.is_dir():
                for target_path in targets_dir.glob("*.yaml"):
                    try:
                        target = _Target.load(target_path)
                    except Exception:
                        continue
                    if any(tcc.name == old_name for tcc in target.toolchains):
                        for tcc in target.toolchains:
                            if tcc.name == old_name:
                                tcc.name = new_name
                        target.save(target_path)
                        updated_targets.append(target_path)
            done_steps.append(("targets", updated_targets))
            if updated_targets:
                self._log_sync(f"  [green]✓ Updated {len(updated_targets)} target(s)[/green]")
            else:
                self._log_sync("  [green]✓ No targets to update[/green]")

            # Step 4: Replace YAML block in toolchains.local.yaml
            if not self._replace_yaml_block(old_name, new_yaml_text):
                raise RuntimeError("failed to write toolchains.local.yaml")
            done_steps.append("yaml_block")
            self._log_sync("  [green]✓ Updated toolchains.local.yaml[/green]")

        except Exception as e:
            # Rollback
            self._log_sync(f"  [red]✗ {t('yaml_rename_failed', err=str(e))}[/red]")
            self._rollback_rename(done_steps, old_dir, new_dir, old_name, new_name, tc)
            self._log_sync(f"  [yellow]{t('yaml_rename_rollback')}[/yellow]")
            self.notify(t("yaml_rename_failed", err=str(e)), severity="error", timeout=10)
            return (False, "")

        return (True, t("yaml_renamed", old=old_name, new=new_name))

    def _rollback_rename(self, done_steps, old_dir, new_dir, old_name, new_name, old_tc) -> None:
        """Rollback rename operations on error."""
        import shutil as _shutil

        for step in reversed(done_steps):
            try:
                if step == "move_dir":
                    if new_dir.exists() and not old_dir.exists():
                        _shutil.move(str(new_dir), str(old_dir))
                elif step == "relocate":
                    # Re-relocate to old path
                    if old_dir.exists():
                        self._fix_pc_files(old_dir)
                        self._fix_la_files(old_dir)
                        self._relocate_toolchain(old_dir, old_tc)
                elif isinstance(step, tuple) and step[0] == "targets":
                    from core.target import Target as _Target
                    for target_path in step[1]:
                        try:
                            target = _Target.load(target_path)
                            for tcc in target.toolchains:
                                if tcc.name == new_name:
                                    tcc.name = old_name
                            target.save(target_path)
                        except Exception:
                            pass
                elif step == "yaml_block":
                    # Should not happen — yaml_block is the last step
                    pass
            except Exception:
                pass

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
        # Clear output before starting
        try:
            self.query_one("#tc-output", RichLog).clear()
            ToolchainsScreen._log_history.clear()
        except Exception:
            pass
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

            # Some vendor tarballs (legacy simplebuild3, OE-SDK) ship dirs
            # with mode 0555 — add u+rwx so later library installs and
            # uninstall don't trip on Permission denied.
            ensure_writable_dirs(install_dir)

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
            # Invalidate the cached registry and re-mount the screen so
            # both the Installed table and the PreBuild category table
            # reflect the new state from a clean slate. Cheaper than
            # patching individual cells and immune to DuplicateKey
            # errors when a row was added once during compose then
            # again here.
            self._registry = None
            self.app.call_from_thread(self._set_installed_status, tc, "done")
            self.app.call_from_thread(self._refresh)

        self._run_in_thread(do_install)

    def _resolve_ctng_dir(self) -> Path:
        """Return the absolute crosstool-ng install dir.

        Empty setting → default `data/builders/crosstool-ng`.
        Relative setting → resolved against project root.
        Tilde expanded.
        """
        import os
        raw = (self.app.settings.ctng_dir or "").strip()
        if not raw:
            return self.app.paths.builders_dir / "crosstool-ng"
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            p = self.app.paths.root / p
        return p

    def _handle_selfbuild(self) -> None:
        """Build the selected SelfBuild template via its toolchain builder."""
        # Clear output before starting a new build
        try:
            self.query_one("#tc-output", RichLog).clear()
            ToolchainsScreen._log_history.clear()
        except Exception:
            pass
        name = ToolchainsScreen._selected_tc
        if not name:
            self.notify(t("no_template_selected"), severity="warning")
            return

        # Source-aware template lookup (respects local vs default selection)
        tpl = self._get_selected_template()
        if not tpl:
            self.notify(t("template_not_found", name=name), severity="error")
            return

        # Supported builders
        if tpl.builder not in ("crosstool-ng", "oe-sdk", "android-ndk"):
            self.notify(t("builder_not_supported", builder=tpl.builder), severity="warning")
            return

        # OE-SDK: validate config before build
        if tpl.builder == "oe-sdk":
            from core.template import load_oesdk_config
            cfg_path = tpl.config_path
            if not cfg_path or not cfg_path.exists():
                self.notify("OE-SDK config missing — run F6 Customize first", severity="error")
                return
            cfg = load_oesdk_config(cfg_path)
            if not cfg.repo_branch:
                self.notify("Branch not set — run F6 Customize first", severity="error")
                return
            if not cfg.machine:
                self.notify("Machine not set — run F6 Customize first", severity="error")
                return
            if not cfg.repo_url:
                self.notify("Repository URL not set — run F6 Customize first", severity="error")
                return

        # Compute relative paths to project root for nicer logging
        def _rel(p):
            if not p:
                return ""
            try:
                return str(p.relative_to(self.app.paths.root))
            except Exception:
                return str(p)

        source_label = "local" if tpl.is_local else "default"
        self._log_sync(f"[bold]SelfBuild: {tpl.name} ({tpl.builder}) — {source_label}[/bold]")
        if tpl.yaml_path:
            self._log_sync(f"  template:    [dim]{_rel(tpl.yaml_path)}[/dim]")
        if tpl.config_path:
            self._log_sync(f"  config:      [dim]{_rel(tpl.config_path)}[/dim]")
        if tpl.builder == "crosstool-ng":
            ctng_path = self._resolve_ctng_dir()
            self._log_sync(f"  ctng dir:    [dim]{_rel(ctng_path)}[/dim]")
        install_dir_log = self.app.paths.toolchains_dir / tpl.name
        self._log_sync(f"  install to:  [dim]{_rel(install_dir_log)}[/dim]")

        self._building = True
        self._build_kind = "selfbuild"
        self._cancel_requested = False
        self.app.build_timer_start("toolchains")
        self._update_footer_state()

        from datetime import datetime
        from core.log_cleanup import resolve_log_dir
        _log_dir = resolve_log_dir(self.app.paths.logs_dir, "selfbuild", self.app.settings.organize_logs)
        log_path = _log_dir / f"{datetime.now().strftime('%Y-%m-%d.%H-%M-%S')}.{tpl.name}.selfbuild.log"
        log_file = open(log_path, "w", encoding="utf-8")

        def blog(msg: str) -> None:
            import re as _re
            plain = _re.sub(r'\[/?[a-z_ #0-9]+\]', '', msg)
            log_file.write(plain + "\n")
            log_file.flush()
            self._log(msg)

        def cancel_check() -> bool:
            return self._cancel_requested

        def do_build():
            try:
                from core.toolchain_builder import build_toolchain
                ok = build_toolchain(
                    tpl=tpl,
                    paths=self.app.paths,
                    settings=self.app.settings,
                    log=blog,
                    cancel_check=cancel_check,
                )
                if ok:
                    self.app.call_from_thread(self.notify, t("selfbuild_done", name=tpl.name))
            finally:
                self.app.call_from_thread(self.app.build_timer_stop)
                elapsed = self.app.build_timer_format()
                if elapsed:
                    blog(f"[bold]Elapsed:[/bold] {elapsed}")
                blog(f"Log: {log_path}")
                log_file.close()
                try:
                    from core.log_cleanup import run_retention
                    run_retention(self.app.paths.logs_dir, self.app.settings, log=self._log)
                except Exception:
                    pass
                self._building = False
                self.app.call_from_thread(self._update_footer_state)
                self.app.call_from_thread(self._refresh)

        self._run_in_thread(do_build)

    def _register_built_toolchain(self, tpl) -> None:
        """Add or update the built toolchain in toolchains.local.yaml.

        Auto-detects the compiler prefix and sysroot from the actual built
        toolchain directory; the template values are kept as fallback when
        detection fails.

        If an entry with the same name already exists in the local yaml
        (e.g. because the toolchain was previously installed via PreBuild
        Install), it is merged in place: the SelfBuild-relevant fields
        (category, selfbuild, prefix, sysroot, cflags, ldflags, arch,
        machines, description) are overwritten with the fresh values,
        while download metadata (url, checksum) and any custom fields
        carried by the existing entry are preserved.
        """
        reg = self._get_registry()
        local_tcs = reg._load_file(reg.local_path)

        from core.toolchain import detect_toolchain_props
        install_dir = self.app.paths.toolchains_dir / tpl.name
        det_prefix, det_sysroot = detect_toolchain_props(install_dir)
        prefix = det_prefix or tpl.prefix
        sysroot = det_sysroot or tpl.sysroot
        if det_prefix or det_sysroot:
            self._log_sync(
                f"  [green]Auto-detected:[/green] prefix=[dim]{prefix or '-'}[/dim] sysroot=[dim]{sysroot or '-'}[/dim]"
            )

        existing = next((tc for tc in local_tcs if tc.name == tpl.name), None)
        if existing is not None:
            existing.description = tpl.description or existing.description or tpl.name
            existing.category = "selfbuild"
            existing.arch = tpl.arch or existing.arch
            existing.machines = tpl.machines or existing.machines
            existing.prefix = prefix
            existing.sysroot = sysroot
            existing.cflags = tpl.cflags or existing.cflags
            existing.ldflags = tpl.ldflags or existing.ldflags
            existing.selfbuild = True
            self._log_sync(f"  [green]Merged into existing local entry → category=selfbuild[/green]")
        else:
            local_tcs.append(Toolchain(
                name=tpl.name,
                description=tpl.description or tpl.name,
                category="selfbuild",
                arch=tpl.arch,
                machines=tpl.machines,
                prefix=prefix,
                sysroot=sysroot,
                cflags=tpl.cflags,
                ldflags=tpl.ldflags,
                selfbuild=True,
            ))
        reg.save_local(local_tcs)

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

        self._log_sync(f"[bold]Backing up {tc.name}...[/bold]")
        self._set_installed_row_desc(tc.name, "backing up...")

        def do_backup():
            from core.toolchain_backup import backup_toolchain
            ok = backup_toolchain(tc, self.app.paths, reg, self.app.settings, log=self._log)
            if ok:
                self.app.call_from_thread(
                    self._set_installed_row_desc, tc.name, f"{tc.description} [green]✓ backed up[/green]"
                )
            else:
                self.app.call_from_thread(
                    self._set_installed_row_desc, tc.name, "[red]backup failed[/red]"
                )

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
            self._relocate_toolchain(install_dir, temp_tc)

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
            self.app.call_from_thread(self._set_installed_status, tc, "done")

        self._run_in_thread(do_restore)

    def _fix_pc_files(self, install_dir: Path) -> tuple:
        """Fix prefix= paths in .pc files. Returns (total, fixed) counts.

        Two passes:
          1. Rewrite ``prefix=`` to match the current install location
             (the original was baked at vendor build time and breaks
             after a tarball relocation).
          2. Sanity-check the resulting ``includedir``/``libdir`` —
             some vendor toolchains ship a stale ``.pc`` whose
             ``${prefix}/include`` doesn't exist on disk (e.g. a
             ``libusb-1.0.pc`` left over from a different sysroot
             layout). pkg-config picks these up and reports headers
             that aren't there, blocking subsequent library installs
             that depend on them. We delete such files so a later,
             correctly-installed copy in another pkgconfig dir wins.
        """
        import re as _re
        pc_files = list(install_dir.glob("**/pkgconfig/*.pc"))
        self._log(f"  Checking {len(pc_files)} .pc files...")
        fixed = 0
        broken = 0
        for pc_file in pc_files:
            try:
                content = pc_file.read_text(encoding="utf-8", errors="replace")
                vars_: dict[str, str] = {}
                for line in content.splitlines():
                    m = _re.match(r"^(\w+)\s*=\s*(.*)$", line)
                    if m:
                        vars_[m.group(1)] = m.group(2).strip()
                old_prefix = vars_.get("prefix", "")
                if not old_prefix:
                    continue

                # New prefix = two levels up from pkgconfig dir
                # e.g. .../sysroot/usr/lib/pkgconfig → .../sysroot/usr
                new_prefix = str(pc_file.parent.parent.parent)

                if old_prefix != new_prefix:
                    content = content.replace(old_prefix, new_prefix)
                    pc_file.write_text(content, encoding="utf-8")
                    self._log(f"  Fixed {pc_file.name}")
                    fixed += 1
                    # Re-read the variable map after the substitution
                    vars_ = {}
                    for line in content.splitlines():
                        m = _re.match(r"^(\w+)\s*=\s*(.*)$", line)
                        if m:
                            vars_[m.group(1)] = m.group(2).strip()

                # Quarantine broken .pc files whose includedir is a
                # phantom path (vendor tarball shipped a stale .pc).
                def _resolve(val: str) -> str:
                    # Naive ${var} expansion against the just-parsed map.
                    out = val
                    for _ in range(5):
                        prev = out
                        for k, v in vars_.items():
                            out = out.replace("${" + k + "}", v)
                        if out == prev:
                            break
                    return out

                inc = _resolve(vars_.get("includedir", ""))
                lib = _resolve(vars_.get("libdir", ""))
                if (inc and not Path(inc).is_dir()) or (lib and not Path(lib).is_dir()):
                    pc_file.unlink()
                    self._log(
                        f"  [yellow]Removed stale {pc_file.name}[/yellow] "
                        f"(includedir/libdir does not exist on disk)"
                    )
                    broken += 1
            except Exception:
                pass
        if broken:
            self._log(f"  Removed {broken} stale .pc file(s)")
        return (len(pc_files), fixed)

    def _fix_la_files(self, install_dir: Path) -> tuple:
        """Fix .la files: replace '=' sysroot prefix. Returns (total, fixed) counts."""
        la_files = list(install_dir.glob("**/*.la"))
        if not la_files:
            return (0, 0)
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
            return (len(la_files), 0)

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
        return (len(la_files), fixed)

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

        # Get arch — try OE-SDK environment-setup file first (most reliable)
        arch = ""
        detected_cflags = ""
        detected_ldflags = ""
        env_setups = list(install_dir.glob("environment-setup-*"))
        if env_setups:
            # Extract arch from filename: environment-setup-cortexa15hf-neon-vfpv4-oe-linux-gnueabi
            env_name = env_setups[0].stem.replace("environment-setup-", "")
            # Trim the -oe-linux-* suffix to get the raw arch part
            raw_arch = ""
            for suffix in ("-oe-linux-gnueabi", "-oe-linux-musl", "-oe-linux"):
                if suffix in env_name:
                    raw_arch = env_name[:env_name.index(suffix)]
                    break
            if not raw_arch:
                raw_arch = env_name.split("-oe-")[0] if "-oe-" in env_name else ""
            # Map to generic arch names
            ra = raw_arch.lower()
            if ra.startswith("aarch64") or ra.startswith("cortexa53") or ra.startswith("cortexa72"):
                arch = "aarch64"
            elif ra.startswith("cortexa") or ra.startswith("armv7") or ra.startswith("arm"):
                arch = "armv7"
            elif ra.startswith("mips32el") or ra.startswith("mipsel"):
                arch = "mipsel"
            elif ra.startswith("mips"):
                arch = "mips"
            elif ra.startswith("sh4"):
                arch = "sh4"
            elif ra.startswith("x86_64") or ra.startswith("x86-64"):
                arch = "x86_64"
            elif ra.startswith("i686") or ra.startswith("i386"):
                arch = "x86"
            elif ra.startswith("powerpc"):
                arch = "ppc"
            else:
                arch = raw_arch

            # Extract CFLAGS from environment-setup
            try:
                env_text = env_setups[0].read_text(encoding="utf-8", errors="ignore")
                import re as _re
                tune_ccargs = ""
                for line in env_text.splitlines():
                    # Best source: OECORE_TUNE_CCARGS (pure arch flags)
                    m = _re.match(r'^export\s+OECORE_TUNE_CCARGS="(.+)"', line)
                    if m:
                        tune_ccargs = m.group(1).strip()
                        break
                # Combine TUNE_CCARGS with extra -m* flags from CC= (e.g. -mbranch-protection)
                cc_extra_flags = []
                for line in env_text.splitlines():
                    m = _re.match(r'^export\s+CC="(.+)"', line)
                    if m:
                        cc_parts = m.group(1).split()
                        cc_extra_flags = [p for p in cc_parts if p.startswith("-m")]
                        break
                if tune_ccargs:
                    # Merge: TUNE_CCARGS as base, add CC= flags not already present
                    base_flags = tune_ccargs.split()
                    for f in cc_extra_flags:
                        if f not in base_flags:
                            base_flags.append(f)
                    detected_cflags = " ".join(base_flags)
                else:
                    detected_cflags = " ".join(cc_extra_flags)
            except Exception:
                pass

        # Fallback: gcc -dumpmachine
        if not arch:
            try:
                dumpmachine = subprocess.check_output(
                    [str(gcc), "-dumpmachine"], text=True, stderr=subprocess.DEVNULL, timeout=5,
                ).strip()
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
                # OE-SDK: filter out the host sysroot (x86_64-*), pick the target one
                target_matches = [m for m in matches if "x86_64" not in m.name and "x86-64" not in m.name]
                sr = target_matches[0] if target_matches else matches[0]
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

        # Auto-detect dynamic linker from sysroot
        # Priority: real loader file (ld-X.Y.so or ld-linux-*.so.*) over symlinks (ld.so.*)
        if sysroot and not detected_ldflags:
            try:
                sr_path = install_dir / sysroot
                # Strip "/usr" suffix if present (sysroot may point to /usr)
                if sr_path.name == "usr":
                    sr_path = sr_path.parent
                lib_dir = sr_path / "lib"
                if lib_dir.is_dir():
                    candidates = []
                    for ld in lib_dir.iterdir():
                        if not ld.name.startswith("ld"):
                            continue
                        if ld.is_symlink():
                            continue  # skip symlinks, prefer real files
                        if ld.name.startswith(("ld-", "ld-linux")) and ".so" in ld.name:
                            candidates.append(ld.name)
                    if candidates:
                        # Prefer ld-X.Y.so (versioned glibc) over ld-linux-*.so.*
                        candidates.sort(key=lambda n: (not n.startswith("ld-linux"), n))
                        # Actually: prefer ld-X.Y.so first (matches s3 behavior)
                        versioned = [c for c in candidates if c.startswith("ld-") and not c.startswith("ld-linux")]
                        chosen = versioned[0] if versioned else candidates[0]
                        detected_ldflags = f"-Wl,--dynamic-linker=/lib/{chosen}"
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
            cflags=detected_cflags,
            ldflags=detected_ldflags,
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
        linux_h_exists = any(
            (sysroot_path / p).exists()
            for p in ("usr/include/linux/version.h", "include/linux/version.h")
        )
        if linux_h_exists:
            linux_ver = read_linux_version(sysroot_path)
            log(fmt_ok("Linux", "version.h", linux_ver or "found"))
        else:
            log(fmt_fail("Linux", "version.h not found"))

        # C-Library detection (like s3: ldd --version for native, file search for cross)
        import re as _re_libc
        libc_type = ""
        libc_ver = ""
        libc_so = None
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
                ver_match = _re_libc.search(r'(\d+\.\d+)', ldd_out)
                if ver_match:
                    libc_ver = ver_match.group(1)
            except Exception:
                pass
        else:
            # Search entire toolchain dir for libc (like s3: find . -name "libc.so.?" ...)
            install_dir = self.app.paths.toolchains_dir / tc.name
            libc_so = None
            for pattern in ["libc.so.6", "libc.so.0", "libc-*.so", "libuClibc-*.so"]:
                matches = [p for p in install_dir.rglob(pattern)
                           if p.is_file() and "x86_64" not in str(p) and ".debug" not in str(p)]
                if matches:
                    libc_so = matches[0]
                    break
            if libc_so:
                resolved = str(libc_so.resolve()) if libc_so.is_symlink() else str(libc_so)
                if "musl" in resolved:
                    libc_type = "musl"
                    # musl: strings libc.so | grep version pattern
                    try:
                        out = subprocess.check_output(
                            ["strings", str(libc_so)], text=True, stderr=subprocess.DEVNULL, timeout=10,
                        )
                        for line in out.splitlines():
                            if line.startswith("musl"):
                                libc_type = "musl"
                            ver_m = _re_libc.search(r'^([0-1]\.[0-9]\.[0-9][0-9]*)$', line)
                            if ver_m:
                                libc_ver = ver_m.group(1)
                                break
                    except Exception:
                        pass
                elif "uclibc" in resolved.lower():
                    libc_type = "uclibc"
                    # uclibc: version from filename (libuClibc-0.9.33.so)
                    try:
                        name = libc_so.resolve().name
                        ver_match = _re_libc.search(r'(\d+\.\d+\.\d+)', name)
                        if ver_match:
                            libc_ver = ver_match.group(1)
                    except Exception:
                        pass
                else:
                    libc_type = "glibc"
                    # glibc: strings libc.so | grep GLIBC_ → highest version (like s3)
                    try:
                        out = subprocess.check_output(
                            ["strings", str(libc_so)], text=True, stderr=subprocess.DEVNULL, timeout=10,
                        )
                        glibc_versions = _re_libc.findall(r'GLIBC_(\d+\.\d+(?:\.\d+)?)', out)
                        if glibc_versions:
                            libc_ver = sorted(glibc_versions, key=lambda v: [int(x) for x in v.split(".")], reverse=True)[0]
                    except Exception:
                        pass
                    # Fallback: version from resolved filename (e.g. libc-2.25.so)
                    if not libc_ver:
                        try:
                            name = libc_so.resolve().name
                            ver_match = _re_libc.search(r'(\d+\.\d+)', name)
                            if ver_match:
                                libc_ver = ver_match.group(1)
                        except Exception:
                            pass
        if libc_type:
            libc_file = libc_so.resolve().name if libc_so else libc_type
            libc_extra = f"{libc_ver}  ({libc_type})" if libc_ver else f"({libc_type})"
            log(fmt_ok("C-Library", libc_file, libc_extra))
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

        # --- Local library builds (data/toolchains/native/) ---
        if tc.native:
            local_pc_dirs = sorted(set(install_dir.glob("**/pkgconfig")))
            for local_pkg_dir in local_pc_dirs:
                try:
                    rel_pkg = local_pkg_dir.resolve().relative_to(install_dir.resolve())
                except ValueError:
                    rel_pkg = local_pkg_dir
                local_pcs = sorted(local_pkg_dir.glob("*.pc"))
                if not local_pcs:
                    continue
                log(f"\n[bold]Library config ----> {rel_pkg}  (local build)[/bold]")
                log(f"  {'=' * 40}")
                for pc_file in local_pcs:
                    try:
                        content = pc_file.read_text(encoding="utf-8", errors="replace")
                        pc_name = ""
                        pc_ver = ""
                        for line in content.splitlines():
                            if line.startswith("Name:"):
                                pc_name = line.split(":", 1)[1].strip()
                            elif line.startswith("Version:"):
                                pc_ver = line.split(":", 1)[1].strip()
                        lib_dir = pc_file.parent.parent
                        base = pc_file.stem
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

        if self.app.settings.confirm_dangerous:
            now = time.time()
            if now - self._remove_confirm_time < 3 and self._remove_confirm_name == name:
                self._remove_confirm_time = 0
                self._remove_confirm_name = ""
            else:
                self._remove_confirm_time = now
                self._remove_confirm_name = name
                self.notify(t("confirm_remove_tc", name=tc.name), severity="warning", timeout=3)
                return

        if tc.native:
            install_dir = self.app.paths.toolchains_dir / tc.name
            rmtree_force(install_dir)
            reg = self._get_registry()
            local_tcs = [t for t in reg._load_file(reg.local_path) if not t.native]
            reg.save_local(local_tcs)
            self._log_sync("[yellow]Native toolchain regenerated[/yellow]")
            self._refresh()
            return

        install_dir = self.app.paths.toolchains_dir / tc.name
        rmtree_force(install_dir)
        # Drop the entry from toolchains.local.yaml too — otherwise it
        # stays as a ghost (installed=False on next load) and its
        # cached fields (sysroot, prefix, libraries, …) keep
        # overriding the catalog defaults forever, even after we
        # actually corrected them upstream.
        reg = self._get_registry()
        local_tcs = [t for t in reg._load_file(reg.local_path) if t.name != tc.name]
        reg.save_local(local_tcs)
        # Invalidate the cached registry so subsequent actions don't
        # see the just-removed entry as installed.
        self._registry = None
        self._log_sync(f"[yellow]Removed {tc.name}[/yellow]")
        self.notify(t("tc_removed", name=tc.name), severity="warning")
        self._refresh()

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

    def action_build(self) -> None:
        """F5 Build/Cancel — only valid when a SelfBuild template is selected."""
        # Cancel a running SelfBuild
        if self._building and getattr(self, "_build_kind", "") == "selfbuild":
            self._cancel_requested = True
            self._log_sync("[yellow]Cancelling build...[/yellow]")
            return
        if not self._is_template_selected():
            return

        name = ToolchainsScreen._selected_tc
        install_dir = self.app.paths.toolchains_dir / name
        # If target toolchain dir already exists, require double-press
        if install_dir.exists():
            if ToolchainsScreen._pending_sb_overwrite == name:
                ToolchainsScreen._pending_sb_overwrite = ""
                self._handle_selfbuild()
                return
            ToolchainsScreen._pending_sb_overwrite = name
            self.notify(
                t("selfbuild_confirm_overwrite", name=name),
                severity="warning",
                timeout=5,
            )
            return

        ToolchainsScreen._pending_sb_overwrite = ""
        self._handle_selfbuild()

    def action_customize(self) -> None:
        """F6 Customize — launch the builder's interactive configurator.

        For crosstool-ng/freetz-ng: menuconfig via app.suspend().
        For oe-sdk: OE-SDK Customize modal (form-based).
        """
        if self._building:
            self.notify(t("build_in_progress"), severity="warning")
            return
        if not self._is_template_selected():
            return
        tpl = self._get_selected_template()
        if not tpl:
            self.notify(t("no_template_selected"), severity="warning")
            return

        # OE-SDK: open the form-based modal instead of menuconfig
        if tpl.builder == "oe-sdk":
            self._customize_oesdk(tpl)
            return

        # Android NDK: open the NDK customize modal
        if tpl.builder == "android-ndk":
            self._customize_android_ndk(tpl)
            return

        # crosstool-ng: menuconfig via app.suspend()
        from core.builders.crosstool_ng import CrosstoolNG

        # Determine install dir
        ctng_path = self._resolve_ctng_dir()

        # Check builder is installed
        binary_path = ctng_path / CrosstoolNG.BINARY
        if not binary_path.is_file():
            self.notify(t("customize_builder_missing", name="crosstool-ng"), severity="warning", timeout=10)
            return

        # If we'd need to fork later, make sure no local copy exists yet
        if not tpl.is_local:
            local_yaml = self.app.paths.templates_local / f"{tpl.name}.yaml"
            if local_yaml.exists():
                self.notify(t("template_local_exists", name=tpl.name), severity="error", timeout=10)
                return

        cfg_src = tpl.config_path
        if not cfg_src or not cfg_src.exists():
            self.notify(t("template_config_missing", name=tpl.name), severity="error")
            return

        import shutil as _shutil
        import hashlib as _hashlib

        def _hash_file(p: Path) -> str:
            try:
                h = _hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        h.update(chunk)
                return h.hexdigest()
            except Exception:
                return ""

        try:
            # Clean any previous .config*
            for stale in ctng_path.glob(".config*"):
                try:
                    stale.unlink()
                except Exception:
                    pass
            _shutil.copy2(cfg_src, ctng_path / ".config")
        except Exception as e:
            self.notify(t("customize_copy_failed", err=str(e)), severity="error")
            return

        working_cfg = ctng_path / ".config"
        hash_before = _hash_file(working_cfg)

        # Run the customize command via app.suspend()
        cmd = list(CrosstoolNG.CUSTOMIZE_CMD)
        def _rel(p):
            if not p:
                return ""
            try:
                return str(p.relative_to(self.app.paths.root))
            except Exception:
                return str(p)
        source_label = "local" if tpl.is_local else "default"
        self._log_sync(f"[bold]Customize: {tpl.name} via crosstool-ng — {source_label}[/bold]")
        if tpl.yaml_path:
            self._log_sync(f"  template: [dim]{_rel(tpl.yaml_path)}[/dim]")
        if cfg_src:
            self._log_sync(f"  config:   [dim]{_rel(cfg_src)}[/dim]")
        self._log_sync(f"[dim]$ {' '.join(cmd)}  (cwd={_rel(ctng_path)})[/dim]")

        import subprocess as _sp
        try:
            self.app._suspended = True
            try:
                with self.app.suspend():
                    _sp.run(cmd, cwd=str(ctng_path))
            finally:
                self.app._suspended = False
        except Exception as e:
            self.notify(t("customize_failed", err=str(e)), severity="error")
            self._log_sync(f"[red]Customize failed: {e}[/red]")
            return

        hash_after = _hash_file(working_cfg)

        # No change → user quit without saving → do nothing
        if hash_before == hash_after:
            self._log_sync(f"  [dim]{t('customize_no_changes')}[/dim]")
            self.notify(t("customize_no_changes"), severity="information", timeout=5)
            return

        # Changes detected — fork to local if we're still on a default template
        if not tpl.is_local:
            new_tpl = self._fork_template_to_local(tpl)
            if not new_tpl:
                return
            tpl = new_tpl

        # Write the updated .config back into the (now definitely local) template
        try:
            _shutil.copy2(working_cfg, tpl.config_path)
            self._log_sync(f"  [green]Saved changes to {tpl.config_path.name}[/green]")
            self.notify(t("customize_saved", name=tpl.name))
        except Exception as e:
            self._log_sync(f"[red]Could not write back .config: {e}[/red]")
            self.notify(t("customize_save_failed", err=str(e)), severity="error")

    def _fork_template_to_local(self, tpl):
        """Copy a default template (yaml + config) to data/templates/.

        Returns the new local Template object, or None on failure.
        Caller must have already verified no local copy exists.
        """
        import shutil as _shutil
        local_dir = self.app.paths.templates_local
        local_yaml = local_dir / f"{tpl.name}.yaml"
        try:
            local_dir.mkdir(parents=True, exist_ok=True)
            if tpl.config and tpl.config_path and tpl.config_path.exists():
                _shutil.copy2(tpl.config_path, local_dir / tpl.config)
            if tpl.yaml_path and tpl.yaml_path.exists():
                _shutil.copy2(tpl.yaml_path, local_yaml)
        except Exception as e:
            self.notify(t("template_fork_failed", err=str(e)), severity="error")
            return None
        self._log_sync(f"  [green]Forked {tpl.name} → data/templates/[/green]")
        ToolchainsScreen._selected_tpl_source = "local"
        try:
            self._refresh_selfbuild_tables()
        except Exception:
            pass
        from core.template import load_template
        new_tpl = load_template(local_yaml)
        if new_tpl:
            new_tpl.is_local = True
        return new_tpl

    def _customize_oesdk(self, tpl: "Template") -> None:
        """F6 for OE-SDK templates: open the form-based customize modal."""
        from core.template import load_oesdk_config, save_oesdk_config
        cfg_path = tpl.config_path
        if not cfg_path or not cfg_path.exists():
            self.notify(t("template_config_missing", name=tpl.name), severity="error")
            return
        cfg = load_oesdk_config(cfg_path)
        from ui.screens.oesdk_customize_modal import OeSdkCustomizeModal
        self.app.push_screen(
            OeSdkCustomizeModal(tpl, cfg),
            callback=lambda result: self._on_oesdk_customized(tpl, result),
        )

    def _on_oesdk_customized(self, tpl: "Template", result) -> None:
        """Callback from OeSdkCustomizeModal — save the config."""
        if result is None:
            return  # Cancelled
        from core.template import save_oesdk_config, load_template, OeSdkConfig
        import shutil as _shutil
        import yaml as _yaml

        cfg: OeSdkConfig = result
        tc_name = getattr(cfg, "_tc_name", "") or tpl.name

        # If the template is a default: fork to local with the new name
        local_dir = self.app.paths.templates_local
        local_dir.mkdir(parents=True, exist_ok=True)

        if not tpl.is_local:
            # Fork: create new local yaml + config with the chosen name
            local_yaml = local_dir / f"{tc_name}.yaml"
            local_config = local_dir / f"{tc_name}.config"
            if local_yaml.exists():
                self.notify(f"Template '{tc_name}' already exists locally", severity="error")
                return
            # Read original yaml, update name + config ref
            try:
                orig_data = _yaml.safe_load(tpl.yaml_path.read_text(encoding="utf-8"))
            except Exception as e:
                self.notify(f"Cannot read template: {e}", severity="error")
                return
            orig_data["name"] = tc_name
            orig_data["exclude_from_automation"] = False
            orig_data["config"] = f"{tc_name}.config"
            local_yaml.write_text(
                _yaml.dump(orig_data, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
            save_oesdk_config(local_config, cfg, description=f"{tc_name}")
            self._log_sync(f"[green]Created local template: {tc_name}[/green]")
        else:
            # Already local — update in place
            save_oesdk_config(tpl.config_path, cfg, description=tpl.description)
            # Update name in yaml if changed
            if tc_name != tpl.name:
                try:
                    data = _yaml.safe_load(tpl.yaml_path.read_text(encoding="utf-8"))
                    data["name"] = tc_name
                    data["config"] = f"{tc_name}.config"
                    tpl.yaml_path.write_text(
                        _yaml.dump(data, default_flow_style=False, sort_keys=False),
                        encoding="utf-8",
                    )
                    # Rename files
                    new_yaml = tpl.yaml_path.parent / f"{tc_name}.yaml"
                    new_config = tpl.yaml_path.parent / f"{tc_name}.config"
                    if tpl.yaml_path != new_yaml:
                        tpl.yaml_path.rename(new_yaml)
                    if tpl.config_path and tpl.config_path != new_config:
                        tpl.config_path.rename(new_config)
                except Exception:
                    pass
            self._log_sync(f"[green]Updated OE-SDK config for {tc_name}[/green]")

        self.notify(f"OE-SDK template '{tc_name}' saved")
        ToolchainsScreen._selected_tc = tc_name
        ToolchainsScreen._selected_tpl_source = "local"
        ToolchainsScreen._active_left_tab = "selfbuild"
        # Full screen refresh so the new local template appears selected
        # and all footer buttons (F5/F6) are correct.
        self._refresh()

    def _customize_android_ndk(self, tpl: "Template") -> None:
        """F6 for android-ndk templates: open the NDK customize modal."""
        from core.template import load_andk_config
        cfg_path = tpl.config_path
        if not cfg_path or not cfg_path.exists():
            self.notify(t("template_config_missing", name=tpl.name), severity="error")
            return
        cfg = load_andk_config(cfg_path)
        from ui.screens.android_ndk_modal import AndroidNdkCustomizeModal
        self.app.push_screen(
            AndroidNdkCustomizeModal(tpl, cfg),
            callback=lambda result: self._on_andk_customized(tpl, result),
        )

    def _on_andk_customized(self, tpl: "Template", result) -> None:
        """Callback from AndroidNdkCustomizeModal — save the config."""
        if result is None:
            return
        from core.template import save_andk_config, AndroidNdkConfig
        import yaml as _yaml

        cfg: AndroidNdkConfig = result
        tc_name = getattr(cfg, "_tc_name", "") or tpl.name

        local_dir = self.app.paths.templates_local
        local_dir.mkdir(parents=True, exist_ok=True)

        if not tpl.is_local:
            local_yaml = local_dir / f"{tc_name}.yaml"
            local_config = local_dir / f"{tc_name}.config"
            if local_yaml.exists():
                self.notify(f"Template '{tc_name}' already exists locally", severity="error")
                return
            try:
                orig_data = _yaml.safe_load(tpl.yaml_path.read_text(encoding="utf-8"))
            except Exception as e:
                self.notify(f"Cannot read template: {e}", severity="error")
                return
            orig_data["name"] = tc_name
            orig_data["exclude_from_automation"] = False
            orig_data["config"] = f"{tc_name}.config"
            local_yaml.write_text(
                _yaml.dump(orig_data, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
            save_andk_config(local_config, cfg, description=tc_name)
            self._log_sync(f"[green]Created local template: {tc_name}[/green]")
        else:
            save_andk_config(tpl.config_path, cfg, description=tpl.description)
            if tc_name != tpl.name:
                try:
                    data = _yaml.safe_load(tpl.yaml_path.read_text(encoding="utf-8"))
                    data["name"] = tc_name
                    data["config"] = f"{tc_name}.config"
                    tpl.yaml_path.write_text(
                        _yaml.dump(data, default_flow_style=False, sort_keys=False),
                        encoding="utf-8",
                    )
                    new_yaml = tpl.yaml_path.parent / f"{tc_name}.yaml"
                    new_config = tpl.yaml_path.parent / f"{tc_name}.config"
                    if tpl.yaml_path != new_yaml:
                        tpl.yaml_path.rename(new_yaml)
                    if tpl.config_path and tpl.config_path != new_config:
                        tpl.config_path.rename(new_config)
                except Exception:
                    pass
            self._log_sync(f"[green]Updated Android NDK config for {tc_name}[/green]")

        self.notify(f"Android NDK template '{tc_name}' saved")
        ToolchainsScreen._selected_tc = tc_name
        ToolchainsScreen._selected_tpl_source = "local"
        ToolchainsScreen._active_left_tab = "selfbuild"
        self._refresh()

    def action_go_back(self) -> None:
        if self._building:
            cancel_key = "F5" if getattr(self, "_build_kind", "") == "selfbuild" else "F9"
            self.notify(t("build_in_progress_cancel", fkey=cancel_key), severity="warning")
            return
        ToolchainsScreen._log_history.clear()
        ToolchainsScreen._selected_tc = ""
        self.app.pop_screen()
