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
from core.oscam_config import read_options, merge_options, OscamOptions, parse_makefile_flags
from translation import t
from ui.header import S4Header
from ui.footer import S4Footer


def _switch_row(label: str, value: bool, widget_id: str) -> Horizontal:
    return Horizontal(
        Static(f" {label}", classes="s-label"),
        Switch(value=value, id=widget_id),
        classes="s-toggle",
    )


def _safe_id(name: str) -> str:
    """Convert a name to a safe Textual widget ID.

    Textual requires IDs to contain only [a-zA-Z0-9_-] and not start with a digit.
    Anything else is replaced with '-'. A leading digit gets an 's' prefix.
    """
    import re as _re
    s = _re.sub(r'[^a-zA-Z0-9_-]', '-', name)
    if s and s[0].isdigit():
        s = "s" + s
    return s


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
        ("g", "switch_tab('general')", "General"),
        ("r", "switch_tab('source')", "Repo/Toolchain"),
        ("o", "switch_tab('oscam')", "Options"),
        ("u", "switch_tab('upload')", "Upload"),
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

    # Hardware flags — everything else goes into Library section
    _HARDWARE_FLAGS = frozenset({
        "STAPI", "STAPI5", "COOLAPI", "COOLAPI2", "SU980", "AZBOX", "AMSMC", "MCA",
    })

    def _load_makefile_flags(self) -> dict:
        """Load USE_*/STATIC_* flags from Makefiles of assigned repos."""
        if hasattr(self, "_makefile_flags") and self._makefile_flags is not None:
            return self._makefile_flags
        repos = load_repos(self.app.paths.repos_config)
        repo_dirs = []
        src_names = [s.name for s in self.target.sources]
        for src_name in src_names:
            for r in repos:
                if r.name == src_name:
                    for d in self._find_repo_dirs(r.name):
                        if d not in repo_dirs:
                            repo_dirs.append(d)
        self._makefile_flags = parse_makefile_flags(repo_dirs) if repo_dirs else {"use": [], "static": []}
        return self._makefile_flags

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
        tgt = self.target
        b = tgt.build
        u = tgt.upload[0] if tgt.upload else UploadConfig()

        repos = load_repos(self.app.paths.repos_config)
        registry = ToolchainRegistry(
            self.app.paths.toolchains_defaults,
            self.app.paths.toolchains_local,
            self.app.paths.toolchains_dir,
        )
        registry.load()
        installed = registry.installed_toolchains()
        tc_names = [tc.name for tc in tgt.toolchains]
        tc_map = {tc.name: tc for tc in tgt.toolchains}
        oscam = self._load_oscam_options()

        yield S4Header()

        # Tab bar
        with Horizontal(id="te-tab-bar"):
            yield Static(" [b]G[/b]eneral ", id="te-tab-general", classes="tab-active")
            yield Static(" [b]R[/b]epo/Toolchain", id="te-tab-source", classes="tab-inactive")
            yield Static(" [b]O[/b]ptions", id="te-tab-oscam", classes="tab-inactive")
            yield Static(" [b]U[/b]pload ", id="te-tab-upload", classes="tab-inactive")

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
            with Collapsible(title=t("section_common"), collapsed=False, id="col-common"):
                yield Horizontal(
                    Static(f" {t('te_name')}", classes="s-label-tall"),
                    Input(value=tgt.name, placeholder=t("ph_target_name"), id="t-name"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(f" {t('te_description')}", classes="s-label-tall"),
                    Input(value=tgt.description, placeholder=t("ph_target_desc"), id="t-desc"),
                    classes="s-row",
                )
                compress_enabled = b.use_compress and bool(self._upx_path)
                upx_switch = Switch(value=compress_enabled, id="f-use-compress", disabled=not bool(self._upx_path))
                import re as _re
                upx_tooltip = _re.sub(r'\[/?[a-z_ ]+\]', '', self._upx_info).strip() if self._upx_path else "upx not found"
                upx_switch.tooltip = upx_tooltip
                upx_binary_val = b.upx_binary or self._upx_path or ""
                upx_path_inp = Input(value=upx_binary_val, placeholder=t("ph_system_default"), id="t-upx-binary")
                upx_path_inp.styles.width = "1fr"
                if compress_enabled:
                    lvl_inp = Input(value=b.compress_level, placeholder="--lzma", id="t-compress-level")
                    lvl_inp.styles.max_width = 25
                    from textual.widgets import Button as _Btn
                    upx_browse_btn = _Btn("📂", id="btn-upx-browse", classes="tc-reset-btn")
                    upx_browse_btn.tooltip = t("ph_browse_upx")
                    yield Horizontal(
                        Static(" USE_COMPRESS", classes="flag-label"),
                        upx_switch,
                        Static(f" {t('te_binary')}", classes="src-opt-label"),
                        upx_path_inp,
                        upx_browse_btn,
                        Static(f" {t('te_compression_level')}", classes="src-opt-label"),
                        lvl_inp,
                        classes="flag-row",
                    )
                else:
                    yield Horizontal(
                        Static(" USE_COMPRESS", classes="flag-label"),
                        upx_switch,
                        classes="flag-row",
                    )
                yield Horizontal(
                    Static(f" {t('te_build_system')}", classes="s-label-tall"),
                    Select(
                        options=[("make", "make"), ("cmake", "cmake")],
                        value=b.build_system or "make",
                        id="t-build-system",
                        prompt=t("select_prompt"),
                    ),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(f" {t('te_binary_name')}", classes="s-label-tall"),
                    Input(value=b.use_oscamname, placeholder=t("ph_empty_default"), id="t-oscamname"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(f" {t('te_config_directory')}", classes="s-label-tall"),
                    Input(value=b.confdir, id="t-confdir"),
                    classes="s-row",
                )
            if has_source:
                mk_flags = self._load_makefile_flags()
                mk_use = mk_flags.get("use", [])
                has_dual_backend = "MBEDTLS" in mk_use and "OPENSSL" in mk_use

                # Known flags that support STATIC_ linking (not in Makefile, passed as make vars)
                _STATIC_CAPABLE = {"LIBCRYPTO", "SSL", "LIBUSB", "PCSC", "LIBDVBCSA"}

                # Split into library and hardware flags
                lib_flags = [f for f in mk_use if f not in self._HARDWARE_FLAGS and f not in ("MBEDTLS", "OPENSSL")]
                hw_flags = [f for f in mk_use if f in self._HARDWARE_FLAGS]

                with Collapsible(title=t("section_library_flags"), collapsed=True, id="col-lib-flags"):
                    # Exclusive MBEDTLS ↔ OPENSSL pair (only if repo supports it)
                    if has_dual_backend:
                        openssl_on = b.use_flags.get("OPENSSL", False)
                        mbedtls_on = b.use_flags.get("MBEDTLS", not openssl_on)
                        yield Horizontal(
                            Static(" USE_MBEDTLS", classes="flag-label"),
                            Switch(value=mbedtls_on, id="f-use-MBEDTLS"),
                            Static(" USE_OPENSSL", classes="flag-label"),
                            Switch(value=openssl_on, id="f-use-OPENSSL"),
                            classes="flag-row",
                        )
                    # Dynamic library flags with STATIC_ partner for known libs
                    for flag in lib_flags:
                        widgets = [
                            Static(f" USE_{flag}", classes="flag-label"),
                            Switch(value=b.use_flags.get(flag, False), id=f"f-use-{flag}"),
                        ]
                        if flag in _STATIC_CAPABLE:
                            widgets += [
                                Static(f" STATIC_{flag}", classes="flag-label"),
                                Switch(value=b.static_flags.get(flag, False), id=f"f-static-{flag}"),
                            ]
                        yield Horizontal(*widgets, classes="flag-row")

                with Collapsible(title=t("section_hardware_flags"), collapsed=True, id="col-hw-flags"):
                    # STAPI row — with optional STAPI_LIB field inline
                    if "STAPI" in hw_flags or "STAPI5" in hw_flags:
                        _stapi_widgets = []
                        if "STAPI" in hw_flags:
                            _stapi_widgets += [
                                Static(" USE_STAPI", classes="flag-label"),
                                Switch(value=b.use_flags.get("STAPI", False), id="f-use-STAPI"),
                            ]
                        if "STAPI5" in hw_flags:
                            _stapi5_sw = Switch(value=b.use_flags.get("STAPI5", False), id="f-use-STAPI5")
                            _stapi_widgets += [
                                Static(" USE_STAPI5", classes="flag-label"), _stapi5_sw,
                            ]
                        if b.use_flags.get("STAPI") or b.use_flags.get("STAPI5"):
                            _stapi_default = ""
                            for _tc in installed:
                                if _tc.name in tc_names:
                                    _stapi_default = _tc.stapi_lib or ""
                                    if _stapi_default:
                                        break
                            _stapi_val = b.stapi_lib if b.stapi_lib else _stapi_default
                            _stapi_is_default = (_stapi_val == _stapi_default)
                            from textual.widgets import Button as _Btn
                            _stapi_inp = Input(value=_stapi_val, placeholder=t("ph_stapi_lib"), id="f-stapi-lib")
                            _stapi_inp.styles.max_width = 25
                            if _stapi_is_default:
                                _stapi_inp.add_class("tc-default")
                            _stapi_lbl = Static(" STAPI_LIB", classes="tc-field-label")
                            _stapi_lbl.tooltip = "Path to precompiled STAPI library"
                            _stapi_rst = _Btn("↺", id="rst-stapi-lib", classes="tc-reset-btn")
                            _stapi_rst.tooltip = t("te_load_default")
                            self._stapi_default = _stapi_default
                            _stapi_widgets += [_stapi_lbl, _stapi_inp, _stapi_rst]
                        yield Horizontal(*_stapi_widgets, classes="flag-row")

                    # Other hardware flags (2 per row)
                    other_hw = [f for f in hw_flags if f not in ("STAPI", "STAPI5")]
                    for i in range(0, len(other_hw), 2):
                        widgets = []
                        for f in other_hw[i:i+2]:
                            widgets += [
                                Static(f" USE_{f}", classes="flag-label"),
                                Switch(value=b.use_flags.get(f, False), id=f"f-use-{f}"),
                            ]
                        yield Horizontal(*widgets, classes="flag-row")
            else:
                yield Static(f"  [dim]{t('te_select_source_flags')}[/dim]")

        # === Tab 2: Repo/Toolchain===
        with VerticalScroll(id="te-panel-source"):
            # Sources
            src_names = [s.name for s in tgt.sources]
            src_map = {s.name: s for s in tgt.sources}
            src_count = len(tgt.sources)
            # Available patches
            patches_dir = self.app.paths.patches_dir
            available_patches = sorted(p.name for p in patches_dir.glob("*.patch")) if patches_dir.is_dir() else []

            with Collapsible(title=f"Repositories ({src_count} {t('te_selected')})", collapsed=False, id="col-sources"):
                for r in repos:
                    enabled = r.name in src_names
                    sc = src_map.get(r.name, SourceConfig(name=r.name))
                    rid = _safe_id(r.name)
                    src_label = Static(f" {r.name}\n [dim]{r.url}[/dim]", classes="src-name")
                    yield Horizontal(
                        src_label,
                        Switch(value=enabled, id=f"src-{rid}"),
                        *([
                            Static(f" [dim]{t('te_update_before_build')}[/dim]", classes="src-opt-label"),
                            Switch(value=sc.update_before_build, id=f"src-upd-{rid}"),
                            Static(f" [dim]{t('te_patch_before_build')}[/dim]", classes="src-opt-label"),
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
                    yield Static(f"  [dim]{t('te_no_repos_hint')}[/dim]")

            # Toolchains
            tc_count = len(tgt.toolchains)
            with Collapsible(title=f"Toolchains ({tc_count} {t('te_selected')})", collapsed=False, id="col-toolchains"):
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
                            b.tooltip = t("te_load_default")
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
                    yield Static(f"  [dim]{t('te_no_tc_hint')}[/dim]")

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
                    yield Static(f"  [dim]{t('te_select_source_first')}[/dim]")

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
                    Button(t("te_cleanup"), id="btn-cleanup", variant="primary"),
                    classes="s-row",
                )

        # === Tab 3: Upload ===
        with VerticalScroll(id="te-panel-upload"):
            upload_count = len(tgt.upload)
            with Collapsible(title=f"{t('te_destination')} ({upload_count})", collapsed=False, id="col-upload"):
                yield Horizontal(
                    Static(f" {t('te_host')}", classes="s-label-tall"),
                    Input(value=u.host, placeholder=t("ph_host"), id="t-up-host"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(f" {t('te_user')}", classes="s-label-tall"),
                    Input(value=u.user, id="t-up-user"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(f" {t('te_port')}", classes="s-label-tall"),
                    Input(value=str(u.port), id="t-up-port", type="integer"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(f" {t('te_ssh_key')}", classes="s-label-tall"),
                    Input(value=u.key, placeholder=t("ph_ssh_key"), id="t-up-key"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(f" {t('te_target_path')}", classes="s-label-tall"),
                    Input(value=u.target_path, id="t-up-path"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(f" {t('te_pre_command')}", classes="s-label-tall"),
                    Input(value=u.pre_cmd, placeholder=t("ph_pre_cmd"), id="t-up-pre"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(f" {t('te_post_command')}", classes="s-label-tall"),
                    Input(value=u.post_cmd, placeholder=t("ph_post_cmd"), id="t-up-post"),
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
        else:
            self.set_timer(0.1, lambda: self.query_one("#t-name", Input).focus())

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

        # Dynamic USE ↔ STATIC dependencies
        if sid.startswith("f-use-") and not event.value:
            # USE deactivated → also deactivate matching STATIC
            flag = sid.replace("f-use-", "")
            try:
                self.query_one(f"#f-static-{flag}", Switch).value = False
            except Exception:
                pass
        if sid.startswith("f-static-") and event.value:
            # STATIC activated → also activate matching USE
            flag = sid.replace("f-static-", "")
            try:
                self.query_one(f"#f-use-{flag}", Switch).value = True
            except Exception:
                pass

        # Exclusive MBEDTLS ↔ OPENSSL toggle
        if sid == "f-use-OPENSSL" and event.value:
            try:
                self.query_one("#f-use-MBEDTLS", Switch).value = False
                # USE_OPENSSL requires USE_LIBCRYPTO
                self.query_one("#f-use-LIBCRYPTO", Switch).value = True
            except Exception:
                pass
        if sid == "f-use-MBEDTLS" and event.value:
            try:
                self.query_one("#f-use-OPENSSL", Switch).value = False
            except Exception:
                pass

        # STAPI toggle — refresh to show/hide stapi_lib field
        if sid in ("f-use-STAPI", "f-use-STAPI5"):
            self._refresh_screen(self._collect(), "general", sid)
            return

        # UPX toggle — refresh to show/hide compress level field
        if sid == "f-use-compress":
            self._refresh_screen(self._collect(), "general", sid)
            return

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-cleanup":
            self._do_cleanup()
        elif event.button.id == "btn-upx-browse":
            self._open_upx_picker()
        elif event.button.id == "rst-stapi-lib":
            try:
                inp = self.query_one("#f-stapi-lib", Input)
                inp.value = getattr(self, '_stapi_default', '')
                inp.add_class("tc-default")
            except Exception:
                pass
        elif event.button.id and event.button.id.startswith("rst-"):
            self._do_reset_field(event.button.id)

    def _open_upx_picker(self) -> None:
        """Open file picker dialog to select UPX binary."""
        from ui.screens.toolchains import FilePickerScreen
        # Default start: data/bin (where s4 keeps the upx symlink)
        start = str(self.app.paths.bin_dir) if self.app.paths.bin_dir.is_dir() else "/"
        self.app.push_screen(
            FilePickerScreen(
                start_path=start,
                title=f"{t('select_file_label')}: {t('te_binary')}",
                simple=True,
                select_label=t("select_btn"),
            ),
            callback=self._on_upx_selected,
        )

    def _on_upx_selected(self, result) -> None:
        """Callback after file picker — set the UPX binary path."""
        if not result:
            return
        path, _ = result if isinstance(result, tuple) else (result, "")
        if not path:
            return
        # Reject the s4-managed symlink to avoid pointing it at itself
        try:
            sel = Path(path)
            bin_link = self.app.paths.bin_dir / "upx"
            if sel.resolve() == bin_link.resolve():
                self.notify(t("upx_pick_self_blocked"), severity="warning", timeout=8)
                return
        except Exception:
            pass
        try:
            self.query_one("#t-upx-binary", Input).value = path
        except Exception:
            pass

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

        # UPX binary path validation
        if iid == "t-upx-binary":
            from pathlib import Path as _P
            import subprocess as _sp
            upx_path = event.value.strip()
            try:
                switch = self.query_one("#f-use-compress", Switch)
                if upx_path and _P(upx_path).exists():
                    try:
                        upx_ver = _sp.check_output([upx_path, "--version"], text=True, stderr=_sp.DEVNULL, timeout=5).splitlines()[0]
                    except Exception:
                        upx_ver = "OK"
                    switch.tooltip = f"{upx_path} — {upx_ver}"
                elif upx_path:
                    switch.tooltip = f"{upx_path} — not found!"
                    self.notify(t("upx_not_found", path=upx_path), severity="warning", timeout=3)
                else:
                    switch.tooltip = "upx not configured"
            except Exception:
                pass

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

        # Collect USE_* and STATIC_* flags dynamically from switches
        mk_flags = self._load_makefile_flags()
        all_use = mk_flags.get("use", [])

        use_flags = {}
        for flag in all_use:
            if self._switch_on(f"f-use-{flag}"):
                use_flags[flag] = True
        # STATIC flags: rendered for _STATIC_CAPABLE libs, not from Makefile
        _STATIC_CAPABLE = {"LIBCRYPTO", "SSL", "LIBUSB", "PCSC", "LIBDVBCSA"}
        static_flags = {}
        for flag in _STATIC_CAPABLE:
            if self._switch_on(f"f-static-{flag}"):
                static_flags[flag] = True

        build = BuildSettings(
            modules=modules,
            protocols=protocols,
            readers=readers,
            card_readers=card_readers,
            use_flags=use_flags,
            static_flags=static_flags,
            use_compress=self._switch_on("f-use-compress"),
            compress_level=self._input_val("t-compress-level") or "--lzma",
            upx_binary=self._input_val("t-upx-binary"),
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
            self.notify(t("target_name_required"), severity="error")
            return ""
        path = self.save_path or (self.app.paths.targets_dir / f"{target.name}.yaml")
        target.save(path)
        self.save_path = path
        # Remember for main screen to select after returning
        self.app._last_saved_target = str(path)
        return t("target_saved", name=target.name)

    def action_go_back(self) -> None:
        self.app.pop_screen()
