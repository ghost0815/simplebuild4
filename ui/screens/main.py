"""Main screen — the primary s4 interface."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Select, Button, RichLog, Input, Collapsible, DataTable
from textual.containers import Horizontal, Vertical, VerticalScroll

from core.target import Target
from core.repo import RepoState
from translation import t
from ui.header import S4Header
from ui.footer import S4Footer
from ui.widgets.artifact_richlog import ArtifactRichLog
from ui.screens.toolchains import S4DataTable


class MainScreen(Screen):
    BINDINGS = [
        Binding("s", "tab_compact", "Summary", show=False),
        Binding("f", "tab_artifacts", show=False),
        Binding("o", "tab_log", "Output", show=False),
    ]

    def action_tab_compact(self) -> None:
        self._show_tab("compact")

    def action_tab_artifacts(self) -> None:
        self._show_tab("artifacts")

    def action_tab_log(self) -> None:
        self._show_tab("log")

    def compose(self) -> ComposeResult:
        yield S4Header()

        with Vertical(id="content"):
            # Target selector + build options
            with Horizontal(id="target-bar"):
                yield Static("  Target:", classes="label")
                targets = self._load_targets()
                yield Select(
                    options=targets if targets else [("(no targets)", None)],
                    id="target-select",
                    prompt=t("select_prompt"),
                )
                yield Input(
                    placeholder=t("ph_build_overrides"),
                    id="build-opts",
                )
                yield Button(t("edit"), id="btn-edit")
                yield Button(t("duplicate"), id="btn-dup")

            # Tabs
            with Horizontal(id="main-tab-bar"):
                yield Static(" [b]S[/b]ummary ", id="main-tab-compact", classes="tab-active")
                yield Static(" [b]F[/b]iles ", id="main-tab-artifacts", classes="tab-inactive")
                yield Static(" [b]O[/b]utput ", id="main-tab-log", classes="tab-inactive")

            # Info panel (hidden in Log tab)
            info = Static(
                "  [bold]No target selected[/bold]\n"
                "  Create a new target to get started.",
                id="info",
            )
            info.can_focus = True
            info.border_title = t("settings_title")
            yield info

            artifacts_box = VerticalScroll(id="artifacts")
            artifacts_box.can_focus = True
            artifacts_box.display = False
            with artifacts_box:
                with Collapsible(title=f"{t('artifacts_binaries')} (0)", collapsed=False, id="art-bin-coll"):
                    yield S4DataTable(id="tbl-art-bin", cursor_type="row", zebra_stripes=True)
                with Collapsible(title=f"{t('artifacts_logs')} (0)", collapsed=False, id="art-log-coll"):
                    yield S4DataTable(id="tbl-art-log", cursor_type="row", zebra_stripes=True)
                with Collapsible(title=f"{t('artifacts_libs')} (0)", collapsed=False, id="art-lib-coll"):
                    yield S4DataTable(id="tbl-art-lib", cursor_type="row", zebra_stripes=True)

            yield ArtifactRichLog(id="output", highlight=False, markup=True, wrap=True)

        yield S4Footer(active_screen="main")

    def on_mount(self) -> None:
        self.query_one("#output", RichLog).border_title = t("output_title")
        self.query_one("#artifacts").border_title = t("artifacts_border")
        # Restore last selected target
        last = self.app.settings.last_target
        if last:
            from pathlib import Path
            p = Path(last)
            if p.exists():
                try:
                    sel = self.query_one("#target-select", Select)
                    sel.value = str(p)
                except Exception:
                    pass

    def _log(self, msg: str) -> None:
        """Append a line to the output panel (RichLog with markup)."""
        self.query_one("#output", RichLog).write(msg)

    def _show_tab(self, tab: str) -> None:
        """Switch between Summary / Artifacts / Log tabs."""
        self.query_one("#info", Static).display = (tab == "compact")
        self.query_one("#artifacts").display = (tab == "artifacts")
        self.query_one("#output").display = (tab != "artifacts")
        self.query_one("#main-tab-compact").set_classes("tab-active" if tab == "compact" else "tab-inactive")
        self.query_one("#main-tab-artifacts").set_classes("tab-active" if tab == "artifacts" else "tab-inactive")
        self.query_one("#main-tab-log").set_classes("tab-active" if tab == "log" else "tab-inactive")
        if tab == "artifacts":
            self._refresh_artifacts()
        fid = {"compact": "#info", "artifacts": "#artifacts", "log": "#output"}[tab]
        try:
            self.query_one(fid).focus()
        except Exception:
            pass

    def on_click(self, event) -> None:
        """Tab-bar clicks + single-click download on artifact tables."""
        widget = event.widget
        if isinstance(widget, DataTable) and widget.id and widget.id.startswith("tbl-art-"):
            header_h = getattr(widget, "header_height", 1)
            if event.y < header_h:
                return
            row = int(widget.scroll_offset.y) + event.y - header_h
            if row < 0 or row >= widget.row_count:
                return
            try:
                from textual.coordinate import Coordinate
                key = widget.coordinate_to_cell_key(Coordinate(row, 0)).row_key
            except Exception:
                return
            if key and key.value:
                self.app.action_deliver(key.value)
            return
        if widget.id == "main-tab-compact":
            self._show_tab("compact")
        elif widget.id == "main-tab-artifacts":
            self._show_tab("artifacts")
        elif widget.id == "main-tab-log":
            self._show_tab("log")

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        tbl = event.data_table
        if isinstance(tbl, S4DataTable):
            tbl.sort_by_column(event.column_index)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        tbl = event.data_table
        if isinstance(tbl, S4DataTable) and tbl.id and tbl.id.startswith("tbl-art-"):
            tbl.mark_row(event.cursor_row)

    def _load_targets(self) -> list[tuple[str, str]]:
        """Load available targets from disk."""
        paths = self.app.paths
        targets = Target.list_targets(paths.targets_dir)
        return [(name, str(path)) for name, path in targets]

    @staticmethod
    def _wrap_values(label: str, values: list[str], width: int = 120) -> str:
        """Format a label + values line, wrapping values to align under the first value."""
        if not values:
            return f"{label}none"
        indent = " " * len(label)
        lines = []
        current = label
        for v in values:
            test = f"{current} {v}" if current != label else f"{current}{v}"
            if len(test) > width and current != label:
                lines.append(current)
                current = f"{indent}{v}"
            else:
                current = test
        lines.append(current)
        return "\n".join(lines)

    def _update_info(self, target: Target) -> None:
        """Update the info panel with target details."""
        b = target.build

        # Options: modules + protocols + readers + card_readers
        all_opts = b.modules + b.protocols + b.readers + b.card_readers

        # Categorize flags from dicts. Single source of truth for
        # what counts as a hardware-descrambler API: core.oscam_config.
        from core.oscam_config import HARDWARE_USE_FLAGS as _hw_names
        lib_flags = [f"USE_{f}" for f, v in sorted(b.use_flags.items()) if v and f not in _hw_names]
        hw_flags = [f"USE_{f}" for f, v in sorted(b.use_flags.items()) if v and f in _hw_names]
        static_flags = [f"STATIC_{f}" for f, v in sorted(b.static_flags.items()) if v]
        static_str = " ".join(static_flags) if static_flags else ""

        # Check if source repo still exists
        from core.repos_config import load_repos
        repos = load_repos(self.app.paths.repos_config)
        repo_names = [r.name for r in repos]
        source_parts = []
        for s in target.sources:
            name = s.name if hasattr(s, 'name') else s
            update = getattr(s, 'update_before_build', False)
            patch = getattr(s, 'patch_before_build', False)
            if update and patch:
                suffix = "(UP)"
            elif update:
                suffix = "(U)"
            elif patch:
                suffix = "(P)"
            else:
                suffix = ""
            if name in repo_names:
                source_parts.append(f"{name}{suffix}")
            else:
                source_parts.append(f"[red]{name} (not found!)[/red]")
        n_repos = len(source_parts)
        repo_str = ", ".join(source_parts) or "none"
        # Toolchains: mark custom-configured ones with *
        tc_parts = []
        for tc in target.toolchains:
            name = tc.name if hasattr(tc, 'name') else tc
            has_custom = hasattr(tc, 'cc_opts') and any([tc.cc_opts, tc.cflags, tc.ldflags, tc.make_vars, tc.stapi_lib, tc.use_compress, tc.oscam_path, tc.confdir])
            tc_parts.append(f"[bold]{name}[/bold]" if has_custom else name)
        n_tcs = len(tc_parts)
        tc_str = ", ".join(tc_parts) or "none"

        # Build info text
        pad = 22  # label width for alignment
        # Build count: sources × toolchains
        build_count = n_repos * n_tcs

        # First line: name — description (builds)
        active_deps = [d for d in target.deployment if d.enabled and d.host]
        dep_count = len(active_deps)
        header = f"  [bold]{target.name}"
        if target.description:
            header += f" — {target.description}"
        if build_count:
            _bs = (b.build_system or "make").strip().lower()
            _word = t('n_builds') if build_count != 1 else t('n_build')
            header += f" ({build_count} {_bs} {_word}, {dep_count} {t('n_deployments') if dep_count != 1 else t('n_deployment')})"
        header += "[/bold]"

        def _lbl(key, count):
            return f"{t(key)} ({count}):"

        lines = [
            header,
            f"  {_lbl('repositories', n_repos):<{pad}} {repo_str}",
            f"  {_lbl('toolchains', n_tcs):<{pad}} {tc_str}",
        ]
        # Options per category
        if b.modules:
            lines.append(self._wrap_values(f"  {_lbl('addons', len(b.modules)):<{pad}} ", b.modules))
        if b.protocols:
            lines.append(self._wrap_values(f"  {_lbl('protocols', len(b.protocols)):<{pad}} ", b.protocols))
        if b.readers:
            lines.append(self._wrap_values(f"  {_lbl('readers', len(b.readers)):<{pad}} ", b.readers))
        if b.card_readers:
            lines.append(self._wrap_values(f"  {_lbl('card_readers', len(b.card_readers)):<{pad}} ", b.card_readers))
        if lib_flags:
            lines.append(
                self._wrap_values(f"  {_lbl('libraries', len(lib_flags)):<{pad}} ", lib_flags)
                + (f"  [dim]Static: {static_str}[/dim]" if static_str else "")
            )
        else:
            lines.append(f"  {_lbl('libraries', 0):<{pad}} no integrated libraries")
        if hw_flags:
            lines.append(self._wrap_values(f"  {_lbl('hardware', len(hw_flags)):<{pad}} ", hw_flags))
        # Collect per-TC overrides for compress/confdir/oscam_path
        _tc_compress_overrides = [tc.name for tc in target.toolchains if hasattr(tc, 'use_compress') and any([tc.use_compress, tc.upx_binary, tc.compress_level])]
        _tc_confdir_overrides = [tc.name for tc in target.toolchains if hasattr(tc, 'confdir') and tc.confdir]
        _tc_oscam_overrides = [tc.name for tc in target.toolchains if hasattr(tc, 'oscam_path') and tc.oscam_path]

        def _override_hint(tc_list):
            if tc_list:
                return f"  [dim]({t('overridden_by')} {', '.join(tc_list)})[/dim]"
            return ""

        if b.use_compress:
            import shutil as _shutil
            import subprocess as _sp
            upx_path = b.upx_binary or _shutil.which("upx") or ""
            upx_info = upx_path or t("none")
            if upx_path:
                try:
                    upx_ver = _sp.check_output(
                        [upx_path, "--version"], text=True, stderr=_sp.DEVNULL, timeout=5,
                    ).splitlines()[0]
                    upx_info = f"{upx_path} — {upx_ver}"
                except Exception:
                    pass
            lines.append(f"  {t('compress') + ':':<{pad}} USE_COMPRESS ({b.compress_level})  [dim]{upx_info}[/dim]{_override_hint(_tc_compress_overrides)}")
        confdir_str = b.confdir.strip() if b.confdir else ""
        lines.append(f"  {t('config_dir') + ':':<{pad}} {confdir_str or '-'}{_override_hint(_tc_confdir_overrides)}")
        oscamname_str = b.use_oscamname.strip() if b.use_oscamname else ""
        lines.append(f"  {t('oscam_binary_path') + ':':<{pad}} {oscamname_str or 'data/binaries/'}{_override_hint(_tc_oscam_overrides)}")
        # Deployment destinations (sorted by name)
        if active_deps:
            names = ", ".join(sorted(d.name or d.host for d in active_deps))
            lines.append(f"  {_lbl('upload', len(active_deps)):<{pad}} {names}")

        info = self.query_one("#info", Static)
        info.update("\n".join(lines))

    def on_screen_resume(self) -> None:
        """Refresh target list and translations when returning from another screen."""
        # Update translated widgets (language may have changed in settings)
        try:
            self.query_one("#info", Static).border_title = t("settings_title")
            self.query_one("#output", RichLog).border_title = t("output_title")
            self.query_one("#btn-edit", Button).label = t("edit")
            self.query_one("#btn-dup", Button).label = t("duplicate")
            # Rebuild footer with new language
            from ui.footer import S4Footer
            old_footer = self.query_one(S4Footer)
            new_footer = S4Footer(active_screen="main")
            old_footer.remove()
            self.mount(new_footer)
        except Exception:
            pass

        sel = self.query_one("#target-select", Select)
        # Prefer last saved target (from target editor), then previous selection
        select_path = getattr(self.app, "_last_saved_target", None)
        if not select_path:
            select_path = sel.value if isinstance(sel.value, str) else None
        self.app._last_saved_target = None
        self._refresh_targets()
        if select_path:
            try:
                sel.value = select_path
            except Exception:
                pass
        # After re-scanning all targets, surface any *current* decrypt
        # failures. If everything decrypted fine (e.g. the user just
        # fixed a password and returned from target_edit), pop returns
        # an empty list and we show nothing — replacing any stale toast
        # that was still hanging around.
        if getattr(self.app, '_decrypt_check_done', False):
            from core.credentials import pop_decrypt_warnings
            _dw = pop_decrypt_warnings()
            if _dw:
                detail = "\n".join(f"  \u2022 {w}" for w in _dw)
                self.notify(
                    f"{t('decrypt_failed_warning')}\n{detail}",
                    severity="warning", timeout=10,
                )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "target-select" and event.value is not None and isinstance(event.value, str):
            from pathlib import Path
            target = Target.load(Path(event.value))
            self._update_info(target)
            try:
                if self.query_one("#artifacts").display:
                    self._refresh_artifacts()
            except Exception:
                pass
            # Warn if target has undecryptable passwords (skip during startup,
            # the app timer handles that)
            if getattr(self.app, '_decrypt_check_done', False):
                from core.credentials import pop_decrypt_warnings
                _dw = pop_decrypt_warnings()
                if _dw:
                    detail = "\n".join(f"  \u2022 {w}" for w in _dw)
                    self.notify(f"{t('decrypt_failed_warning')}\n{detail}", severity="warning", timeout=10)
            # Remember last target
            self.app.settings.last_target = event.value
            self.app.settings.save(self.app.settings_path)

    @staticmethod
    def _fmt_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    def _refresh_artifacts(self) -> None:
        import os
        from datetime import datetime
        from core.artifacts import list_target_artifacts

        is_web = os.environ.get("S4_WEB_MODE") == "1"
        result = self._get_selected_target()

        sections = [
            ("binary", t("artifacts_binaries"), "#tbl-art-bin", "#art-bin-coll"),
            ("log",    t("artifacts_logs"),     "#tbl-art-log", "#art-log-coll"),
            ("lib",    t("artifacts_libs"),     "#tbl-art-lib", "#art-lib-coll"),
        ]
        cols_web = ("📥", t("artifacts_col_path"), t("artifacts_col_size"), t("artifacts_col_modified"))
        cols_native = (t("artifacts_col_path"), t("artifacts_col_size"), t("artifacts_col_modified"))

        for _, _, tbl_id, _ in sections:
            tbl = self.query_one(tbl_id, DataTable)
            tbl.clear(columns=True)
            tbl.add_columns(*(cols_web if is_web else cols_native))

        if not result:
            for _, title, _, coll_id in sections:
                self.query_one(coll_id, Collapsible).title = f"{title} (0)"
            return
        target, _ = result

        items = list_target_artifacts(
            target,
            self.app.paths.binaries_dir,
            self.app.paths.logs_dir,
        )
        by_kind = {"binary": [], "log": [], "lib": []}
        for a in items:
            by_kind[a.kind].append(a)

        for kind, title, tbl_id, coll_id in sections:
            group = by_kind[kind]
            self.query_one(coll_id, Collapsible).title = f"{title} ({len(group)})"
            if not group:
                continue
            tbl = self.query_one(tbl_id, DataTable)
            for a in group:
                mtime = datetime.fromtimestamp(a.mtime).strftime("%Y-%m-%d %H:%M")
                size = self._fmt_size(a.size)
                if is_web:
                    tbl.add_row("📥", str(a.path), size, mtime, key=str(a.path))
                else:
                    tbl.add_row(str(a.path), size, mtime, key=str(a.path))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if not str(event.data_table.id or "").startswith("tbl-art-"):
            return
        key = event.row_key.value if event.row_key else None
        if key:
            self.app.action_deliver(key)

    def _get_selected_target(self) -> tuple[Target, "Path"] | None:
        """Get currently selected target and its path."""
        from pathlib import Path as P
        sel = self.query_one("#target-select", Select)
        if sel.value is None or not isinstance(sel.value, str):
            return None
        path = P(sel.value)
        return Target.load(path), path

    def _refresh_targets(self) -> None:
        """Reload the target dropdown after changes."""
        # A full re-scan re-runs decrypt() for every target, which would
        # otherwise *accumulate* warnings on top of whatever was already
        # in the transient list. Reset it first so the post-scan state
        # reflects reality (and a just-saved target that now decrypts
        # cleanly actually removes its warning instead of piling up).
        from core.credentials import clear_transient_warnings
        clear_transient_warnings()
        sel = self.query_one("#target-select", Select)
        targets = self._load_targets()
        sel.set_options(targets if targets else [("(no targets)", None)])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        from ui.screens.target_edit import TargetEditScreen
        btn = event.button.id

        if btn == "btn-edit":
            result = self._get_selected_target()
            if result:
                target, path = result
                self.app.push_screen(TargetEditScreen(target=target, save_path=path))
            else:
                self.notify(t("no_target_selected"), severity="warning")
        elif btn == "btn-dup":
            result = self._get_selected_target()
            if result:
                target, path = result
                target.name = f"{target.name}_copy"
                target.description = f"Copy of {target.description}"
                self.app.push_screen(TargetEditScreen(target=target, options_seeded=True))
            else:
                self.notify(t("no_target_selected"), severity="warning")

    def do_add(self) -> None:
        """F10 Add — create new target."""
        from ui.screens.target_edit import TargetEditScreen
        self.app.push_screen(TargetEditScreen())

    _remove_confirm_time: float = 0

    def do_remove(self) -> None:
        """F11 Remove — delete selected target (double-press to confirm)."""
        import time
        result = self._get_selected_target()
        if not result:
            self.notify(t("no_target_selected"), severity="warning")
            return
        target, path = result
        if self.app.settings.confirm_dangerous:
            now = time.time()
            if now - self._remove_confirm_time < 3:
                self._remove_confirm_time = 0
            else:
                self._remove_confirm_time = now
                self.notify(t("confirm_remove_target", name=target.name), severity="warning", timeout=3)
                return
        path.unlink(missing_ok=True)
        self._refresh_targets()
        self.query_one("#info", Static).update(
            "  [bold]No target selected[/bold]\n"
            "  Create a new target to get started."
        )
        self.notify(t("target_deleted", name=target.name), severity="warning")

    _building = False
    _cancel_requested = False

    def action_build(self) -> None:
        """F5 Build / Cancel — run or cancel the build pipeline."""
        if self._building:
            self._cancel_requested = True
            self._log("[yellow]Cancelling build...[/yellow]")
            return

        import threading
        result = self._get_selected_target()
        if not result:
            self.notify(t("no_target_selected"), severity="warning")
            return
        target, path = result

        # Parse build overrides from input
        build_overrides = self.query_one("#build-opts", Input).value.strip()

        # Clear output
        self.query_one("#output", RichLog).clear()
        self._building = True
        self._cancel_requested = False
        self.app.build_timer_start("main")
        self.call_later(self._update_build_button)

        def log(msg: str):
            try:
                self.app.call_from_thread(self._log, msg)
            except Exception:
                pass

        def cancel_check() -> bool:
            return self._cancel_requested

        def do_build():
            from core.builder import BuildEngine
            try:
                engine = BuildEngine(
                    target=target,
                    repos_dir=self.app.paths.repos_dir,
                    backup_dir=self.app.paths.backup_dir,
                    repos_config=self.app.paths.repos_config,
                    patches_dir=self.app.paths.patches_dir,
                    logs_dir=self.app.paths.logs_dir,
                    binaries_dir=self.app.paths.binaries_dir,
                    bin_dir=self.app.paths.bin_dir,
                    toolchains_dir=self.app.paths.toolchains_dir,
                    toolchains_local=self.app.paths.toolchains_local,
                    toolchains_defaults=self.app.paths.toolchains_defaults,
                    max_cpus=self.app.settings.max_cpus,
                    compiler_opt=self.app.settings.compiler_opt,
                    sign_cert=self.app.settings.sign_cert,
                    sign_key=self.app.settings.sign_key,
                    delete_debug=self.app.settings.delete_debug,
                    save_smargo=self.app.settings.save_smargo,
                    patch_webif=self.app.settings.patch_webif,
                    qemu_test=self.app.settings.qemu_test,
                    qemu_timeout=self.app.settings.qemu_timeout,
                    qemu_expect=self.app.settings.qemu_expect,
                    qemu_run_unit_tests=self.app.settings.qemu_run_unit_tests,
                    build_overrides=build_overrides,
                    log_max_age_days=self.app.settings.log_max_age_days,
                    log_max_count=self.app.settings.log_max_count,
                    log_max_size_mb=self.app.settings.log_max_size_mb,
                    organize_logs=self.app.settings.organize_logs,
                    default_confdir=self.app.settings.default_confdir,
                    default_build_system=self.app.settings.default_build_system,
                    log=log,
                    cancel_check=cancel_check,
                )
                engine.build()
            finally:
                self.app.call_from_thread(self.app.build_timer_stop)
                elapsed = self.app.build_timer_format()
                if elapsed:
                    log(f"[bold]Elapsed:[/bold] {elapsed}")
                self._building = False
                self._cancel_requested = False
                self.app.call_from_thread(self._update_build_button)

        thread = threading.Thread(target=do_build, daemon=True)
        thread.start()

    def _update_build_button(self) -> None:
        """Update F5 footer label and highlight: Build or Cancel."""
        try:
            from ui.footer import FooterItem
            found = False
            for item in self.query(FooterItem):
                if item._key == "F5":
                    item._label = t("cancel") if self._building else t("build")
                    item._active = self._building
                    item.refresh(layout=True)
                    break
        except Exception:
            pass

