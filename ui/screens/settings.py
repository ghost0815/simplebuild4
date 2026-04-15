"""Settings screen — s4 application settings."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Select, Switch, Input, Collapsible, Button
from ui.icons import icon
from textual.containers import Horizontal, VerticalScroll

from translation import t, available_languages
from ui.header import S4Header
from ui.footer import S4Footer

SWITCH_MAP = {
    "set-update-check": "update_check",
    "set-warn-unsaved": "warn_unsaved_on_exit",
    "set-confirm-dangerous": "confirm_dangerous",
    "set-ascii-mode": "ascii_mode",
    "set-patch-webif": "patch_webif",
    "set-delete-debug": "delete_debug",
    "set-save-smargo": "save_smargo",
    "set-export-libs-zip": "export_libs_zip",
    "set-organize-logs": "organize_logs",
    "set-qemu-test": "qemu_test",
    "set-web-enabled": "web_enabled",
    "set-web-ssl": "web_ssl",
}

INPUT_MAP = {
    "set-s4-url": "s4_url",
    "set-compiler-opt": "compiler_opt",
    "set-max-cpus": "max_cpus",
    "set-sign-cert": "sign_cert",
    "set-sign-key": "sign_key",
    "set-qemu-timeout": "qemu_timeout",
    "set-qemu-expect": "qemu_expect",
    "set-web-host": "web_host",
    "set-web-port": "web_port",
    "set-web-dns": "web_dns_name",
    "set-web-ssl-cert": "web_ssl_cert",
    "set-web-ssl-key": "web_ssl_key",
    "set-web-auth-user": "web_auth_user",
    "set-web-auth-pass": "web_auth_password",
    "set-ctng-dir": "ctng_dir",
    "set-log-max-age": "log_max_age_days",
    "set-log-max-count": "log_max_count",
    "set-log-max-size": "log_max_size_mb",
}

SELECT_MAP = {
    "set-language": "language",
}


def _switch_cell(label: str, value: bool, widget_id: str) -> Horizontal:
    return Horizontal(
        Static(f" {label}", classes="s-toggle-label"),
        Switch(value=value, id=widget_id),
        classes="s-toggle-cell",
    )


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
            from textual.widgets import Button as _Btn

            # General
            with Collapsible(title=t('section_general'), collapsed=False, id="col-general"):
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
                from textual.containers import Grid
                with Grid(classes="s-toggle-grid"):
                    yield _switch_cell(t("check_updates_start"), s.update_check, "set-update-check")
                    yield _switch_cell(t("warn_unsaved_on_exit"), s.warn_unsaved_on_exit, "set-warn-unsaved")
                    yield _switch_cell(t("confirm_dangerous"), s.confirm_dangerous, "set-confirm-dangerous")
                    yield _switch_cell(t("ascii_mode"), s.ascii_mode, "set-ascii-mode")

            # Log retention
            with Collapsible(title=t('section_logs'), collapsed=True, id="col-logs"):
                yield _switch_row(t("organize_logs"), s.organize_logs, "set-organize-logs")
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

            # Build
            with Collapsible(title=t('section_build'), collapsed=True, id="col-build"):
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
                with Grid(classes="s-toggle-grid"):
                    yield _switch_cell(t("patch_webif_info"), s.patch_webif, "set-patch-webif")
                    yield _switch_cell(t("delete_debug_binary"), s.delete_debug, "set-delete-debug")
                    yield _switch_cell(t("save_list_smargo"), s.save_smargo, "set-save-smargo")
                    yield _switch_cell(t("export_libs_zip"), s.export_libs_zip, "set-export-libs-zip")

            # Signing
            with Collapsible(title=t('section_signing'), collapsed=True, id="col-signing"):
                cert_browse = _Btn(icon("browse"), id="btn-cert-browse", classes="tc-reset-btn")
                cert_browse.tooltip = t("ph_browse_file")
                key_browse = _Btn(icon("browse"), id="btn-key-browse", classes="tc-reset-btn")
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
            with Collapsible(title=t('section_qemu'), collapsed=True, id="col-qemu"):
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

            # Web Service
            with Collapsible(title=t('section_webservice'), collapsed=True, id="col-webservice"):
                _port = Input(value=str(s.web_port), id="set-web-port", type="integer", max_length=5)
                _port.styles.width = 12
                _port.styles.min_width = 12
                _port.styles.max_width = 12
                _cert_browse = Button(icon("browse"), id="btn-web-cert-browse", classes="tc-reset-btn")
                _cert_browse.tooltip = t("ph_browse_file")
                _key_browse = Button(icon("browse"), id="btn-web-key-browse", classes="tc-reset-btn")
                _key_browse.tooltip = t("ph_browse_file")
                _show_pw = Button(icon("eye"), id="btn-web-auth-pw-toggle", classes="tc-reset-btn")
                _show_pw.tooltip = t("tt_show_password")
                def _sp():
                    s_ = Static(" ")
                    s_.styles.width = 1
                    s_.styles.min_width = 1
                    s_.styles.max_width = 1
                    return s_
                # Row 1: Enable/Test toggle | HTTPS toggle
                with Grid(classes="web-grid"):
                    _web_toggle = Switch(value=s.web_enabled, id="set-web-enabled")
                    _web_toggle.tooltip = t("tt_web_embedded")
                    yield Horizontal(
                        Static(f" {t('enable_webservice')}", classes="s-label"),
                        _web_toggle,
                        Static("", id="web-embedded-status"),
                        classes="s-toggle",
                    )
                    yield Horizontal(
                        Static(f" {t('web_ssl')}", classes="s-label"),
                        Switch(value=s.web_ssl, id="set-web-ssl"),
                        classes="s-toggle",
                    )
                # Row 2: Host/Port | DNS
                # Row 3: Username | Password
                with Grid(classes="web-grid"):
                    yield Horizontal(
                        Static(f" {t('web_host')} / {t('web_port')}", classes="s-label"),
                        Input(value=s.web_host, placeholder="e.g. 0.0.0.0", id="set-web-host"),
                        _port,
                        classes="s-row",
                    )
                    yield Horizontal(
                        Static(f" {t('web_dns_name')}", classes="s-label"),
                        Input(value=s.web_dns_name, placeholder=t("web_dns_placeholder"), id="set-web-dns"),
                        classes="s-row",
                    )
                    yield Horizontal(
                        Static(f" {t('web_auth_user')}", classes="s-label"),
                        Input(value=s.web_auth_user, placeholder=t("web_auth_placeholder"), id="set-web-auth-user"),
                        classes="s-row",
                    )
                    yield Horizontal(
                        Static(f" {t('web_auth_pass_ph')}", classes="s-label"),
                        Input(value=s.web_auth_password, placeholder=t("web_auth_pass_ph"), id="set-web-auth-pass", password=True),
                        _show_pw, _sp(),
                        classes="s-row",
                    )
                # Row 4: Cert | Key
                with Grid(classes="web-grid"):
                    yield Horizontal(
                        Static(f" {t('web_ssl_cert')}", classes="s-label"),
                        Input(value=s.web_ssl_cert, placeholder=t("web_ssl_auto"), id="set-web-ssl-cert"),
                        _cert_browse, _sp(),
                        classes="s-row",
                    )
                    yield Horizontal(
                        Static(f" {t('web_ssl_key')}", classes="s-label"),
                        Input(value=s.web_ssl_key, placeholder=t("web_ssl_auto"), id="set-web-ssl-key"),
                        _key_browse, _sp(),
                        classes="s-row",
                    )
                # Status + service buttons
                with Grid(classes="web-grid", id="web-svc-buttons"):
                    with Horizontal(classes="web-btn-row"):
                        yield Static("", id="web-svc-status", classes="web-svc-status-label")
                        yield Button(t("web_start"), id="btn-web-start", variant="success", disabled=True)
                        yield Button(t("web_stop"), id="btn-web-stop", variant="warning", disabled=True)
                    with Horizontal(classes="web-btn-row"):
                        yield Static("", id="web-btn-gap")
                        yield Button(t("web_setup"), id="btn-web-setup", variant="primary")

            # Toolchain Builders
            with Collapsible(title=t('section_builders'), collapsed=True, id="col-builders"):
                try:
                    ctng_default = str((self.app.paths.builders_dir / "crosstool-ng").relative_to(self.app.paths.root))
                except Exception:
                    ctng_default = str(self.app.paths.builders_dir / "crosstool-ng")
                ctng_browse = _Btn(icon("browse"), id="btn-ctng-browse", classes="tc-reset-btn")
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
        self._refresh_web_svc_status()

    def on_switch_changed(self, event) -> None:
        sid = event.switch.id or ""
        if sid == "set-web-enabled":
            mgr = self._get_web_svc_mgr()
            svc_running = mgr.systemd_available() and mgr.is_installed() and mgr.is_running()
            if not svc_running:
                # Save host/port from current inputs before starting
                s = self.app.settings
                s.web_enabled = event.value
                try:
                    s.web_host = self.query_one("#set-web-host", Input).value
                    s.web_port = int(self.query_one("#set-web-port", Input).value)
                except (ValueError, Exception):
                    pass
                # Validate port before starting
                if event.value:
                    from core.web_service import validate_port
                    err = validate_port(s.web_port)
                    if err:
                        self.notify(err, severity="error", timeout=5)
                        event.switch.value = False
                        self._refresh_web_svc_status()
                        return
                s.save(self.app.settings_path)
                if event.value:
                    self.app._web_start()
                else:
                    self.app._web_stop()
            self._refresh_web_svc_status()
        if sid == "set-qemu-test" and event.value:
            # Check if any qemu binary is installed
            import shutil
            qemu_bins = ["qemu-arm", "qemu-aarch64", "qemu-mipsel"]
            found = any(shutil.which(q) for q in qemu_bins)
            if not found:
                self.notify(
                    t("dep_qemu_missing"),
                    severity="warning", timeout=8,
                )

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
            # Pass user_dir so the data/translation/ overlay still wins
            # after a runtime language switch (otherwise the bundled
            # translation would replace the merged dict).
            translation.load(s.language, user_dir=self.app.paths.translation_user_dir)

    _language_changed: bool = False

    def _get_web_svc_mgr(self):
        from core.web_service import WebServiceManager
        return WebServiceManager(self.app.paths.root)

    def _refresh_web_svc_status(self) -> None:
        """Update service status label and button states."""
        mgr = self._get_web_svc_mgr()
        available = mgr.systemd_available()
        installed = mgr.is_installed() if available else False
        running = mgr.is_running() if installed else False

        try:
            toggle = self.query_one("#set-web-enabled", Switch)
            toggle_on = toggle.value

            # Service running → lock the embedded toggle off
            if running and toggle_on:
                toggle.value = False
                toggle_on = False
            toggle.disabled = running

            # Build status text
            embedded_running = (
                self.app._web_proc is not None
                and self.app._web_proc.poll() is None
            )
            # Embedded status (top row, next to toggle)
            try:
                embed_label = self.query_one("#web-embedded-status", Static)
                if embedded_running:
                    embed_label.update(f" [green]{t('web_embedded_running')}[/green]")
                else:
                    embed_label.update("")
            except Exception:
                pass
            # Service status (bottom row, next to buttons)
            if running:
                svc_status = f"[green]{mgr.service}: {t(mgr.status_text())}[/green]"
            elif installed:
                svc_status = f"{mgr.service}: {t(mgr.status_text())}"
            else:
                svc_status = t(mgr.status_text())
            self.query_one("#web-svc-status", Static).update(
                f" {svc_status}")
            # Start/Restart combined button
            start_btn = self.query_one("#btn-web-start", Button)
            start_btn.label = t("web_restart") if running else t("web_start")
            start_btn.disabled = (not installed or toggle_on)
            self.query_one("#btn-web-stop", Button).disabled = (
                not installed or not running)
        except Exception:
            pass

    def _web_svc_action(self, action: str) -> None:
        """Run a service action in a background thread."""
        import threading
        threading.Thread(
            target=self._do_web_svc_action, args=(action,), daemon=True,
        ).start()

    def _do_web_svc_action(self, action: str) -> None:
        mgr = self._get_web_svc_mgr()
        s = self.app.settings

        def _log(msg):
            self.app.call_from_thread(self.notify, msg, timeout=5)

        if action in ("start", "restart") and mgr.is_installed():
            # Update unit file with current settings before start/restart
            if not mgr.update_unit(
                host=s.web_host, port=s.web_port, dns_name=s.web_dns_name,
                ssl=s.web_ssl, ssl_cert=s.web_ssl_cert, ssl_key=s.web_ssl_key,
                auth_user=s.web_auth_user, auth_password=s.web_auth_password,
                logs_dir=self.app.paths.logs_dir,
                organize_logs=s.organize_logs, theme=s.theme,
                log=_log,
            ):
                def _warn(msg):
                    self.app.call_from_thread(self.notify, msg, severity="warning", timeout=8)
                _warn("Could not update service config — run Setup to refresh permissions")

        if action == "start":
            mgr.start(log=_log)
        elif action == "stop":
            mgr.stop(log=_log)
        elif action == "restart":
            mgr.restart(log=_log)

        self.app.call_from_thread(self._refresh_web_svc_status)

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-cert-browse":
            self._open_file_picker("set-sign-cert", t("signing_certificate"))
        elif event.button.id == "btn-key-browse":
            self._open_file_picker("set-sign-key", t("private_key"))
        elif event.button.id == "btn-ctng-browse":
            self._open_dir_picker("set-ctng-dir", t("ctng_dir"))
        elif event.button.id == "btn-web-cert-browse":
            self._open_file_picker("set-web-ssl-cert", t("web_ssl_cert"))
        elif event.button.id == "btn-web-key-browse":
            self._open_file_picker("set-web-ssl-key", t("web_ssl_key"))
        elif event.button.id == "btn-web-auth-pw-toggle":
            try:
                pw = self.query_one("#set-web-auth-pass", Input)
                pw.password = not pw.password
            except Exception:
                pass
        elif event.button.id == "btn-web-setup":
            from ui.screens.web_service_modal import WebServiceSetupModal
            self.app.push_screen(
                WebServiceSetupModal(),
                callback=lambda _: self._refresh_web_svc_status(),
            )
        elif event.button.id == "btn-web-start":
            mgr = self._get_web_svc_mgr()
            self._web_svc_action("restart" if mgr.is_running() else "start")
        elif event.button.id == "btn-web-stop":
            self._web_svc_action("stop")

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
        old_ascii = self.app.settings.ascii_mode
        self._save()
        from core.credentials import clear_persistent_warnings
        clear_persistent_warnings()
        if self._language_changed:
            self.notify(t("language_changed"), severity="warning", timeout=5)
        if self.app.settings.ascii_mode != old_ascii:
            self.notify(t("ascii_mode_restart"), severity="warning", timeout=10)
        return t("settings_saved")

    def action_go_back(self) -> None:
        # Do NOT auto-save on leave. F12 is the only save path; Esc
        # just pops the screen, which (together with is_dirty) lets
        # the app-level warning handle unsaved state.
        self.app.pop_screen()
