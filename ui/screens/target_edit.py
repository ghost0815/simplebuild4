"""Target editor screen — create or edit a target configuration."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Select, Switch, Input
from textual.containers import Horizontal, VerticalScroll

from core.target import Target, BuildSettings, UploadConfig
from core.toolchain import ToolchainRegistry
from core.repos_config import load_repos
from ui.header import S4Header
from ui.footer import S4Footer

# All available oscam modules by category
ADDONS = [
    "WEBIF", "WEBIF_LIVELOG", "WEBIF_JQUERY", "TOUCH", "WITH_SSL",
    "HAVE_DVBAPI", "READ_SDT_CHARSETS", "CS_ANTICASC", "WITH_DEBUG",
    "MODULE_MONITOR", "WITH_LB", "CS_CACHEEX", "CS_CACHEEX_AIO",
    "CW_CYCLE_CHECK", "CLOCKFIX", "IPV6SUPPORT", "WITH_ARM_NEON",
]

PROTOCOLS = [
    "MODULE_CAMD33", "MODULE_CAMD35", "MODULE_CAMD35_TCP",
    "MODULE_NEWCAMD", "MODULE_CCCAM", "MODULE_CCCSHARE",
    "MODULE_GBOX", "MODULE_RADEGAST", "MODULE_SCAM",
    "MODULE_SERIAL", "MODULE_CONSTCW", "MODULE_PANDORA",
    "MODULE_GHTTP", "MODULE_STREAMRELAY",
]

READERS = [
    "READER_NAGRA", "READER_NAGRA_MERLIN", "READER_IRDETO",
    "READER_CONAX", "READER_CRYPTOWORKS", "READER_SECA",
    "READER_VIACCESS", "READER_VIDEOGUARD", "READER_DRE",
    "READER_TONGFANG", "READER_BULCRYPT", "READER_GRIFFIN",
    "READER_DGCRYPT",
]

CARD_READERS = [
    "CARDREADER_PHOENIX", "CARDREADER_INTERNAL_SCI",
    "CARDREADER_INTERNAL_AZBOX", "CARDREADER_INTERNAL_COOLAPI",
    "CARDREADER_INTERNAL_COOLAPI2", "CARDREADER_SC8IN1",
    "CARDREADER_MP35", "CARDREADER_SMARGO", "CARDREADER_DB2COM",
    "CARDREADER_STAPI", "CARDREADER_STAPI5", "CARDREADER_STINGER",
    "CARDREADER_DRECAS", "CARDREADER_SMART", "CARDREADER_PCSC",
]


def _switch_row(label: str, value: bool, widget_id: str) -> Horizontal:
    return Horizontal(
        Static(f" {label}", classes="s-label"),
        Switch(value=value, id=widget_id),
        classes="s-toggle",
    )


class TargetEditScreen(Screen):
    """Edit or create a target configuration."""

    BINDINGS = []

    def __init__(self, target: Target | None = None, save_path: Path | None = None) -> None:
        super().__init__()
        self.target = target or Target()
        self.save_path = save_path

    def compose(self) -> ComposeResult:
        t = self.target
        b = t.build
        u = t.upload[0] if t.upload else UploadConfig()

        # Source repo options
        repos = load_repos(self.app.paths.repos_config)
        source_options = [(r.name, r.name) for r in repos]
        repo_names = [r.name for r in repos]
        if t.source and t.source in repo_names:
            source_value = t.source
        elif repos:
            source_value = repos[0].name
        else:
            source_value = None

        # Toolchain options
        registry = ToolchainRegistry(
            self.app.paths.toolchains_defaults,
            self.app.paths.toolchains_local,
            self.app.paths.toolchains_dir,
        )
        registry.load()
        tc_options = registry.get_select_options(include_native=True)
        tc_names = [name for _, name in tc_options]
        tc_value = t.toolchain if t.toolchain in tc_names else "native"

        yield S4Header()

        with VerticalScroll(id="settings-content"):
            # Basics
            yield Static(" [bold]Target[/bold]", classes="section-title")
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
            yield Horizontal(
                Static(" Source", classes="s-label-tall"),
                Select(
                    options=source_options,
                    value=source_value if source_value else Select.BLANK,
                    id="t-source",
                    prompt="select source",
                ),
                classes="s-row-tall",
            )
            yield Horizontal(
                Static(" Toolchain", classes="s-label-tall"),
                Select(options=tc_options, value=tc_value, id="t-toolchain"),
                classes="s-row-tall",
            )

            # Addons
            yield Static(" [bold]Addons[/bold]", classes="section-title")
            for mod in ADDONS:
                enabled = mod in b.modules
                yield _switch_row(mod, enabled, f"m-{mod}")

            # Protocols
            yield Static(" [bold]Protocols[/bold]", classes="section-title")
            for mod in PROTOCOLS:
                enabled = mod in b.protocols
                yield _switch_row(mod, enabled, f"m-{mod}")

            # Readers
            yield Static(" [bold]Readers[/bold]", classes="section-title")
            for mod in READERS:
                enabled = mod in b.readers
                yield _switch_row(mod, enabled, f"m-{mod}")

            # Card Readers
            yield Static(" [bold]Card Readers[/bold]", classes="section-title")
            for mod in CARD_READERS:
                enabled = mod in b.card_readers
                yield _switch_row(mod, enabled, f"m-{mod}")

            # Build options
            yield Static(" [bold]Build Options[/bold]", classes="section-title")
            yield Horizontal(
                Static(" USE flags", classes="s-label-tall"),
                Input(value=b.useflags, placeholder="e.g. USE_LIBCRYPTO USE_EXTRA", id="t-useflags"),
                classes="s-row",
            )
            yield Horizontal(
                Static(" CC options", classes="s-label-tall"),
                Input(value=b.cc_opts, placeholder="Compiler options → CC_OPTS", id="t-cc-opts"),
                classes="s-row",
            )
            yield Horizontal(
                Static(" C flags", classes="s-label-tall"),
                Input(value=b.cflags, placeholder="Architecture flags → EXTRA_CFLAGS", id="t-cflags"),
                classes="s-row",
            )
            yield Horizontal(
                Static(" LD flags", classes="s-label-tall"),
                Input(value=b.ldflags, placeholder="Linker flags → EXTRA_LDFLAGS", id="t-ldflags"),
                classes="s-row",
            )
            yield Horizontal(
                Static(" Make variables", classes="s-label-tall"),
                Input(value=b.make_vars, placeholder="Raw make assignments e.g. LIB_RT=", id="t-make-vars"),
                classes="s-row",
            )
            yield Horizontal(
                Static(" Config directory", classes="s-label-tall"),
                Input(value=b.confdir, id="t-confdir"),
                classes="s-row",
            )
            yield Horizontal(
                Static(" STAPI library", classes="s-label-tall"),
                Input(value=b.stapi_lib, placeholder="empty=disabled, default=liboscam_stapi.a", id="t-stapi-lib"),
                classes="s-row",
            )

            # Upload
            yield Static(" [bold]Upload (optional)[/bold]", classes="section-title")
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

    def _collect(self) -> Target:
        """Collect all values from widgets into a Target."""
        name = self.query_one("#t-name", Input).value.strip()
        desc = self.query_one("#t-desc", Input).value.strip()
        src_sel = self.query_one("#t-source", Select)
        source = src_sel.value if src_sel.value is not None and isinstance(src_sel.value, str) else "default"
        tc_sel = self.query_one("#t-toolchain", Select)
        toolchain = tc_sel.value if tc_sel.value is not None and isinstance(tc_sel.value, str) else "native"

        # Collect enabled modules per category
        modules = [m for m in ADDONS if self.query_one(f"#m-{m}", Switch).value]
        protocols = [m for m in PROTOCOLS if self.query_one(f"#m-{m}", Switch).value]
        readers = [m for m in READERS if self.query_one(f"#m-{m}", Switch).value]
        card_readers = [m for m in CARD_READERS if self.query_one(f"#m-{m}", Switch).value]

        build = BuildSettings(
            modules=modules,
            protocols=protocols,
            readers=readers,
            card_readers=card_readers,
            useflags=self.query_one("#t-useflags", Input).value.strip(),
            cc_opts=self.query_one("#t-cc-opts", Input).value.strip(),
            cflags=self.query_one("#t-cflags", Input).value.strip(),
            ldflags=self.query_one("#t-ldflags", Input).value.strip(),
            make_vars=self.query_one("#t-make-vars", Input).value.strip(),
            confdir=self.query_one("#t-confdir", Input).value.strip(),
            stapi_lib=self.query_one("#t-stapi-lib", Input).value.strip(),
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
            source=source,
            toolchain=toolchain,
            build=build,
            upload=upload,
        )

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
