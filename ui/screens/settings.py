"""Settings screen — s4 application settings."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Select, Switch, Input
from textual.containers import Horizontal, VerticalScroll

from ui.header import s4Header
from ui.footer import s4Footer

LANGUAGES = [
    ("English", "en"),
    ("Deutsch", "de"),
    ("Español", "es"),
    ("Français", "fr"),
    ("Italiano", "it"),
    ("Nederlands", "nl"),
    ("Polski", "pl"),
    ("Português", "pt"),
    ("Türkçe", "tr"),
    ("Български", "bg"),
]

SWITCH_MAP = {
    "set-update-check": "update_check",
    "set-patch-webif": "patch_webif",
    "set-delete-debug": "delete_debug",
    "set-save-smargo": "save_smargo",
    "set-qemu-test": "qemu_test",
}

INPUT_MAP = {
    "set-s4-url": "s4_url",
    "set-compiler-opt": "compiler_opt",
    "set-max-cpus": "max_cpus",
    "set-sign-cert": "sign_cert",
    "set-sign-key": "sign_key",
    "set-qemu-timeout": "qemu_timeout",
    "set-qemu-expect": "qemu_expect",
}

SELECT_MAP = {
    "set-language": "language",
}


def _switch_row(label: str, value: bool, widget_id: str) -> Horizontal:
    return Horizontal(
        Static(f" {label}", classes="s-label"),
        Switch(value=value, id=widget_id),
        classes="s-toggle",
    )


class SettingsScreen(Screen):
    BINDINGS = [
    ]

    def compose(self) -> ComposeResult:
        s = self.app.settings
        yield s4Header()

        with VerticalScroll(id="settings-content"):
            # General
            yield Static(" [bold]General[/bold]", classes="section-title")
            yield Horizontal(
                Static(" Language", classes="s-label-tall"),
                Select(options=LANGUAGES, value=s.language, id="set-language"),
                classes="s-row-tall",
            )
            yield Horizontal(
                Static(" s4 update URL", classes="s-label"),
                Input(value=s.s4_url, id="set-s4-url"),
                classes="s-row",
            )
            yield _switch_row("Check for updates on start", s.update_check, "set-update-check")

            # Build
            yield Static(" [bold]Build[/bold]", classes="section-title")
            yield Horizontal(
                Static(" Compiler flags", classes="s-label"),
                Input(value=s.compiler_opt, id="set-compiler-opt"),
                classes="s-row",
            )
            yield Horizontal(
                Static(" Max CPU threads", classes="s-label"),
                Input(value=str(s.max_cpus), id="set-max-cpus", type="integer"),
                classes="s-row",
            )
            yield _switch_row("Patch WebIf with build info", s.patch_webif, "set-patch-webif")
            yield _switch_row("Delete debug binary after build", s.delete_debug, "set-delete-debug")
            yield _switch_row("Save list_smargo", s.save_smargo, "set-save-smargo")

            # Signing
            yield Static(" [bold]Binary Signing[/bold]", classes="section-title")
            yield Horizontal(
                Static(" X.509 certificate", classes="s-label"),
                Input(value=s.sign_cert, placeholder="/path/to/certificate.crt", id="set-sign-cert"),
                classes="s-row",
            )
            yield Horizontal(
                Static(" Private key", classes="s-label"),
                Input(value=s.sign_key, placeholder="/path/to/private.key", id="set-sign-key"),
                classes="s-row",
            )

            # QEMU
            yield Static(" [bold]QEMU Post-Build Test[/bold]", classes="section-title")
            yield _switch_row("Enable QEMU smoke test", s.qemu_test, "set-qemu-test")
            yield Horizontal(
                Static(" Timeout (seconds)", classes="s-label"),
                Input(value=str(s.qemu_timeout), id="set-qemu-timeout", type="integer"),
                classes="s-row",
            )
            yield Horizontal(
                Static(" Expected output", classes="s-label"),
                Input(value=s.qemu_expect, id="set-qemu-expect"),
                classes="s-row",
            )

        yield s4Footer(active_screen="settings")

    def _save(self) -> None:
        s = self.app.settings
        for wid, attr in SWITCH_MAP.items():
            setattr(s, attr, self.query_one(f"#{wid}", Switch).value)
        for wid, attr in INPUT_MAP.items():
            val = self.query_one(f"#{wid}", Input).value
            current = getattr(s, attr)
            if isinstance(current, int):
                try:
                    val = int(val)
                except ValueError:
                    val = current
            setattr(s, attr, val)
        for wid, attr in SELECT_MAP.items():
            val = self.query_one(f"#{wid}", Select).value
            if val is not None:
                setattr(s, attr, val)
        s.save(self.app.settings_path)

    def do_save(self) -> str:
        """Save settings (called by App)."""
        self._save()
        return "Settings saved"

    def action_go_back(self) -> None:
        self._save()
        self.app.pop_screen()
