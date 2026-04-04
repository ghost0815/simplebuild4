"""Target editor screen — create or edit a target configuration."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Switch, Input, Collapsible
from textual.containers import Horizontal, Vertical, VerticalScroll

from core.target import Target, BuildSettings, UploadConfig
from core.toolchain import ToolchainRegistry
from core.repos_config import load_repos
from core.oscam_config import read_options, OscamOptions
from ui.header import S4Header
from ui.footer import S4Footer


def _switch_row(label: str, value: bool, widget_id: str) -> Horizontal:
    return Horizontal(
        Static(f" {label}", classes="s-label"),
        Switch(value=value, id=widget_id),
        classes="s-toggle",
    )


class TargetEditScreen(Screen):
    """Edit or create a target configuration."""

    BINDINGS = [
        ("1", "switch_tab('source')", "Source/Toolchain"),
        ("2", "switch_tab('oscam')", "oscam"),
        ("3", "switch_tab('build')", "Build"),
        ("4", "switch_tab('upload')", "Upload"),
    ]

    def __init__(self, target: Target | None = None, save_path: Path | None = None) -> None:
        super().__init__()
        self.target = target or Target()
        self.save_path = save_path
        self._oscam_opts: OscamOptions | None = None

    def _load_oscam_options(self) -> OscamOptions:
        """Load oscam options from the first selected source repo."""
        if self._oscam_opts:
            return self._oscam_opts
        repos = load_repos(self.app.paths.repos_config)
        # Try first selected source, then first available repo
        for src_name in self.target.sources:
            for r in repos:
                if r.name == src_name:
                    repo_dir = self.app.paths.repos_dir / r.name
                    opts = read_options(repo_dir)
                    if opts:
                        self._oscam_opts = opts
                        return opts
        # Fallback: try any repo
        for r in repos:
            repo_dir = self.app.paths.repos_dir / r.name
            opts = read_options(repo_dir)
            if opts:
                self._oscam_opts = opts
                return opts
        # Empty fallback
        self._oscam_opts = OscamOptions()
        return self._oscam_opts

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
        oscam = self._load_oscam_options()

        yield S4Header()

        # Tab bar
        with Horizontal(id="te-tab-bar"):
            yield Static(" Source/Toolchain ", id="te-tab-source", classes="tab-active")
            yield Static(" oscam ", id="te-tab-oscam", classes="tab-inactive")
            yield Static(" Build ", id="te-tab-build", classes="tab-inactive")
            yield Static(" Upload ", id="te-tab-upload", classes="tab-inactive")

        # === Tab 1: Source/Toolchain ===
        with VerticalScroll(id="te-panel-source"):
            yield Static(" [bold]Target[/bold]", classes="section-title-top")
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

            # Sources
            src_count = len(t.sources)
            with Collapsible(title=f"Sources ({src_count} selected)", collapsed=False, id="col-sources"):
                for r in repos:
                    enabled = r.name in t.sources
                    yield _switch_row(f"{r.name} [dim]({r.url})[/dim]", enabled, f"src-{r.name}")
                if not repos:
                    yield Static("  [dim]No repositories configured — use F3 Repositories first[/dim]")

            # Toolchains
            tc_count = len(t.toolchains)
            installed = registry.installed_toolchains()
            with Collapsible(title=f"Toolchains ({tc_count} selected)", collapsed=False, id="col-toolchains"):
                for tc in installed:
                    enabled = tc.name in t.toolchains
                    label = f"{tc.name} [dim]({tc.arch})[/dim]" if tc.arch else tc.name
                    if tc.native:
                        label = f"[bold]{tc.name}[/bold] [dim]({tc.arch})[/dim]"
                    yield _switch_row(label, enabled, f"tc-{tc.name}")
                if not installed:
                    yield Static("  [dim]No toolchains installed — use F4 Toolchains first[/dim]")

        # === Tab 2: oscam Modules ===
        with VerticalScroll(id="te-panel-oscam"):
            addon_count = len(b.modules)
            with Collapsible(title=f"Addons ({addon_count}/{len(oscam.addons)})", collapsed=False, id="col-addons"):
                for mod in oscam.addons:
                    enabled = mod in b.modules
                    yield _switch_row(mod, enabled, f"m-{mod}")
                if not oscam.addons:
                    yield Static("  [dim]Clone a source repository first[/dim]")

            proto_count = len(b.protocols)
            with Collapsible(title=f"Protocols ({proto_count}/{len(oscam.protocols)})", collapsed=False, id="col-protocols"):
                for mod in oscam.protocols:
                    enabled = mod in b.protocols
                    yield _switch_row(mod, enabled, f"m-{mod}")

            reader_count = len(b.readers)
            with Collapsible(title=f"Readers ({reader_count}/{len(oscam.readers)})", collapsed=False, id="col-readers"):
                for mod in oscam.readers:
                    enabled = mod in b.readers
                    yield _switch_row(mod, enabled, f"m-{mod}")

            cr_count = len(b.card_readers)
            with Collapsible(title=f"Card Readers ({cr_count}/{len(oscam.card_readers)})", collapsed=False, id="col-card-readers"):
                for mod in oscam.card_readers:
                    enabled = mod in b.card_readers
                    yield _switch_row(mod, enabled, f"m-{mod}")

        # === Tab 3: Build Options ===
        with VerticalScroll(id="te-panel-build"):
            with Collapsible(title="Library Flags", collapsed=False, id="col-lib-flags"):
                yield _switch_row("USE_LIBCRYPTO", b.use_libcrypto, "f-use-libcrypto")
                yield _switch_row("USE_SSL", b.use_ssl, "f-use-ssl")
                yield _switch_row("USE_LIBUSB", b.use_libusb, "f-use-libusb")
                yield _switch_row("USE_PCSC", b.use_pcsc, "f-use-pcsc")
                yield _switch_row("USE_LIBDVBCSA", b.use_libdvbcsa, "f-use-libdvbcsa")

            with Collapsible(title="Hardware Flags", collapsed=True, id="col-hw-flags"):
                yield _switch_row("USE_STAPI", b.use_stapi, "f-use-stapi")
                yield _switch_row("USE_STAPI5", b.use_stapi5, "f-use-stapi5")
                yield _switch_row("USE_COOLAPI", b.use_coolapi, "f-use-coolapi")
                yield _switch_row("USE_COOLAPI2", b.use_coolapi2, "f-use-coolapi2")
                yield _switch_row("USE_SU980", b.use_su980, "f-use-su980")
                yield _switch_row("USE_AZBOX", b.use_azbox, "f-use-azbox")

            with Collapsible(title="Static Linking", collapsed=True, id="col-static"):
                yield _switch_row("USE_STATIC (all)", b.use_static, "f-use-static")
                yield _switch_row("STATIC_LIBCRYPTO", b.static_libcrypto, "f-static-libcrypto")
                yield _switch_row("STATIC_SSL", b.static_ssl, "f-static-ssl")
                yield _switch_row("STATIC_LIBUSB", b.static_libusb, "f-static-libusb")
                yield _switch_row("STATIC_PCSC", b.static_pcsc, "f-static-pcsc")
                yield _switch_row("STATIC_LIBDVBCSA", b.static_libdvbcsa, "f-static-libdvbcsa")

            with Collapsible(title="Other", collapsed=True, id="col-other-flags"):
                yield _switch_row("USE_COMPRESS (UPX)", b.use_compress, "f-use-compress")
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
                yield Horizontal(
                    Static(" Make variables", classes="s-label-tall"),
                    Input(value=b.make_vars, placeholder="Raw make assignments e.g. LIB_RT=", id="t-make-vars"),
                    classes="s-row",
                )

        # === Tab 4: Upload ===
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
        # Show only first tab
        self._show_tab("source")

    def on_click(self, event) -> None:
        widget = event.widget if hasattr(event, 'widget') else None
        if widget:
            if widget.id == "te-tab-source":
                self._show_tab("source")
            elif widget.id == "te-tab-oscam":
                self._show_tab("oscam")
            elif widget.id == "te-tab-build":
                self._show_tab("build")
            elif widget.id == "te-tab-upload":
                self._show_tab("upload")

    def action_switch_tab(self, tab: str) -> None:
        self._show_tab(tab)

    def _show_tab(self, tab: str) -> None:
        panels = {
            "source": ("#te-panel-source", "#te-tab-source"),
            "oscam": ("#te-panel-oscam", "#te-tab-oscam"),
            "build": ("#te-panel-build", "#te-tab-build"),
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

        # Collect selected sources
        repos = load_repos(self.app.paths.repos_config)
        sources = []
        for r in repos:
            try:
                if self.query_one(f"#src-{r.name}", Switch).value:
                    sources.append(r.name)
            except Exception:
                pass
        if not sources:
            sources = ["default"]

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
                if self.query_one(f"#tc-{tc.name}", Switch).value:
                    toolchains.append(tc.name)
            except Exception:
                pass

        # Collect enabled modules (dynamically from oscam options)
        oscam = self._load_oscam_options()
        modules = [m for m in oscam.addons if self._switch_on(f"m-{m}")]
        protocols = [m for m in oscam.protocols if self._switch_on(f"m-{m}")]
        readers = [m for m in oscam.readers if self._switch_on(f"m-{m}")]
        card_readers = [m for m in oscam.card_readers if self._switch_on(f"m-{m}")]

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
            use_static=self._switch_on("f-use-static"),
            static_libcrypto=self._switch_on("f-static-libcrypto"),
            static_ssl=self._switch_on("f-static-ssl"),
            static_libusb=self._switch_on("f-static-libusb"),
            static_pcsc=self._switch_on("f-static-pcsc"),
            static_libdvbcsa=self._switch_on("f-static-libdvbcsa"),
            use_compress=self._switch_on("f-use-compress"),
            use_oscamname=self.query_one("#t-oscamname", Input).value.strip(),
            confdir=self.query_one("#t-confdir", Input).value.strip(),
            make_vars=self.query_one("#t-make-vars", Input).value.strip(),
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
