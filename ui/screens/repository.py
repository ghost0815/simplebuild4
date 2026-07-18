"""Repository screen — manage oscam source repositories."""

from __future__ import annotations

import os
from ui.icons import icon
import threading

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Input, Button, Select, Switch, RichLog
from ui.widgets.artifact_richlog import ArtifactRichLog
from textual.containers import Horizontal, Vertical, VerticalScroll, Grid

from core.repo import RepoConfig, RepoManager, RepoState
from core.repos_config import load_repos, save_repos
from core.target import Target
from core.patches import load_patch_sources, download_patches
from translation import t
from ui.header import S4Header
from ui.footer import S4Footer
from ui.screens.target_edit import _column_major


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


class RepositoryScreen(Screen):
    BINDINGS = [
        Binding("s", "tab_summary", "Summary", show=False),
        Binding("p", "tab_patches", "Patches", show=False),
        Binding("o", "tab_output", "Output", show=False),
    ]

    def action_tab_summary(self) -> None:
        self._show_tab("summary")

    def action_tab_patches(self) -> None:
        self._show_tab("patches")

    def action_tab_output(self) -> None:
        self._show_tab("output")

    def action_patch(self) -> None:
        repo = self._get_selected_repo()
        if not repo:
            self.notify(t("no_repo_selected"), severity="warning")
            return
        selected = self._get_selected_patches()
        if not selected:
            self.notify("No patches selected", severity="warning")
            return

        import re
        from datetime import datetime
        from core.log_cleanup import resolve_log_dir, run_retention
        from core.env import deliver_link
        _log_dir = resolve_log_dir(
            self.app.paths.logs_dir, "patch", self.app.settings.organize_logs,
        )
        timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
        log_path = _log_dir / f"{timestamp}.{repo.name}.patch.log"
        log_file = open(log_path, "w", encoding="utf-8")

        def _log_both(msg: str) -> None:
            plain = re.sub(r"\[/?[a-z_ #0-9]+\]", "", msg)
            log_file.write(plain + "\n")
            log_file.flush()
            self._log(msg)

        self._log_sync(f"[bold]Applying {len(selected)} patches to {repo.name}...[/bold]")
        mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)

        def do_patch():
            try:
                patches_dir = self.app.paths.patches_dir
                success = mgr.apply_patches(
                    patches_dir, log=_log_both, selected_patches=selected,
                )
                if success and self.app.settings.patch_webif:
                    mgr.patch_webif(log=_log_both)
            finally:
                log_file.close()
                self._log("")
                self._log(f"Log: {deliver_link(log_path)}")
                try:
                    run_retention(
                        self.app.paths.logs_dir, self.app.settings, log=self._log,
                    )
                except Exception:
                    pass

        self._run_in_thread(do_patch)

    def action_download(self) -> None:
        patches_yaml = self.app.paths.root / "data" / "patches.yaml"
        sources = load_patch_sources(patches_yaml)
        if not sources:
            self.notify("No patch URLs configured in data/patches.yaml", severity="warning")
            return
        self._log_sync(f"[bold]Downloading {len(sources)} patches...[/bold]")

        def do_download():
            count = download_patches(sources, self.app.paths.patches_dir, log=self._log)
            self._log(f"[green]Downloaded {count}/{len(sources)} patches[/green]")

        self._run_in_thread(do_download)

    def _update_footer_state(self, tab: str) -> None:
        try:
            from ui.footer import FooterItem
        except Exception:
            return
        patches_yaml = self.app.paths.root / "data" / "patches.yaml"
        sources = load_patch_sources(patches_yaml) if patches_yaml.exists() else []
        if sources:
            f6_tt = f"{len(sources)} patch URL(s) configured in patches.yaml"
        elif patches_yaml.exists():
            f6_tt = "No patch URLs configured in data/patches.yaml"
        else:
            f6_tt = "No patches.yaml found"
        any_patch_on = bool(self._get_selected_patches())
        def _set(item: "FooterItem", disabled: bool, label: str) -> None:
            item._disabled = disabled
            item._label = label
            if disabled:
                item.add_class("disabled")
            else:
                item.remove_class("disabled")
            item.refresh(layout=True)

        for item in self.query(FooterItem):
            if item._key == "F5":
                _set(item, (tab != "patches") or not any_patch_on, t("patch"))
            elif item._key == "F6":
                _set(item, (tab != "patches") or not sources, t("download"))
                item.tooltip = f6_tt

    # Class-level state shared across screen instances
    _log_history: list[str] = []
    _patch_states: dict[str, bool] = {}  # patch stem -> enabled
    _last_focus_id: str = ""
    # Tracks the repo whose output is currently in `_log_history`. When the
    # user switches the dropdown to a different repo, the output is cleared
    # so lines from different repos don't get mixed in one panel.
    _last_output_repo: str = ""

    def __init__(self) -> None:
        super().__init__()
        self._repos: list[RepoConfig] = []

    def compose(self) -> ComposeResult:
        self._repos = load_repos(self.app.paths.repos_config)

        yield S4Header()

        with Vertical(id="content"):
            # Repository selector + actions (top bar, no border — analog F2)
            with Horizontal(id="repo-bar"):
                yield Static("  Repository:", classes="label")
                repo_names = [(r.name, r.name) for r in self._repos]
                yield Select(
                    options=repo_names,
                    value=self._repos[0].name if self._repos else Select.BLANK,
                    allow_blank=True,
                    id="repo-select",
                    prompt=t("select_prompt"),
                )
                repo = self._get_selected_repo_from_list()
                clone_label = t("clone") if (not repo or RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir).is_git) else t("checkout")
                clone_btn = Button(clone_label, id="btn-clone", variant="primary")
                clone_btn.tooltip = "Clone/checkout the selected repository from its URL"
                yield clone_btn
                update_btn = Button(t("update"), id="btn-update")
                update_btn.tooltip = "Pull latest changes from remote (git pull / svn update)"
                yield update_btn
                restore_btn = Button(t("restore"), id="btn-restore")
                restore_btn.tooltip = "Restore repository from last backup (removes patches)"
                yield restore_btn
                clean_btn = Button(t("remove"), id="btn-clean", variant="error")
                clean_btn.tooltip = "Remove the checked-out repository from disk"
                yield clean_btn
                yield Static("", classes="spacer")
                yield Button(t("edit"), id="btn-edit")
                yield Button(t("duplicate"), id="btn-dup")

            # Tab bar
            with Horizontal(id="repo-tab-bar"):
                yield Static(" [b]S[/b]ummary ", id="repo-tab-summary", classes="tab-active")
                yield Static(" [b]P[/b]atches ", id="repo-tab-patches", classes="tab-inactive")
                yield Static(" [b]O[/b]utput ", id="repo-tab-output", classes="tab-inactive")

            with VerticalScroll(id="repo-scroll"):
                # Status box — populated by _refresh_status() from on_mount /
                # on_select_changed. Only shows the currently selected repo.
                info = Static(f"  [dim]{t('re_no_repo_selected')}[/dim]", id="repo-info")
                info.can_focus = True
                info.border_title = t("re_status")
                yield info

                # Patches box (shown on Patches tab; hidden on Summary/Output)
                patches_dir = self.app.paths.patches_dir
                available = sorted(patches_dir.glob("*.patch")) if patches_dir.is_dir() else []
                patches_box = Vertical(id="patches-box")
                patches_box.can_focus = True
                patches_box.border_title = f"Patches ({len(available)})" if available else "Patches"
                with patches_box:
                    if available:
                        any_on = any(RepositoryScreen._patch_states.get(p.stem, False) for p in available)
                        all_on = all(RepositoryScreen._patch_states.get(p.stem, False) for p in available)
                        sw_classes = "section-switch all-on" if all_on else "section-switch"
                        yield Grid(
                            Horizontal(
                                Switch(value=any_on, id="patch-toggle-all", classes=sw_classes),
                                Static(
                                    f" [b]{t('te_section_toggle_tip', section='Patches')}[/b]",
                                    classes="sc-label",
                                ),
                                classes="s-compact",
                            ),
                            classes="mod-grid patch-toggle-row",
                        )
                        with Grid(id="patch-grid"):
                            for p in _column_major(available, 3):
                                if p is None:
                                    yield Static("", classes="patch-item")
                                else:
                                    enabled = RepositoryScreen._patch_states.get(p.stem, False)
                                    yield Horizontal(
                                        Switch(value=enabled, id=f"patch-{_safe_id(p.stem)}"),
                                        Static(f" {p.name}", classes="patch-name"),
                                        classes="patch-item",
                                    )
                    else:
                        yield Static(f"  [dim]no .patch files in {patches_dir}[/dim]", classes="patch-info")

                # Output box
                yield ArtifactRichLog(id="repo-output", highlight=False, markup=True, wrap=True)

        yield S4Footer(active_screen="repository")

    _current_tab: str = "summary"

    def _show_tab(self, tab: str) -> None:
        RepositoryScreen._current_tab = tab
        self.query_one("#repo-info").display = (tab == "summary")
        self.query_one("#patches-box").display = (tab == "patches")
        output = self.query_one("#repo-output")
        output.styles.height = "1fr"
        self.query_one("#repo-tab-summary").set_classes("tab-active" if tab == "summary" else "tab-inactive")
        self.query_one("#repo-tab-patches").set_classes("tab-active" if tab == "patches" else "tab-inactive")
        self.query_one("#repo-tab-output").set_classes("tab-active" if tab == "output" else "tab-inactive")
        self._update_footer_state(tab)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        sid = event.switch.id or ""
        if sid == "patch-toggle-all":
            for sw in self.query("#patch-grid Switch"):
                with sw.prevent(Switch.Changed):
                    sw.value = event.value
            (event.switch.add_class if event.value else event.switch.remove_class)("all-on")
            self._update_footer_state(RepositoryScreen._current_tab)
            return
        if sid.startswith("patch-"):
            try:
                master = self.query_one("#patch-toggle-all", Switch)
            except Exception:
                return
            values = [sw.value for sw in self.query("#patch-grid Switch")]
            any_on = any(values)
            all_on = bool(values) and all(values)
            with master.prevent(Switch.Changed):
                master.value = any_on
            (master.add_class if all_on else master.remove_class)("all-on")
            self._update_footer_state(RepositoryScreen._current_tab)

    def on_click(self, event) -> None:
        widget_id = getattr(event.widget, "id", None)
        if widget_id == "repo-tab-summary":
            self._show_tab("summary")
        elif widget_id == "repo-tab-patches":
            self._show_tab("patches")
        elif widget_id == "repo-tab-output":
            self._show_tab("output")

    def on_mount(self) -> None:
        """Restore selection and log on mount."""
        if self._repos:
            target = RepositoryScreen._last_selected or self.app.settings.last_repo
            repo = next((r for r in self._repos if r.name == target), None) or self._repos[0]
            try:
                self.query_one("#repo-select", Select).value = repo.name
            except Exception:
                pass
        self.query_one("#repo-output", RichLog).border_title = t("output_title")
        # Populate the status box for the initially selected repo (or the
        # "no repo" placeholder when the list is empty).
        self._refresh_status()
        self._refresh_clone_button()
        self._show_tab(RepositoryScreen._current_tab or "summary")
        # Restore log history
        if RepositoryScreen._log_history:
            self._update_output()
        # Restore focus
        if RepositoryScreen._last_focus_id:
            try:
                self.query_one(f"#{RepositoryScreen._last_focus_id}").focus()
                RepositoryScreen._last_focus_id = ""
            except Exception:
                self.query_one("#repo-select", Select).focus()
        else:
            self.query_one("#repo-select", Select).focus()

    def on_screen_resume(self) -> None:
        """Reload repos when returning from a pushed screen (Edit/Add/Duplicate).

        Without this, actions triggered afterwards would run against the stale
        config snapshot taken at compose time (old URL / branch / auth / flags).
        """
        old_names = [r.name for r in self._repos]
        try:
            self._repos = load_repos(self.app.paths.repos_config)
        except Exception:
            return
        new_names = [r.name for r in self._repos]
        try:
            sel = self.query_one("#repo-select", Select)
        except Exception:
            self._refresh_status()
            return
        current_val = sel.value if sel.value is not Select.BLANK else ""
        # `_last_selected` is only set by the Edit screen on a successful save.
        # Consume it so a later Edit-then-ESC doesn't reapply a stale name.
        want = RepositoryScreen._last_selected or current_val
        RepositoryScreen._last_selected = ""
        try:
            if old_names != new_names:
                sel.set_options([(n, n) for n in new_names])
            if want and want in new_names:
                sel.value = want
            elif new_names and current_val not in new_names:
                sel.value = new_names[0]
        except Exception:
            pass
        self._refresh_status()
        self._refresh_clone_button()
        # A background switch started from the Edit screen may have written
        # lines into _log_history while F3 was paused — flush them into the
        # output panel now that we're back.
        if RepositoryScreen._log_history:
            try:
                self._update_output()
            except Exception:
                pass

    def _format_status(self, repo: RepoConfig, state: RepoState) -> str:
        from rich.markup import escape as _esc
        safe_url = _esc(repo.url) if repo.url else "-"
        pad = 14
        # Effective VCS for label lookup (state wins once cloned, otherwise
        # heuristics on the URL / config).
        if state.vcs:
            eff_vcs = state.vcs
        elif repo.vcs:
            eff_vcs = repo.vcs
        elif repo.url:
            from core.vcs_branches import detect_vcs
            eff_vcs = detect_vcs(repo.url)
        else:
            eff_vcs = ""
        if repo.branch == ".":
            # SVN escape hatch — show the same label the dropdown uses.
            cfg_branch = f"[dim]{t('branch_url_as_is')}[/dim]"
        elif repo.branch:
            cfg_branch = _esc(repo.branch)
        else:
            # (default) on both VCS: git → origin/HEAD, svn → trunk.
            cfg_branch = f"[dim]{t('re_default_branch')}[/dim]"
        auth_labels = {
            "": t("re_auth_none"),
            "ssh_key": t("re_auth_ssh_key"),
            "userpass": t("re_auth_userpass"),
        }
        auth_val = auth_labels.get(repo.auth_method or "", repo.auth_method or "-")

        # Applied patches (empty list if not checked out)
        mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)
        applied = mgr.list_applied_patches() if state.exists else []
        n_patches = len(applied)
        patches_val = ", ".join(_esc(p) for p in applied) if applied else "-"

        pin_str = (repo.switch_to or "").strip()
        pinned = bool(pin_str)
        switch_val = _esc(repo.switch_to) if pinned else f"[dim]{t('re_latest')}[/dim]"

        def _pin_matches_state() -> bool:
            """Does the current checkout actually satisfy repo.switch_to?"""
            if not (pinned and state.exists):
                return False
            if state.vcs == "svn":
                return pin_str in (str(state.revision), f"r{state.revision}")
            # git: pin may be a full sha, short sha, tag, or branch name.
            # Try cheap string comparisons first.
            if state.commit and (
                pin_str == state.commit
                or state.commit.startswith(pin_str)
                or pin_str.startswith(state.commit)
            ):
                return True
            if state.tag and pin_str == state.tag:
                return True
            # Last resort: ask git to resolve the pin to a sha and
            # compare with HEAD. Handles branches and full tag refs.
            try:
                import subprocess as _sp
                pin_sha = _sp.check_output(
                    ["git", "rev-parse", "--verify", "--short", pin_str],
                    cwd=state.path, text=True, stderr=_sp.DEVNULL, timeout=3,
                ).strip()
                head_sha = _sp.check_output(
                    ["git", "rev-parse", "--verify", "--short", "HEAD"],
                    cwd=state.path, text=True, stderr=_sp.DEVNULL, timeout=3,
                ).strip()
                return bool(pin_sha) and pin_sha == head_sha
            except Exception:
                return False

        if not state.exists:
            header = f"  [bold]{_esc(repo.name)}[/bold] — [red]{t('re_not_checked_out')}[/red]"
            # Not yet cloned — fall back to the URL-heuristic so the user
            # can already tell whether this repo will clone or checkout.
            if repo.vcs:
                type_val = repo.vcs
            else:
                from core.vcs_branches import detect_vcs
                type_val = detect_vcs(repo.url) if repo.url else "-"
        else:
            vcs_up = state.vcs.upper()
            if state.detached:
                head_label = "detached"
            else:
                head_label = state.branch or "—"
            # Status flag reduces to one message: does the checkout match
            # what the settings say?
            #   pinned & matches      → green pinned
            #   pinned & mismatch     → yellow pending
            #   not pinned & on branch tip (attached, right branch) → green current
            #   anything else         → yellow pending
            # "current" means: attached to exactly the branch settings ask for.
            # Config-branch empty → the remote's default; otherwise the literal name.
            expected_branch = repo.branch or state.default_branch
            on_config_branch = (
                not state.detached
                and expected_branch
                and expected_branch == state.branch
            )
            flags: list[str] = []
            if pinned:
                flags.append(
                    f"[green]{t('re_pinned')}[/green]"
                    if _pin_matches_state()
                    else f"[yellow]{t('re_pending')}[/yellow]"
                )
            elif on_config_branch:
                flags.append(f"[green]{t('re_current')}[/green]")
            else:
                flags.append(f"[yellow]{t('re_pending')}[/yellow]")
            if applied:
                flags.append(f"[yellow]{t('re_patched')}[/yellow]")
            flags_str = f" ({', '.join(flags)})" if flags else ""
            if state.vcs == "git":
                version = state.tag if state.tag else f"r{state.revision}"
                header = (
                    f"  [bold]{_esc(repo.name)} — {vcs_up} {version} "
                    f"@ {state.commit} @ {head_label}[/bold]{flags_str}"
                )
            else:
                header = (
                    f"  [bold]{_esc(repo.name)} — {vcs_up} r{state.revision} "
                    f"@ {head_label}[/bold]{flags_str}"
                )
            type_val = state.vcs

        patches_label = f"{t('re_patches_lbl')} ({n_patches}):"

        return "\n".join([
            header,
            f"  {t('re_url')+':':<{pad}} {safe_url}",
            f"  {t('re_type_auth')+':':<{pad}} {type_val} / {auth_val}",
            f"  {t('re_branch')+':':<{pad}} {cfg_branch}",
            f"  {t('re_switch_to')+':':<{pad}} {switch_val}",
            f"  {patches_label:<{pad}} {patches_val}",
        ])

    def _get_selected_repo_from_list(self) -> RepoConfig | None:
        """Get first repo (for use in compose before Select is mounted)."""
        return self._repos[0] if self._repos else None

    def _get_selected_repo(self) -> RepoConfig | None:
        sel = self.query_one("#repo-select", Select)
        if sel.value is None:
            return None
        for r in self._repos:
            if r.name == sel.value:
                return r
        return None

    def _persist_detected_vcs(self, repo: RepoConfig) -> None:
        """After a successful clone, write the detected vcs back into the config."""
        try:
            clone = self.app.paths.repos_dir / f"oscam-{repo.name}"
            detected = ""
            if (clone / ".git").is_dir():
                detected = "git"
            elif (clone / ".svn").is_dir():
                detected = "svn"
            if detected and detected != repo.vcs:
                repo.vcs = detected
                from core.repos_config import save_repos
                save_repos(self._repos, self.app.paths.repos_config)
                self._log_sync(f"  [dim]Detected vcs=[/dim][bold]{detected}[/bold]")
        except Exception:
            pass


    def _get_selected_patches(self) -> list[str]:
        """Get list of selected patch filenames from switches."""
        selected = []
        patches_dir = self.app.paths.patches_dir
        if patches_dir.is_dir():
            for p in sorted(patches_dir.glob("*.patch")):
                try:
                    sw = self.query_one(f"#patch-{_safe_id(p.stem)}", Switch)
                    if sw.value:
                        selected.append(p.name)
                except Exception:
                    pass
        return selected

    def _update_targets_source(self, old_name: str, new_name: str) -> None:
        """Update all targets that reference old_name to new_name."""
        targets_dir = self.app.paths.targets_dir
        count = 0
        for name, path in Target.list_targets(targets_dir):
            target = Target.load(path)
            changed = False
            for src in target.sources:
                if src.name == old_name:
                    src.name = new_name
                    changed = True
            if changed:
                target.save(path)
                count += 1
        if count > 0:
            self._log_sync(f"Updated {count} target(s) from '{old_name}' → '{new_name}'")

    def _refresh_select(self, select_name: str = "") -> None:
        sel = self.query_one("#repo-select", Select)
        repo_names = [(r.name, r.name) for r in self._repos]
        sel.set_options(repo_names)
        if select_name:
            sel.value = select_name
        elif self._repos:
            sel.value = self._repos[0].name
        else:
            sel.value = Select.BLANK

    def _refresh_status(self) -> None:
        try:
            repo = self._get_selected_repo()
            if repo is None:
                text = f"  [dim]{t('re_no_repo_selected')}[/dim]"
            else:
                mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)
                state = mgr.state()
                text = self._format_status(repo, state)
            self.query_one("#repo-info", Static).update(text)
        except Exception:
            pass

    def _save_patch_states(self) -> None:
        """Save current patch switch states and collapsed state before refresh."""
        patches_dir = self.app.paths.patches_dir
        if patches_dir.is_dir():
            for p in patches_dir.glob("*.patch"):
                try:
                    sw = self.query_one(f"#patch-{_safe_id(p.stem)}", Switch)
                    RepositoryScreen._patch_states[p.stem] = sw.value
                except Exception:
                    pass
    _last_selected: str = ""

    def _full_refresh(self, select_name: str = "") -> None:
        """Reload the screen by replacing it."""
        self._save_patch_states()
        # Remember focused widget
        try:
            RepositoryScreen._last_focus_id = self.app.focused.id if self.app.focused else ""
        except Exception:
            RepositoryScreen._last_focus_id = ""
        # Remember which repo was active so the fresh instance can restore it.
        RepositoryScreen._last_selected = (
            select_name
            or (self._get_selected_repo().name if self._get_selected_repo() else "")
        )
        self.app.pop_screen()
        self.app.push_screen(RepositoryScreen())

    def _log(self, msg: str) -> None:
        RepositoryScreen._log_history.append(msg)
        self.app.call_from_thread(self._update_output)

    def _log_sync(self, msg: str) -> None:
        RepositoryScreen._log_history.append(msg)
        self._update_output()

    def _update_output(self) -> None:
        """Update the output RichLog widget with all log lines."""
        try:
            log = self.query_one("#repo-output", RichLog)
            log.clear()
            for line in RepositoryScreen._log_history:
                log.write(line)
        except Exception:
            pass

    def _run_in_thread(self, func) -> None:
        def wrapper():
            func()
            self.app.call_from_thread(self._full_refresh)
        thread = threading.Thread(target=wrapper, daemon=True)
        thread.start()

    def on_select_changed(self, event: Select.Changed) -> None:
        """Persist repo-select changes and refresh the status box."""
        if event.select.id == "repo-select":
            repo = self._get_selected_repo()
            if repo:
                self.app.settings.last_repo = repo.name
                self.app.settings.save(self.app.settings_path)
                if (
                    RepositoryScreen._last_output_repo
                    and RepositoryScreen._last_output_repo != repo.name
                ):
                    RepositoryScreen._log_history.clear()
                    try:
                        self.query_one("#repo-output", RichLog).clear()
                    except Exception:
                        pass
                RepositoryScreen._last_output_repo = repo.name
            self._refresh_status()
            self._refresh_clone_button()

    def _refresh_clone_button(self) -> None:
        """Adjust the Clone/Checkout button label to match the selected repo's VCS."""
        repo = self._get_selected_repo()
        try:
            btn = self.query_one("#btn-clone", Button)
        except Exception:
            return
        if repo is None:
            btn.label = t("clone")
            return
        mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)
        btn.label = t("clone") if mgr.is_git else t("checkout")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id

        if btn == "btn-edit":
            repo = self._get_selected_repo()
            if not repo:
                self.notify(t("no_repo_selected"), severity="warning")
                return
            from ui.screens.repository_edit import RepositoryEditScreen
            self.app.push_screen(RepositoryEditScreen(repo=repo))
            return

        if btn == "btn-dup":
            repo = self._get_selected_repo()
            if not repo:
                self.notify(t("no_repo_selected"), severity="warning")
                return
            from ui.screens.repository_edit import RepositoryEditScreen
            copy = RepoConfig(
                name=f"{repo.name}_copy",
                url=repo.url,
                branch=repo.branch,
                vcs=repo.vcs,
                auth_method=repo.auth_method,
                ssh_key=repo.ssh_key,
                auth_user=repo.auth_user,
                auth_pass=repo.auth_pass,
                exclude_from_automation=repo.exclude_from_automation,
            )
            self.app.push_screen(RepositoryEditScreen(repo=copy, is_new=True))
            return

        if btn == "btn-clone":
            repo = self._get_selected_repo()
            if repo:
                self._log_sync(f"[bold]Cloning {repo.name}...[/bold]")
                mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)

                def do_clone():
                    success = mgr.clone(log=self._log)
                    if success:
                        self._log("[green]Clone complete[/green]")
                        self.app.call_from_thread(self._persist_detected_vcs, repo)
                    else:
                        self._log("[red]Clone failed[/red]")

                self._run_in_thread(do_clone)

        elif btn == "btn-update":
            repo = self._get_selected_repo()
            if repo:
                self._log_sync(f"[bold]Updating {repo.name}...[/bold]")
                mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)

                def do_update():
                    success = mgr.update(log=self._log)
                    if success:
                        self._log("[green]Update complete[/green]")
                    else:
                        self._log("[red]Update failed[/red]")

                self._run_in_thread(do_update)

        elif btn == "btn-restore":
            repo = self._get_selected_repo()
            if repo:
                self._log_sync(f"[bold]Restoring {repo.name}...[/bold]")
                mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)

                def do_restore():
                    success = mgr.restore(log=self._log)
                    if success:
                        self._log("[green]Restore complete[/green]")
                    else:
                        self._log("[red]No backup found[/red]")

                self._run_in_thread(do_restore)

        elif btn == "btn-clean":
            repo = self._get_selected_repo()
            if not repo:
                return
            if self.app.settings.confirm_dangerous:
                import time as _time
                now = _time.time()
                if (now - RepositoryScreen._clean_confirm_time < 3
                        and RepositoryScreen._clean_confirm_name == repo.name):
                    RepositoryScreen._clean_confirm_time = 0
                    RepositoryScreen._clean_confirm_name = ""
                else:
                    RepositoryScreen._clean_confirm_time = now
                    RepositoryScreen._clean_confirm_name = repo.name
                    self.notify(
                        t("confirm_remove_repo_checkout", name=repo.name),
                        severity="warning", timeout=3,
                    )
                    return
            mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)
            mgr.clean(log=self._log_sync)
            self._log_sync(f"[yellow]Repository {repo.name} removed[/yellow]")
            self._refresh_status()

    def do_add(self) -> None:
        """F10 Add — open the Repository Edit screen for a new entry."""
        from ui.screens.repository_edit import RepositoryEditScreen
        self.app.push_screen(RepositoryEditScreen(is_new=True))

    _remove_confirm_time: float = 0
    _clean_confirm_time: float = 0
    _clean_confirm_name: str = ""

    def do_remove(self) -> None:
        """F11 Remove — remove selected repo (double-press to confirm)."""
        import time
        repo = self._get_selected_repo()
        if not repo:
            self.notify(t("no_repo_selected"), severity="warning")
            return
        if repo.name == "default":
            self.notify(t("cannot_remove_default"), severity="error")
            return
        if self.app.settings.confirm_dangerous:
            now = time.time()
            if now - self._remove_confirm_time < 3:
                self._remove_confirm_time = 0
            else:
                self._remove_confirm_time = now
                self.notify(t("confirm_remove_repo", name=repo.name), severity="warning", timeout=3)
                return
        self._repos = [r for r in self._repos if r.name != repo.name]
        save_repos(self._repos, self.app.paths.repos_config)
        self._log_sync(f"[yellow]Removed repo '{repo.name}'[/yellow]")
        self.notify(t("repo_removed", name=repo.name), severity="warning")
        self._full_refresh()

    def action_go_back(self) -> None:
        RepositoryScreen._log_history.clear()
        RepositoryScreen._patch_states.clear()
        self.app.pop_screen()
