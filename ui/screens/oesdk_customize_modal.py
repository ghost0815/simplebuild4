"""OE-SDK Customize modal — configure OE-SDK template before build.

Opens when F6 is pressed on an OE-SDK template. Lets the user set the
branch (with remote fetch like F3), build directory, machine, distro and
SDK extras. On save, forks the default template to data/templates/ with
the configured .config.
"""

from __future__ import annotations

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
    Template, OeSdkConfig,
    load_oesdk_config, save_oesdk_config,
)


class OeSdkCustomizeModal(ModalScreen[Optional[OeSdkConfig]]):
    """Modal for editing OE-SDK template build parameters."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    OeSdkCustomizeModal {
        align: center middle;
    }
    #oesdk-box {
        width: 100;
        height: auto;
        max-height: 85%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    #oesdk-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    .oesdk-row {
        height: 3;
        margin-bottom: 0;
    }
    .oesdk-label {
        width: 20;
        padding: 1 1 0 0;
    }
    .oesdk-row Input, .oesdk-row Select {
        width: 1fr;
    }
    .oesdk-browse-btn {
        width: 5;
        min-width: 5;
        margin: 0 0 0 1;
    }
    #oesdk-name-hint {
        color: $text-muted;
        padding: 0 0 0 20;
        height: 1;
    }
    #oesdk-status {
        height: 1;
        margin: 0 0 0 21;
        color: $text-muted;
    }
    #oesdk-buttons {
        height: 3;
        margin-top: 0;
    }
    #oesdk-btn-spacer {
        width: 1fr;
    }
    #oesdk-buttons Button {
        margin: 0 0 0 1;
    }
    """

    _suppress_branch_changed: bool = False
    _oesdk_base: str = "data/builders/oe-sdk"  # overridden from settings in compose

    def __init__(self, tpl: Template, cfg: OeSdkConfig) -> None:
        super().__init__()
        self._tpl = tpl
        self._cfg = cfg

    def compose(self) -> ComposeResult:
        cfg = self._cfg
        # Read OE-SDK base dir from settings (F1), fallback to default
        self._oesdk_base = getattr(self.app.settings, "oesdk_dir", "") or "data/builders/oe-sdk"
        _machines = [m.strip() for m in self._tpl.machines.split() if m.strip()]
        with Vertical(id="oesdk-box"):
            yield Static(
                f"[bold]OE-SDK Customize — {self._tpl.name}[/bold]",
                id="oesdk-title",
            )

            # 1. Repository URL + commits button
            with Horizontal(classes="oesdk-row"):
                yield Static("Repository URL", classes="oesdk-label")
                yield Input(
                    value=cfg.repo_url,
                    placeholder="https://github.com/...",
                    id="oesdk-repo-url",
                )
                _commits = Button(icon("log"), id="oesdk-repo-commits", classes="oesdk-browse-btn")
                _commits.tooltip = "Show commit history (requires cloned repo in Build Directory)"
                yield _commits

            # 2. Branch — exact same pattern as F3 Repository screen
            with Horizontal(classes="oesdk-row"):
                yield Static("Branch", classes="oesdk-label")
                # Same as F3: options=[(prompt_text, "")], allow_blank=False, prompt=same
                yield Select(
                    options=[(t("ph_repo_branch"), "")],
                    id="oesdk-branch",
                    allow_blank=False,
                    prompt=t("ph_repo_branch"),
                )
                _refresh = Button(icon("refresh"), id="oesdk-branch-refresh", classes="oesdk-browse-btn")
                _refresh.tooltip = t("tt_branch_refresh")
                yield _refresh

            # 3. Toolchain Name
            with Horizontal(classes="oesdk-row"):
                yield Static("Toolchain Name", classes="oesdk-label")
                yield Input(
                    value=self._auto_name(),
                    id="oesdk-tc-name",
                )
            # 4. Build Directory + browse
            _base = self._oesdk_base
            _default_build = f"{_base}/{self._auto_name()}" if cfg.repo_branch else _base
            _initial_build = cfg.build_dir or _default_build
            # Remember what we auto-set so a later toolchain-name change
            # can tell whether the user has customised the field.
            self._last_auto_build_dir = _default_build if not cfg.build_dir else ""
            with Horizontal(classes="oesdk-row"):
                yield Static("Build Directory", classes="oesdk-label")
                yield Input(
                    value=_initial_build,
                    placeholder=_default_build,
                    id="oesdk-build-dir",
                )
                _browse = Button(icon("browse"), id="oesdk-build-dir-browse", classes="oesdk-browse-btn")
                _browse.tooltip = "Browse for directory"
                yield _browse

            # 5. Machine — dropdown from template machines
            with Horizontal(classes="oesdk-row"):
                yield Static("Machine", classes="oesdk-label")
                if _machines:
                    _machine_opts = [(m, m) for m in sorted(_machines)]
                    yield Select(
                        options=_machine_opts,
                        value=cfg.machine if cfg.machine in _machines else _machines[0],
                        id="oesdk-machine",
                    )
                else:
                    yield Input(
                        value=cfg.machine,
                        placeholder="e.g. pulse4k",
                        id="oesdk-machine",
                    )

            # 6. Distro — Select, populated from repo if available
            with Horizontal(classes="oesdk-row"):
                yield Static("Distro", classes="oesdk-label")
                _distro_opts = self._scan_distro_options()
                yield Select(
                    options=_distro_opts,
                    value=cfg.distro if any(v == cfg.distro for _, v in _distro_opts) else (_distro_opts[0][1] if _distro_opts else ""),
                    id="oesdk-distro",
                )

            # 7. SDK Extras
            with Horizontal(classes="oesdk-row"):
                yield Static("SDK Extras", classes="oesdk-label")
                yield Input(
                    value=cfg.sdk_extras,
                    placeholder="openssl-dev libusb1-dev ...",
                    id="oesdk-sdk-extras",
                )

            # Status line for clone/update progress
            yield Static("", id="oesdk-status")

            # Buttons — Clone left-aligned under the fields, Save/Cancel right
            with Horizontal(id="oesdk-buttons"):
                yield Static("", classes="oesdk-label")  # spacer matching label width
                yield Button(t("clone"), id="oesdk-clone", variant="success")
                yield Static("", id="oesdk-btn-spacer")
                yield Button(t("save"), id="oesdk-save", variant="primary")
                yield Button(t("cancel"), id="oesdk-cancel")

    def on_mount(self) -> None:
        # Detect if repo is already cloned → update button label + detect branch
        build_dir = self._cfg.build_dir
        if build_dir:
            repo_path = Path(build_dir)
            if not repo_path.is_absolute():
                repo_path = self.app.paths.root / repo_path
            if (repo_path / ".git").is_dir():
                # Repo exists locally → button = "Update"
                try:
                    self.query_one("#oesdk-clone", Button).label = t("update")
                except Exception:
                    pass
                import subprocess
                # Detect origin URL from local repo
                try:
                    result = subprocess.run(
                        ["git", "remote", "get-url", "origin"],
                        cwd=str(repo_path), capture_output=True,
                        text=True, timeout=5,
                    )
                    local_url = (result.stdout or "").strip()
                    if local_url:
                        try:
                            self.query_one("#oesdk-repo-url", Input).value = local_url
                        except Exception:
                            pass
                except Exception:
                    pass
                # Detect current branch from local repo
                if not self._cfg.repo_branch:
                    try:
                        result = subprocess.run(
                            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=str(repo_path), capture_output=True,
                            text=True, timeout=5,
                        )
                        local_branch = (result.stdout or "").strip()
                        if local_branch and local_branch != "HEAD":
                            self._cfg.repo_branch = local_branch
                    except Exception:
                        pass
        # Pre-select saved/detected branch
        if self._cfg.repo_branch:
            self._apply_branch_options([], self._cfg.repo_branch)
            # Also update toolchain name + build dir
            try:
                name_input = self.query_one("#oesdk-tc-name", Input)
                cur = name_input.value.strip()
                auto = self._auto_name()
                if not cur or cur == self._tpl.name or cur == auto:
                    name_input.value = auto
                    self._update_build_dir(auto)
            except Exception:
                pass

    def _base_name(self) -> str:
        """Template name without any branch suffix."""
        name = self._tpl.name
        branch = self._cfg.repo_branch
        if branch and name.endswith(f"_{branch}"):
            return name[:-len(f"_{branch}")]
        return name

    def _auto_name(self) -> str:
        branch = self._cfg.repo_branch
        base = self._base_name()
        if branch:
            return f"{base}_{branch}"
        return base

    def _auto_name_with(self, branch: str) -> str:
        """Generate name with a specific branch (used on branch change)."""
        base = self._base_name()
        if branch:
            return f"{base}_{branch}"
        return base

    # ----------------------------------------------------------------
    # Branch handling — copied 1:1 from F3 Repository screen
    # ----------------------------------------------------------------

    def _apply_branch_options(self, branches: list, current: str) -> None:
        """Populate the branch dropdown — 1:1 from repository.py."""
        try:
            sel = self.query_one("#oesdk-branch", Select)
            self._suppress_branch_changed = True
            try:
                merged = list(branches or [])
                if current and current not in merged:
                    merged.append(current)
                merged.sort(key=str.lower)
                from core.vcs_branches import get_default_branch
                from rich.text import Text as _Text
                url = ""
                try:
                    url = self.query_one("#oesdk-repo-url", Input).value.strip()
                except Exception:
                    url = self._cfg.repo_url
                default = get_default_branch(url, "git") if url else ""
                opts: list[tuple[object, str]] = [(t("ph_repo_branch"), "")]
                for b in merged:
                    label = _Text(b, style="bold") if b == default else b
                    opts.append((label, b))
                sel.set_options(opts)
                sel.value = current if current else ""
            finally:
                self._suppress_branch_changed = False
        except Exception:
            pass

    def _load_branches(self, force: bool = True) -> None:
        """Fetch the branch list — 1:1 from repository.py."""
        try:
            url = self.query_one("#oesdk-repo-url", Input).value.strip()
        except Exception:
            return
        if not url:
            self.notify(t("branch_no_url"), severity="warning")
            return
        current_branch = self._get_branch_value()
        # Show "loading…" placeholder
        try:
            sel = self.query_one("#oesdk-branch", Select)
            self._suppress_branch_changed = True
            try:
                placeholder = [(t("branch_loading"), "__loading__")]
                if current_branch:
                    placeholder.insert(0, (current_branch, current_branch))
                else:
                    placeholder.insert(0, (t("ph_repo_branch"), ""))
                sel.set_options(placeholder)
                sel.value = current_branch if current_branch else ""
            finally:
                self._suppress_branch_changed = False
        except Exception:
            pass

        def do_fetch():
            from core.vcs_branches import list_branches
            branches = list_branches(url, "git", force_refresh=force)

            def apply():
                self._apply_branch_options(branches, current_branch)
                if not branches:
                    self.notify(t("branch_fetch_failed"), severity="warning")

            self.app.call_from_thread(apply)

        threading.Thread(target=do_fetch, daemon=True).start()

    def _get_branch_value(self) -> str:
        """Read the current branch — same normalisation as F3."""
        try:
            sel = self.query_one("#oesdk-branch", Select)
            val = sel.value
            if val in ("__loading__",) or val is Select.BLANK:
                return ""
            return str(val) if val else ""
        except Exception:
            return ""

    # ----------------------------------------------------------------
    # Event handlers
    # ----------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "oesdk-branch-refresh":
            self._load_branches()
        elif bid == "oesdk-repo-commits":
            self._show_repo_commits()
        elif bid == "oesdk-build-dir-browse":
            self._browse_build_dir()
        elif bid == "oesdk-clone":
            self._clone_repo()
        elif bid == "oesdk-save":
            self._do_save()
        elif bid == "oesdk-cancel":
            self.dismiss(None)

    def on_select_changed(self, event: Select.Changed) -> None:
        """Update toolchain name + build dir when branch changes."""
        if self._suppress_branch_changed:
            return
        if event.select.id == "oesdk-branch":
            branch = self._get_branch_value()
            if branch:
                try:
                    name_input = self.query_one("#oesdk-tc-name", Input)
                    cur = name_input.value.strip()
                    # Only auto-update if it still looks auto-generated
                    if not cur or cur == self._tpl.name or cur.startswith(self._tpl.name + "_"):
                        new_name = self._auto_name_with(branch)
                        name_input.value = new_name
                        self._update_build_dir(new_name)
                except Exception:
                    pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "oesdk-tc-name":
            self._update_build_dir(event.value.strip())
        elif event.input.id == "oesdk-build-dir":
            self._update_clone_button()

    def on_blur(self, event) -> None:
        """When Build Directory loses focus, detect repo URL + branch."""
        try:
            widget = event.widget if hasattr(event, "widget") else None
            if widget and getattr(widget, "id", "") == "oesdk-build-dir":
                self._detect_repo_from_build_dir()
        except Exception:
            pass

    def _detect_repo_from_build_dir(self) -> None:
        """Read origin URL and branch from a local repo in the build dir."""
        try:
            build_dir = self.query_one("#oesdk-build-dir", Input).value.strip()
        except Exception:
            return
        if not build_dir:
            return
        repo_path = Path(build_dir)
        if not repo_path.is_absolute():
            repo_path = self.app.paths.root / repo_path
        if not (repo_path / ".git").is_dir():
            return
        import subprocess
        # Detect origin URL
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(repo_path), capture_output=True,
                text=True, timeout=5,
            )
            local_url = (result.stdout or "").strip()
            if local_url:
                self.query_one("#oesdk-repo-url", Input).value = local_url
        except Exception:
            pass
        # Detect current branch
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(repo_path), capture_output=True,
                text=True, timeout=5,
            )
            local_branch = (result.stdout or "").strip()
            if local_branch and local_branch != "HEAD":
                self._apply_branch_options([], local_branch)
        except Exception:
            pass

    def _scan_distro_options(self) -> list[tuple[str, str]]:
        """Scan the build directory for available OE distros.

        All three OE flavors follow the same pattern:
          meta-*/conf/distro/*.conf
        e.g. meta-oe-alliance/meta-oe/conf/distro/openatv.conf
             meta-opendreambox/conf/distro/opendreambox.conf
             meta-openpli/conf/distro/openpli.conf

        Skips: openembedded-core defaults, include-only files (*-common,
        reporefs), build/sstate cache dirs.
        """
        # Helper configs that are includes, not standalone distros
        _SKIP_NAMES = {"defaultsetup", "reporefs"}
        _SKIP_SUFFIXES = ("-common",)

        build_dir = self._cfg.build_dir
        if build_dir:
            repo_path = Path(build_dir)
            if not repo_path.is_absolute():
                try:
                    repo_path = self.app.paths.root / repo_path
                except Exception:
                    repo_path = Path(build_dir)
            if repo_path.is_dir():
                distros = set()
                # Targeted globs at known depths — avoids rglob over
                # 100+ GB build/sstate trees (which takes 10+ seconds).
                for pattern in (
                    "meta-*/conf/distro/*.conf",           # opendreambox, openpli
                    "meta-*/meta-*/conf/distro/*.conf",    # oe-alliance (meta-oe-alliance/meta-oe/...)
                ):
                    for conf in repo_path.glob(pattern):
                        name = conf.stem
                        if name in _SKIP_NAMES:
                            continue
                        if any(name.endswith(s) for s in _SKIP_SUFFIXES):
                            continue
                        distros.add(name)
                if distros:
                    return [(d, d) for d in sorted(distros)]
        # Fallback: just the configured distro
        d = self._cfg.distro
        if d:
            return [(d, d)]
        return [("openatv", "openatv")]

    def _refresh_distro_select(self) -> None:
        """Re-scan distros and update the Select widget."""
        try:
            sel = self.query_one("#oesdk-distro", Select)
            current = str(sel.value) if sel.value else ""
            opts = self._scan_distro_options()
            sel.set_options(opts)
            if current and any(v == current for _, v in opts):
                sel.value = current
            elif opts:
                sel.value = opts[0][1]
        except Exception:
            pass

    def _update_clone_button(self) -> None:
        """Update Clone/Update label based on whether build dir has a .git repo."""
        try:
            build_dir = self.query_one("#oesdk-build-dir", Input).value.strip()
            btn = self.query_one("#oesdk-clone", Button)
        except Exception:
            return
        if not build_dir:
            btn.label = t("clone")
            return
        repo_path = Path(build_dir)
        if not repo_path.is_absolute():
            repo_path = self.app.paths.root / repo_path
        btn.label = t("update") if (repo_path / ".git").is_dir() else t("clone")

    def _update_build_dir(self, tc_name: str) -> None:
        """Keep build dir in sync if it still holds an auto-generated value.

        Only overwrite the field when it is empty or exactly equals
        the value we last auto-set ourselves. The previous heuristic
        ``cur.startswith(base + "/")`` was too broad — it treated any
        path under ``oesdk_dir`` as auto, so a user-supplied directory
        like ``/home/x/oe-alliance/oatv-8.0`` got mangled into
        ``…/oatv-8.0/<toolchain_name>`` whenever the toolchain name
        was rebuilt.
        """
        try:
            bd = self.query_one("#oesdk-build-dir", Input)
            cur = bd.value.strip()
            base = self._oesdk_base
            previous_auto = getattr(self, "_last_auto_build_dir", "")
            new_value = f"{base}/{tc_name}" if tc_name else base
            if cur and cur != previous_auto and cur != base:
                # User-edited — leave it alone.
                self._update_clone_button()
                return
            bd.value = new_value
            self._last_auto_build_dir = new_value
            self._update_clone_button()
        except Exception:
            pass

    # ----------------------------------------------------------------
    # Browse / Commits
    # ----------------------------------------------------------------

    def _show_repo_commits(self) -> None:
        """Open CommitLogModal for the repo in the build directory."""
        try:
            build_dir = self.query_one("#oesdk-build-dir", Input).value.strip()
        except Exception:
            return
        if not build_dir:
            self.notify("Set Build Directory first", severity="warning")
            return
        repo_path = Path(build_dir)
        if not repo_path.is_absolute():
            repo_path = self.app.paths.root / repo_path
        if not (repo_path / ".git").is_dir():
            self.notify(f"No git repo at {repo_path}", severity="warning")
            return
        branch = self._get_branch_value()
        from ui.screens.commit_log import CommitLogModal
        self.app.push_screen(
            CommitLogModal(repo_path=repo_path, vcs="git", branch=branch),
        )

    def _clone_repo(self) -> None:
        """Clone or update the repository in the build directory."""
        try:
            url = self.query_one("#oesdk-repo-url", Input).value.strip()
            build_dir = self.query_one("#oesdk-build-dir", Input).value.strip()
            branch = self._get_branch_value()
        except Exception:
            return
        if not url:
            self.notify("Repository URL is required", severity="warning")
            return
        if not build_dir:
            self.notify("Build Directory is required", severity="warning")
            return
        repo_path = Path(build_dir)
        if not repo_path.is_absolute():
            repo_path = self.app.paths.root / repo_path
        is_update = (repo_path / ".git").is_dir()

        # Disable button during operation
        try:
            btn = self.query_one("#oesdk-clone", Button)
            btn.disabled = True
        except Exception:
            pass
        import subprocess

        def _set_status(msg: str) -> None:
            """Update the status line from any thread."""
            def _do():
                try:
                    self.query_one("#oesdk-status", Static).update(msg)
                except Exception:
                    pass
            self.app.call_from_thread(_do)

        def do_work():
            try:
                if is_update:
                    steps = [(["make", "update"], "Running make update (updating layers)...")]
                    ok = True
                    last_err = ""
                    for cmd, status_msg in steps:
                        _set_status(f"[yellow]{status_msg}[/yellow]")
                        r = subprocess.run(
                            cmd, cwd=str(repo_path), capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=600,
                        )
                        if r.returncode != 0:
                            last_err = (r.stderr or "").strip().splitlines()[-1] if r.stderr else "unknown error"
                            ok = False
                            break
                else:
                    # git clone
                    _set_status("[yellow]Cloning repository...[/yellow]")
                    repo_path.mkdir(parents=True, exist_ok=True)
                    cmd = ["git", "clone"]
                    if branch:
                        cmd += ["-b", branch]
                    cmd += [url, str(repo_path)]
                    r = subprocess.run(
                        cmd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=600,
                    )
                    ok = r.returncode == 0
                    last_err = (r.stderr or "").strip().splitlines()[-1] if r.stderr else "unknown error"
                    # After clone: make update to pull submodules / layers
                    if ok:
                        _set_status("[yellow]Running make update (updating layers)...[/yellow]")
                        r2 = subprocess.run(
                            ["make", "update"], cwd=str(repo_path),
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600,
                        )
                        if r2.returncode != 0:
                            last_err = (r2.stderr or "").strip().splitlines()[-1] if r2.stderr else "make update failed"
                            ok = False

                def on_done():
                    try:
                        btn = self.query_one("#oesdk-clone", Button)
                        btn.disabled = False
                        if ok and not is_update:
                            btn.label = t("update")
                    except Exception:
                        pass
                    try:
                        status = self.query_one("#oesdk-status", Static)
                        if ok:
                            action = "Updated" if is_update else "Cloned"
                            status.update(f"[green]{action} successfully[/green]")
                        else:
                            status.update(f"[red]Failed: {last_err}[/red]")
                    except Exception:
                        pass
                    if ok:
                        action = "Updated" if is_update else "Cloned"
                        self.notify(f"{action}: {repo_path.name}", severity="information")
                        # Refresh distro dropdown from newly cloned/updated repo
                        self._refresh_distro_select()
                    else:
                        action = "Update" if is_update else "Clone"
                        self.notify(f"{action} failed: {last_err}", severity="error", timeout=10)
                self.app.call_from_thread(on_done)
            except Exception as e:
                def on_err():
                    try:
                        self.query_one("#oesdk-clone", Button).disabled = False
                    except Exception:
                        pass
                    self.notify(f"Failed: {e}", severity="error", timeout=10)
                self.app.call_from_thread(on_err)

        threading.Thread(target=do_work, daemon=True).start()

    def _browse_build_dir(self) -> None:
        """Open file picker for the build directory."""
        from ui.screens.toolchains import FilePickerScreen
        try:
            current = self.query_one("#oesdk-build-dir", Input).value.strip()
        except Exception:
            current = ""
        start = current or str(self.app.paths.root)
        self.app.push_screen(
            FilePickerScreen(
                start_path=start,
                title="OE Build Directory",
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
                self.query_one("#oesdk-build-dir", Input).value = str(path)
            except Exception:
                pass

    # ----------------------------------------------------------------
    # Save
    # ----------------------------------------------------------------

    def _do_save(self) -> None:
        branch = self._get_branch_value()
        if not branch:
            self.notify("Branch is required — press ↻ to fetch", severity="error")
            return
        try:
            build_dir = self.query_one("#oesdk-build-dir", Input).value.strip()
            repo_url = self.query_one("#oesdk-repo-url", Input).value.strip()
            try:
                machine_w = self.query_one("#oesdk-machine", Select)
                machine = str(machine_w.value) if machine_w.value else ""
            except Exception:
                machine = self.query_one("#oesdk-machine", Input).value.strip()
            try:
                distro = str(self.query_one("#oesdk-distro", Select).value or "")
            except Exception:
                distro = ""
            sdk_extras = self.query_one("#oesdk-sdk-extras", Input).value.strip()
            tc_name = self.query_one("#oesdk-tc-name", Input).value.strip()
        except Exception as e:
            self.notify(f"Error reading fields: {e}", severity="error")
            return
        if not repo_url:
            self.notify("Repository URL is required", severity="error")
            return
        if not machine:
            self.notify("Machine is required", severity="error")
            return
        if not tc_name:
            self.notify("Toolchain name is required", severity="error")
            return

        cfg = OeSdkConfig(
            machine=machine,
            distro=distro,
            repo_url=repo_url,
            repo_branch=branch,
            build_dir=build_dir or "data/oe-sdk",
            sdk_extras=sdk_extras,
        )
        cfg._tc_name = tc_name  # type: ignore[attr-defined]
        self.dismiss(cfg)

    def action_cancel(self) -> None:
        self.dismiss(None)
