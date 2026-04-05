"""Main screen — the primary s4 interface."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Select, Button, RichLog, Input
from textual.containers import Horizontal, Vertical

from core.target import Target
from core.repo import RepoState
from ui.header import s4Header
from ui.footer import s4Footer


class MainScreen(Screen):
    BINDINGS = [
        Binding("1", "tab_compact", "Compact", show=False),
        Binding("2", "tab_log", "Log", show=False),
    ]

    def action_tab_compact(self) -> None:
        self._show_tab("compact")

    def action_tab_log(self) -> None:
        self._show_tab("log")

    def compose(self) -> ComposeResult:
        yield s4Header()

        with Vertical(id="content"):
            # Target selector + build options
            with Horizontal(id="target-bar"):
                yield Static("  Target:", classes="label")
                targets = self._load_targets()
                yield Select(
                    options=targets if targets else [("(no targets)", None)],
                    id="target-select",
                    prompt="select target",
                )
                yield Input(
                    placeholder="build overrides, e.g. USE_SSL USE_PCSC_off",
                    id="build-opts",
                )
                yield Button("Edit", id="btn-edit")
                yield Button("Duplicate", id="btn-dup")

            # Tabs
            with Horizontal(id="main-tab-bar"):
                yield Static(" Compact ", id="main-tab-compact", classes="tab-active")
                yield Static(" Log ", id="main-tab-log", classes="tab-inactive")

            # Info panel (hidden in Log tab)
            info = Static(
                "  [bold]No target selected[/bold]\n"
                "  Create a new target to get started.",
                id="info",
            )
            info.border_title = "Settings"
            yield info

            # Output log (always visible, grows to fill space)
            yield RichLog(id="output", highlight=False, markup=True, wrap=True)

        yield s4Footer(active_screen="main")

    def on_mount(self) -> None:
        self.query_one("#output", RichLog).border_title = "Output"
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

        # Library flags
        lib_flags = []
        if b.use_libcrypto: lib_flags.append("USE_LIBCRYPTO")
        if b.use_ssl: lib_flags.append("USE_SSL")
        if b.use_libusb: lib_flags.append("USE_LIBUSB")
        if b.use_pcsc: lib_flags.append("USE_PCSC")
        if b.use_libdvbcsa: lib_flags.append("USE_LIBDVBCSA")
        lib_str = " ".join(lib_flags) if lib_flags else "none"

        # Static linking
        static_flags = []
        if b.static_libcrypto: static_flags.append("STATIC_LIBCRYPTO")
        if b.static_ssl: static_flags.append("STATIC_SSL")
        if b.static_libusb: static_flags.append("STATIC_LIBUSB")
        if b.static_pcsc: static_flags.append("STATIC_PCSC")
        if b.static_libdvbcsa: static_flags.append("STATIC_LIBDVBCSA")
        static_str = " ".join(static_flags) if static_flags else ""

        # Hardware flags
        hw_flags = []
        if b.use_stapi: hw_flags.append("USE_STAPI")
        if b.use_stapi5: hw_flags.append("USE_STAPI5")
        if b.use_coolapi: hw_flags.append("USE_COOLAPI")
        if b.use_coolapi2: hw_flags.append("USE_COOLAPI2")
        if b.use_su980: hw_flags.append("USE_SU980")
        if b.use_azbox: hw_flags.append("USE_AZBOX")
        if b.use_amsmc: hw_flags.append("USE_AMSMC")
        if b.use_mca: hw_flags.append("USE_MCA")
        hw_str = " ".join(hw_flags) if hw_flags else "none"

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
        lines = [
            f"  [bold]{target.name}[/bold] — {target.description}",
            f"  Repositories: {repo_str}",
            f"  Toolchains:   {tc_str}",
            self._wrap_values("  Options:      ", all_opts),
            self._wrap_values("  Libraries:    ", lib_flags) + (f"  [dim]Static: {static_str}[/dim]" if static_str else ""),
            self._wrap_values("  Hardware:     ", hw_flags),
        ]
        if b.use_compress:
            lines.append(f"  Compress:     USE_COMPRESS ({b.compress_level})")
        lines.append(f"  Upload:       {upload_str}")

        info = self.query_one("#info", Static)
        info.update("\n".join(lines))

    def on_screen_resume(self) -> None:
        """Refresh target list when returning from another screen."""
        sel = self.query_one("#target-select", Select)
        previous = sel.value if isinstance(sel.value, str) else None
        self._refresh_targets()
        if previous:
            try:
                sel.value = previous
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
                self.notify("No target selected", severity="warning")
        elif btn == "btn-dup":
            result = self._get_selected_target()
            if result:
                target, path = result
                target.name = f"{target.name}_copy"
                target.description = f"Copy of {target.description}"
                self.app.push_screen(TargetEditScreen(target=target))
            else:
                self.notify("No target selected", severity="warning")

    def do_add(self) -> None:
        """F10 Add — create new target."""
        from ui.screens.target_edit import TargetEditScreen
        self.app.push_screen(TargetEditScreen())

    def do_remove(self) -> None:
        """F11 Remove — delete selected target."""
        result = self._get_selected_target()
        if result:
            target, path = result
            path.unlink(missing_ok=True)
            self._refresh_targets()
            self.query_one("#info", Static).update(
                "  [bold]No target selected[/bold]\n"
                "  Create a new target to get started."
            )
            self.notify(f"Target '{target.name}' deleted")
        else:
            self.notify("No target selected", severity="warning")

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
            self.notify("No target selected", severity="warning")
            return
        target, path = result

        # Parse build overrides from input
        build_overrides = self.query_one("#build-opts", Input).value.strip()

        # Clear output
        self.query_one("#output", RichLog).clear()
        self._building = True
        self._cancel_requested = False
        self._update_build_button()

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
            for item in self.app.query("FooterItem"):
                if hasattr(item, '_key') and item._key == "F5":
                    item._label = "Cancel" if self._building else "Build"
                    item._active = self._building
                    item.refresh()
                    break
        except Exception:
            pass
