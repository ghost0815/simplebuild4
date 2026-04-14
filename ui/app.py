"""Main Textual application."""

from textual.app import App, ComposeResult
from textual.binding import Binding

from core.paths import Paths
from core.settings import Settings
from translation import t
from ui.commands import S4Commands


class S4App(App):
    """SimpleBuild4 TUI application."""

    TITLE = "SimpleBuild4"
    CSS_PATH = "theme.tcss"

    ENABLE_COMMAND_PALETTE = True
    # Single provider with deterministic ordering (s4 commands first, then system)
    COMMANDS = {S4Commands}

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False),
        Binding("escape", "esc", show=False),
        Binding("ctrl+c", "copy_text", show=False, priority=True),
        Binding("ctrl+a", "select_all", show=False),
        Binding("ctrl+p", "command_palette", show=False),
        Binding("u", "quick_update", show=False),
        Binding("f12", "save_action", show=False),
        Binding("f1", "s4_settings", show=False),
        Binding("f2", "open_targets", show=False),
        Binding("f3", "open_repository", show=False),
        Binding("f4", "open_toolchains", show=False),
        Binding("f5", "build", show=False),
        Binding("f6", "customize", show=False),
        Binding("f9", "tc_update", show=False),
        Binding("f10", "add", show=False),
        Binding("f11", "remove", show=False),
    ]

    def __init__(self, paths: Paths, **kwargs):
        # Load settings early so we can configure CSS_PATH before super().__init__
        settings = Settings.load(paths.root / "data" / "settings.yaml")
        if settings.ascii_mode:
            self.CSS_PATH = ["theme.tcss", "theme_compat.tcss"]
        super().__init__(**kwargs)
        self.paths = paths
        self.settings = settings
        self._update_available: bool = False
        # Build/library-update timer state (read by ui.footer.BuildTimerWidget)
        self._timer_screen: str | None = None
        self._timer_start: float | None = None
        self._timer_stopped_at: float | None = None
        # Web service (textual-serve) subprocess
        self._web_proc: "subprocess.Popen | None" = None

    _UPDATE_NOTIFY_TIMEOUT = 10

    def _expire_update_shortcut(self) -> None:
        self._update_available = False

    # ---------------------------------------------------------------- timer
    def build_timer_start(self, screen: str) -> None:
        """Start (or restart) the build timer for the given screen."""
        import time
        self._timer_screen = screen
        self._timer_start = time.monotonic()
        self._timer_stopped_at = None
        self._refresh_build_timer()

    def build_timer_stop(self) -> None:
        """Freeze the timer at the current elapsed time."""
        import time
        if self._timer_start is not None and self._timer_stopped_at is None:
            self._timer_stopped_at = time.monotonic()
        self._refresh_build_timer()

    def build_timer_reset(self) -> None:
        """Clear the timer entirely."""
        self._timer_screen = None
        self._timer_start = None
        self._timer_stopped_at = None
        self._refresh_build_timer()

    def build_timer_elapsed(self) -> float | None:
        """Return current elapsed seconds, or None if no timer."""
        if self._timer_start is None:
            return None
        import time
        end = self._timer_stopped_at if self._timer_stopped_at is not None else time.monotonic()
        return max(0.0, end - self._timer_start)

    def build_timer_format(self) -> str:
        """Format current elapsed time as HH:MM:SS / MM:SS, or '' if no timer."""
        e = self.build_timer_elapsed()
        if e is None:
            return ""
        h = int(e // 3600)
        m = int((e % 3600) // 60)
        s = int(e % 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _refresh_build_timer(self) -> None:
        try:
            from ui.footer import BuildTimerWidget
            for w in self.query(BuildTimerWidget):
                w.refresh_display()
        except Exception:
            pass

    @property
    def settings_path(self) -> "Path":
        from pathlib import Path
        return self.paths.root / "data" / "settings.yaml"

    # --------------------------------------------------------- web service
    _web_log_file = None

    def _check_decrypt_warnings(self) -> None:
        """Show a single warning if any encrypted passwords failed to decrypt."""
        from core.credentials import pop_decrypt_warnings
        if pop_decrypt_warnings():
            self.notify(t("decrypt_failed_warning"), severity="warning", timeout=15)
        self._decrypt_check_done = True

    def _web_start(self) -> None:
        """Start textual-serve as a separate subprocess."""
        import os
        # Skip when running inside a textual-serve web session to avoid
        # binding the same port twice.
        if "web" in os.environ.get("TEXTUAL_DRIVER", "").lower():
            return
        if self._web_proc is not None and self._web_proc.poll() is None:
            return  # already running
        import subprocess, sys
        # Auto-install textual-serve if missing
        try:
            import textual_serve  # noqa: F401
        except ImportError:
            self.notify("Installing textual-serve …", timeout=5)
            pip_cmd = [sys.executable, "-m", "pip", "install", "--user"]
            try:
                subprocess.check_call(
                    pip_cmd + ["textual-serve"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            except Exception:
                try:
                    subprocess.check_call(
                        pip_cmd + ["--break-system-packages", "textual-serve"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                except subprocess.CalledProcessError as e:
                    err = (e.stderr or b"").decode(errors="replace").strip()
                    self.notify(
                        f"Failed to install textual-serve:\n{err[:200]}",
                        severity="error", timeout=15,
                    )
                    return
                except Exception as e:
                    self.notify(f"Failed to install textual-serve: {e}",
                                severity="error", timeout=15)
                    return
        host = self.settings.web_host or "0.0.0.0"
        port = self.settings.web_port or 8080
        from core.web_service import validate_port
        err = validate_port(port)
        if err:
            self.notify(err, severity="error", timeout=5)
            return

        # public_url is only used for the startup log message now —
        # URLs in the browser are derived from the request Host header.
        from core.web_service import _format_host_for_url
        dns = self.settings.web_dns_name
        _wildcards = ("0.0.0.0", "::", "::/0")
        public_host = dns or ("localhost" if host in _wildcards else host)
        public_host = _format_host_for_url(public_host)
        app_py = str(self.paths.root / "app.py")
        templates = str(self.paths.root / "ui" / "web" / "templates")

        # SSL: auto-generate cert if enabled but no cert provided
        use_ssl = self.settings.web_ssl
        ssl_cert = self.settings.web_ssl_cert
        ssl_key = self.settings.web_ssl_key
        if use_ssl and (not ssl_cert or not ssl_key):
            from core.web_service import generate_self_signed_cert
            web_dir = self.paths.root / "data" / "web"
            ssl_cert, ssl_key = generate_self_signed_cert(web_dir, dns, host)

        protocol = "https" if use_ssl else "http"
        auth_user = self.settings.web_auth_user
        auth_password = self.settings.web_auth_password

        # Open logfile
        from datetime import datetime
        from core.log_cleanup import resolve_log_dir
        _log_dir = resolve_log_dir(
            self.paths.logs_dir, "webservice", self.settings.organize_logs,
        )
        timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
        log_path = _log_dir / f"{timestamp}.webservice.log"
        self._web_log_file = open(log_path, "w", encoding="utf-8")

        # Build S4Server launch code
        from core.web_service import _build_server_code
        server_code = _build_server_code(
            python=sys.executable, app_py=app_py, host=host, port=port,
            public_host=public_host, templates=templates,
            ssl=use_ssl, ssl_cert=ssl_cert, ssl_key=ssl_key,
            auth_user=auth_user, auth_password=auth_password,
            theme=self.settings.theme,
        )

        # Launch as a standalone subprocess to avoid asyncio conflicts
        # with the running Textual app
        self._web_proc = subprocess.Popen(
            [sys.executable, "-c", server_code],
            cwd=str(self.paths.root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid,
        )
        # Background thread to read output and log it
        import threading
        threading.Thread(target=self._web_log_reader, daemon=True).start()
        self.notify(f"Web service started on {protocol}://{public_host}:{port}")

    def _web_log(self, msg: str) -> None:
        """Write a message to the web service logfile and RichLog."""
        import re
        plain = re.sub(r'\[/?[a-z_ #0-9]+\]', '', msg)
        if self._web_log_file:
            self._web_log_file.write(plain + "\n")
            self._web_log_file.flush()
        try:
            from ui.screens.main import MainScreen
            from textual.widgets import RichLog
            # Find MainScreen even if it's not the active screen
            for screen in self.screen_stack:
                if isinstance(screen, MainScreen):
                    try:
                        screen.query_one("#output", RichLog).write(msg)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

    def _web_log_reader(self) -> None:
        """Background thread: read textual-serve stdout and log each line."""
        proc = self._web_proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            stripped = line.rstrip()
            if stripped:
                self._web_log(f"[dim]\\[WEB][/dim] {stripped}")

    def _web_stop(self) -> None:
        """Stop the textual-serve subprocess and all its children."""
        import os, signal
        if self._web_proc is not None and self._web_proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._web_proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self._web_proc.kill()
            try:
                self._web_proc.wait(timeout=1)
            except Exception:
                pass
            self._web_log("[bold]Web service stopped[/bold]")
            self.notify("Web service stopped")
        self._web_proc = None
        if self._web_log_file:
            try:
                self._web_log_file.close()
            except Exception:
                pass
            self._web_log_file = None

    def _web_restart(self) -> None:
        """Restart the web service (e.g. after settings change)."""
        self._web_stop()
        self._web_start()

    def watch_theme(self, theme: str) -> None:
        """Save theme to settings when changed via command palette."""
        if hasattr(self, 'settings') and self.settings.theme != theme:
            self.settings.theme = theme
            self.settings.save(self.settings_path)

    def on_mount(self) -> None:
        self.paths.ensure_dirs()
        # Backup user YAML configs before anything else touches them
        try:
            self.paths.backup_yaml()
        except Exception:
            pass
        # Load translations
        from translation import load as load_translations
        load_translations(self.settings.language)
        # Initialize ASCII mode for icon helper
        from ui.icons import init as _init_icons
        _init_icons(self.settings.ascii_mode)
        # Patch Collapsible symbols and scrollbar chars for ASCII mode
        if self.settings.ascii_mode:
            try:
                import textual.widgets._collapsible as _col
                _kw = _col.Collapsible.__init__.__kwdefaults__
                if _kw and _kw.get("collapsed_symbol") == "▶":
                    _kw["collapsed_symbol"] = "►"
            except Exception:
                pass
            try:
                from textual.scrollbar import ScrollBarRender
                ScrollBarRender.VERTICAL_BARS = ["█", "█", "█", "█", "█", "█", "█", " "]
                ScrollBarRender.HORIZONTAL_BARS = ["█", "█", "█", "█", "█", "█", "█", " "]
            except Exception:
                pass
        # Restore saved theme
        if self.settings.theme:
            self.theme = self.settings.theme
        # Apply log retention at startup so old logs are cleaned
        # even when the user never triggers a build.
        try:
            from core.log_cleanup import cleanup_logs
            cleanup_logs(
                self.paths.logs_dir,
                max_age_days=self.settings.log_max_age_days,
                max_count=self.settings.log_max_count,
                max_size_mb=self.settings.log_max_size_mb,
            )
        except Exception:
            pass

        from ui.screens.main import MainScreen
        self.push_screen(MainScreen())

        # Check for decrypt failures after all configs have loaded (settings,
        # targets, repos).  A short timer ensures the MainScreen's on_mount
        # has finished loading the last target before we collect warnings.
        self.set_timer(1.0, self._check_decrypt_warnings)

        # Start web service if enabled
        if self.settings.web_enabled:
            self._web_start()

        # Check for updates in background
        if self.settings.update_check:
            import threading
            threading.Thread(target=self._check_for_updates, daemon=True).start()

    def _check_for_updates(self) -> None:
        """Background check: git fetch and compare local vs remote."""
        import subprocess
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        if not (root / ".git").exists():
            return
        try:
            subprocess.run(
                ["git", "fetch", "--prune"], cwd=str(root),
                capture_output=True, text=True, timeout=15,
            )
            branch = subprocess.run(
                ["git", "branch", "--show-current"], cwd=str(root),
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            result = subprocess.run(
                ["git", "rev-list", f"HEAD..origin/{branch}", "--count"],
                cwd=str(root), capture_output=True, text=True, timeout=5,
            )
            behind = int(result.stdout.strip() or "0")
            if behind > 0:
                # Collect the actual commit subjects so the user sees what
                # would be applied. Cap to keep the toast readable.
                commits = subprocess.run(
                    ["git", "log", f"HEAD..origin/{branch}",
                     "--pretty=format:%h %s", "--no-merges"],
                    cwd=str(root), capture_output=True, text=True, timeout=5,
                ).stdout.strip().splitlines()
                max_show = 10
                shown = commits[:max_show]
                lines = [f"  • {c}" for c in shown]
                if len(commits) > max_show:
                    lines.append(f"  … +{len(commits) - max_show} more")
                header = t("s4_update_available", count=behind, s="s" if behind > 1 else "")
                msg = f"[bold]{header}[/bold]\n\n" + "\n".join(lines)
                self._update_available = True
                self.call_from_thread(
                    self.notify,
                    msg,
                    severity="information",
                    timeout=self._UPDATE_NOTIFY_TIMEOUT,
                )
                self.call_from_thread(
                    self.set_timer,
                    self._UPDATE_NOTIFY_TIMEOUT,
                    self._expire_update_shortcut,
                )
            else:
                self.call_from_thread(
                    self.notify,
                    t("s4_up_to_date"),
                    timeout=3,
                )
        except Exception:
            pass

    def action_copy_text(self) -> None:
        """Copy selected text to clipboard via Ctrl+C.

        Priority:
        1. Mouse text selection on any widget → copy selection.
        2. Focused RichLog → copy all lines as plain text.
        3. Focused Static → copy its rendered text.
        4. Focused VerticalScroll containing a Static → copy child text.
        """
        from textual.widgets import RichLog, Static, TextArea
        from textual.containers import VerticalScroll
        import re as _re

        selection = self.screen.get_selected_text()
        if selection:
            self.copy_to_clipboard(selection)
            self.notify("Copied to clipboard")
            return

        focused = self.focused
        def _box_title(widget) -> str:
            """Return the border_title of the widget or its parent, or a fallback."""
            for w in (widget, widget.parent):
                if w and hasattr(w, 'border_title') and w.border_title:
                    return str(w.border_title)
            return "Content"

        # RichLog: lines are Strip objects containing Segments.
        if isinstance(focused, RichLog) and focused.lines:
            plain_lines = []
            for line in focused.lines:
                try:
                    plain_lines.append("".join(seg.text for seg in line))
                except (TypeError, AttributeError):
                    plain_lines.append(str(line))
            plain = "\n".join(plain_lines)
            if plain.strip():
                self.copy_to_clipboard(plain)
                self.notify(f"{_box_title(focused)} copied to clipboard")
            return

        # Static widget: strip Rich markup and copy.
        if isinstance(focused, Static):
            raw = str(focused._Static__content) if hasattr(focused, '_Static__content') else ""
            plain = _re.sub(r'\[/?[a-z_ #0-9]+\]', '', raw)
            if plain.strip():
                self.copy_to_clipboard(plain)
                self.notify(f"{_box_title(focused)} copied to clipboard")
            return

        # TextArea (e.g. tc-editor): copy full text.
        if isinstance(focused, TextArea):
            plain = focused.text
            if plain.strip():
                self.copy_to_clipboard(plain)
                self.notify(f"{_box_title(focused)} copied to clipboard")
            return

        # VerticalScroll containing Static children (e.g. repo output, libraries).
        container = focused if isinstance(focused, VerticalScroll) else None
        if not container and focused and focused.parent:
            p = focused.parent
            # Walk up at most 2 levels (widget → Horizontal → VerticalScroll)
            for _ in range(3):
                if isinstance(p, VerticalScroll):
                    container = p
                    break
                p = getattr(p, 'parent', None)
                if p is None:
                    break
        if container is not None:
            from textual.widgets import Select
            # Libraries panel: collect status + action per row.
            lib_statics = container.query(".lib-status")
            if lib_statics:
                lines = []
                for st in lib_statics:
                    raw = str(st._Static__content) if hasattr(st, '_Static__content') else ""
                    text = _re.sub(r'\[/?[a-z_ #0-9.]+\]', '', raw).rstrip()
                    # Find sibling Select in same row
                    row = st.parent
                    action = ""
                    if row:
                        try:
                            sel = row.query_one(Select)
                            val = str(sel.value) if sel.value and sel.value is not Select.BLANK else ""
                            action = val
                        except Exception:
                            pass
                    if action:
                        lines.append(f"{text:<30s} {action}")
                    else:
                        lines.append(text)
                plain = "\n".join(lines)
            else:
                # Generic: collect all Static text.
                parts = []
                for child in container.query(Static):
                    raw = str(child._Static__content) if hasattr(child, '_Static__content') else ""
                    parts.append(_re.sub(r'\[/?[a-z_ #0-9]+\]', '', raw))
                plain = "\n".join(parts)
            if plain.strip():
                self.copy_to_clipboard(plain)
                self.notify(f"{_box_title(container)} copied to clipboard")
            return

    def action_show_help_panel(self) -> None:
        """Override Textual's default Keys to show our keyboard reference."""
        self.action_show_keys()

    def action_show_license(self) -> None:
        """Show COPYING license file in a modal."""
        from textual.screen import ModalScreen
        from textual.widgets import Static
        from textual.containers import VerticalScroll
        from textual.binding import Binding
        from pathlib import Path

        copying = Path(__file__).resolve().parent.parent / "COPYING"
        try:
            content = copying.read_text(encoding="utf-8", errors="replace")
        except Exception:
            self.notify("COPYING file not found", severity="error")
            return

        class LicenseScreen(ModalScreen):
            BINDINGS = [Binding("escape", "dismiss", show=False)]
            DEFAULT_CSS = """
            LicenseScreen {
                align: center middle;
            }
            LicenseScreen > VerticalScroll {
                width: 80;
                height: 85%;
                border: solid $primary;
                background: $surface;
                padding: 1 2;
            }
            """
            def compose(self):
                with VerticalScroll():
                    yield Static(f"[bold]License[/bold]  [dim](Esc to close)[/dim]\n\n{content}")

        self.push_screen(LicenseScreen())

    def action_show_keys(self) -> None:
        """Show keyboard shortcuts help."""
        from textual.screen import ModalScreen
        from textual.widgets import Static
        from textual.containers import VerticalScroll
        from textual.binding import Binding

        class KeysScreen(ModalScreen):
            BINDINGS = [
                Binding("escape", "dismiss", show=False),
                Binding("ctrl+p", "dismiss", show=False),
            ]
            DEFAULT_CSS = """
            KeysScreen {
                align: center middle;
            }
            KeysScreen > VerticalScroll {
                width: 80;
                height: 85%;
                border: solid $primary;
                background: $surface;
                padding: 1 2;
            }
            """
            def compose(self):
                with VerticalScroll():
                    yield Static(HELP_TEXT)

        from translation import t as _t
        K = 20  # key column width
        D = 40  # description column width
        def _r(key, desc):
            return f"  {key:<{K}}{desc}"

        HELP_TEXT = f"""\
[bold]{_t('keys_title')}[/bold]  [dim]({_t('keys_esc_close')})[/dim]

[bold]{_t('keys_navigation')}[/bold]
{_r('Tab / Shift+Tab', _t('keys_next_prev'))}
{_r('Arrow Up/Down', _t('keys_nav_lists'))}
{_r('Enter', _t('keys_activate'))}
{_r('Space', _t('keys_toggle_open'))}
{_r('Escape', _t('keys_close_back'))}

[bold]{_t('keys_text_input')}[/bold]
{_r('Backspace', _t('keys_delete_char'))}
{_r('Ctrl+A', _t('keys_select_all'))}
{_r('Ctrl+C', _t('keys_copy'))}
{_r('Home / End', _t('keys_jump_field'))}

[bold]{_t('keys_dropdowns')}[/bold]
{_r('Space / Enter', _t('keys_open_dropdown'))}
{_r('Arrow Up/Down', _t('keys_nav_options'))}
{_r('Enter', _t('keys_select_option'))}
{_r('Escape', _t('keys_close_no_select'))}

[bold]{_t('keys_switches')}[/bold]
{_r('Space / Enter', _t('keys_toggle'))}

[bold]{_t('keys_tables')}[/bold]
{_r('Arrow Up/Down', _t('keys_select_row'))}
{_r('Home / End', _t('keys_first_last'))}
{_r('Page Up/Down', _t('keys_scroll_page'))}

[bold]{_t('keys_collapsible')}[/bold]
{_r('Enter / Space', _t('keys_expand_collapse'))}

[bold]{_t('keys_scrolling')}[/bold]
{_r('Arrow Up/Down', _t('keys_scroll_content'))}
{_r('Page Up/Down', _t('keys_scroll_page'))}
{_r('Mouse wheel', _t('keys_scroll'))}

[bold]{_t('keys_screens')}[/bold]
{_r('F1', _t('settings_nav'))}
{_r('F2', _t('targets_nav') + ' (Main)')}
{_r('F3', _t('repositories_nav'))}
{_r('F4', _t('toolchains_nav'))}
{_r('Esc', _t('keys_back_quit'))}

[bold]{_t('keys_tabs')}[/bold]
  Main                [bold]C[/bold]ompact  [bold]O[/bold]utput
  Target              [bold]G[/bold]eneral  [bold]R[/bold]epo/Toolchain  [bold]O[/bold]ptions  [bold]U[/bold]pload
  Toolchain left      [bold]P[/bold]reBuild  [bold]S[/bold]elfBuild
  Toolchain right     [bold]D[/bold]etails  [bold]L[/bold]ibraries  [bold]O[/bold]utput

[bold]{_t('keys_actions')}[/bold]
{_r('F5', _t('keys_build_cancel'))}
{_r('F9', _t('keys_update_cancel'))}
{_r('F10', _t('keys_add'))}
{_r('F11', _t('keys_remove'))}
{_r('F12', _t('keys_save'))}
{_r('Ctrl+P', _t('keys_cmd_palette'))}

[bold]{_t('keys_cli')}[/bold]
  ./s4 build <target> [--make|--cmake] [OPTION_FLAG OPTION_FLAG_off ...]
{_r('./s4 targets', _t('keys_list_targets'))}
{_r('./s4 version', _t('keys_show_version'))}
{_r('./s4 update', _t('keys_self_update'))}"""

        self.push_screen(KeysScreen())

    def action_select_all(self) -> None:
        """Select all text in focused widget via Ctrl+A."""
        widget = self.focused
        if widget and hasattr(widget, 'text_select_all'):
            widget.text_select_all()

    def _switch_to(self, screen) -> None:
        """Replace the current non-main screen instead of stacking."""
        from ui.screens.main import MainScreen
        if not isinstance(self.screen, MainScreen):
            self.pop_screen()
        self.push_screen(screen)

    def action_s4_settings(self) -> None:
        from ui.screens.settings import SettingsScreen
        if self._is_building():
            self.notify(t("build_in_progress"), severity="warning")
            return
        if not isinstance(self.screen, SettingsScreen):
            self._switch_to(SettingsScreen())

    def action_check_deps(self) -> None:
        from ui.screens.dep_check import DependencyCheckModal
        self.push_screen(DependencyCheckModal(
            deps_yaml=self.paths.defaults_dir / "dependencies.yaml",
        ))

    def action_rescan_toolchains(self) -> None:
        from core.toolchain import ToolchainRegistry
        registry = ToolchainRegistry(
            self.paths.toolchains_defaults,
            self.paths.toolchains_local,
            self.paths.toolchains_dir,
        )
        registry.load()
        messages = []
        count = registry.rescan_installed(log=lambda msg: messages.append(msg))
        if count:
            self.notify(t("tc_rescan_done").format(count=count), timeout=5)
        else:
            self.notify(t("tc_rescan_none"), timeout=3)
        # Log details to main screen output if available
        try:
            from textual.widgets import RichLog
            output = self.screen.query_one("#output", RichLog)
            for msg in messages:
                output.write(msg)
        except Exception:
            pass

    def _footer_disabled(self, key: str) -> bool:
        """Return True if the footer item for the given F-key is currently disabled.

        Only the current screen's footer is consulted — other screens on the
        stack would otherwise pollute the result.
        """
        try:
            from ui.footer import FooterItem
            for item in self.screen.query(FooterItem):
                if item._key == key:
                    return bool(item._disabled)
        except Exception:
            pass
        return False

    def action_save_action(self) -> None:
        """Dispatch F12 Save to the current screen."""
        if self._footer_disabled("F12"):
            return
        screen = self.screen
        if hasattr(screen, 'do_save'):
            # Snapshot web settings before save to detect changes
            from ui.screens.settings import SettingsScreen
            is_settings = isinstance(screen, SettingsScreen)
            if is_settings:
                _was_web = self.settings.web_enabled
                _old_host = self.settings.web_host
                _old_port = self.settings.web_port
            # Remember focused widget so we can restore after save/refresh.
            focus_id = self.focused.id if self.focused else None
            msg = screen.do_save()
            if msg:
                self.notify(msg)
            # Manage web service after settings save
            if is_settings:
                if self.settings.web_enabled:
                    if (not _was_web
                            or self.settings.web_host != _old_host
                            or self.settings.web_port != _old_port):
                        self._web_restart()
                elif _was_web:
                    self._web_stop()
            # Restore focus after save (screen may have refreshed).
            if focus_id:
                try:
                    self.screen.query_one(f"#{focus_id}").focus()
                except Exception:
                    pass

    def action_build(self) -> None:
        """Dispatch F5 Build to main screen."""
        if self._footer_disabled("F5"):
            return
        screen = self.screen
        if hasattr(screen, 'action_build'):
            screen.action_build()

    def action_customize(self) -> None:
        """Dispatch F6 Customize to the current screen (Toolchains SelfBuild)."""
        if self._footer_disabled("F6"):
            return
        screen = self.screen
        if hasattr(screen, 'action_customize'):
            screen.action_customize()

    _pending_esc_discard: float = 0.0
    _pending_esc_screen_id: int = 0

    @property
    def _is_web_session(self) -> bool:
        import os
        return "web" in os.environ.get("TEXTUAL_DRIVER", "").lower()

    def _web_logout(self) -> None:
        """Redirect the browser to /logout to clear session cookie."""
        # Inject JS redirect via Textual's run_action mechanism
        self.exit()

    def action_quit(self) -> None:
        if self._is_web_session:
            self._web_logout()
            return
        self.exit()

    def action_esc(self) -> None:
        """Esc: quit on main (logout in browser), back on sub-screens.

        When the current screen has unsaved changes the first Esc
        shows a warning toast and refuses to leave; a second Esc
        within 5 s actually discards the changes and pops the screen.
        """
        from ui.screens.main import MainScreen
        if isinstance(self.screen, MainScreen):
            if self._is_web_session:
                self._web_logout()
                return
            self.exit()
            return

        import time
        screen = self.screen
        is_dirty = getattr(screen, "is_dirty", None)
        dirty = False
        if callable(is_dirty) and getattr(self.settings, "warn_unsaved_on_exit", False):
            try:
                dirty = bool(is_dirty())
            except Exception:
                dirty = False

        if dirty:
            now = time.time()
            same_screen = id(screen) == self._pending_esc_screen_id
            if not same_screen or now - self._pending_esc_discard > 5.0:
                self._pending_esc_discard = now
                self._pending_esc_screen_id = id(screen)
                self.notify(
                    t("unsaved_changes_confirm", fkey="Esc"),
                    severity="warning",
                    timeout=5,
                )
                return
            # Second press within the grace window → discard
            self._pending_esc_discard = 0.0
            self._pending_esc_screen_id = 0

        if hasattr(screen, 'action_go_back'):
            screen.action_go_back()
        else:
            self.pop_screen()

    def action_open_targets(self) -> None:
        from ui.screens.main import MainScreen
        if isinstance(self.screen, MainScreen):
            return
        if self._is_building():
            self.notify(t("build_in_progress"), severity="warning")
            return
        # Going back to main pops every screen above it — if any of
        # those have unsaved changes, warn once and require a second
        # press to actually discard them. Same grace window as Esc.
        import time
        dirty_screens: list[str] = []
        if getattr(self.settings, "warn_unsaved_on_exit", False):
            for scr in self.screen_stack:
                if isinstance(scr, MainScreen):
                    continue
                is_dirty = getattr(scr, "is_dirty", None)
                if callable(is_dirty):
                    try:
                        if is_dirty():
                            dirty_screens.append(type(scr).__name__)
                    except Exception:
                        pass
        if dirty_screens:
            now = time.time()
            if now - self._pending_esc_discard > 5.0 or self._pending_esc_screen_id != 0:
                self._pending_esc_discard = now
                self._pending_esc_screen_id = 0  # special marker for mass-pop
                self.notify(
                    t("unsaved_changes_confirm", fkey="F2"),
                    severity="warning",
                    timeout=5,
                )
                return
            self._pending_esc_discard = 0.0
        while not isinstance(self.screen, MainScreen):
            self.pop_screen()

    def action_open_repository(self) -> None:
        from ui.screens.repository import RepositoryScreen
        if self._is_building():
            self.notify(t("build_in_progress"), severity="warning")
            return
        if not isinstance(self.screen, RepositoryScreen):
            self._switch_to(RepositoryScreen())

    def action_open_toolchains(self) -> None:
        from ui.screens.toolchains import ToolchainsScreen
        if self._is_building():
            self.notify(t("build_in_progress"), severity="warning")
            return
        if not isinstance(self.screen, ToolchainsScreen):
            self._switch_to(ToolchainsScreen())

    def action_tc_update(self) -> None:
        """Dispatch F9 Update to toolchains screen."""
        if self._footer_disabled("F9"):
            return
        screen = self.screen
        if hasattr(screen, 'do_tc_update'):
            screen.do_tc_update()

    def action_add(self) -> None:
        """Dispatch F10 Add to the current screen."""
        if self._footer_disabled("F10"):
            return
        screen = self.screen
        if hasattr(screen, 'do_add'):
            screen.do_add()

    def action_remove(self) -> None:
        """Dispatch F11 Remove to the current screen."""
        if self._footer_disabled("F11"):
            return
        screen = self.screen
        if hasattr(screen, 'do_remove'):
            screen.do_remove()

    def _is_building(self) -> bool:
        """Return True if any screen on the stack has a running build."""
        try:
            for screen in self.screen_stack:
                if getattr(screen, "_building", False):
                    return True
        except Exception:
            pass
        return False

    def _check_busy_or_dirty(self) -> tuple:
        """Check if any screen has a running operation or actual unsaved changes.

        Returns (busy_screen_name, dirty_screen_name). Either may be empty.

        A screen is only considered dirty if it explicitly implements an
        is_dirty() method that returns True. The previous heuristic
        (any screen with do_save() = dirty) produced too many false
        positives — Settings/Repository/Toolchains/TargetEdit always have
        do_save() but are only actually dirty when the user changed
        something.
        """
        busy = ""
        dirty = ""
        try:
            for screen in self.screen_stack:
                if getattr(screen, "_building", False):
                    busy = type(screen).__name__
                    break
            for screen in self.screen_stack:
                is_dirty = getattr(screen, "is_dirty", None)
                if callable(is_dirty):
                    try:
                        if is_dirty():
                            dirty = type(screen).__name__
                            break
                    except Exception:
                        pass
        except Exception:
            pass
        return (busy, dirty)

    def action_quick_update(self) -> None:
        """Shortcut 'u' — triggers s4 update only while the update notification is visible."""
        from textual.widgets import Input, TextArea
        # Don't intercept 'u' when typing in an input field
        if isinstance(self.focused, (Input, TextArea)):
            return
        if not self._update_available:
            return
        self._update_available = False
        self.action_s4_update()

    def action_s4_update(self) -> None:
        """Self-update s4 — direct update to latest (called by 'u' shortcut and startup check)."""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent

        # Check for running operations — block update entirely
        busy, dirty = self._check_busy_or_dirty()
        if busy:
            self.notify(t("update_busy"), severity="warning", timeout=10)
            return
        if dirty:
            self.notify(t("update_dirty_warning"), severity="warning", timeout=15)

        # Direct update to latest
        self._do_s4_update(root, target_sha="")

    def action_s4_update_modal(self) -> None:
        """Show commit selection modal for s4 update (called from Ctrl+P menu)."""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent

        busy, dirty = self._check_busy_or_dirty()
        if busy:
            self.notify(t("update_busy"), severity="warning", timeout=10)
            return
        if dirty:
            self.notify(t("update_dirty_warning"), severity="warning", timeout=15)

        if (root / ".git").exists():
            from ui.screens.s4_update_modal import S4UpdateModal
            self.push_screen(S4UpdateModal(root), callback=self._on_s4_update_selected)
        else:
            self._do_s4_update(root, target_sha="")

    def _on_s4_update_selected(self, sha: str | None) -> None:
        """Callback from S4UpdateModal — perform the actual update."""
        if not sha:
            return
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        self._do_s4_update(root, target_sha=sha)

    def _do_s4_update(self, root, target_sha: str = "") -> None:
        """Perform the actual s4 update. If target_sha is set, reset to that commit."""
        import subprocess

        # Track success for auto-restart at the end
        updated_ok = False

        # Setup logging: RichLog + file
        import re
        from datetime import datetime
        from core.log_cleanup import resolve_log_dir
        _log_dir = resolve_log_dir(self.paths.logs_dir, "s4", self.settings.organize_logs)
        log_path = _log_dir / f"{datetime.now().strftime('%Y-%m-%d.%H-%M-%S')}.s4-update.log"
        log_file = open(log_path, "w", encoding="utf-8")

        def _log(msg: str) -> None:
            # Write plain to file
            plain = re.sub(r'\[/?[a-z_ #0-9]+\]', '', msg)
            log_file.write(plain + "\n")
            log_file.flush()
            # Write markup to RichLog
            try:
                from ui.screens.main import MainScreen
                from textual.widgets import RichLog
                screen = self.screen
                if isinstance(screen, MainScreen):
                    screen.query_one("#output", RichLog).write(msg)
            except Exception:
                pass

        has_git = (root / ".git").exists()

        try:
            _log("[bold]s4 self-update[/bold]")
            _log("")

            if has_git:
                # --- Git repo: fetch + reset to target ---
                local_commit = subprocess.run(
                    ["git", "log", "HEAD", "-n1", "--oneline"], cwd=str(root),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                local_rev = subprocess.run(
                    ["git", "rev-list", "HEAD", "--count"], cwd=str(root),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                _log(f"Local:  r{local_rev} {local_commit}")

                if target_sha:
                    target_commit = subprocess.run(
                        ["git", "log", target_sha, "-n1", "--oneline"], cwd=str(root),
                        capture_output=True, text=True, timeout=5,
                    ).stdout.strip()
                    _log(f"Target: {target_commit}")
                _log("")

                subprocess.run(["git", "checkout", "."], cwd=str(root), capture_output=True, timeout=10)
                subprocess.run(["git", "clean", "-fd"], cwd=str(root), capture_output=True, timeout=10)

                _log("Updating...")
                result = subprocess.run(
                    ["git", "reset", "--hard", target_sha or "origin/HEAD"], cwd=str(root),
                    capture_output=True, text=True, timeout=30,
                )

                if result.returncode == 0:
                    new_rev = subprocess.run(
                        ["git", "rev-list", "HEAD", "--count"], cwd=str(root),
                        capture_output=True, text=True, timeout=5,
                    ).stdout.strip()
                    new_commit = subprocess.run(
                        ["git", "log", "HEAD", "-n1", "--oneline"], cwd=str(root),
                        capture_output=True, text=True, timeout=5,
                    ).stdout.strip()
                    _log(f"[green]Updated to r{new_rev}: {new_commit}[/green]")
                    updated_ok = True
                else:
                    _log(f"[red]Update failed: {result.stderr.strip()}[/red]")

            else:
                # --- No git repo: clone to temp, copy files over ---
                import shutil
                s4_url = self.settings.s4_url
                _log(f"No git repo — updating from {s4_url}")

                tmp_dir = Path("/tmp/s4_update")
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir)

                _log("Cloning...")
                result = subprocess.run(
                    ["git", "clone", "--depth", "1", s4_url, str(tmp_dir)],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    _log(f"[red]Clone failed: {result.stderr.strip()}[/red]")
                    self.notify("Update failed", severity="error")
                    return

                _log("Updating files...")
                for item in tmp_dir.iterdir():
                    if item.name in ("data", ".git"):
                        continue
                    dest = root / item.name
                    if dest.exists():
                        if dest.is_dir():
                            shutil.rmtree(dest)
                        else:
                            dest.unlink()
                    if item.is_dir():
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)
                    _log(f"  {item.name}")

                shutil.rmtree(tmp_dir)
                _log(f"[green]Updated from {s4_url}[/green]")
                updated_ok = True
        except Exception as e:
            _log(f"[red]Update failed: {e}[/red]")
            self.notify(f"Update failed: {e}", severity="error")
        finally:
            _log(f"Log: {log_path}")
            log_file.close()
            try:
                from core.log_cleanup import run_retention
                run_retention(self.paths.logs_dir, self.settings, log=_log)
            except Exception:
                pass

        # Auto-restart if update succeeded and no unsaved changes
        if updated_ok:
            _, dirty = self._check_busy_or_dirty()
            if dirty:
                self.notify(t("update_done_manual_restart"), severity="warning", timeout=15)
            else:
                self.notify(t("update_done_restarting"), severity="information", timeout=3)
                self.set_timer(1.5, self._restart_s4)

    def _restart_s4(self) -> None:
        """Restart s4 in-place via os.execv()."""
        import os
        import sys
        # Restart systemd web service if it's running (picks up new code)
        try:
            from core.web_service import WebServiceManager
            mgr = WebServiceManager(self.paths.root)
            if mgr.systemd_available() and mgr.is_installed() and mgr.is_running():
                mgr.restart()
        except Exception:
            pass
        # In a web session, just exit — the outer s4 or systemd handles restart
        if self._is_web_session:
            try:
                self.exit()
            except Exception:
                pass
            return
        # Stop embedded web service
        try:
            self._web_stop()
        except Exception:
            pass
        try:
            self.exit()
        except Exception:
            pass
        # Replace current process with a fresh s4 invocation
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception:
            pass
