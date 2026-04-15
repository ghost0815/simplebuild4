"""Automation screen — cron-based job management."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Static, Switch, Input, RichLog, Collapsible, Checkbox, OptionList
from textual.widgets.option_list import Option
from textual.containers import Horizontal, Vertical, VerticalScroll, Grid

from translation import t
from ui.header import S4Header
from ui.footer import S4Footer
from ui.multi_select import MultiSelect
from core.automation import (
    JobConfig, load_jobs, save_jobs, load_state,
    cron_to_human, cron_next_run, install_crontab, uninstall_crontab, is_crontab_installed,
)

# Atomic actions in execution order
_ACTION_ITEMS = [
    "build-toolchains",
    "update-libraries",
    "update-repositories",
    "build-targets-conditional",
    "build-targets-force",
]

class LogBox(RichLog):
    """RichLog that posts TitleClicked when the user clicks the top border (title area)."""

    class TitleClicked(Message):
        """Posted when the border-title row is clicked."""
        def __init__(self, overlay_id: str) -> None:
            super().__init__()
            self.overlay_id = overlay_id

    def on_click(self, event) -> None:
        if event.offset.y == 0:
            event.stop()
            overlay_id = (
                "log-history-output" if self.id == "sched-log-big"
                else "log-history-details"
            )
            self.post_message(self.TitleClicked(overlay_id))


class LogHistoryOverlay(OptionList):
    """Floating single-select list of log files for the current job."""

    def on_blur(self) -> None:
        self.display = False

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.display = False
            event.stop()


def _fmt_duration(sec: int) -> str:
    """Format duration in seconds as 'HHh MMm SSs', 'MMm SSs', or 'SSs'."""
    try:
        s = max(0, int(sec))
    except Exception:
        return ""
    h, rem = divmod(s, 3600)
    m, sec2 = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m {sec2:02d}s"
    if m > 0:
        return f"{m}m {sec2:02d}s"
    return f"{sec2}s"


def _running_duration(started_at_iso: str) -> int:
    """Compute live duration in seconds from an ISO 'started_at' timestamp."""
    from datetime import datetime as _dt
    if not started_at_iso:
        return 0
    try:
        start = _dt.fromisoformat(started_at_iso)
        return max(0, int((_dt.now() - start).total_seconds()))
    except Exception:
        return 0


def _action_description(action: str) -> str:
    """Translated description for an action (used in tooltips)."""
    key = {
        "update-libraries": "auto_desc_update_libs",
        "update-repositories": "auto_desc_update_repos",
        "build-targets-conditional": "auto_desc_build_cond",
        "build-targets-force": "auto_desc_build_force",
        "build-toolchains": "auto_desc_build_toolchains",
    }.get(action, "")
    return t(key) if key else action


# Legacy alias kept for places still looking up by action name.
_ACTION_DESCRIPTIONS = {a: None for a in _ACTION_ITEMS}


def _validate_job(sched: JobConfig) -> tuple[list[str], list[str]]:
    """Validate a job config. Returns (errors, warnings).

    Errors block save, warnings are only informational.
    """
    errors = []
    warnings = []

    # Name must not contain chars that break shell/cron/YAML parsing
    if sched.name:
        for ch in ("'", '"', "\n", "\\"):
            if ch in sched.name:
                errors.append(t("auto_err_name_char").format(ch=repr(ch)))
                break

    # Expand 'all' to the full action set so downstream checks see
    # concrete action names rather than the collapsing sentinel.
    actions = list(_ACTION_ITEMS) if sched.actions == ["all"] else sched.actions

    if not actions:
        if sched.enabled:
            errors.append(t("auto_err_no_actions"))
        return errors, warnings  # nothing more to validate

    if "update-repositories" in actions and not sched.repos and not sched.targets:
        warnings.append(t("auto_warn_repos_needs"))

    if "update-libraries" in actions and not sched.toolchains and not sched.targets:
        warnings.append(t("auto_warn_libs_needs"))

    if "build-targets-conditional" in actions and not sched.targets:
        warnings.append(t("auto_warn_cond_no_targets"))

    if "build-targets-force" in actions and not sched.targets:
        warnings.append(t("auto_warn_force_no_targets"))

    if "build-toolchains" in actions and not sched.templates:
        warnings.append(t("auto_warn_build_toolchains_needs_templates"))

    if "build-toolchains" in actions and "update-libraries" not in actions:
        warnings.append(t("auto_warn_build_toolchains_without_libs"))

    if "build-targets-conditional" in actions and "update-repositories" not in actions:
        warnings.append(t("auto_warn_cond_needs_repos"))

    if "build-targets-conditional" in actions and "build-targets-force" in actions:
        warnings.append(t("auto_warn_cond_vs_force"))

    return errors, warnings


def _actions_tooltip(selected: list[str]) -> str:
    """Generate a tooltip describing the selected action pipeline."""
    if not selected:
        return t("auto_actions_tooltip_none")
    # Expand 'all' to every action, keep the defined pipeline order
    effective = _ACTION_ITEMS if selected == ["all"] else selected
    ordered = [a for a in _ACTION_ITEMS if a in effective]
    lines = []
    for i, action in enumerate(ordered, 1):
        lines.append(f"  {i}. {action}\n     {_action_description(action)}")
    return t("auto_actions_tooltip_header") + "\n" + "\n".join(lines)

# Transient in-memory state shared across screen rebuilds (not persisted until F12)
_pending_jobs: list[JobConfig] | None = None


class AutomationScreen(Screen):
    BINDINGS = [
        ("d", "switch_tab('details')", "Details"),
        ("o", "switch_tab('output')", "Output"),
    ]

    # Class-level so the active tab is preserved across rebuilds
    _active_tab: str = "details"

    def __init__(self) -> None:
        super().__init__()
        self._log_timer = None

    def compose(self) -> ComposeResult:
        global _pending_jobs
        if _pending_jobs is not None:
            self._schedules = _pending_jobs
            _pending_jobs = None
        else:
            self._schedules = load_jobs(self.app.paths.automation_yaml)
        if not self._schedules:
            self._schedules.append(JobConfig(name="", enabled=True, cron="0 3 * * *"))

        states = load_state(self.app.paths.automation_state_yaml)

        yield S4Header()

        crontab_on = is_crontab_installed(self.app.paths.root)
        with Horizontal(id="sched-controls"):
            _crontab_label = Static(f" {t('auto_crontab')} ", id="sched-crontab-label")
            _crontab_label.styles.color = "white" if crontab_on else "gray"
            yield _crontab_label
            yield Switch(value=crontab_on, id="sched-enabled")
            yield Static(t("auto_crontab_help"), id="sched-crontab-help")

        # Tab bar — key accessors: D = Details, O = Output
        def _boldfirst(label: str) -> str:
            return f"[b]{label[:1]}[/b]{label[1:]}" if label else ""
        with Horizontal(id="sched-tab-bar"):
            yield Static(
                f" {_boldfirst(t('auto_tab_details'))} ",
                id="sched-tab-details",
                classes="tab-active" if self._active_tab == "details" else "tab-inactive",
            )
            yield Static(
                f" {_boldfirst(t('auto_tab_output'))} ",
                id="sched-tab-output",
                classes="tab-active" if self._active_tab == "output" else "tab-inactive",
            )

        # Details panel: job entries + small live log at the bottom
        with Vertical(id="sched-panel-details"):
            with VerticalScroll(id="sched-entries"):
                for idx, sched in enumerate(self._schedules):
                    yield from self._compose_entry(idx, sched, states, crontab_on)
            yield LogHistoryOverlay(id="log-history-details")
            yield LogBox(id="sched-log", wrap=True, markup=True)

        # Output panel: big log view (same stream, larger area)
        with Vertical(id="sched-panel-output"):
            yield LogHistoryOverlay(id="log-history-output")
            yield LogBox(id="sched-log-big", wrap=True, markup=True)

        yield S4Footer(active_screen="automation")

    def _compose_entry(self, idx: int, sched: JobConfig, states: dict, crontab_on: bool | None = None):
        """Yield widgets for a single schedule entry."""
        if crontab_on is None:
            crontab_on = is_crontab_installed(self.app.paths.root)
        state = states.get(sched.name)
        sched_name = sched.name or f"#{idx + 1}"
        is_running = bool(state and state.running)
        job_prefix = t("auto_job_prefix")
        if is_running:
            title = f"[bold yellow]{job_prefix} {sched_name}[/bold yellow]"
        else:
            title = f"{job_prefix} {sched_name}"
        if not sched.enabled:
            title += f" {t('auto_disabled_suffix')}"
        if is_running:
            started = state.started_at[:16].replace("T", " ") if state.started_at else ""
            dur = _fmt_duration(_running_duration(state.started_at))
            running = t("auto_running_since").format(started=started, dur=dur)
            title += f" [bold cyan]— {running}[/bold cyan]"
        else:
            if state and state.last_run:
                run_time = state.last_run[:16].replace("T", " ")
                dur_str = ""
                if getattr(state, "duration_sec", 0):
                    dur_str = f" ({t('auto_duration').format(dur=_fmt_duration(state.duration_sec))})"
                last = t("auto_last_run").format(time=run_time)
                if state.last_result == "ok":
                    info = f"[green]— {last}{dur_str} — {t('auto_result_ok')}[/green]"
                elif state.last_result == "fail":
                    info = f"[green]— {last}{dur_str} — {t('auto_result_prefix')}[/green] [red]{t('auto_result_failed')}[/red]"
                elif state.last_result == "canceled":
                    info = f"[green]— {last}{dur_str} — {t('auto_result_prefix')}[/green] [yellow]{t('auto_result_canceled')}[/yellow]"
                else:
                    info = f"[green]— {last}{dur_str}[/green]"
                title += f" {info}"
            if sched.enabled and sched.cron:
                if not crontab_on:
                    # Crontab master switch is off → job is effectively paused
                    title += f" [dim]— {t('auto_paused_crontab_off')}[/dim]"
                else:
                    nxt = cron_next_run(sched.cron)
                    if nxt:
                        title += f" — {t('auto_next_run').format(time=nxt.strftime('%Y-%m-%d %H:%M'))}"

        _cb = Checkbox("", value=sched.enabled, id=f"s-enabled-{idx}")
        _cb.styles.width = 5
        _cb.styles.min_width = 5
        _cb.styles.max_width = 5
        _cb.styles.height = 3
        _cb.styles.padding = (0, 0)

        _actions_ms = MultiSelect(
            items=_ACTION_ITEMS,
            selected=sched.actions,
            placeholder=t("auto_ph_select_actions"),
            show_all_none=False,
            id=f"s-actions-{idx}",
            classes="actions-ms",
        )
        _actions_ms.tooltip = _actions_tooltip(sched.actions)

        # Load available items for multi-selects
        from core.repos_config import load_repos
        from core.target import Target
        from core.toolchain import ToolchainRegistry
        from core.template import load_templates

        avail_repos = [r.name for r in load_repos(self.app.paths.repos_config) if not r.exclude_from_automation]
        avail_targets = [
            name for name, path in Target.list_targets(self.app.paths.targets_dir)
            if not Target.load(path).exclude_from_automation
        ]
        reg = ToolchainRegistry(
            self.app.paths.toolchains_defaults,
            self.app.paths.toolchains_local,
            self.app.paths.toolchains_dir,
        )
        reg.load()
        avail_toolchains = [tc.name for tc in reg.installed_toolchains() if not tc.exclude_from_automation]
        avail_templates = list({
            tpl.name: tpl.name
            for tpl in load_templates(self.app.paths.templates_defaults, self.app.paths.templates_local)
            if not tpl.exclude_from_automation
        })

        def _sel(items, saved):
            """Convert saved list to selected set. Default = None (empty)."""
            if not saved:
                return []
            if saved == ["all"]:
                return items
            return saved

        with Collapsible(title=title, collapsed=idx > 0, id=f"col-sched-{idx}"):
            with Grid(classes="up-grid"):
                # Row 1 left (40%): Enabled + Name
                yield Horizontal(
                    Static(f" {t('re_name')}", classes="s-label-tall"),
                    _cb,
                    Input(value=sched.name, placeholder=f"#{idx + 1}", id=f"s-name-{idx}"),
                    classes="s-row",
                )
                # Row 1 right (60%): Actions + Schedule
                _cron_inp = Input(value=sched.cron, placeholder="*/30 * * * *", id=f"s-cron-{idx}")
                _cron_help = cron_to_human(sched.cron) if sched.cron else ""
                _cron_inp.tooltip = (
                    f"→ {_cron_help}\n\n" if _cron_help else ""
                ) + t("auto_cron_tooltip")
                yield Horizontal(
                    Static(f" {t('auto_label_actions')} / {t('auto_label_schedule')}", classes="s-label-tall"),
                    _actions_ms,
                    _cron_inp,
                    classes="s-row",
                )
                # Row 2: Repositories + Targets (50/50)
                with Horizontal(classes="row2-wrapper"):
                    yield Horizontal(
                        Static(t("auto_label_repositories"), classes="s-label-tall"),
                        MultiSelect(items=avail_repos, selected=_sel(avail_repos, sched.repos), placeholder=t("auto_ph_none"), id=f"s-repos-{idx}"),
                        classes="s-row row2-half",
                    )
                    yield Horizontal(
                        Static(t("auto_label_targets_sched"), classes="s-label-tall"),
                        MultiSelect(items=avail_targets, selected=_sel(avail_targets, sched.targets), placeholder=t("auto_ph_none"), id=f"s-targets-{idx}"),
                        classes="s-row row2-half",
                    )
                # Row 3: Toolchains + Templates (50/50)
                with Horizontal(classes="row3-wrapper"):
                    yield Horizontal(
                        Static(t("auto_label_toolchains_sched"), classes="s-label-tall"),
                        MultiSelect(items=avail_toolchains, selected=_sel(avail_toolchains, sched.toolchains), placeholder=t("auto_ph_none"), id=f"s-toolchains-{idx}"),
                        classes="s-row row3-half",
                    )
                    yield Horizontal(
                        Static(" Templates", classes="s-label-tall"),
                        MultiSelect(items=avail_templates, selected=_sel(avail_templates, sched.templates), placeholder=t("auto_ph_none"), id=f"s-templates-{idx}"),
                        classes="s-row row3-half",
                    )

    CSS = """
    #sched-controls {
        height: 3;
        padding: 0 1;
    }
    #sched-tab-bar {
        height: 1;
        padding: 0 0 0 1;
        margin-top: 1;
    }
    .sched-title {
        width: auto;
        padding: 0 1;
        text-style: bold;
    }
    #sched-panel-details, #sched-panel-output {
        height: 1fr;
    }
    #sched-entries {
        height: 1fr;
        margin: 0 2;
    }
    #sched-entries .up-grid {
        grid-columns: 2fr 3fr;
    }
    #sched-entries .up-grid .row2-wrapper,
    #sched-entries .up-grid .row3-wrapper {
        column-span: 2;
        height: auto;
    }
    #sched-entries .row2-half,
    #sched-entries .row3-half {
        width: 1fr;
    }
    .actions-ms > MultiSelectOverlay {
        min-width: 28;
    }
    #sched-log {
        height: 40%;
        margin: 1 2;
        background: $boost;
        border: solid $accent;
        border-title-color: $text;
        border-title-style: bold;
        overflow-y: auto;
    }
    #sched-log:focus {
        border: heavy $accent;
    }
    #sched-log-big {
        height: 1fr;
        margin: 1 2;
        background: $boost;
        border: solid $accent;
        border-title-color: $text;
        border-title-style: bold;
        overflow-y: auto;
    }
    #sched-log-big:focus {
        border: heavy $accent;
    }
    LogHistoryOverlay {
        display: none;
        overlay: screen;
        constrain: none inside;
        width: 1fr;
        height: auto;
        max-height: 14;
        margin: 1 2;
        background: $surface;
        border: tall $border-blurred;
        color: $foreground;
    }
    LogHistoryOverlay:focus {
        border: tall $border;
    }
    #sched-controls Switch {
        margin: 0 1;
    }
    #sched-crontab-label {
        width: auto;
        padding: 1 1 0 1;
        margin-right: 1;
    }
    #sched-crontab-help {
        width: 1fr;
        padding: 1 0 0 2;
        color: $text-muted;
    }
    """

    def on_mount(self) -> None:
        _ob = t("auto_output_border")
        try:
            self.query_one("#sched-log", RichLog).border_title = _ob
        except Exception:
            pass
        try:
            self.query_one("#sched-log-big", RichLog).border_title = _ob
        except Exception:
            pass
        self._show_tab(self._active_tab)
        # Track log file tailing for the currently expanded job
        self._log_tail_path = None  # Path being tailed
        self._log_tail_pos = 0      # Byte offset within that file
        self._log_tail_job = ""     # Job name whose log is shown
        self._log_pinned_path = None  # Path | None — pinned historic log (None = live)
        self._active_job_idx = -1   # Index of the job currently 'active' (focus-driven)
        self._state_mtime = 0       # last seen mtime of state file
        # Initial lock-state + F9/F11 update for running jobs
        try:
            initial_states = load_state(self.app.paths.automation_state_yaml)
        except Exception:
            initial_states = {}
        self._refresh_locked_inputs(initial_states)
        self._refresh_f9_state(initial_states)
        # Force an initial title refresh so the "paused (crontab off)"
        # / "next run …" suffix is correct on first open, independent of
        # what compose() computed moments before mount.
        self._refresh_titles(initial_states)
        # Three timers:
        # - Fast (0.2s): log tail for live output while a job runs (smooth streaming)
        # - Medium (1s): refresh titles for running jobs so live duration counts up
        # - Slow (5s): state file changes (titles, locks, F9/F11)
        self._log_timer = self.set_interval(0.2, self._tick_log_tail)
        self._running_title_timer = self.set_interval(1.0, self._tick_running_titles)
        self._refresh_timer = self.set_interval(5.0, self._refresh_screen_state)

    def on_unmount(self) -> None:
        if hasattr(self, "_refresh_timer") and self._refresh_timer:
            self._refresh_timer.stop()
        if hasattr(self, "_log_timer") and self._log_timer:
            self._log_timer.stop()
        if hasattr(self, "_running_title_timer") and self._running_title_timer:
            self._running_title_timer.stop()

    def _tick_running_titles(self) -> None:
        """1s tick — if any job is running, update titles so duration counts up."""
        try:
            states = load_state(self.app.paths.automation_state_yaml)
        except Exception:
            return
        if not any(s.running for s in states.values()):
            return
        self._refresh_titles(states)

    def _tick_log_tail(self) -> None:
        """Fast tick — only tails the log of the expanded job.

        Does NOT reload the state file on every tick — that was the main
        source of stutter. The log tail only needs the file itself.
        """
        self._refresh_log_tail({})

    def _refresh_screen_state(self) -> None:
        """Periodic refresh: update titles, log tail, F9/F11 state, lock inputs.

        Skips title refresh if the state file has not changed since last
        check (reduces UI churn during editing). Log tail runs always but
        only reads new bytes.
        """
        state_path = self.app.paths.automation_state_yaml
        try:
            current_mtime = state_path.stat().st_mtime if state_path.exists() else 0
        except Exception:
            current_mtime = 0

        states = None
        if current_mtime != self._state_mtime:
            # State file changed — reload and update titles/locks
            self._state_mtime = current_mtime
            try:
                states = load_state(state_path)
            except Exception:
                states = {}
            self._refresh_titles(states)
            self._refresh_locked_inputs(states)

        # F9/F11 depend on expanded collapsible state — always refresh
        if states is None:
            try:
                states = load_state(state_path)
            except Exception:
                states = {}
        self._refresh_f9_state(states)

    def _refresh_locked_inputs(self, states: dict) -> None:
        """Disable input fields for jobs that are currently running."""
        for idx, sched in enumerate(self._schedules):
            if not sched.name:
                continue
            state = states.get(sched.name)
            locked = bool(state and state.running)
            for wid in (
                f"#s-name-{idx}", f"#s-cron-{idx}",
                f"#s-enabled-{idx}",
                f"#s-actions-{idx}", f"#s-repos-{idx}",
                f"#s-targets-{idx}", f"#s-toolchains-{idx}",
            ):
                try:
                    w = self.query_one(wid)
                    w.disabled = locked
                except Exception:
                    pass

    def _find_latest_log(self, job_name: str) -> "Path | None":
        """Find the newest automation log file for the given job."""
        if not job_name:
            return None
        # Job name sanitized the same way as run_job does
        import re as _re
        safe = _re.sub(r'[^A-Za-z0-9_-]+', '_', job_name)
        from core.log_cleanup import resolve_log_dir
        log_dir = resolve_log_dir(
            self.app.paths.logs_dir, "automation",
            self.app.settings.organize_logs,
        )
        candidates = list(log_dir.glob(f"*.{safe}.automation.log"))
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _find_all_logs(self, job_name: str) -> list:
        """Return all automation log files for the given job, newest first."""
        if not job_name:
            return []
        import re as _re
        safe = _re.sub(r'[^A-Za-z0-9_-]+', '_', job_name)
        from core.log_cleanup import resolve_log_dir
        log_dir = resolve_log_dir(
            self.app.paths.logs_dir, "automation",
            self.app.settings.organize_logs,
        )
        candidates = list(log_dir.glob(f"*.{safe}.automation.log"))
        return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)

    def _get_expanded_job(self) -> tuple[int, "JobConfig | None"]:
        """Return (index, JobConfig) of the currently expanded collapsible."""
        for idx, sched in enumerate(self._schedules):
            try:
                col = self.query_one(f"#col-sched-{idx}", Collapsible)
                if not col.collapsed:
                    # Use current name from UI (may have been edited)
                    name = self.query_one(f"#s-name-{idx}", Input).value.strip()
                    if name:
                        sched = JobConfig(
                            name=name, enabled=sched.enabled,
                            cron=sched.cron, actions=sched.actions,
                            targets=sched.targets, repos=sched.repos,
                            toolchains=sched.toolchains,
                        )
                    return idx, sched
            except Exception:
                continue
        return -1, None

    def on_collapsible_toggled(self, event: Collapsible.Toggled) -> None:
        """When a collapsible opens, set it as active and refresh log/F9."""
        if event.collapsible.collapsed:
            return
        opened_id = event.collapsible.id or ""
        if opened_id.startswith("col-sched-"):
            try:
                self._active_job_idx = int(opened_id[len("col-sched-"):])
            except ValueError:
                pass
        try:
            states = load_state(self.app.paths.automation_state_yaml)
        except Exception:
            states = {}
        self._refresh_log_tail(states)
        self._refresh_f9_state(states)

    def _idx_from_widget_id(self, wid: str) -> int:
        """Extract N from ids like 's-name-3', 'col-sched-3', 's-actions-3'."""
        if not wid:
            return -1
        # Trailing integer after the last '-'
        tail = wid.rsplit("-", 1)[-1]
        try:
            return int(tail)
        except ValueError:
            return -1

    def _detect_active_from_focus(self) -> None:
        """If focus is inside a job's controls or collapsible, make it active.

        This is the source of truth for 'which job is currently being
        worked on', independent of any collapsible's open/closed state.
        """
        focused = getattr(self.app, "focused", None)
        if focused is None:
            return
        # Walk the focused widget and its ancestors looking for an id
        # that identifies a job entry.
        node = focused
        while node is not None:
            wid = getattr(node, "id", None) or ""
            if wid.startswith("col-sched-") or wid.startswith("s-"):
                idx = self._idx_from_widget_id(wid)
                if 0 <= idx < len(self._schedules):
                    if idx != getattr(self, "_active_job_idx", -1):
                        self._active_job_idx = idx
                    return
            node = getattr(node, "parent", None)

    def _log_widgets(self) -> list[RichLog]:
        """Return both log widgets (details tab + output tab)."""
        result = []
        for wid in ("#sched-log", "#sched-log-big"):
            try:
                result.append(self.query_one(wid, RichLog))
            except Exception:
                pass
        return result

    def _refresh_log_tail(self, states: dict) -> None:
        """Show the log of the 'active' job (tail-follow for running).

        'Active' is determined by which job entry currently owns the
        keyboard focus — not by collapsed/expanded state. Writes to
        both the small Details log and the big Output log so switching
        tabs always shows the same stream.
        """
        # A pinned historic log is shown statically — don't tail
        if getattr(self, "_log_pinned_path", None) is not None:
            return

        # Update active index from current focus before picking a job
        self._detect_active_from_focus()
        idx = getattr(self, "_active_job_idx", -1)
        if idx < 0 or idx >= len(self._schedules):
            return
        sched = self._schedules[idx]
        # Pull possibly-edited name from the UI
        try:
            name = self.query_one(f"#s-name-{idx}", Input).value.strip()
            if name:
                sched = JobConfig(
                    name=name, enabled=sched.enabled, cron=sched.cron,
                    actions=sched.actions, targets=sched.targets,
                    repos=sched.repos, toolchains=sched.toolchains,
                )
        except Exception:
            pass
        if not sched.name:
            return
        widgets = self._log_widgets()
        if not widgets:
            return
        log_path = self._find_latest_log(sched.name)

        if log_path is None:
            # No log for this job yet — clear if switched from another job
            if self._log_tail_job != sched.name:
                for w in widgets:
                    w.clear()
                self._log_tail_path = None
                self._log_tail_pos = 0
                self._log_tail_job = sched.name
                self._update_log_border_title(sched.name)
            return

        # Switched to a different job → reset pin and reload
        if self._log_tail_job != sched.name:
            self._log_pinned_path = None

        # Switched to a different job or different log file → reset
        if self._log_tail_path != log_path or self._log_tail_job != sched.name:
            for w in widgets:
                w.clear()
            self._log_tail_path = log_path
            self._log_tail_pos = 0
            self._log_tail_job = sched.name
            self._update_log_border_title(sched.name)

        # Read new bytes from current position
        try:
            current_size = log_path.stat().st_size
            if current_size < self._log_tail_pos:
                # File was truncated/rotated — reset
                for w in widgets:
                    w.clear()
                self._log_tail_pos = 0
            if current_size > self._log_tail_pos:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._log_tail_pos)
                    new_content = f.read()
                    self._log_tail_pos = f.tell()
                for line in new_content.splitlines():
                    for w in widgets:
                        w.write(line)
        except Exception:
            pass

    def _post_action_refresh(self) -> None:
        """Immediate refresh after F9 Run/Cancel — don't wait 5s."""
        try:
            states = load_state(self.app.paths.automation_state_yaml)
        except Exception:
            states = {}
        self._refresh_titles(states)
        self._refresh_locked_inputs(states)
        self._refresh_f9_state(states)
        # Bump mtime cache so the regular 5s tick recognises the change
        try:
            self._state_mtime = self.app.paths.automation_state_yaml.stat().st_mtime
        except Exception:
            pass

    def _refresh_f9_state(self, states: dict) -> None:
        """Update F9 label/state and F11 state based on the active job.

        F9 flips between 'Run' (job idle) and 'Cancel' (job running) —
        it stays enabled in both modes so the user can always abort.
        F11 Remove is disabled while the active job is running.
        """
        self._detect_active_from_focus()
        idx = getattr(self, "_active_job_idx", -1)
        sched = self._schedules[idx] if 0 <= idx < len(self._schedules) else None
        is_running = False
        if sched is not None and sched.name:
            state = states.get(sched.name)
            if state and state.running:
                is_running = True
        run_label = t("cancel") if is_running else t("run")
        try:
            from ui.footer import FooterItem
            for item in self.query(FooterItem):
                if item._key == "F9":
                    item._label = run_label
                    item._disabled = False
                    item.remove_class("disabled")
                    # Explicit update() — refresh() alone sometimes
                    # doesn't re-render a Static whose content comes
                    # from an overridden render() method.
                    try:
                        item.update(item.render())
                    except Exception:
                        item.refresh()
                elif item._key == "F11":
                    item._disabled = is_running
                    if is_running:
                        item.add_class("disabled")
                    else:
                        item.remove_class("disabled")
                    try:
                        item.update(item.render())
                    except Exception:
                        item.refresh()
        except Exception:
            pass

    def _refresh_titles(self, states: dict | None = None) -> None:
        """Update collapsible titles from latest state on disk."""
        if states is None:
            try:
                states = load_state(self.app.paths.automation_state_yaml)
            except Exception:
                return
        # Don't refresh titles if user is currently editing a name input
        focused = self.app.focused
        if focused and getattr(focused, "id", "") and focused.id.startswith("s-name-"):
            return
        # Read the master toggle (fall back to on-disk crontab state)
        try:
            crontab_on = self.query_one("#sched-enabled", Switch).value
        except Exception:
            crontab_on = is_crontab_installed(self.app.paths.root)
        for idx, sched in enumerate(self._schedules):
            try:
                col = self.query_one(f"#col-sched-{idx}", Collapsible)
                state = states.get(sched.name)
                name_input = self.query_one(f"#s-name-{idx}", Input)
                enabled_cb = self.query_one(f"#s-enabled-{idx}", Checkbox)
                name = name_input.value.strip() or f"#{idx + 1}"
                enabled = enabled_cb.value
                is_running = bool(state and state.running)
                job_prefix = t("auto_job_prefix")
                if is_running:
                    title = f"[bold yellow]{job_prefix} {name}[/bold yellow]"
                else:
                    title = f"{job_prefix} {name}"
                if not enabled:
                    title += f" {t('auto_disabled_suffix')}"
                if is_running:
                    started = state.started_at[:16].replace("T", " ") if state.started_at else ""
                    dur = _fmt_duration(_running_duration(state.started_at))
                    running = t("auto_running_since").format(started=started, dur=dur)
                    title += f" [bold cyan]— {running}[/bold cyan]"
                else:
                    if state and state.last_run:
                        run_time = state.last_run[:16].replace("T", " ")
                        dur_str = ""
                        if getattr(state, "duration_sec", 0):
                            dur_str = f" ({t('auto_duration').format(dur=_fmt_duration(state.duration_sec))})"
                        last = t("auto_last_run").format(time=run_time)
                        if state.last_result == "ok":
                            title += f" [green]— {last}{dur_str} — {t('auto_result_ok')}[/green]"
                        elif state.last_result == "fail":
                            title += f" [green]— {last}{dur_str} — {t('auto_result_prefix')}[/green] [red]{t('auto_result_failed')}[/red]"
                        elif state.last_result == "canceled":
                            title += f" [green]— {last}{dur_str} — {t('auto_result_prefix')}[/green] [yellow]{t('auto_result_canceled')}[/yellow]"
                        else:
                            title += f" [green]— {last}{dur_str}[/green]"
                    if enabled and sched.cron:
                        if not crontab_on:
                            title += f" [dim]— {t('auto_paused_crontab_off')}[/dim]"
                        else:
                            nxt = cron_next_run(sched.cron)
                            if nxt:
                                title += f" — {t('auto_next_run').format(time=nxt.strftime('%Y-%m-%d %H:%M'))}"
                col.title = title
            except Exception:
                continue

    # ── Log history overlay ───────────────────────────────────────────────

    def on_log_box_title_clicked(self, event: LogBox.TitleClicked) -> None:
        """Open the history overlay when the log box border-title is clicked."""
        self._open_log_history_overlay(event.overlay_id)

    def _open_log_history_overlay(self, overlay_id: str) -> None:
        """Populate and show the log history overlay for the given panel."""
        try:
            overlay = self.query_one(f"#{overlay_id}", LogHistoryOverlay)
        except Exception:
            return

        _, sched = self._get_expanded_job()
        job_name = sched.name if sched else ""
        logs = self._find_all_logs(job_name)

        overlay.clear_options()
        overlay.add_option(Option("⟳  current", id="__current__"))
        for p in logs:
            overlay.add_option(Option(p.name, id=str(p)))

        # Pre-highlight the active selection
        if self._log_pinned_path is None:
            overlay.highlighted = 0
        else:
            for i, p in enumerate(logs, start=1):
                if str(p) == str(self._log_pinned_path):
                    overlay.highlighted = i
                    break

        overlay.display = True
        overlay.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle a log file selection from either history overlay."""
        if not isinstance(event.control, LogHistoryOverlay):
            return
        event.control.display = False

        option_id = event.option.id or ""
        if option_id == "__current__":
            # Back to live tail
            self._log_pinned_path = None
            self._log_tail_path = None
            self._log_tail_pos = 0
            self._log_tail_job = ""
            for w in self._log_widgets():
                w.clear()
            self._update_log_border_title()
        else:
            from pathlib import Path
            pinned = Path(option_id)
            self._log_pinned_path = pinned
            self._load_pinned_log(pinned)
            self._update_log_border_title()

    def _load_pinned_log(self, path) -> None:
        """Load a historic log file fully into both log widgets."""
        for w in self._log_widgets():
            w.clear()
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                for w in self._log_widgets():
                    w.write(line)
        except Exception:
            pass

    def _update_log_border_title(self, job_name: str = "") -> None:
        """Sync border_title on both log widgets to reflect current state.

        Format: ``Output — <jobname> — <logfile>``
        The log file is the pinned path (history mode) or the currently
        tailed path (live mode). If no log exists yet, the filename part
        is omitted.
        """
        if not job_name:
            _, sched = self._get_expanded_job()
            job_name = sched.name if sched else ""
        _ob = t("auto_output_border")
        # Pinned log takes priority; fall back to the live-tail file
        active_log = (
            getattr(self, "_log_pinned_path", None)
            or getattr(self, "_log_tail_path", None)
        )
        if active_log and job_name:
            suffix = f" — {job_name} — {active_log.name}"
        elif active_log:
            suffix = f" — {active_log.name}"
        elif job_name:
            suffix = f" — {job_name}"
        else:
            suffix = ""
        title = f"{_ob}{suffix} ▾"
        for w in self._log_widgets():
            w.border_title = title

    def _parse_csv(self, value: str) -> list[str]:
        """Parse comma-separated input, 'all' = ['all'], empty = []."""
        v = value.strip()
        if not v:
            return []
        if v.lower() == "all":
            return ["all"]
        return [x.strip() for x in v.split(",") if x.strip()]

    def _collect_from_ui(self) -> list[JobConfig]:
        """Read all schedule entries from the UI inputs."""
        schedules = []
        idx = 0
        while True:
            try:
                name = self.query_one(f"#s-name-{idx}", Input).value.strip()
            except Exception:
                break
            cron = self.query_one(f"#s-cron-{idx}", Input).value.strip()
            enabled = self.query_one(f"#s-enabled-{idx}", Checkbox).value
            try:
                actions = self.query_one(f"#s-actions-{idx}", MultiSelect).get_selected_values()
            except Exception:
                actions = []
            try:
                repos = self.query_one(f"#s-repos-{idx}", MultiSelect).get_selected_values()
            except Exception:
                repos = []
            try:
                targets = self.query_one(f"#s-targets-{idx}", MultiSelect).get_selected_values()
            except Exception:
                targets = []
            try:
                toolchains = self.query_one(f"#s-toolchains-{idx}", MultiSelect).get_selected_values()
            except Exception:
                toolchains = []
            try:
                templates = self.query_one(f"#s-templates-{idx}", MultiSelect).get_selected_values()
            except Exception:
                templates = []

            schedules.append(JobConfig(
                name=name, enabled=enabled, cron=cron,
                actions=actions, targets=targets, repos=repos,
                toolchains=toolchains, templates=templates,
            ))
            idx += 1
        return schedules

    def _rebuild(self, schedules: list[JobConfig]) -> None:
        """Rebuild screen with given schedules (in-memory, no save)."""
        global _pending_jobs
        _pending_jobs = schedules
        self.app._switch_to(AutomationScreen())

    def _save(self, silent: bool = False) -> bool:
        """Save current UI state to YAML.

        ``silent=True`` suppresses the 'Jobs saved'/'Jobs saved — …'
        notification (but still reports validation errors and installs
        the crontab); useful when F9 Run triggers a save as a side
        effect and the user would find the notify spammy. Errors still
        block save and are always reported.

        Returns True on successful save, False if blocked by errors.
        """
        schedules = self._collect_from_ui()
        # Filter out completely empty entries on save
        schedules = [s for s in schedules if s.name or s.cron]

        # Validate all entries — errors block save, warnings inform only
        all_errors = []
        all_warnings = []
        for sched in schedules:
            errors, warnings = _validate_job(sched)
            if errors:
                all_errors.extend(f"{sched.name or t('auto_unnamed')}: {e}" for e in errors)
            if sched.enabled and warnings:
                all_warnings.extend(f"{sched.name}: {w}" for w in warnings)

        if all_errors:
            self.notify(t("auto_notify_cannot_save") + "\n" + "\n".join(all_errors), severity="error", timeout=8)
            return False

        if all_warnings and not silent:
            self.notify("\n".join(all_warnings), severity="warning", timeout=8)

        save_jobs(schedules, self.app.paths.automation_yaml)
        # Update crontab if the master toggle is on (user intent),
        # not based on current crontab content — the crontab can be
        # empty if all jobs are disabled while the toggle is still on.
        toggle_on = False
        try:
            toggle_on = self.query_one("#sched-enabled", Switch).value
        except Exception:
            toggle_on = is_crontab_installed(self.app.paths.root)
        if toggle_on:
            msg = install_crontab(schedules, self.app.paths.root, self.app.settings.organize_logs)
            if not silent:
                self.notify(t("auto_notify_jobs_saved_with").format(msg=msg))
        else:
            if not silent:
                self.notify(t("auto_notify_jobs_saved"))
        return True

    # -- Update collapsible title on name/enabled change --

    def _update_title(self, idx: int) -> None:
        """Rebuild a single collapsible title using the full refresh logic.

        Delegates to _refresh_titles so the title always includes the
        correct suffix (last run, next run, paused (crontab off), …) —
        previously this used a shortcut that stripped those, which meant
        the first Checkbox/Input change event after mount wiped the
        'paused' suffix set by compose() until the 5s refresh restored it.
        """
        try:
            states = load_state(self.app.paths.automation_state_yaml)
        except Exception:
            states = {}
        self._refresh_titles(states)

    def _validate_entry(self, idx: int) -> None:
        """Validate a single job entry and show errors/warnings."""
        try:
            sched = self._collect_entry(idx)
            errors, warnings = _validate_job(sched)
            if errors:
                self.notify("\n".join(errors), severity="error", timeout=5)
            elif warnings:
                self.notify("\n".join(warnings), severity="warning", timeout=5)
        except Exception:
            pass

    def _collect_entry(self, idx: int) -> JobConfig:
        """Collect a single schedule entry from the UI."""
        name = self.query_one(f"#s-name-{idx}", Input).value.strip()
        cron = self.query_one(f"#s-cron-{idx}", Input).value.strip()
        enabled = self.query_one(f"#s-enabled-{idx}", Checkbox).value
        try:
            actions = self.query_one(f"#s-actions-{idx}", MultiSelect).get_selected_values()
        except Exception:
            actions = []
        try:
            repos = self.query_one(f"#s-repos-{idx}", MultiSelect).get_selected_values()
        except Exception:
            repos = []
        try:
            targets = self.query_one(f"#s-targets-{idx}", MultiSelect).get_selected_values()
        except Exception:
            targets = []
        try:
            toolchains = self.query_one(f"#s-toolchains-{idx}", MultiSelect).get_selected_values()
        except Exception:
            toolchains = []
        try:
            templates = self.query_one(f"#s-templates-{idx}", MultiSelect).get_selected_values()
        except Exception:
            templates = []
        return JobConfig(
            name=name, enabled=enabled, cron=cron,
            actions=actions, targets=targets, repos=repos, toolchains=toolchains,
            templates=templates,
        )

    def on_multi_select_changed(self, event: MultiSelect.Changed) -> None:
        """Update actions tooltip and validate on selection change."""
        wid = event.multi_select.id or ""
        if wid.startswith("s-actions-"):
            selected = sorted(event.selected, key=lambda a: _ACTION_ITEMS.index(a) if a in _ACTION_ITEMS else 99)
            event.multi_select.tooltip = _actions_tooltip(selected)
        # Validate the entry this multi-select belongs to
        for prefix in ("s-actions-", "s-repos-", "s-targets-", "s-toolchains-", "s-templates-"):
            if wid.startswith(prefix):
                try:
                    idx = int(wid[len(prefix):])
                    self._validate_entry(idx)
                except Exception:
                    pass
                break

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id or ""
        if cid.startswith("s-enabled-"):
            try:
                idx = int(cid.split("-")[-1])
                self._update_title(idx)
            except Exception:
                pass

    def on_input_changed(self, event: Input.Changed) -> None:
        iid = event.input.id or ""
        if iid.startswith("s-name-"):
            try:
                idx = int(iid.split("-")[-1])
                self._update_title(idx)
            except Exception:
                pass
        elif iid.startswith("s-cron-"):
            val = event.value.strip()
            human = cron_to_human(val) if val else ""
            _help = t("auto_cron_tooltip")
            event.input.tooltip = f"→ {human}\n\n{_help}" if human else _help

    # -- Switch handler for crontab install/uninstall --

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "sched-enabled":
            # Update the "Crontab" label color
            try:
                lbl = self.query_one("#sched-crontab-label", Static)
                lbl.styles.color = "white" if event.value else "gray"
            except Exception:
                pass
            if event.value:
                # _save() itself installs the crontab and notifies
                self._save()
            else:
                msg = uninstall_crontab(self.app.paths.root)
                self.notify(msg)
            # Refresh titles so the "paused (crontab off)" / "next run …"
            # suffix reflects the new master-toggle state immediately.
            try:
                states = load_state(self.app.paths.automation_state_yaml)
            except Exception:
                states = {}
            self._refresh_titles(states)

    # -- Footer actions dispatched by app (F9/F10/F11/F12) --

    def do_add(self) -> None:
        """F10 Add — append a new job entry (in-memory only).

        Clones the settings of the currently *active* job (the one that
        owns keyboard focus). The new entry gets an empty name and starts
        disabled so it can't fire before the user has named it. Falls
        back to a sensible default when no job is active yet.
        """
        self._detect_active_from_focus()
        schedules = self._collect_from_ui()
        template = None
        idx = getattr(self, "_active_job_idx", -1)
        if 0 <= idx < len(schedules):
            template = schedules[idx]
        if template is not None:
            new = JobConfig(
                name="",
                enabled=False,
                cron=template.cron or "0 3 * * *",
                actions=list(template.actions),
                targets=list(template.targets),
                repos=list(template.repos),
                toolchains=list(template.toolchains),
            )
        else:
            new = JobConfig(name="", enabled=False, cron="0 3 * * *")
        schedules.append(new)
        self._rebuild(schedules)

    def do_remove(self) -> None:
        """F11 Remove — remove the currently active job (focus-driven).

        Keep at least one entry — clear it instead of deleting (like deployments).
        Running jobs cannot be removed.
        """
        self._detect_active_from_focus()
        idx = getattr(self, "_active_job_idx", -1)
        try:
            states = load_state(self.app.paths.automation_state_yaml)
        except Exception:
            states = {}
        schedules = self._collect_from_ui()
        if not (0 <= idx < len(schedules)):
            return
        job_state = states.get(schedules[idx].name)
        if job_state and job_state.running:
            self.notify(
                t("auto_notify_cannot_remove_running").format(name=schedules[idx].name),
                severity="warning",
            )
            return
        if len(schedules) > 1:
            schedules.pop(idx)
        else:
            schedules = [JobConfig(name="", enabled=True, cron="0 3 * * *")]
        self._rebuild(schedules)

    # -- Tabs (Details / Output) --

    def action_switch_tab(self, tab: str) -> None:
        self._show_tab(tab)

    def _show_tab(self, tab: str) -> None:
        panels = {
            "details": ("#sched-panel-details", "#sched-tab-details"),
            "output": ("#sched-panel-output", "#sched-tab-output"),
        }
        if tab not in panels:
            tab = "details"
        for key, (panel_id, tab_id) in panels.items():
            try:
                self.query_one(panel_id).display = (key == tab)
                self.query_one(tab_id).set_classes(
                    "tab-active" if key == tab else "tab-inactive"
                )
            except Exception:
                pass
        AutomationScreen._active_tab = tab

    def on_click(self, event) -> None:
        """Click on tab labels switches tabs."""
        try:
            wid = getattr(event, "widget", None)
            if wid is None:
                return
            if wid.id == "sched-tab-details":
                self._show_tab("details")
            elif wid.id == "sched-tab-output":
                self._show_tab("output")
        except Exception:
            pass

    def do_tc_update(self) -> None:
        """F9 — Run the active job, or Cancel it if already running."""
        self._detect_active_from_focus()
        idx = getattr(self, "_active_job_idx", -1)
        if not (0 <= idx < len(self._schedules)):
            self.notify(t("auto_notify_click_to_select"), severity="warning")
            return
        sched = self._schedules[idx]
        if not sched.name:
            self.notify(t("auto_notify_name_first"), severity="warning")
            return
        try:
            states = load_state(self.app.paths.automation_state_yaml)
        except Exception:
            states = {}
        state = states.get(sched.name)

        # --- Cancel path: job is currently running → send SIGTERM ---
        if state and state.running and state.pid > 0:
            import os
            import signal
            try:
                # Kill the whole process group so child subprocesses
                # (git, make, gcc, …) are also stopped. Falls back to
                # a single-process kill if the pgid cannot be read.
                try:
                    pgid = os.getpgid(state.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except Exception:
                    os.kill(state.pid, signal.SIGTERM)
                self.notify(t("auto_notify_cancel_sent").format(name=sched.name))
            except ProcessLookupError:
                # Already gone — let load_state clean it up next tick
                pass
            except Exception as e:
                self.notify(t("auto_notify_cancel_failed").format(err=str(e)), severity="error")
            # Poll the state file so F9/F11/titles snap back to Run
            # once the subprocess has run its `finally` and persisted
            # last_result=canceled. SIGTERM → signal handler raises
            # _JobCanceled → finally writes state, typically < 1s.
            for delay in (0.3, 0.7, 1.2, 2.0, 3.0, 5.0):
                self.set_timer(delay, self._post_action_refresh)
            return

        # --- Run path: save current UI state silently, then spawn ---
        if not self._save(silent=True):
            # Validation errors were reported by _save
            return
        for w in self._log_widgets():
            w.clear()
        # Reset tail tracking so the new run's log is picked up fresh
        self._log_tail_path = None
        self._log_tail_pos = 0
        self._log_tail_job = ""
        self.notify(t("auto_notify_running").format(name=sched.name))
        # Spawn as a detached subprocess (same command cron uses) so
        # the run survives the TUI exiting, writes its own PID into
        # the state file, and can later be cancelled by signal.
        import subprocess
        import shlex
        root = self.app.paths.root
        s4_cmd = root / "s4"
        if not s4_cmd.exists():
            s4_cmd = root / "s4.py"
        cmd = ["/bin/sh", "-c",
               f"cd {shlex.quote(str(root))} && exec ./{s4_cmd.name} run-job {shlex.quote(sched.name)}"]
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # own process group for clean kill
                close_fds=True,
            )
        except Exception as e:
            self.notify(t("auto_notify_cancel_failed").format(err=str(e)), severity="error")
            return
        # Poll the state file so F9/F11/titles snap to "Cancel" as
        # soon as the subprocess has written its running=True + pid
        # (usually < 1s). If the subprocess fails to start, polling
        # simply never sees running=True and the footer stays on Run.
        for delay in (0.3, 0.7, 1.2, 2.0, 3.0, 5.0):
            self.set_timer(delay, self._post_action_refresh)

    def do_save(self) -> str:
        """F12 Save — save all schedule entries."""
        self._save()
        return ""
