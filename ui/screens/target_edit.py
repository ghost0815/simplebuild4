"""Target editor screen — create or edit a target configuration."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Switch, Input, Collapsible, Select
from textual.containers import Horizontal, Vertical, VerticalScroll, Grid

from core.target import Target, BuildSettings, UploadConfig, SourceConfig, ToolchainConfig
from core.toolchain import ToolchainRegistry
from core.repos_config import load_repos
from core.oscam_config import read_options, merge_options, OscamOptions
from ui.header import S4Header
from ui.footer import S4Footer


def _switch_row(label: str, value: bool, widget_id: str) -> Horizontal:
    return Horizontal(
        Static(f" {label}", classes="s-label"),
        Switch(value=value, id=widget_id),
        classes="s-toggle",
    )


def _safe_id(name: str) -> str:
    """Convert a name to a safe Textual widget ID (no dots)."""
    return name.replace(".", "-")


def _switch_compact(label: str, value: bool, widget_id: str, removed: bool = False) -> Horizontal:
    """Compact switch for multi-column grid layout."""
    display_label = f" [red strike]{label}[/red strike]" if removed else f" {label}"
    return Horizontal(
        Switch(value=value, id=widget_id),
        Static(display_label, classes="sc-label"),
        classes="s-compact",
    )


class TargetEditScreen(Screen):
    """Edit or create a target configuration."""

    BINDINGS = [
        ("1", "switch_tab('general')", "General"),
        ("2", "switch_tab('source')", "Repo/Toolchain"),
        ("3", "switch_tab('oscam')", "Options"),
        ("4", "switch_tab('upload')", "Upload"),
    ]

    _collapsible_states: dict[str, bool] = {}  # class-level: id → collapsed

    def __init__(self, target: Target | None = None, save_path: Path | None = None, active_tab: str = "general", focus_id: str = "") -> None:
        super().__init__()
        self.target = target or Target()
        self.save_path = save_path
        self._oscam_opts: OscamOptions | None = None
        self._active_tab = active_tab
        self._focus_id = focus_id
        self._tc_defaults: dict[str, dict] = {}  # sid → {cc, cf, ld, mv}

    def _load_oscam_options(self) -> OscamOptions:
        """Load and merge oscam options from all assigned source repos."""
        if self._oscam_opts:
            return self._oscam_opts
        repos = load_repos(self.app.paths.repos_config)
        repo_dirs = []

        # Collect repo dirs from assigned sources only
        src_names = [s.name for s in self.target.sources]
        for src_name in src_names:
            for r in repos:
                if r.name == src_name:
                    for d in self._find_repo_dirs(r.name):
                        if d not in repo_dirs:
                            repo_dirs.append(d)

        self._oscam_opts = merge_options(repo_dirs) if repo_dirs else OscamOptions()
        return self._oscam_opts

    def _find_repo_dirs(self, name: str) -> list[Path]:
        """Find possible repo directories for a given repo name."""
        repos_dir = self.app.paths.repos_dir
        candidates = [
            repos_dir / name,
            repos_dir / f"oscam-{name}",
        ]
        # Also check any directory containing the name
        if repos_dir.is_dir():
            for d in repos_dir.iterdir():
                if d.is_dir() and name in d.name and d not in candidates:
                    candidates.append(d)
        return [d for d in candidates if d.is_dir()]

    def compose(self) -> ComposeResult:
        t = self.target
        b = t.build
        u = t.upload[0] if t.upload else UploadConfig()

        repos = load_repos(self.app.paths.repos_config)
        registry = ToolchainRegistry(
            self.app.paths.toolchains_defaults,
            self.app.paths.toolchains_local,
            self.app.paths.toolchains_dir,
        )
        registry.load()
        installed = registry.installed_toolchains()
        tc_names = [tc.name for tc in t.toolchains]
        tc_map = {tc.name: tc for tc in t.toolchains}
        oscam = self._load_oscam_options()

        yield S4Header()

        # Tab bar
        with Horizontal(id="te-tab-bar"):
            yield Static(" General ", id="te-tab-general", classes="tab-active")
            yield Static(" Repo/Toolchain", id="te-tab-source", classes="tab-inactive")
            yield Static(" Options", id="te-tab-oscam", classes="tab-inactive")
            yield Static(" Upload ", id="te-tab-upload", classes="tab-inactive")

        # === Tab 1: General ===
        # UPX detection (once)
        import shutil as _shutil
        import subprocess as _sp
        self._upx_path = _shutil.which("upx") or ""
        self._upx_info = ""
        if self._upx_path:
            try:
                _upx_ver = _sp.check_output([self._upx_path, "--version"], text=True, stderr=_sp.DEVNULL, timeout=5).splitlines()[0]
                self._upx_info = f" [dim]{self._upx_path} — {_upx_ver}[/dim]"
            except Exception:
                self._upx_info = f" [dim]{self._upx_path}[/dim]"

        has_source = bool(oscam.addons)  # True if any source repo is assigned and cloned

        with VerticalScroll(id="te-panel-general"):
            with Collapsible(title="Common", collapsed=False, id="col-common"):
                yield Horizontal(
                    Static(" Name", classes="s-label-tall"),
                    Input(value=t.name, placeholder="e.g. DM920", id="t-name"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(" Description", classes="s-label-tall"),
                    Input(value=t.description, placeholder="e.g. Dreambox 920 living room", id="t-desc"),
                    classes="s-row",
                )
                compress_enabled = b.use_compress and bool(self._upx_path)
                upx_switch = Switch(value=compress_enabled, id="f-use-compress", disabled=not bool(self._upx_path))
                upx_text = self._upx_info.strip() if self._upx_path else "upx not found"
                upx_info = Static(f" [dim]{upx_text}[/dim]")
                upx_info.styles.padding = (1, 0)
                upx_info.styles.width = "auto"
                if compress_enabled:
                    lvl_inp = Input(value=b.compress_level, placeholder="--best", id="t-compress-level")
                    lvl_inp.styles.max_width = 25
                    yield Horizontal(
                        Static(" USE_COMPRESS", classes="flag-label"),
                        upx_switch,
                        upx_info,
                        Static("", classes="spacer"),
                        Static(" Compression level", classes="src-opt-label"),
                        lvl_inp,
                        classes="flag-row",
                    )
                else:
                    yield Horizontal(
                        Static(" USE_COMPRESS", classes="flag-label"),
                        upx_switch,
                        upx_info,
                        classes="flag-row",
                    )
                yield Horizontal(
                    Static(" Build system", classes="s-label-tall"),
                    Select(
                        options=[("make", "make"), ("cmake", "cmake")],
                        value=b.build_system or "make",
                        id="t-build-system",
                    ),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(" Binary name", classes="s-label-tall"),
                    Input(value=b.use_oscamname, placeholder="empty = default", id="t-oscamname"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(" Config directory", classes="s-label-tall"),
                    Input(value=b.confdir, id="t-confdir"),
                    classes="s-row",
                )
            if has_source:
                with Collapsible(title="Library + Static Flags", collapsed=True, id="col-lib-flags"):
                    yield Horizontal(
                        Static(" USE_LIBCRYPTO", classes="flag-label"), Switch(value=b.use_libcrypto, id="f-use-libcrypto"),
                        Static(" STATIC_LIBCRYPTO", classes="flag-label"), Switch(value=b.static_libcrypto, id="f-static-libcrypto"),
                        classes="flag-row",
                    )
                    yield Horizontal(
                        Static(" USE_SSL", classes="flag-label"), Switch(value=b.use_ssl, id="f-use-ssl"),
                        Static(" STATIC_SSL", classes="flag-label"), Switch(value=b.static_ssl, id="f-static-ssl"),
                        classes="flag-row",
                    )
                    yield Horizontal(
                        Static(" USE_LIBUSB", classes="flag-label"), Switch(value=b.use_libusb, id="f-use-libusb"),
                        Static(" STATIC_LIBUSB", classes="flag-label"), Switch(value=b.static_libusb, id="f-static-libusb"),
                        classes="flag-row",
                    )
                    yield Horizontal(
                        Static(" USE_PCSC", classes="flag-label"), Switch(value=b.use_pcsc, id="f-use-pcsc"),
                        Static(" STATIC_PCSC", classes="flag-label"), Switch(value=b.static_pcsc, id="f-static-pcsc"),
                        classes="flag-row",
                    )
                    yield Horizontal(
                        Static(" USE_LIBDVBCSA", classes="flag-label"), Switch(value=b.use_libdvbcsa, id="f-use-libdvbcsa"),
                        Static(" STATIC_LIBDVBCSA", classes="flag-label"), Switch(value=b.static_libdvbcsa, id="f-static-libdvbcsa"),
                        classes="flag-row",
                    )

                with Collapsible(title="Hardware Flags", collapsed=True, id="col-hw-flags"):
                    # STAPI row — with optional STAPI_LIB field inline
                    _stapi5_sw = Switch(value=b.use_stapi5, id="f-use-stapi5")
                    _stapi_widgets = [
                        Static(" USE_STAPI", classes="flag-label"), Switch(value=b.use_stapi, id="f-use-stapi"),
                        Static(" USE_STAPI5", classes="flag-label"), _stapi5_sw,
                    ]
                    if b.use_stapi or b.use_stapi5:
                        _stapi5_sw.styles.margin = (0, 1, 0, 0)
                        _stapi_default = ""
                        for _tc in installed:
                            if _tc.name in tc_names:
                                _stapi_default = _tc.stapi_lib or ""
                                if _stapi_default:
                                    break
                        _stapi_val = b.stapi_lib if b.stapi_lib else _stapi_default
                        _stapi_is_default = (_stapi_val == _stapi_default)
                        from textual.widgets import Button as _Btn
                        _stapi_inp = Input(value=_stapi_val, placeholder="e.g. liboscam_stapi.a", id="f-stapi-lib")
                        _stapi_inp.styles.max_width = 25
                        if _stapi_is_default:
                            _stapi_inp.add_class("tc-default")
                        _stapi_lbl = Static(" STAPI_LIB", classes="tc-field-label")
                        _stapi_lbl.tooltip = "Path to precompiled STAPI library. Passed as STAPI_LIB= or STAPI5_LIB= to make."
                        _stapi_rst = _Btn("↺", id="rst-stapi-lib", classes="tc-reset-btn")
                        _stapi_rst.tooltip = "Load toolchain default"
                        self._stapi_default = _stapi_default
                        _stapi_widgets += [_stapi_lbl, _stapi_inp, _stapi_rst]
                    yield Horizontal(*_stapi_widgets, classes="flag-row")
                    yield Horizontal(
                        Static(" USE_COOLAPI", classes="flag-label"), Switch(value=b.use_coolapi, id="f-use-coolapi"),
                        Static(" USE_COOLAPI2", classes="flag-label"), Switch(value=b.use_coolapi2, id="f-use-coolapi2"),
                        classes="flag-row",
                    )
                    yield Horizontal(
                        Static(" USE_SU980", classes="flag-label"), Switch(value=b.use_su980, id="f-use-su980"),
                        Static(" USE_AZBOX", classes="flag-label"), Switch(value=b.use_azbox, id="f-use-azbox"),
                        classes="flag-row",
                    )
                    yield Horizontal(
                        Static(" USE_AMSMC", classes="flag-label"), Switch(value=b.use_amsmc, id="f-use-amsmc"),
                        Static(" USE_MCA", classes="flag-label"), Switch(value=b.use_mca, id="f-use-mca"),
                        classes="flag-row",
                    )
            else:
                yield Static("  [dim]Select a source repository to configure build flags[/dim]")

        # === Tab 2: Repo/Toolchain===
        with VerticalScroll(id="te-panel-source"):
            # Sources
            src_names = [s.name for s in t.sources]
            src_map = {s.name: s for s in t.sources}
            src_count = len(t.sources)
            # Available patches
            patches_dir = self.app.paths.patches_dir
            available_patches = sorted(p.name for p in patches_dir.glob("*.patch")) if patches_dir.is_dir() else []

            with Collapsible(title=f"Sources ({src_count} selected)", collapsed=False, id="col-sources"):
                for r in repos:
                    enabled = r.name in src_names
                    sc = src_map.get(r.name, SourceConfig(name=r.name))
                    rid = _safe_id(r.name)
                    src_label = Static(f" {r.name}\n [dim]{r.url}[/dim]", classes="src-name")
                    yield Horizontal(
                        src_label,
                        Switch(value=enabled, id=f"src-{rid}"),
                        *([
                            Static(" [dim]Update before build[/dim]", classes="src-opt-label"),
                            Switch(value=sc.update_before_build, id=f"src-upd-{rid}"),
                            Static(" [dim]Patch before build[/dim]", classes="src-opt-label"),
                            Switch(value=sc.patch_before_build, id=f"src-patch-{rid}"),
                        ] if enabled else []),
                        classes="src-row",
                    )
                    if enabled and sc.patch_before_build and available_patches:
                        with Grid(classes="mod-grid patch-grid"):
                            for patch in available_patches:
                                pid = _safe_id(patch.replace(".patch", ""))
                                selected = patch in sc.patches
                                yield _switch_compact(patch, selected, f"p-{rid}-{pid}")
                if not repos:
                    yield Static("  [dim]No repositories configured — use F3 Repositories first[/dim]")

            # Toolchains
            tc_count = len(t.toolchains)
            with Collapsible(title=f"Toolchains ({tc_count} selected)", collapsed=False, id="col-toolchains"):
                for tc in installed:
                    enabled = tc.name in tc_names
                    tcc = tc_map.get(tc.name, ToolchainConfig(name=tc.name))
                    if tc.native:
                        label = f" [bold]{tc.name}[/bold] [dim]({tc.arch})[/dim]"
                    elif tc.arch:
                        label = f" {tc.name} [dim]({tc.arch})[/dim]"
                    else:
                        label = f" {tc.name}"
                    yield Horizontal(
                        Static(label, classes="src-name"),
                        Switch(value=enabled, id=f"tc-{_safe_id(tc.name)}"),
                        classes="src-row",
                    )
                    if enabled:
                        # Per-toolchain build flags (only shown when active)
                        sid = _safe_id(tc.name)
                        def_cc = tc.cc_opts or ""
                        def_cf = tc.cflags or ""
                        def_ld = tc.ldflags or ""
                        def_mv = tc.make_vars or ""
                        from textual.widgets import Button as Btn

                        def _tc_input(val: str, default: str, input_id: str) -> Input:
                            """Create input: show override or default, style accordingly."""
                            display = val if val else default
                            is_override = val != "" and val != default
                            inp = Input(value=display, id=input_id)
                            if not is_override:
                                inp.add_class("tc-default")
                            return inp

                        lbl_cc = Static("  [dim]CC_OPTS[/dim]", classes="tc-field-label")
                        lbl_cc.tooltip = "Compiler optimization flags (e.g. -O2, -ggdb)"
                        lbl_cf = Static("  [dim]EXTRA_CFLAGS[/dim]", classes="tc-field-label")
                        lbl_cf.tooltip = "Architecture-specific C flags (e.g. -mfpu=neon, -mcpu=cortex-a15)"
                        lbl_ld = Static("  [dim]EXTRA_LDFLAGS[/dim]", classes="tc-field-label")
                        lbl_ld.tooltip = "Linker flags (e.g. -Wl,--dynamic-linker=...)"
                        lbl_mv = Static("  [dim]EXTRA_MAKE[/dim]", classes="tc-field-label")
                        lbl_mv.tooltip = "Additional make variable assignments (e.g. LIB_RT= EXTRA_TARGET=-mybox)"
                        def _rst_btn(bid: str) -> "Btn":
                            b = Btn("↺", id=bid, classes="tc-reset-btn")
                            b.tooltip = "Load toolchain default"
                            return b

                        yield Horizontal(
                            lbl_cf,
                            _tc_input(tcc.cflags, def_cf, f"tc-cf-{sid}"),
                            _rst_btn(f"rst-cf-{sid}"),
                            lbl_cc,
                            _tc_input(tcc.cc_opts, def_cc, f"tc-cc-{sid}"),
                            _rst_btn(f"rst-cc-{sid}"),
                            classes="tc-fields-row",
                        )
                        yield Horizontal(
                            lbl_ld,
                            _tc_input(tcc.ldflags, def_ld, f"tc-ld-{sid}"),
                            _rst_btn(f"rst-ld-{sid}"),
                            lbl_mv,
                            _tc_input(tcc.make_vars, def_mv, f"tc-mv-{sid}"),
                            _rst_btn(f"rst-mv-{sid}"),
                            classes="tc-fields-row",
                        )
                        # Store defaults for reset
                        self._tc_defaults[sid] = {"cc": def_cc, "cf": def_cf, "ld": def_ld, "mv": def_mv}
                if not installed:
                    yield Static("  [dim]No toolchains installed — use F4 Toolchains first[/dim]")

        # === Tab 2: Options (oscam modules) ===
        # Merge target config with oscam options:
        # - New target (empty): use oscam defaults
        # - Existing target: keep saved state, new options get oscam default
        # - Removed options: warn user
        is_new = not b.modules and not b.protocols and not b.readers and not b.card_readers
        self._removed_opts = {}  # category → list of removed option names

        def _merge(saved: list[str], available: list[str], defaults: list[str], category: str) -> dict[str, bool]:
            """Merge saved target config with available oscam options."""
            result = {}
            for opt in available:
                if is_new:
                    result[opt] = opt in defaults
                elif opt in saved:
                    result[opt] = True
                elif opt not in saved and opt in defaults:
                    result[opt] = True
                else:
                    result[opt] = False
            # Detect removed options (saved but no longer in any repo)
            removed = [opt for opt in saved if opt not in available and available]
            if removed:
                self._removed_opts[category] = removed
            return result

        merged_addons = _merge(b.modules, oscam.addons, oscam.default_enabled.get("addons", []), "addons")
        merged_protos = _merge(b.protocols, oscam.protocols, oscam.default_enabled.get("protocols", []), "protocols")
        merged_readers = _merge(b.readers, oscam.readers, oscam.default_enabled.get("readers", []), "readers")
        merged_crs = _merge(b.card_readers, oscam.card_readers, oscam.default_enabled.get("card_readers", []), "card_readers")

        with VerticalScroll(id="te-panel-oscam"):
            addons_sorted = sorted(oscam.addons)
            removed_addons = sorted(self._removed_opts.get("addons", []))
            all_addons = addons_sorted + removed_addons
            addon_count = sum(1 for m in all_addons if merged_addons.get(m) or m in removed_addons)
            with Collapsible(title=f"Addons ({addon_count}/{len(addons_sorted)})", collapsed=False, id="col-addons"):
                if oscam.addons or removed_addons:
                    with Grid(classes="mod-grid"):
                        for mod in addons_sorted:
                            yield _switch_compact(mod, merged_addons.get(mod, False), f"m-{mod}")
                        for mod in removed_addons:
                            yield _switch_compact(mod, True, f"m-{mod}", removed=True)
                else:
                    yield Static("  [dim]Select a source repository first[/dim]")

            protos_sorted = sorted(oscam.protocols)
            removed_protos = sorted(self._removed_opts.get("protocols", []))
            proto_count = sum(1 for m in protos_sorted if merged_protos.get(m))
            with Collapsible(title=f"Protocols ({proto_count}/{len(protos_sorted)})", collapsed=False, id="col-protocols"):
                with Grid(classes="mod-grid"):
                    for mod in protos_sorted:
                        yield _switch_compact(mod, merged_protos.get(mod, False), f"m-{mod}")
                    for mod in removed_protos:
                        yield _switch_compact(mod, True, f"m-{mod}", removed=True)

            readers_sorted = sorted(oscam.readers)
            removed_readers = sorted(self._removed_opts.get("readers", []))
            reader_count = sum(1 for m in readers_sorted if merged_readers.get(m))
            with Collapsible(title=f"Readers ({reader_count}/{len(readers_sorted)})", collapsed=False, id="col-readers"):
                with Grid(classes="mod-grid"):
                    for mod in readers_sorted:
                        yield _switch_compact(mod, merged_readers.get(mod, False), f"m-{mod}")
                    for mod in removed_readers:
                        yield _switch_compact(mod, True, f"m-{mod}", removed=True)

            crs_sorted = sorted(oscam.card_readers)
            removed_crs = sorted(self._removed_opts.get("card_readers", []))
            cr_count = sum(1 for m in crs_sorted if merged_crs.get(m))
            with Collapsible(title=f"Card Readers ({cr_count}/{len(crs_sorted)})", collapsed=False, id="col-card-readers"):
                with Grid(classes="mod-grid"):
                    for mod in crs_sorted:
                        yield _switch_compact(mod, merged_crs.get(mod, False), f"m-{mod}")
                    for mod in removed_crs:
                        yield _switch_compact(mod, True, f"m-{mod}", removed=True)

            # Cleanup button (only show if there are removed options)
            if self._removed_opts:
                from textual.widgets import Button
                yield Horizontal(
                    Static("", classes="spacer"),
                    Button("Cleanup", id="btn-cleanup", variant="primary"),
                    classes="s-row",
                )

        # === Tab 3: Upload ===
        with VerticalScroll(id="te-panel-upload"):
            upload_count = len(t.upload)
            with Collapsible(title=f"Destination ({upload_count})", collapsed=False, id="col-upload"):
                yield Horizontal(
                    Static(" Host", classes="s-label-tall"),
                    Input(value=u.host, placeholder="e.g. 192.168.1.50", id="t-up-host"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(" User", classes="s-label-tall"),
                    Input(value=u.user, id="t-up-user"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(" Port", classes="s-label-tall"),
                    Input(value=str(u.port), id="t-up-port", type="integer"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(" SSH key", classes="s-label-tall"),
                    Input(value=u.key, placeholder="~/.ssh/id_rsa", id="t-up-key"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(" Target path", classes="s-label-tall"),
                    Input(value=u.target_path, id="t-up-path"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(" Pre command", classes="s-label-tall"),
                    Input(value=u.pre_cmd, placeholder="e.g. killall oscam", id="t-up-pre"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(" Post command", classes="s-label-tall"),
                    Input(value=u.post_cmd, placeholder="e.g. /usr/bin/oscam -b", id="t-up-post"),
                    classes="s-row",
                )

        yield S4Footer(active_screen="target_edit")

    def on_mount(self) -> None:
        self._show_tab(self._active_tab)
        # Restore collapsible states from previous screen instance
        for cid, collapsed in TargetEditScreen._collapsible_states.items():
            try:
                self.query_one(f"#{cid}", Collapsible).collapsed = collapsed
            except Exception:
                pass
        if self._focus_id:
            self.set_timer(0.1, self._restore_focus)

    def _restore_focus(self) -> None:
        try:
            self.query_one(f"#{self._focus_id}", Switch).focus()
        except Exception:
            pass

    def _save_collapsible_states(self) -> None:
        """Save current collapsible states before screen refresh."""
        for col in self.query(Collapsible):
            if col.id:
                TargetEditScreen._collapsible_states[col.id] = col.collapsed

    def _refresh_screen(self, target: Target, active_tab: str, focus_id: str = "") -> None:
        """Save state and refresh the screen."""
        self._save_collapsible_states()
        self.app.pop_screen()
        self.app.push_screen(TargetEditScreen(target=target, save_path=self.save_path, active_tab=active_tab, focus_id=focus_id))

    def on_click(self, event) -> None:
        widget = event.widget if hasattr(event, 'widget') else None
        if widget:
            if widget.id == "te-tab-general":
                self._show_tab("general")
            elif widget.id == "te-tab-source":
                self._show_tab("source")
            elif widget.id == "te-tab-oscam":
                self._show_tab("oscam")
            elif widget.id == "te-tab-upload":
                self._show_tab("upload")

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """When a source or toolchain switch changes, refresh to update UI."""
        sid = event.switch.id or ""
        is_source = sid.startswith("src-") and not sid.startswith("src-upd-") and not sid.startswith("src-patch-")
        is_patch_toggle = sid.startswith("src-patch-")
        is_toolchain = sid.startswith("tc-") and not sid.startswith("tc-cc-") and not sid.startswith("tc-cf-") and not sid.startswith("tc-ld-") and not sid.startswith("tc-mv-")
        if is_source or is_patch_toggle or is_toolchain:
            self._refresh_screen(self._collect(), "source", sid)
            return

        # Library + Static flag dependencies
        _lib_pairs = {
            "f-use-libcrypto": "f-static-libcrypto",
            "f-use-ssl": "f-static-ssl",
            "f-use-libusb": "f-static-libusb",
            "f-use-pcsc": "f-static-pcsc",
            "f-use-libdvbcsa": "f-static-libdvbcsa",
        }
        _static_to_use = {v: k for k, v in _lib_pairs.items()}

        # USE deactivated → also deactivate STATIC
        if sid in _lib_pairs and not event.value:
            try:
                self.query_one(f"#{_lib_pairs[sid]}", Switch).value = False
            except Exception:
                pass

        # STATIC activated → also activate USE
        if sid in _static_to_use and event.value:
            try:
                self.query_one(f"#{_static_to_use[sid]}", Switch).value = True
            except Exception:
                pass

        # STAPI toggle — refresh to show/hide stapi_lib field
        if sid in ("f-use-stapi", "f-use-stapi5"):
            self._refresh_screen(self._collect(), "general", sid)
            return

        # UPX toggle — refresh to show/hide compress level field
        if sid == "f-use-compress":
            self._refresh_screen(self._collect(), "general", sid)
            return

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-cleanup":
            self._do_cleanup()
        elif event.button.id == "rst-stapi-lib":
            try:
                inp = self.query_one("#f-stapi-lib", Input)
                inp.value = getattr(self, '_stapi_default', '')
                inp.add_class("tc-default")
            except Exception:
                pass
        elif event.button.id and event.button.id.startswith("rst-"):
            self._do_reset_field(event.button.id)

    def _do_reset_field(self, btn_id: str) -> None:
        """Reset a toolchain field to its default value."""
        # btn_id format: rst-{field}-{sid}
        parts = btn_id.split("-", 2)
        if len(parts) < 3:
            return
        field = parts[1]  # cc, cf, ld, mv
        sid = parts[2]
        defaults = self._tc_defaults.get(sid, {})
        if field not in ("cc", "cf", "ld", "mv"):
            return
        default_val = defaults.get(field, "")
        try:
            inp = self.query_one(f"#tc-{field}-{sid}", Input)
            inp.value = default_val
            inp.add_class("tc-default")
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        """Track when user modifies a toolchain field — switch to override style."""
        iid = event.input.id or ""
        if iid.startswith("tc-cc-") or iid.startswith("tc-cf-") or iid.startswith("tc-ld-") or iid.startswith("tc-mv-"):
            # Determine if value differs from default
            parts = iid.split("-", 2)
            field = parts[1]  # cc, cf, ld, mv
            sid = parts[2]
            defaults = self._tc_defaults.get(sid, {})
            default_val = defaults.get(field, "")
            if event.value != default_val:
                event.input.remove_class("tc-default")
            else:
                event.input.add_class("tc-default")

        # STAPI_LIB field style tracking
        if iid == "f-stapi-lib":
            default_val = getattr(self, '_stapi_default', '')
            if event.value != default_val:
                event.input.remove_class("tc-default")
            else:
                event.input.add_class("tc-default")

    def _do_cleanup(self) -> None:
        """Remove stale options that no longer exist in oscam."""
        if not self._removed_opts:
            self.notify("Nothing to clean up", severity="information")
            return
        count = sum(len(v) for v in self._removed_opts.values())
        # Collect current state without removed options
        target = self._collect()
        oscam = self._load_oscam_options()
        # Filter out removed options from the collected target
        target.build.modules = [m for m in target.build.modules if m in oscam.addons]
        target.build.protocols = [m for m in target.build.protocols if m in oscam.protocols]
        target.build.readers = [m for m in target.build.readers if m in oscam.readers]
        target.build.card_readers = [m for m in target.build.card_readers if m in oscam.card_readers]
        self._refresh_screen(target, "oscam")
        self.notify(f"Cleaned up {count} removed option(s) — save to apply", severity="information")

    def action_switch_tab(self, tab: str) -> None:
        self._show_tab(tab)

    def _show_tab(self, tab: str) -> None:
        panels = {
            "general": ("#te-panel-general", "#te-tab-general"),
            "source": ("#te-panel-source", "#te-tab-source"),
            "oscam": ("#te-panel-oscam", "#te-tab-oscam"),
            "upload": ("#te-panel-upload", "#te-tab-upload"),
        }
        for key, (panel_id, tab_id) in panels.items():
            try:
                self.query_one(panel_id).display = (key == tab)
                self.query_one(tab_id).set_classes("tab-active" if key == tab else "tab-inactive")
            except Exception:
                pass

    def _collect(self) -> Target:
        """Collect all values from widgets into a Target."""
        name = self.query_one("#t-name", Input).value.strip()
        desc = self.query_one("#t-desc", Input).value.strip()

        # Collect selected sources with per-source options
        repos = load_repos(self.app.paths.repos_config)
        sources = []
        for r in repos:
            try:
                rid = _safe_id(r.name)
                if self.query_one(f"#src-{rid}", Switch).value:
                    patch_on = self._switch_on(f"src-patch-{rid}")
                    # Collect selected patches
                    patches = []
                    if patch_on:
                        patches_dir = self.app.paths.patches_dir
                        if patches_dir.is_dir():
                            for p in sorted(patches_dir.glob("*.patch")):
                                pid = _safe_id(p.name.replace(".patch", ""))
                                if self._switch_on(f"p-{rid}-{pid}"):
                                    patches.append(p.name)
                    sources.append(SourceConfig(
                        name=r.name,
                        update_before_build=self._switch_on(f"src-upd-{rid}"),
                        patch_before_build=patch_on,
                        patches=patches,
                    ))
            except Exception:
                pass

        # Collect selected toolchains
        registry = ToolchainRegistry(
            self.app.paths.toolchains_defaults,
            self.app.paths.toolchains_local,
            self.app.paths.toolchains_dir,
        )
        registry.load()
        toolchains = []
        for tc in registry.installed_toolchains():
            try:
                sid = _safe_id(tc.name)
                if self.query_one(f"#tc-{sid}", Switch).value:
                    # Read override fields — if value == toolchain default, store empty (no override)
                    defaults = self._tc_defaults.get(sid, {})
                    cc = self._input_val(f"tc-cc-{sid}")
                    cf = self._input_val(f"tc-cf-{sid}")
                    ld = self._input_val(f"tc-ld-{sid}")
                    mv = self._input_val(f"tc-mv-{sid}")
                    toolchains.append(ToolchainConfig(
                        name=tc.name,
                        cc_opts="" if cc == defaults.get("cc", "") else cc,
                        cflags="" if cf == defaults.get("cf", "") else cf,
                        ldflags="" if ld == defaults.get("ld", "") else ld,
                        make_vars="" if mv == defaults.get("mv", "") else mv,
                    ))
            except Exception:
                pass

        # Collect enabled modules (dynamically from oscam options)
        # Removed options are shown as switches too — collect them the same way
        oscam = self._load_oscam_options()
        all_addons = oscam.addons + self._removed_opts.get("addons", [])
        all_protos = oscam.protocols + self._removed_opts.get("protocols", [])
        all_readers = oscam.readers + self._removed_opts.get("readers", [])
        all_crs = oscam.card_readers + self._removed_opts.get("card_readers", [])
        modules = [m for m in all_addons if self._switch_on(f"m-{m}")]
        protocols = [m for m in all_protos if self._switch_on(f"m-{m}")]
        readers = [m for m in all_readers if self._switch_on(f"m-{m}")]
        card_readers = [m for m in all_crs if self._switch_on(f"m-{m}")]

        build = BuildSettings(
            modules=modules,
            protocols=protocols,
            readers=readers,
            card_readers=card_readers,
            use_libcrypto=self._switch_on("f-use-libcrypto"),
            use_ssl=self._switch_on("f-use-ssl"),
            use_libusb=self._switch_on("f-use-libusb"),
            use_pcsc=self._switch_on("f-use-pcsc"),
            use_libdvbcsa=self._switch_on("f-use-libdvbcsa"),
            use_stapi=self._switch_on("f-use-stapi"),
            use_stapi5=self._switch_on("f-use-stapi5"),
            use_coolapi=self._switch_on("f-use-coolapi"),
            use_coolapi2=self._switch_on("f-use-coolapi2"),
            use_su980=self._switch_on("f-use-su980"),
            use_azbox=self._switch_on("f-use-azbox"),
            use_amsmc=self._switch_on("f-use-amsmc"),
            use_mca=self._switch_on("f-use-mca"),
            static_libcrypto=self._switch_on("f-static-libcrypto"),
            static_ssl=self._switch_on("f-static-ssl"),
            static_libusb=self._switch_on("f-static-libusb"),
            static_pcsc=self._switch_on("f-static-pcsc"),
            static_libdvbcsa=self._switch_on("f-static-libdvbcsa"),
            use_compress=self._switch_on("f-use-compress"),
            compress_level=self._input_val("t-compress-level") or "--best",
            stapi_lib=self._input_val("f-stapi-lib"),
            use_oscamname=self.query_one("#t-oscamname", Input).value.strip(),
            build_system=self.query_one("#t-build-system", Select).value or "make",
            confdir=self.query_one("#t-confdir", Input).value.strip(),
        )

        # Upload
        host = self.query_one("#t-up-host", Input).value.strip()
        upload = []
        if host:
            try:
                port = int(self.query_one("#t-up-port", Input).value)
            except ValueError:
                port = 22
            upload.append(UploadConfig(
                host=host,
                user=self.query_one("#t-up-user", Input).value.strip() or "root",
                port=port,
                key=self.query_one("#t-up-key", Input).value.strip(),
                target_path=self.query_one("#t-up-path", Input).value.strip(),
                pre_cmd=self.query_one("#t-up-pre", Input).value.strip(),
                post_cmd=self.query_one("#t-up-post", Input).value.strip(),
            ))

        return Target(
            name=name,
            description=desc,
            sources=sources,
            toolchains=toolchains,
            build=build,
            upload=upload,
        )

    def _input_val(self, widget_id: str) -> str:
        try:
            return self.query_one(f"#{widget_id}", Input).value.strip()
        except Exception:
            return ""

    def _switch_on(self, widget_id: str) -> bool:
        try:
            return self.query_one(f"#{widget_id}", Switch).value
        except Exception:
            return False

    def do_save(self) -> str:
        """Save target (called by App)."""
        target = self._collect()
        if not target.name:
            self.notify("Target name is required", severity="error")
            return ""
        path = self.save_path or (self.app.paths.targets_dir / f"{target.name}.yaml")
        target.save(path)
        return f"Target '{target.name}' saved"

    def action_go_back(self) -> None:
        self.app.pop_screen()
