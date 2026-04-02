"""Toolchains screen — browse, install, remove toolchains."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Input, Button, Select
from textual.containers import Horizontal, Vertical, VerticalScroll

from core.toolchain import Toolchain, ToolchainRegistry
from ui.header import S4Header
from ui.footer import S4Footer

# Categories in display order
CATEGORIES = [
    ("common", "Common (universal)"),
    ("stb", "STB (OE-Alliance/OpenPLi/OpenDreambox)"),
    ("android", "Android"),
    ("openwrt", "OpenWrt"),
    ("router", "Router (Fritz/Ubiquiti)"),
    ("nas", "NAS (Synology/QNAP)"),
    ("minipc", "Mini PC (RPi/Pogoplug)"),
    ("tv", "TV"),
    ("legacy", "Legacy"),
    ("custom", "Custom"),
]


class ToolchainsScreen(Screen):
    BINDINGS = []

    _log_history: list[str] = []
    _selected_tc: str = ""
    _selected_category: str = "all"
    _search_text: str = ""

    def __init__(self) -> None:
        super().__init__()
        self._registry: ToolchainRegistry | None = None

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

        with VerticalScroll(id="tc-scroll"):
            # Status box — installed toolchains
            installed = reg.installed_toolchains()
            if installed:
                lines = []
                for tc in installed:
                    lines.append(f"  [bold]{tc.name}[/bold]  {tc.description}  [dim]{tc.arch}[/dim]")
                status_text = "\n".join(lines)
            else:
                status_text = "  [dim]No toolchains installed[/dim]"
            info = Static(status_text, id="tc-info")
            info.border_title = f"Installed ({len(installed)})"
            yield info

            # Output box
            with VerticalScroll(id="tc-output"):
                yield Static("", id="tc-output-text")

            # Toolchains actions
            # Toolchain actions
            with Vertical(id="tc-box"):
                with Horizontal(classes="repo-actions-row"):
                    cat = ToolchainsScreen._selected_category
                    search = ToolchainsScreen._search_text
                    tc_options = self._build_tc_options(reg, cat, search)
                    saved = ToolchainsScreen._selected_tc
                    tc_value = saved if saved and any(v == saved for _, v in tc_options) else (tc_options[0][1] if tc_options else Select.BLANK)
                    yield Select(options=tc_options, value=tc_value, id="tc-select", prompt="select toolchain")
                    yield Static("", classes="spacer")
                    install_btn = Button("Install", id="btn-install", variant="primary")
                    install_btn.tooltip = "Download and install the selected toolchain"
                    yield install_btn
                    check_btn = Button("Check", id="btn-check")
                    check_btn.tooltip = "Verify the selected toolchain is working"
                    yield check_btn
                    remove_btn = Button("Remove", id="btn-remove", variant="error")
                    remove_btn.tooltip = "Delete the selected toolchain from disk"
                    yield remove_btn

            # Browse available
            with Vertical(id="tc-browse-box"):
                with Horizontal(classes="repo-actions-row"):
                    cat_options = [("All", "all")] + [(label, key) for key, label in CATEGORIES]
                    yield Select(options=cat_options, value=ToolchainsScreen._selected_category, id="tc-category")
                    yield Input(value=ToolchainsScreen._search_text, placeholder="Search...", id="tc-search")

                available_text = self._build_available_text(reg, ToolchainsScreen._selected_category, ToolchainsScreen._search_text)
                avail = Static(available_text, id="tc-available")
                yield avail

            # Toolchain details
            with Vertical(id="tc-detail-box"):
                yield Static("", id="tc-detail")

        yield S4Footer(active_screen="toolchains")

    def on_mount(self) -> None:
        self.query_one("#tc-output", VerticalScroll).border_title = "Output"
        self.query_one("#tc-box", Vertical).border_title = "Toolchains"
        self.query_one("#tc-browse-box", Vertical).border_title = "Browse"
        self.query_one("#tc-detail-box", Vertical).border_title = "Details"
        # Restore log
        if ToolchainsScreen._log_history:
            self._update_output()
        # Show first toolchain details
        self._update_detail()

    def _build_tc_options(self, reg: ToolchainRegistry, category: str, search: str) -> list[tuple[str, str]]:
        """Build Select options filtered by category and search."""
        options = []
        search_lower = search.lower()
        for tc in reg.toolchains:
            if category != "all" and tc.category != category:
                continue
            if search_lower and search_lower not in tc.name.lower() and search_lower not in tc.description.lower() and search_lower not in tc.machines.lower():
                continue
            status = "[green]●[/green] " if tc.installed else "  "
            options.append((f"{status}{tc.name}", tc.name))
        return options

    def _build_available_text(self, reg: ToolchainRegistry, category: str, search: str) -> str:
        """Build available toolchains text grouped by category."""
        search_lower = search.lower()
        lines = []
        for cat_key, cat_label in CATEGORIES:
            if category != "all" and category != cat_key:
                continue
            tcs = [tc for tc in reg.toolchains if tc.category == cat_key]
            if search_lower:
                tcs = [tc for tc in tcs if search_lower in tc.name.lower() or search_lower in tc.description.lower() or search_lower in tc.machines.lower()]
            if not tcs:
                continue
            lines.append(f"  [bold]{cat_label}[/bold]")
            for tc in tcs:
                status = "[green]●[/green]" if tc.installed else "[dim]○[/dim]"
                lines.append(f"    {status} {tc.name:40s} {tc.description}")
            lines.append("")
        return "\n".join(lines) if lines else "  [dim]No toolchains match filter[/dim]"

    def _update_detail(self) -> None:
        """Update detail panel for selected toolchain."""
        try:
            sel = self.query_one("#tc-select", Select)
            if sel.value is None or not isinstance(sel.value, str):
                return
            reg = self._get_registry()
            tc = reg.get(sel.value)
            if not tc:
                return
            status = "[green]installed[/green]" if tc.installed else "[red]not installed[/red]"
            lines = [
                f"  [bold]{tc.name}[/bold] — {tc.description}",
                f"  Status:    {status}",
                f"  Category:  {tc.category}",
                f"  Arch:      {tc.arch}",
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
                lines.append(f"  URL:       {tc.url}")
            if tc.checksum:
                lines.append(f"  Checksum:  {tc.checksum}")
            self.query_one("#tc-detail", Static).update("\n".join(lines))
        except Exception:
            pass

    def _save_state(self) -> None:
        """Save current UI state before refresh."""
        try:
            sel = self.query_one("#tc-select", Select)
            if isinstance(sel.value, str):
                ToolchainsScreen._selected_tc = sel.value
            cat = self.query_one("#tc-category", Select)
            if isinstance(cat.value, str):
                ToolchainsScreen._selected_category = cat.value
            ToolchainsScreen._search_text = self.query_one("#tc-search", Input).value
        except Exception:
            pass

    def _refresh(self) -> None:
        """Reload the screen."""
        self._save_state()
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

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "tc-select":
            self._update_detail()
        elif event.select.id == "tc-category":
            self._update_filter()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tc-search":
            self._update_filter()

    def _update_filter(self) -> None:
        """Update available list and select when filter changes."""
        try:
            reg = self._get_registry()
            cat_sel = self.query_one("#tc-category", Select)
            category = cat_sel.value if isinstance(cat_sel.value, str) else "all"
            search = self.query_one("#tc-search", Input).value

            # Update available text
            available_text = self._build_available_text(reg, category, search)
            self.query_one("#tc-available", Static).update(available_text)

            # Update toolchain select
            tc_sel = self.query_one("#tc-select", Select)
            options = self._build_tc_options(reg, category, search)
            tc_sel.set_options(options if options else [("(none)", None)])
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id

        if btn == "btn-install":
            self._handle_install()
        elif btn == "btn-check":
            self._handle_check()
        elif btn == "btn-remove":
            self._handle_remove()

    def _handle_install(self) -> None:
        """Download and install selected toolchain."""
        sel = self.query_one("#tc-select", Select)
        if sel.value is None or not isinstance(sel.value, str):
            self.notify("No toolchain selected", severity="warning")
            return

        reg = self._get_registry()
        tc = reg.get(sel.value)
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

            # Resolve ${pversion}
            url = tc.url.replace("${pversion}", "0.27.0")
            filename = url.split("/")[-1]
            filepath = downloads_dir / filename

            # Download
            self._log(f"Downloading {filename}...")
            try:
                result = subprocess.run(
                    ["wget", "-q", "--show-progress", "-O", str(filepath), url],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode != 0:
                    # Try curl
                    result = subprocess.run(
                        ["curl", "-L", "-o", str(filepath), url],
                        capture_output=True, text=True, timeout=600,
                    )
                if result.returncode != 0:
                    self._log(f"[red]Download failed[/red]")
                    return
            except Exception as e:
                self._log(f"[red]Download failed: {e}[/red]")
                return

            # Verify checksum
            if tc.checksum:
                self._log("Verifying checksum...")
                ok = self._verify_checksum(filepath, tc.checksum)
                if not ok:
                    self._log("[red]Checksum mismatch![/red]")
                    filepath.unlink(missing_ok=True)
                    return
                self._log("[green]Checksum OK[/green]")

            # Extract using system tar (handles symlinks, absolute paths)
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

            # Run relocate-sdk.sh (Bootlin) or relocate_sdk.py (OE-SDK)
            self._relocate_toolchain(install_dir, tc)

            self._log(f"[green]{tc.name} installed successfully[/green]")

        self._run_in_thread(do_install)

    def _handle_check(self) -> None:
        """Check if selected toolchain is working."""
        sel = self.query_one("#tc-select", Select)
        if sel.value is None or not isinstance(sel.value, str):
            self.notify("No toolchain selected", severity="warning")
            return

        reg = self._get_registry()
        tc = reg.get(sel.value)
        if not tc:
            return

        if not tc.installed:
            self.notify(f"{tc.name} is not installed", severity="warning")
            return

        self._log_sync(f"[bold]Checking {tc.name}...[/bold]")
        install_dir = self.app.paths.toolchains_dir / tc.name
        gcc = install_dir / "bin" / f"{tc.prefix}gcc"

        if not gcc.exists():
            # Search for it
            found = list(install_dir.glob(f"**/bin/{tc.prefix}gcc"))
            if found:
                gcc = found[0]

        if not gcc.exists():
            self._log_sync(f"  [red]gcc not found:[/red] {tc.prefix}gcc")
            return

        self._log_sync(f"  gcc:     {gcc}")
        try:
            version = subprocess.check_output(
                [str(gcc), "--version"], text=True, stderr=subprocess.STDOUT, timeout=10,
            ).splitlines()[0]
            self._log_sync(f"  version: {version}")

            target = subprocess.check_output(
                [str(gcc), "-dumpmachine"], text=True, stderr=subprocess.STDOUT, timeout=10,
            ).strip()
            self._log_sync(f"  target:  {target}")

            sysroot = subprocess.check_output(
                [str(gcc), "-print-sysroot"], text=True, stderr=subprocess.STDOUT, timeout=10,
            ).strip()
            self._log_sync(f"  sysroot: {sysroot}")

            self._log_sync(f"  [green]{tc.name} OK[/green]")
        except subprocess.CalledProcessError as e:
            self._log_sync(f"  [red]gcc error: {e.output.strip() if e.output else e}[/red]")
        except FileNotFoundError:
            self._log_sync(f"  [red]gcc not executable[/red]")
        except Exception as e:
            self._log_sync(f"  [red]check failed: {e}[/red]")

    def _handle_remove(self) -> None:
        """Remove selected installed toolchain."""
        sel = self.query_one("#tc-select", Select)
        if sel.value is None or not isinstance(sel.value, str):
            self.notify("No toolchain selected", severity="warning")
            return

        reg = self._get_registry()
        tc = reg.get(sel.value)
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
        """Run relocate-sdk.sh (Bootlin) or OE-SDK relocate_sdk.py."""
        # Bootlin: relocate-sdk.sh
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

        # OE-SDK: relocate_sdk.py
        nss = install_dir / "sysroots" / "x86_64-oesdk-linux"
        if not nss.is_dir():
            return

        # Find gcc (may be a shell wrapper), resolve to real ELF via ranlib
        gcc_scripts = list(nss.glob("usr/bin/*/**/*-gcc"))
        gcc_scripts = [g for g in gcc_scripts if "++" not in g.name]
        if not gcc_scripts:
            return

        # Use ranlib from same dir — it's always a real ELF binary
        gcc_dir = gcc_scripts[0].parent
        ranlib_bins = list(gcc_dir.glob("*-ranlib"))
        if not ranlib_bins:
            return

        real_bin = ranlib_bins[0].resolve()  # resolve symlinks

        # Read ELF interpreter
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

        # Check if loader exists at expected path — if yes, no relocate needed
        if Path(interp).exists():
            return

        rpy = install_dir / "relocate_sdk.py"
        if not rpy.exists():
            return

        # Derive old prefix and sdk loader
        old_prefix = interp.split("/sysroots/")[0] if "/sysroots/" in interp else ""
        if not old_prefix:
            return

        sdk_loader = nss / "lib" / Path(interp).name
        if not sdk_loader.exists():
            return

        self._log(f"Relocating OE-SDK to {install_dir.name}...")

        # Update old_prefix in relocate_sdk.py
        rpy_text = rpy.read_text(encoding="utf-8", errors="replace")
        import re
        rpy_text = re.sub(
            r'old_prefix = .*',
            f'old_prefix = re.compile(b("{old_prefix}"))',
            rpy_text,
        )
        rpy.write_text(rpy_text, encoding="utf-8")

        # Find all executables in native sysroot (same as s3)
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
        """Verify file checksum. Format: 'md5:hash' or 'sha256:hash'."""
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

    def _strip_components(self, directory: Path, levels: int) -> None:
        """Simulate tar --strip-components by moving nested contents up."""
        for _ in range(levels):
            subdirs = [d for d in directory.iterdir() if d.is_dir()]
            if len(subdirs) == 1:
                subdir = subdirs[0]
                for item in subdir.iterdir():
                    item.rename(directory / item.name)
                subdir.rmdir()
            else:
                break

    def action_go_back(self) -> None:
        ToolchainsScreen._log_history.clear()
        ToolchainsScreen._selected_tc = ""
        ToolchainsScreen._selected_category = "all"
        ToolchainsScreen._search_text = ""
        self.app.pop_screen()
