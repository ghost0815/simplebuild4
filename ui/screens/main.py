"""Main screen — the primary s4 interface."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Select, Button, RichLog, Input
from textual.containers import Horizontal, Vertical

from core.target import Target
from core.repo import RepoState
from translation import t
from ui.header import S4Header
from ui.footer import S4Footer


class MainScreen(Screen):
    BINDINGS = [
        Binding("c", "tab_compact", "Compact", show=False),
        Binding("o", "tab_log", "Output", show=False),
    ]

    def action_tab_compact(self) -> None:
        self._show_tab("compact")

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
                yield Static(" [b]C[/b]ompact ", id="main-tab-compact", classes="tab-active")
                yield Static(" [b]O[/b]utput ", id="main-tab-log", classes="tab-inactive")

            # Info panel (hidden in Log tab)
            info = Static(
                "  [bold]No target selected[/bold]\n"
                "  Create a new target to get started.",
                id="info",
            )
            info.border_title = t("settings_title")
            yield info

            # Output log (always visible, grows to fill space)
            yield RichLog(id="output", highlight=False, markup=True, wrap=True)

        yield S4Footer(active_screen="main")

    def on_mount(self) -> None:
        self.query_one("#output", RichLog).border_title = t("output_title")
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
        """Switch between Compact and Log tabs."""
        # Compact: show info panel; Log: hide info panel (output fills the space)
        self.query_one("#info", Static).display = (tab == "compact")
        self.query_one("#main-tab-compact").set_classes("tab-active" if tab == "compact" else "tab-inactive")
        self.query_one("#main-tab-log").set_classes("tab-active" if tab == "log" else "tab-inactive")

    def on_click(self, event) -> None:
        """Handle tab clicks."""
        widget = event.widget
        if widget.id == "main-tab-compact":
            self._show_tab("compact")
        elif widget.id == "main-tab-log":
            self._show_tab("log")

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
        upload_str = ", ".join(f"{u.user}@{u.host}" for u in target.upload) or "none"

        # Options: modules + protocols + readers + card_readers
        all_opts = b.modules + b.protocols + b.readers + b.card_readers

        # Categorize flags from dicts
        _hw_names = {"STAPI", "STAPI5", "COOLAPI", "COOLAPI2", "SU980", "AZBOX", "AMSMC", "MCA"}
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
            if name in repo_names:
                source_parts.append(name)
            else:
                source_parts.append(f"[red]{name} (not found!)[/red]")
        repo_str = ", ".join(source_parts) or "none"
        tc_str = ", ".join(tc.name if hasattr(tc, 'name') else tc for tc in target.toolchains) or "none"

        # Build info text
        pad = 16  # label width for alignment
        lines = [
            f"  [bold]{target.name}[/bold] — {target.description}",
            f"  {t('repositories') + ':':<{pad}} {repo_str}",
            f"  {t('toolchains') + ':':<{pad}} {tc_str}",
        ]
        # Options per category
        if b.modules:
            lines.append(self._wrap_values(f"  {t('addons') + ':':<{pad}} ", b.modules))
        if b.protocols:
            lines.append(self._wrap_values(f"  {t('protocols') + ':':<{pad}} ", b.protocols))
        if b.readers:
            lines.append(self._wrap_values(f"  {t('readers') + ':':<{pad}} ", b.readers))
        if b.card_readers:
            lines.append(self._wrap_values(f"  {t('card_readers') + ':':<{pad}} ", b.card_readers))
        lines.extend([
            self._wrap_values(f"  {t('libraries') + ':':<{pad}} ", lib_flags) + (f"  [dim]Static: {static_str}[/dim]" if static_str else ""),
            self._wrap_values(f"  {t('hardware') + ':':<{pad}} ", hw_flags),
        ])
        if b.use_compress:
            import shutil as _shutil
            upx_path = b.upx_binary or _shutil.which("upx") or t("none")
            lines.append(f"  {t('compress') + ':':<{pad}} USE_COMPRESS ({b.compress_level})  [dim]{upx_path}[/dim]")
        lines.append(f"  {t('upload') + ':':<{pad}} {upload_str}")

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

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "target-select" and event.value is not None and isinstance(event.value, str):
            from pathlib import Path
            target = Target.load(Path(event.value))
            self._update_info(target)
            # Remember last target
            self.app.settings.last_target = event.value
            self.app.settings.save(self.app.settings_path)

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
                self.app.push_screen(TargetEditScreen(target=target))
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
        now = time.time()
        if now - self._remove_confirm_time < 3:
            path.unlink(missing_ok=True)
            self._refresh_targets()
            self.query_one("#info", Static).update(
                "  [bold]No target selected[/bold]\n"
                "  Create a new target to get started."
            )
            self.notify(t("target_deleted", name=target.name))
            self._remove_confirm_time = 0
            return
        self._remove_confirm_time = now
        self.notify(t("confirm_remove_target", name=target.name), severity="warning", timeout=3)

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
                    build_overrides=build_overrides,
                    log=log,
                    cancel_check=cancel_check,
                )
                engine.build()
            finally:
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
