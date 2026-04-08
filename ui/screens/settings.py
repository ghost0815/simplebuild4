"""Settings screen — s4 application settings."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Select, Switch, Input
from textual.containers import Horizontal, VerticalScroll

from translation import t, available_languages
from ui.header import S4Header
from ui.footer import S4Footer

SWITCH_MAP = {
    "set-update-check": "update_check",
    "set-warn-unsaved": "warn_unsaved_on_exit",
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
    "set-ctng-dir": "ctng_dir",
    "set-log-max-age": "log_max_age_days",
    "set-log-max-count": "log_max_count",
    "set-log-max-size": "log_max_size_mb",
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
        yield S4Header()

        with VerticalScroll(id="settings-content"):
            # General
            yield Static(f" [bold]{t('section_general')}[/bold]", classes="section-title")
            yield Horizontal(
                Static(f" {t('language')}", classes="s-label-tall"),
                Select(options=available_languages(), value=s.language, id="set-language", prompt=t("select_prompt")),
                classes="s-row-tall",
            )
            yield Horizontal(
                Static(f" {t('s4_update_url')}", classes="s-label"),
                Input(value=s.s4_url, id="set-s4-url"),
                classes="s-row",
            )
            yield _switch_row(t("check_updates_start"), s.update_check, "set-update-check")
            yield _switch_row(t("warn_unsaved_on_exit"), s.warn_unsaved_on_exit, "set-warn-unsaved")

            # Build
            yield Static(f" [bold]{t('section_build')}[/bold]", classes="section-title")
            yield Horizontal(
                Static(f" {t('compiler_flags')}", classes="s-label"),
                Input(value=s.compiler_opt, id="set-compiler-opt"),
                classes="s-row",
            )
            yield Horizontal(
                Static(f" {t('max_cpu_threads')}", classes="s-label"),
                Input(value=str(s.max_cpus), id="set-max-cpus", type="integer"),
                classes="s-row",
            )
            yield _switch_row(t("patch_webif_info"), s.patch_webif, "set-patch-webif")
            yield _switch_row(t("delete_debug_binary"), s.delete_debug, "set-delete-debug")
            yield _switch_row(t("save_list_smargo"), s.save_smargo, "set-save-smargo")

            # Signing
            yield Static(f" [bold]{t('section_signing')}[/bold]", classes="section-title")
            from textual.widgets import Button as _Btn
            cert_browse = _Btn("📂", id="btn-cert-browse", classes="tc-reset-btn")
            cert_browse.tooltip = t("ph_browse_file")
            key_browse = _Btn("📂", id="btn-key-browse", classes="tc-reset-btn")
            key_browse.tooltip = t("ph_browse_file")
            yield Horizontal(
                Static(f" {t('signing_certificate')}", classes="s-label"),
                Input(value=s.sign_cert, placeholder=t("ph_sign_cert"), id="set-sign-cert"),
                cert_browse,
                classes="s-row",
            )
            yield Horizontal(
                Static(f" {t('private_key')}", classes="s-label"),
                Input(value=s.sign_key, placeholder=t("ph_sign_key"), id="set-sign-key"),
                key_browse,
                classes="s-row",
            )

            # QEMU
            yield Static(f" [bold]{t('section_qemu')}[/bold]", classes="section-title")
            yield _switch_row(t("enable_qemu_test"), s.qemu_test, "set-qemu-test")
            yield Horizontal(
                Static(f" {t('timeout_seconds')}", classes="s-label"),
                Input(value=str(s.qemu_timeout), id="set-qemu-timeout", type="integer"),
                classes="s-row",
            )
            yield Horizontal(
                Static(f" {t('expected_output')}", classes="s-label"),
                Input(value=s.qemu_expect, id="set-qemu-expect"),
                classes="s-row",
            )

            # Log retention
            yield Static(f" [bold]{t('section_logs')}[/bold]", classes="section-title")
            yield Horizontal(
                Static(f" {t('log_max_age_days')}", classes="s-label"),
                Input(value=str(s.log_max_age_days), id="set-log-max-age", type="integer"),
                classes="s-row",
            )
            yield Horizontal(
                Static(f" {t('log_max_count')}", classes="s-label"),
                Input(value=str(s.log_max_count), id="set-log-max-count", type="integer"),
                classes="s-row",
            )
            yield Horizontal(
                Static(f" {t('log_max_size_mb')}", classes="s-label"),
                Input(value=str(s.log_max_size_mb), id="set-log-max-size", type="integer"),
                classes="s-row",
            )

            # Toolchain Builders
            yield Static(f" [bold]{t('section_builders')}[/bold]", classes="section-title")
            try:
                ctng_default = str((self.app.paths.builders_dir / "crosstool-ng").relative_to(self.app.paths.root))
            except Exception:
                ctng_default = str(self.app.paths.builders_dir / "crosstool-ng")
            ctng_browse = _Btn("📂", id="btn-ctng-browse", classes="tc-reset-btn")
            ctng_browse.tooltip = t("ph_browse_dir")
            yield Horizontal(
                Static(f" {t('ctng_dir')}", classes="s-label"),
                Input(value=s.ctng_dir, placeholder=ctng_default, id="set-ctng-dir"),
                ctng_browse,
                classes="s-row",
            )

        yield S4Footer(active_screen="settings")

    def on_mount(self) -> None:
        self.query_one("#set-language", Select).focus()

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

        # Track if language changed
        import translation
        self._language_changed = s.language != translation._current_lang
        if self._language_changed:
            translation.load(s.language)

    _language_changed: bool = False

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-cert-browse":
            self._open_file_picker("set-sign-cert", t("signing_certificate"))
        elif event.button.id == "btn-key-browse":
            self._open_file_picker("set-sign-key", t("private_key"))
        elif event.button.id == "btn-ctng-browse":
            self._open_dir_picker("set-ctng-dir", t("ctng_dir"))

    def _open_file_picker(self, target_input_id: str, label: str) -> None:
        """Open file picker dialog and write the result into the given input."""
        from ui.screens.toolchains import FilePickerScreen
        from pathlib import Path as _P
        start = str(_P.home()) if _P.home().exists() else "/"
        self._picker_target = target_input_id
        self.app.push_screen(
            FilePickerScreen(
                start_path=start,
                title=f"{t('select_file_label')}: {label}",
                simple=True,
                select_label=t("select_btn"),
            ),
            callback=self._on_file_selected,
        )

    def _on_file_selected(self, result) -> None:
        if not result:
            return
        path, _ = result if isinstance(result, tuple) else (result, "")
        if not path:
            return
        target_id = getattr(self, "_picker_target", "")
        if not target_id:
            return
        try:
            self.query_one(f"#{target_id}", Input).value = path
        except Exception:
            pass

    def _open_dir_picker(self, target_input_id: str, label: str) -> None:
        """Open file picker in directory-only mode."""
        from ui.screens.toolchains import FilePickerScreen
        from pathlib import Path as _P
        # Start in current value's parent or home
        try:
            current = self.query_one(f"#{target_input_id}", Input).value.strip()
        except Exception:
            current = ""
        if current:
            p = _P(current)
            start = str(p if p.is_dir() else p.parent)
        else:
            start = str(self.app.paths.builders_dir if self.app.paths.builders_dir.exists() else _P.home())
        self._picker_target = target_input_id
        self.app.push_screen(
            FilePickerScreen(
                start_path=start,
                title=f"{t('select_file_label')}: {label}",
                simple=True,
                select_label=t("select_btn"),
                directories_only=True,
            ),
            callback=self._on_file_selected,
        )

    def is_dirty(self) -> bool:
        """True if any widget value differs from the loaded settings."""
        try:
            s = self.app.settings
            for wid, attr in SWITCH_MAP.items():
                if self.query_one(f"#{wid}", Switch).value != getattr(s, attr):
                    return True
            for wid, attr in INPUT_MAP.items():
                cur = getattr(s, attr)
                val = self.query_one(f"#{wid}", Input).value
                if isinstance(cur, int):
                    try:
                        val = int(val)
                    except ValueError:
                        val = cur
                if val != cur:
                    return True
            for wid, attr in SELECT_MAP.items():
                val = self.query_one(f"#{wid}", Select).value
                if val is not None and val != getattr(s, attr):
                    return True
        except Exception:
            return False
        return False

    def do_save(self) -> str:
        """Save settings (called by App)."""
        self._save()
        if self._language_changed:
            self.notify(t("language_changed"), severity="warning", timeout=5)
        return t("settings_saved")

    def action_go_back(self) -> None:
        # Do NOT auto-save on leave. F12 is the only save path; Esc
        # just pops the screen, which (together with is_dirty) lets
        # the app-level warning handle unsaved state.
        self.app.pop_screen()
