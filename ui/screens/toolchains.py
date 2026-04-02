"""Toolchains screen — browse, install, remove toolchains."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import re
import tarfile
import threading
from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Input, Button, Collapsible, DataTable, Select
from textual.containers import Horizontal, Vertical, VerticalScroll

from core.toolchain import Toolchain, ToolchainRegistry, scan_libraries, load_library_recipes, check_library_updates, KNOWN_LIBS
from ui.header import S4Header
from ui.footer import S4Footer

CATEGORIES = [
    ("common", "Common"),
    ("stb", "STB"),
    ("android", "Android"),
    ("openwrt", "OpenWrt"),
    ("router", "Router"),
    ("nas", "NAS"),
    ("minipc", "Mini PC"),
    ("tv", "TV"),
    ("legacy", "Legacy"),
    ("custom", "Custom"),
]


class ToolchainsScreen(Screen):
    BINDINGS = []

    _log_history: list[str] = []
    _selected_tc: str = ""

    def __init__(self) -> None:
        super().__init__()
        self._registry: ToolchainRegistry | None = None
        self._mounted = False

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
            with VerticalScroll(id="tc-browser"):
                # Installed (virtual category, always expanded)
                installed = reg.installed_toolchains()
                with Collapsible(title=f"Installed ({len(installed)})", collapsed=False, id="cat-installed"):
                    table = DataTable(id="tbl-installed", cursor_type="row", zebra_stripes=True)
                    yield table

                # Registry categories
                for cat_key, cat_label in CATEGORIES:
                    tcs = [tc for tc in reg.toolchains if tc.category == cat_key]
                    if not tcs:
                        continue
                    with Collapsible(title=f"{cat_label} ({len(tcs)})", collapsed=True, id=f"cat-{cat_key}"):
                        table = DataTable(id=f"tbl-{cat_key}", cursor_type="row", zebra_stripes=True)
                        yield table

            # Right: Details + Output
            with Vertical(id="tc-right"):
                # Actions
                with Horizontal(id="tc-actions"):
                    install_btn = Button("Install", id="btn-install", variant="primary")
                    install_btn.tooltip = "Download and install the selected toolchain"
                    yield install_btn
                    check_btn = Button("Check", id="btn-check")
                    check_btn.tooltip = "Verify the selected toolchain is working"
                    yield check_btn
                    remove_btn = Button("Remove", id="btn-remove", variant="error")
                    remove_btn.tooltip = "Delete the selected toolchain from disk"
                    yield remove_btn

                # Detail/Libraries tab buttons
                with Horizontal(id="tc-tab-bar"):
                    yield Static(" Details ", id="btn-tab-details", classes="tab-active")
                    yield Static(" Libraries ", id="btn-tab-libs", classes="tab-inactive")

                # Details panel
                detail = Static("", id="tc-detail")
                detail.auto_links = False
                yield detail

                # Libraries panel (hidden by default)
                with VerticalScroll(id="tc-libs"):
                    yield Static("", id="tc-libs-header")
                    # Pre-create rows for all known libraries
                    for lib_name, _, _ in KNOWN_LIBS:
                        safe_id = lib_name.replace(".", "-").replace(" ", "-")
                        with Horizontal(classes="lib-action-row", id=f"lib-row-{safe_id}"):
                            yield Static(f"  {lib_name:<15s} {'—':<12s}", id=f"lib-status-{safe_id}", classes="lib-status")
                            yield Select(
                                options=[("skip", "skip")],
                                value="skip",
                                id=f"lib-action-{safe_id}",
                                classes="lib-select",
                            )

                # Output
                with VerticalScroll(id="tc-output"):
                    yield Static("", id="tc-output-text")

        yield S4Footer(active_screen="toolchains")

    def on_mount(self) -> None:
        reg = self._get_registry()

        # Populate installed table
        tbl = self.query_one("#tbl-installed", DataTable)
        tbl.add_columns("Name", "Arch", "Description")
        for tc in reg.installed_toolchains():
            tbl.add_row(tc.name, tc.arch, tc.description, key=tc.name)

        # Populate category tables
        for cat_key, _ in CATEGORIES:
            tcs = [tc for tc in reg.toolchains if tc.category == cat_key]
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

        # Border titles
        self.query_one("#tc-detail", Static).border_title = "Details"
        self.query_one("#tc-output", VerticalScroll).border_title = "Output"

        # Restore log
        if ToolchainsScreen._log_history:
            self._update_output()

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
        # Delay enabling event handling — tables fire RowHighlighted during render
        self.set_timer(0.5, self._enable_events)

    def _enable_events(self) -> None:
        self._mounted = True

    def on_click(self, event) -> None:
        """Handle clicks on tab labels."""
        widget = event.widget if hasattr(event, 'widget') else None
        if widget:
            if widget.id == "btn-tab-details":
                self._show_tab("details")
            elif widget.id == "btn-tab-libs":
                self._show_tab("libs")

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
        if not self._mounted:
            return
        if event.row_key and event.row_key.value:
            ToolchainsScreen._selected_tc = event.row_key.value
            self._update_active_tab()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
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
                header.update(f"  [bold]{tc.name}[/bold]\n  [dim]Not installed — install first to scan libraries[/dim]")
                return

            install_dir = self.app.paths.toolchains_dir / tc.name
            libs = scan_libraries(install_dir, tc.sysroot)

            recipes = load_library_recipes(self.app.paths.defaults_dir / "libraries.yaml")
            libs = check_library_updates(libs, recipes, has_c11=True)

            recipe_map = {r["name"]: r for r in recipes}

            header.update(f"  [bold]{tc.name}[/bold] — Libraries")

            for lib in libs:
                safe_id = lib.name.replace(".", "-").replace(" ", "-")
                installed = lib.version if lib.found else "—"

                # Update status label
                try:
                    status_widget = self.query_one(f"#lib-status-{safe_id}", Static)
                    if lib.found and lib.up_to_date:
                        status_widget.update(f"  [green]{lib.name:<15s}[/green] {installed:<12s}")
                    elif lib.found:
                        status_widget.update(f"  [yellow]{lib.name:<15s}[/yellow] {installed:<12s}")
                    else:
                        status_widget.update(f"  [dim]{lib.name:<15s}[/dim] {'—':<12s}")
                except Exception:
                    continue

                # Build dropdown options
                recipe = recipe_map.get(lib.name, {})
                variants = recipe.get("variants", [])

                options: list[tuple[str, str]] = [("skip", "skip")]

                if lib.found and lib.up_to_date:
                    options.append((f"reinstall {lib.version}", f"reinstall:{lib.version}"))
                elif lib.found and lib.update_version:
                    options.append((f"→ {lib.update_version} (recommended)", f"update:{lib.update_version}"))
                elif not lib.found and lib.update_version:
                    options.append((f"install {lib.update_version} (recommended)", f"install:{lib.update_version}"))

                for v in variants:
                    ver = v.get("version", "")
                    label = v.get("label", ver)
                    req = " [c11]" if v.get("requires") == "c11" else ""
                    action = "install" if not lib.found else "update"
                    opt_value = f"{action}:{ver}"
                    if opt_value not in [o[1] for o in options]:
                        options.append((f"{action} {ver} — {label}{req}", opt_value))

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

    def _update_f10_state(self) -> None:
        """Enable/disable F10 Update button based on active tab."""
        from ui.footer import FooterItem
        try:
            for item in self.query("FooterItem"):
                if item._key == "F10":
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

    def do_add(self) -> None:
        """F10 — execute library actions (install/update)."""
        if not self._is_libs_tab_active():
            return

        name = ToolchainsScreen._selected_tc
        if not name:
            self.notify("No toolchain selected", severity="warning")
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

        self._log_sync(f"[bold]Building {len(actions)} library action(s) for {tc.name}...[/bold]")

        for lib_name, action_str in actions:
            parts = action_str.split(":", 1)
            if len(parts) == 2:
                action_type, version = parts
                self._log_sync(f"  {action_type}: {lib_name} {version}")
            else:
                self._log_sync(f"  {action_str}: {lib_name}")

        self._log_sync("[dim]Library building not yet implemented — recipes ready in libraries.yaml[/dim]")

    def _refresh(self) -> None:
        self._registry = None
        self.app.pop_screen()
        self.app.push_screen(ToolchainsScreen())

    def _log(self, msg: str) -> None:
        ToolchainsScreen._log_history.append(msg)
        self.app.call_from_thread(self._update_output)

    def _log_sync(self, msg: str) -> None:
        ToolchainsScreen._log_history.append(msg)
        self._update_output()

    def _update_output(self) -> None:
        try:
            text = "\n".join(ToolchainsScreen._log_history)
            self.query_one("#tc-output-text", Static).update(text)
            scroll = self.query_one("#tc-output", VerticalScroll)
            scroll.scroll_end(animate=False)
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
        elif btn == "btn-check":
            self._handle_check()
        elif btn == "btn-remove":
            self._handle_remove()
        pass

    def _show_tab(self, tab: str) -> None:
        """Switch between Details and Libraries views."""
        detail = self.query_one("#tc-detail", Static)
        libs = self.query_one("#tc-libs", VerticalScroll)
        tab_details = self.query_one("#btn-tab-details", Static)
        tab_libs = self.query_one("#btn-tab-libs", Static)

        if tab == "details":
            detail.display = True
            libs.display = False
            tab_details.set_classes("tab-active")
            tab_libs.set_classes("tab-inactive")
            self._update_detail()
        else:
            detail.display = False
            libs.display = True
            tab_details.set_classes("tab-inactive")
            tab_libs.set_classes("tab-active")
            self._update_libraries()

        # Update F10 button state in footer
        self._update_f10_state()

    def _handle_install(self) -> None:
        name = ToolchainsScreen._selected_tc
        if not name:
            self.notify("No toolchain selected", severity="warning")
            return
        reg = self._get_registry()
        tc = reg.get(name)
        if not tc:
            return
        if tc.installed:
            self.notify(f"{tc.name} is already installed", severity="warning")
            return
        if not tc.url:
            self.notify(f"{tc.name} has no download URL", severity="error")
            return

        self._log_sync(f"[bold]Installing {tc.name}...[/bold]")

        def do_install():
            install_dir = self.app.paths.toolchains_dir / tc.name
            downloads_dir = self.app.paths.downloads_dir
            downloads_dir.mkdir(parents=True, exist_ok=True)

            url = tc.url.replace("${pversion}", "0.27.0")
            filename = url.split("/")[-1]
            filepath = downloads_dir / filename

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
                    return
            except Exception as e:
                self._log(f"[red]Download failed: {e}[/red]")
                return

            if tc.checksum:
                self._log("Verifying checksum...")
                if not self._verify_checksum(filepath, tc.checksum):
                    self._log("[red]Checksum mismatch![/red]")
                    filepath.unlink(missing_ok=True)
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
                    return
            except Exception as e:
                self._log(f"[red]Extract failed: {e}[/red]")
                shutil.rmtree(install_dir, ignore_errors=True)
                return

            self._relocate_toolchain(install_dir, tc)
            self._log(f"[green]{tc.name} installed successfully[/green]")

        self._run_in_thread(do_install)

    def _handle_check(self) -> None:
        name = ToolchainsScreen._selected_tc
        if not name:
            self.notify("No toolchain selected", severity="warning")
            return
        reg = self._get_registry()
        tc = reg.get(name)
        if not tc:
            return
        if not tc.installed:
            self.notify(f"{tc.name} is not installed", severity="warning")
            return

        self._log_sync(f"[bold]Checking {tc.name}...[/bold]")
        install_dir = self.app.paths.toolchains_dir / tc.name
        ok = True

        gcc = install_dir / "bin" / f"{tc.prefix}gcc"
        if not gcc.exists():
            found = list(install_dir.glob(f"**/bin/{tc.prefix}gcc"))
            if found:
                gcc = found[0]

        if not gcc.exists():
            self._log_sync(f"  [red]gcc not found:[/red] {tc.prefix}gcc")
            return

        self._log_sync(f"  gcc:       {gcc}")

        try:
            version = subprocess.check_output(
                [str(gcc), "--version"], text=True, stderr=subprocess.STDOUT, timeout=10,
            ).splitlines()[0]
            self._log_sync(f"  version:   {version}")
        except Exception as e:
            self._log_sync(f"  [red]gcc not working: {e}[/red]")
            ok = False

        try:
            target = subprocess.check_output(
                [str(gcc), "-dumpmachine"], text=True, stderr=subprocess.STDOUT, timeout=10,
            ).strip()
            self._log_sync(f"  target:    {target}")
        except Exception:
            pass

        if tc.sysroot:
            sysroot_path = install_dir / tc.sysroot
            if sysroot_path.is_dir():
                self._log_sync(f"  sysroot:   {tc.sysroot} [green]OK[/green]")
            else:
                self._log_sync(f"  sysroot:   {tc.sysroot} [red]NOT FOUND[/red]")
                ok = False

        nss = install_dir / "sysroots" / "x86_64-oesdk-linux"
        if nss.is_dir():
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
                                self._log_sync(f"  relocate:  [green]OK[/green]")
                            else:
                                self._log_sync(f"  relocate:  [red]FAILED — {interp}[/red]")
                                ok = False
                            break
                except Exception:
                    pass

        self._log_sync(f"  [green]{tc.name} OK[/green]" if ok else f"  [red]{tc.name} has issues[/red]")

    def _handle_remove(self) -> None:
        name = ToolchainsScreen._selected_tc
        if not name:
            self.notify("No toolchain selected", severity="warning")
            return
        reg = self._get_registry()
        tc = reg.get(name)
        if not tc:
            return
        if not tc.installed:
            self.notify(f"{tc.name} is not installed", severity="warning")
            return

        install_dir = self.app.paths.toolchains_dir / tc.name
        if install_dir.exists():
            shutil.rmtree(install_dir)
        self._log_sync(f"[yellow]Removed {tc.name}[/yellow]")
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

    def action_go_back(self) -> None:
        ToolchainsScreen._log_history.clear()
        ToolchainsScreen._selected_tc = ""
        self.app.pop_screen()
