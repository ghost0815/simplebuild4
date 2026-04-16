"""Android NDK Customize modal — configure NDK template before build.

Opens when F6 is pressed on an android-ndk template. Lets the user
select the NDK version, install/download it, then choose the API level
(populated dynamically from the installed NDK's meta/platforms.json).
On save, forks the default template to data/templates/ with the
configured .config.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, Button, Input, Select

from translation import t
from ui.icons import icon

from core.template import (
    Template, AndroidNdkConfig,
    load_andk_config, save_andk_config,
    ANDK_KNOWN_VERSIONS, ANDK_API_LEVELS,
)


class AndroidNdkCustomizeModal(ModalScreen[Optional[AndroidNdkConfig]]):
    """Modal for editing Android NDK template build parameters."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    AndroidNdkCustomizeModal {
        align: center middle;
    }
    #andk-box {
        width: 80;
        height: auto;
        max-height: 75%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    #andk-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    .andk-row {
        height: 3;
        margin-bottom: 0;
    }
    .andk-label {
        width: 20;
        padding: 1 1 0 0;
    }
    .andk-row Input, .andk-row Select {
        width: 1fr;
    }
    .andk-browse-btn {
        width: 5;
        min-width: 5;
    }
    #andk-status {
        height: 1;
        margin: 0 0 0 21;
        color: $text-muted;
    }
    #andk-btn-spacer {
        width: 1fr;
    }
    #andk-buttons {
        height: 3;
        margin-top: 0;
    }
    #andk-buttons Button {
        margin: 0 0 0 1;
    }
    """

    _andk_base: str = "data/builders/android-ndk"  # overridden from settings in compose
    _install_confirm: bool = False  # True after first press on "Reinstall"

    def __init__(self, tpl: Template, cfg: AndroidNdkConfig) -> None:
        super().__init__()
        self._tpl = tpl
        self._cfg = cfg

    def compose(self) -> ComposeResult:
        cfg = self._cfg
        # Read Android NDK base dir from settings (F1), fallback to default
        _custom = (getattr(self.app.settings, "andk_dir", "") or "").strip()
        self._andk_base = _custom or "data/builders/android-ndk"
        with Vertical(id="andk-box"):
            yield Static(
                f"[bold]Android NDK Customize — {self._tpl.name}[/bold]",
                id="andk-title",
            )

            # 1. NDK Version
            with Horizontal(classes="andk-row"):
                yield Static("NDK Version", classes="andk-label")
                _ndk_opts = list(ANDK_KNOWN_VERSIONS)
                yield Select(
                    options=_ndk_opts,
                    value=cfg.ndk_version if any(v == cfg.ndk_version for _, v in _ndk_opts) else _ndk_opts[0][1],
                    id="andk-ndk-version",
                )

            # 2. Build Directory
            with Horizontal(classes="andk-row"):
                yield Static("Build Directory", classes="andk-label")
                yield Input(
                    value=self._build_dir_value(),
                    id="andk-build-dir",
                )
                _browse = Button(icon("browse"), id="andk-build-dir-browse", classes="andk-browse-btn")
                _browse.tooltip = "Browse for directory"
                yield _browse

            # 3. API Level — initially empty or populated from installed NDK
            with Horizontal(classes="andk-row"):
                yield Static("API Level", classes="andk-label")
                yield Select(
                    options=[("(install NDK first)", "__none__")],
                    id="andk-api",
                )

            # 4. Toolchain Name (auto-generated)
            with Horizontal(classes="andk-row"):
                yield Static("Toolchain Name", classes="andk-label")
                yield Input(
                    value=self._auto_name(),
                    id="andk-tc-name",
                )

            # Status line
            yield Static("", id="andk-status")

            # Buttons — Install left-aligned, Save/Cancel right
            with Horizontal(id="andk-buttons"):
                yield Static("", classes="andk-label")
                yield Button("Install", id="andk-install", variant="success")
                yield Static("", id="andk-btn-spacer")
                yield Button(t("save"), id="andk-save", variant="primary")
                yield Button(t("cancel"), id="andk-cancel")

    def on_mount(self) -> None:
        # If NDK version is set, try to populate API levels from installed NDK
        if self._cfg.ndk_version:
            self._refresh_api_levels(self._cfg.ndk_version)
        self._update_install_button()

    def _auto_name(self) -> str:
        base = self._base_name()
        api = self._cfg.api
        if api:
            return f"{base}_{api}"
        return base

    def _base_name(self) -> str:
        name = self._tpl.name
        # Strip any numeric API suffix
        parts = name.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return parts[0]
        return name

    def _build_dir_value(self) -> str:
        """Current build directory string for display (relative like OE-SDK)."""
        base = self._andk_base
        version = self._cfg.ndk_version
        if version:
            return f"{base}/android-ndk-{version}"
        return base

    # -- NDK install + API level population --

    def _ndk_base_dir(self) -> Path:
        """Base dir for NDK installs — resolves _andk_base to absolute path."""
        p = Path(self._andk_base)
        return p if p.is_absolute() else self.app.paths.root / self._andk_base

    def _ndk_dir(self, version: str) -> Path:
        """Path where the NDK would be installed.

        If the user edited the Build Directory input, use that value;
        otherwise derive from _ndk_base_dir().
        """
        try:
            bd = self.query_one("#andk-build-dir", Input).value.strip()
            if bd:
                p = Path(bd)
                return p if p.is_absolute() else self.app.paths.root / bd
        except Exception:
            pass
        return self._ndk_base_dir() / f"android-ndk-{version}"

    def _update_install_button(self) -> None:
        """Update Install button label based on whether NDK is present.

        If already installed, show "Installed ✓" — first press switches to
        "Reinstall?", second press actually reinstalls.
        """
        self._install_confirm = False
        try:
            version = str(self.query_one("#andk-ndk-version", Select).value or "")
            btn = self.query_one("#andk-install", Button)
        except Exception:
            return
        btn.disabled = False
        if not version:
            btn.label = "Install"
            return
        ndk_dir = self._ndk_dir(version)
        btn.label = "Installed ✓" if ndk_dir.is_dir() else "Install"

    def _refresh_api_levels(self, version: str) -> None:
        """Populate API Level dropdown from installed NDK's platforms.json."""
        ndk_dir = self._ndk_dir(version)
        platforms_file = ndk_dir / "meta" / "platforms.json"
        if not platforms_file.exists():
            try:
                sel = self.query_one("#andk-api", Select)
                sel.set_options([("(install NDK first)", "__none__")])
            except Exception:
                pass
            return
        try:
            data = json.loads(platforms_file.read_text(encoding="utf-8"))
            min_api = data.get("min", 21)
            max_api = data.get("max", 35)
            aliases = data.get("aliases", {})

            # Build reverse map: api_number → codename
            codenames = {}
            for name, num in aliases.items():
                num_str = str(num)
                # Prefer the short single-letter codenames
                if num_str not in codenames or len(name) < len(codenames[num_str]):
                    codenames[num_str] = name

            # Android version names for display
            _android_versions = {
                "35": "Android 15", "34": "Android 14", "33": "Android 13",
                "32": "Android 12L", "31": "Android 12", "30": "Android 11",
                "29": "Android 10", "28": "Android 9 Pie", "27": "Android 8.1",
                "26": "Android 8.0", "25": "Android 7.1", "24": "Android 7.0",
                "23": "Android 6.0", "22": "Android 5.1", "21": "Android 5.0",
            }

            opts = []
            for api in range(max_api, min_api - 1, -1):
                api_str = str(api)
                label = api_str
                ver = _android_versions.get(api_str)
                if ver:
                    label = f"{api_str} — {ver}"
                opts.append((label, api_str))

            sel = self.query_one("#andk-api", Select)
            sel.set_options(opts)
            # Pre-select saved API or highest
            if self._cfg.api and any(v == self._cfg.api for _, v in opts):
                sel.value = self._cfg.api
            elif opts:
                sel.value = opts[0][1]
        except Exception:
            pass

    def _install_ndk(self) -> None:
        """Download and extract the NDK.

        If the target directory already exists, the first press changes
        the button label to "Reinstall?" — a second press proceeds.
        """
        try:
            version = str(self.query_one("#andk-ndk-version", Select).value or "")
        except Exception:
            return
        if not version:
            self.notify("Select an NDK version first", severity="warning")
            return
        ndk_dir = self._ndk_dir(version)
        if ndk_dir.is_dir() and not self._install_confirm:
            # First press on an existing install → ask for confirmation
            self._install_confirm = True
            try:
                self.query_one("#andk-install", Button).label = "Reinstall?"
            except Exception:
                pass
            return
        self._install_confirm = False

        # Disable button during download
        try:
            btn = self.query_one("#andk-install", Button)
            btn.disabled = True
        except Exception:
            pass

        import subprocess

        def _set_status(msg: str) -> None:
            def _do():
                try:
                    self.query_one("#andk-status", Static).update(msg)
                except Exception:
                    pass
            self.app.call_from_thread(_do)

        def do_install():
            downloads_dir = self.app.paths.downloads_dir
            downloads_dir.mkdir(parents=True, exist_ok=True)
            ndk_filename = f"android-ndk-{version}-linux.zip"
            ndk_zip = downloads_dir / ndk_filename
            url = f"https://dl.google.com/android/repository/{ndk_filename}"

            try:
                # Download
                if not ndk_zip.exists():
                    _set_status(f"[yellow]Downloading {ndk_filename}...[/yellow]")
                    r = subprocess.run(
                        ["wget", "-q", "-O", str(ndk_zip), url],
                        capture_output=True, text=True, timeout=1800,
                    )
                    if r.returncode != 0:
                        r = subprocess.run(
                            ["curl", "-sL", "-o", str(ndk_zip), url],
                            capture_output=True, text=True, timeout=1800,
                        )
                    if r.returncode != 0:
                        ndk_zip.unlink(missing_ok=True)
                        _set_status("[red]Download failed[/red]")
                        return
                    size_mb = ndk_zip.stat().st_size / (1024 * 1024)
                    _set_status(f"[yellow]Downloaded ({size_mb:.0f} MB), extracting...[/yellow]")
                else:
                    _set_status(f"[yellow]Using cached {ndk_filename}, extracting...[/yellow]")

                # Extract
                builders_ndk = self._ndk_base_dir()
                builders_ndk.mkdir(parents=True, exist_ok=True)
                r = subprocess.run(
                    ["unzip", "-o", "-q", "-d", str(builders_ndk), str(ndk_zip)],
                    capture_output=True, text=True, timeout=600,
                )
                if r.returncode != 0:
                    _set_status(f"[red]Extract failed: {r.stderr[:100]}[/red]")
                    return

                def on_done():
                    try:
                        self.query_one("#andk-install", Button).disabled = False
                    except Exception:
                        pass
                    if ndk_dir.is_dir():
                        try:
                            status = self.query_one("#andk-status", Static)
                            status.update(f"[green]NDK {version} installed[/green]")
                        except Exception:
                            pass
                        self._refresh_api_levels(version)
                        self._update_install_button()
                    else:
                        try:
                            self.query_one("#andk-status", Static).update(
                                "[red]NDK directory not found after extract[/red]"
                            )
                        except Exception:
                            pass

                self.app.call_from_thread(on_done)
            except Exception as e:
                def on_err():
                    try:
                        self.query_one("#andk-install", Button).disabled = False
                        self.query_one("#andk-status", Static).update(f"[red]Failed: {e}[/red]")
                    except Exception:
                        pass
                self.app.call_from_thread(on_err)

        threading.Thread(target=do_install, daemon=True).start()

    # -- Event handlers --

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "andk-install":
            self._install_ndk()
        elif event.button.id == "andk-build-dir-browse":
            self._browse_build_dir()
        elif event.button.id == "andk-save":
            self._do_save()
        elif event.button.id == "andk-cancel":
            self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "andk-build-dir":
            # Build dir changed → re-check API levels + install button
            try:
                version = str(self.query_one("#andk-ndk-version", Select).value or "")
            except Exception:
                return
            if version:
                self._refresh_api_levels(version)
                self._update_install_button()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "andk-ndk-version":
            version = str(event.select.value) if event.select.value else ""
            if version:
                self._refresh_api_levels(version)
                self._update_install_button()
                try:
                    bd = self.query_one("#andk-build-dir", Input)
                    base = self._andk_base
                    cur = bd.value.strip()
                    if not cur or cur == base or cur.startswith(base + "/"):
                        bd.value = f"{base}/android-ndk-{version}"
                except Exception:
                    pass
        elif event.select.id == "andk-api":
            api = str(event.select.value) if event.select.value else ""
            if api and api != "__none__":
                try:
                    name_input = self.query_one("#andk-tc-name", Input)
                    cur = name_input.value.strip()
                    base = self._base_name()
                    if not cur or cur == self._tpl.name or cur.startswith(base + "_"):
                        name_input.value = f"{base}_{api}"
                except Exception:
                    pass

    # -- Browse --

    def _browse_build_dir(self) -> None:
        """Open file picker for the build directory."""
        from ui.screens.toolchains import FilePickerScreen
        try:
            current = self.query_one("#andk-build-dir", Input).value.strip()
        except Exception:
            current = ""
        start = current or str(self.app.paths.root)
        self.app.push_screen(
            FilePickerScreen(
                start_path=start,
                title="NDK Build Directory",
                directories_only=True,
                select_label=t("select_btn"),
            ),
            callback=self._on_build_dir_picked,
        )

    def _on_build_dir_picked(self, result) -> None:
        if not result:
            return
        path = result[0] if isinstance(result, tuple) else str(result)
        if path:
            try:
                self.query_one("#andk-build-dir", Input).value = str(path)
            except Exception:
                pass

    # -- Save --

    def _do_save(self) -> None:
        try:
            ndk_version = str(self.query_one("#andk-ndk-version", Select).value or "")
            api = str(self.query_one("#andk-api", Select).value or "")
            tc_name = self.query_one("#andk-tc-name", Input).value.strip()
        except Exception as e:
            self.notify(f"Error reading fields: {e}", severity="error")
            return
        if not ndk_version:
            self.notify("NDK Version is required", severity="error")
            return
        if not api or api == "__none__":
            self.notify("API Level is required — install NDK first", severity="error")
            return
        if not tc_name:
            self.notify("Toolchain name is required", severity="error")
            return

        cfg = AndroidNdkConfig(
            api=api,
            arch=self._cfg.arch,
            ndk_version=ndk_version,
        )
        cfg._tc_name = tc_name  # type: ignore[attr-defined]
        self.dismiss(cfg)

    def action_cancel(self) -> None:
        self.dismiss(None)
