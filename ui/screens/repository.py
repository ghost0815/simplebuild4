"""Repository screen — manage oscam source repositories."""

from __future__ import annotations

import threading

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Input, Button, Select, Switch, RichLog, Collapsible
from textual.containers import Horizontal, Vertical, VerticalScroll, Grid

from core.repo import RepoConfig, RepoManager, RepoState
from core.repos_config import load_repos, save_repos
from core.target import Target
from core.patches import load_patch_sources, download_patches
from translation import t
from ui.header import S4Header
from ui.footer import S4Footer


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
    BINDINGS = []

    # Class-level state shared across screen instances
    _log_history: list[str] = []
    _patch_states: dict[str, bool] = {}  # patch stem -> enabled
    _patches_collapsed: bool = True
    _edit_collapsed: bool = True
    _last_focus_id: str = ""
    _adding_new: bool = False

    def __init__(self) -> None:
        super().__init__()
        self._repos: list[RepoConfig] = []

    def compose(self) -> ComposeResult:
        self._repos = load_repos(self.app.paths.repos_config)

        yield S4Header()

        with VerticalScroll(id="repo-scroll"):
            # Status box
            status_lines = []
            for repo in self._repos:
                mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)
                state = mgr.state()
                status_lines.append(self._format_status(repo, state))
            status_text = "\n".join(status_lines)
            info = Static(status_text, id="repo-info")
            info.can_focus = True
            info.border_title = t("re_status")
            yield info

            # Output box
            yield RichLog(id="repo-output", highlight=False, markup=True, wrap=True)

            # Repositories actions
            patch_sources = load_patch_sources(self.app.paths.root / "data" / "patches.yaml")
            has_sources = len(patch_sources) > 0
            patches_yaml = self.app.paths.root / "data" / "patches.yaml"
            if has_sources:
                dl_tooltip = f"{len(patch_sources)} patch URL(s) configured in patches.yaml"
            elif patches_yaml.exists():
                dl_tooltip = "No patch URLs configured in data/patches.yaml"
            else:
                dl_tooltip = "No patches.yaml found"

            with Vertical(id="repo-box"):
                with Horizontal(classes="repo-actions-row"):
                    repo_names = [(r.name, r.name) for r in self._repos]
                    yield Select(options=repo_names, value=self._repos[0].name if self._repos else None, id="repo-select", prompt=t("select_prompt"))
                    yield Static("", classes="spacer")
                    # Dynamic label: Clone for git, Checkout for svn
                    repo = self._get_selected_repo_from_list()
                    clone_label = t("clone") if (not repo or RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir).is_git) else t("checkout")
                    clone_btn = Button(clone_label, id="btn-clone", variant="primary")
                    clone_btn.tooltip = "Clone/checkout the selected repository from its URL"
                    yield clone_btn
                    update_btn = Button(t("update"), id="btn-update")
                    update_btn.tooltip = "Pull latest changes from remote (git pull / svn update)"
                    yield update_btn
                    patch_btn = Button(t("patch"), id="btn-patch", variant="success")
                    patch_btn.tooltip = "Apply selected patches to the repository"
                    yield patch_btn
                    dl_btn = Button(t("download"), id="btn-download", variant="success")
                    dl_btn.tooltip = dl_tooltip
                    yield dl_btn
                    restore_btn = Button(t("restore"), id="btn-restore")
                    restore_btn.tooltip = "Restore repository from last backup (removes patches)"
                    yield restore_btn
                    clean_btn = Button(t("clean"), id="btn-clean", variant="error")
                    clean_btn.tooltip = "Delete the checked-out repository from disk"
                    yield clean_btn

            # Patches
            patches_dir = self.app.paths.patches_dir
            available = sorted(patches_dir.glob("*.patch")) if patches_dir.is_dir() else []
            patch_label = f"Patches ({len(available)})" if available else "Patches"
            with Collapsible(title=patch_label, collapsed=RepositoryScreen._patches_collapsed, id="col-patches"):
                if available:
                    with Grid(id="patch-grid"):
                        for p in available:
                            enabled = RepositoryScreen._patch_states.get(p.stem, False)
                            yield Horizontal(
                                Switch(value=enabled, id=f"patch-{_safe_id(p.stem)}"),
                                Static(f" {p.name}", classes="patch-name"),
                                classes="patch-item",
                            )
                else:
                    yield Static(f"  [dim]no .patch files in {patches_dir}[/dim]", classes="patch-info")

            # Edit / Add
            with Collapsible(title=t("re_edit_add"), collapsed=RepositoryScreen._edit_collapsed, id="col-edit"):
                with Horizontal(classes="repo-row"):
                    yield Static(f" {t('re_name')}", classes="repo-label")
                    yield Input(placeholder=t("ph_repo_name"), id="edit-repo-name")
                    yield Static("", classes="repo-row-spacer")

                with Horizontal(classes="repo-row"):
                    yield Static(f" {t('re_url')}", classes="repo-label")
                    yield Input(placeholder=t("ph_repo_url"), id="edit-repo-url")
                    log_btn = Button("📜", id="btn-repo-log", classes="tc-reset-btn")
                    log_btn.tooltip = t("tt_repo_log")
                    yield log_btn

                with Horizontal(classes="repo-row"):
                    yield Static(f" {t('re_branch')}", classes="repo-label")
                    yield Select(
                        options=[(t("ph_repo_branch"), "")],
                        id="edit-repo-branch",
                        allow_blank=False,
                        prompt=t("ph_repo_branch"),
                    )
                    refresh_btn = Button("🔄", id="btn-branch-refresh", classes="tc-reset-btn")
                    refresh_btn.tooltip = t("tt_branch_refresh")
                    yield refresh_btn

                with Horizontal(classes="repo-row"):
                    yield Static(f" {t('re_switch_to')}", classes="repo-label")
                    yield Input(placeholder=t("ph_repo_switch"), id="edit-repo-switch")
                    yield Static("", classes="repo-row-spacer")

        yield S4Footer(active_screen="repository")

    def on_mount(self) -> None:
        """Fill edit fields and restore log on mount."""
        if self._repos:
            target = RepositoryScreen._last_selected
            repo = next((r for r in self._repos if r.name == target), None) or self._repos[0]
            try:
                self.query_one("#repo-select", Select).value = repo.name
            except Exception:
                pass
            self._fill_edit_fields(repo)
        # Set border titles
        self.query_one("#repo-output", RichLog).border_title = t("output_title")
        try:
            self.query_one("#patch-grid", Grid).border_title = "Patches"
        except Exception:
            pass
        self.query_one("#repo-box", Vertical).border_title = t("repositories")
        # edit-box is now a Collapsible — title set in compose
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

    def _format_status(self, repo: RepoConfig, state: RepoState) -> str:
        from rich.markup import escape as _esc
        safe_url = _esc(repo.url)
        if not state.exists:
            return (
                f"  [bold]{repo.name}[/bold]  {safe_url}\n"
                f"  Status: [red]not checked out[/red]"
            )
        vcs = state.vcs.upper()
        branch = state.branch or "—"

        # Get applied patch names
        mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)
        applied = mgr.list_applied_patches()
        if applied:
            patched = f" [yellow]patched: {', '.join(applied)}[/yellow]"
        else:
            patched = ""

        if state.vcs == "git":
            version = state.tag if state.tag else f"r{state.revision}"
            return (
                f"  [bold]{repo.name}[/bold]  {safe_url}\n"
                f"  {vcs}  {version} @ {state.commit} @ {branch}{patched}"
            )
        else:
            return (
                f"  [bold]{repo.name}[/bold]  {safe_url}\n"
                f"  {vcs}  r{state.revision} @ {branch}{patched}"
            )

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

    def _fill_edit_fields(self, repo: RepoConfig) -> None:
        """Fill edit fields with repo data."""
        self.query_one("#edit-repo-name", Input).value = repo.name
        self.query_one("#edit-repo-url", Input).value = repo.url
        # Keep the last picked switch value visible across refreshes;
        # it only ever matches one repo, so guard by _last_selected.
        keep = (
            RepositoryScreen._last_switch_rev
            if RepositoryScreen._last_selected == repo.name
            else ""
        )
        self.query_one("#edit-repo-switch", Input).value = keep
        # Populate the branch dropdown — either from session cache or,
        # if nothing cached yet, with a minimal option set that shows
        # the current value and a "click to load" entry.
        try:
            from core.vcs_branches import _cache as _branch_cache, _cache_key
            key = _cache_key(repo.url, self._current_vcs(repo)) if repo.url else ""
            if key and key in _branch_cache:
                self._apply_branch_options(_branch_cache[key], repo.branch)
            else:
                self._apply_branch_options([], repo.branch)
        except Exception:
            pass

    def _open_commit_log(self) -> None:
        """Open the commit log modal for the currently selected repo."""
        repo = self._get_selected_repo()
        if not repo:
            return
        repo_path = self.app.paths.repos_dir / f"oscam-{repo.name}"
        if not repo_path.exists():
            self.notify(t("log_not_cloned"), severity="warning")
            return
        vcs = self._current_vcs(repo)
        branch = self._get_branch_value() if vcs == "git" else ""
        from ui.screens.commit_log import CommitLogModal
        self.app.push_screen(
            CommitLogModal(repo_path=repo_path, vcs=vcs, branch=branch),
            callback=self._on_commit_picked,
        )

    def _on_commit_picked(self, rev: str | None) -> None:
        """Callback from the commit log modal.

        Writes the picked SHA/revision into the Switch-to field (so it
        is visible) and runs the same switch_revision flow that F12
        save would run — but without the full save+refresh dance which
        would destroy the current screen before the log messages show.
        """
        if not rev:
            return
        # The SVN log returns revs as "r12345"; strip the leading 'r' so
        # the value lands in Switch-to as a plain number and svn's
        # -r argument stays happy.
        if rev.startswith("r") and rev[1:].isdigit():
            rev = rev[1:]
        RepositoryScreen._last_switch_rev = rev
        try:
            self.query_one("#edit-repo-switch", Input).value = rev
        except Exception:
            pass
        repo = self._get_selected_repo()
        if not repo:
            return
        mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)
        if not mgr.state().exists:
            self.notify(t("log_not_cloned"), severity="warning")
            return
        self._log_sync(f"[bold]Switching to {rev}...[/bold]")

        def do_switch():
            ok = mgr.switch_revision(rev, log=self._log)
            if ok:
                self._log("[green]Switch complete[/green]")
            else:
                self._log("[red]Switch failed[/red]")

        self._run_in_thread(do_switch)

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

    def _current_vcs(self, repo: RepoConfig) -> str:
        """Return vcs type: config.vcs wins, then local clone probe, then URL heuristic."""
        from core.vcs_branches import detect_vcs
        clone = self.app.paths.repos_dir / f"oscam-{repo.name}"
        if repo.vcs:
            return repo.vcs
        return detect_vcs(repo.url, clone_path=clone)

    def _apply_branch_options(self, branches: list, current: str) -> None:
        """Populate the branch dropdown.

        Entries:
          1. "(default branch)" → empty string
          2. the saved branch if not already in the fetched list (so the
             current selection survives even before the first fetch)
          3. every fetched branch (if any)

        The user triggers a (re)fetch via the 🔄 button next to the
        dropdown; there is no in-list "load" placeholder.
        """
        try:
            sel = self.query_one("#edit-repo-branch", Select)
            self._suppress_branch_changed = True
            try:
                # Merge saved branch into the fetched list (if not already
                # present) and sort alphabetically. "(default branch)"
                # stays at the very top.
                merged = list(branches or [])
                if current and current not in merged:
                    merged.append(current)
                merged.sort(key=str.lower)
                from core.vcs_branches import get_default_branch
                from rich.text import Text as _Text
                repo = self._get_selected_repo()
                default = get_default_branch(repo.url, self._current_vcs(repo)) if repo else ""
                opts: list[tuple[object, str]] = [(t("ph_repo_branch"), "")]
                for b in merged:
                    label = _Text(b, style="bold") if b == default else b
                    opts.append((label, b))
                sel.set_options(opts)
                sel.value = current if current else ""
            finally:
                self._suppress_branch_changed = False
        except Exception:
            pass

    _suppress_branch_changed: bool = False

    def _load_branches(self, force: bool = False) -> None:
        """Fetch the branch list for the currently selected repo and
        populate the dropdown. Runs the network call in a worker thread.
        """
        import threading
        repo = self._get_selected_repo()
        if not repo:
            return
        if not repo.url:
            self.notify(t("branch_no_url"), severity="warning")
            return
        vcs = self._current_vcs(repo)
        current_branch = self._get_branch_value()
        current_url = repo.url
        # Show a transient "loading…" option while the fetch runs
        try:
            sel = self.query_one("#edit-repo-branch", Select)
            self._suppress_branch_changed = True
            try:
                placeholder = [(t("branch_loading"), "__loading__")]
                if current_branch:
                    placeholder.insert(0, (current_branch, current_branch))
                else:
                    placeholder.insert(0, (t("ph_repo_branch"), ""))
                sel.set_options(placeholder)
                sel.value = current_branch if current_branch else ""
            finally:
                self._suppress_branch_changed = False
        except Exception:
            pass

        def do_fetch():
            from core.vcs_branches import list_branches
            branches = list_branches(current_url, vcs, force_refresh=force)
            def apply():
                try:
                    sel_repo = self._get_selected_repo()
                    if sel_repo and sel_repo.url == current_url:
                        self._apply_branch_options(branches, current_branch)
                        if not branches:
                            self.notify(t("branch_fetch_failed"), severity="warning")
                except Exception:
                    pass
            self.app.call_from_thread(apply)

        threading.Thread(target=do_fetch, daemon=True).start()

    def _get_branch_value(self) -> str:
        """Read the current branch from the dropdown, normalising sentinels."""
        try:
            sel = self.query_one("#edit-repo-branch", Select)
            val = sel.value
            if val in ("__load__", "__loading__") or val is Select.BLANK:
                return ""
            return str(val) if val else ""
        except Exception:
            return ""

    def on_select_changed(self, event: "Select.Changed") -> None:
        """No-op: branch dropdown changes are read at save time."""
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

    def _refresh_status(self) -> None:
        try:
            status_lines = []
            for repo in self._repos:
                mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)
                state = mgr.state()
                status_lines.append(self._format_status(repo, state))
            self.query_one("#repo-info", Static).update("\n".join(status_lines))
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
        try:
            RepositoryScreen._patches_collapsed = self.query_one("#col-patches", Collapsible).collapsed
        except Exception:
            pass
        try:
            RepositoryScreen._edit_collapsed = self.query_one("#col-edit", Collapsible).collapsed
        except Exception:
            pass

    _last_selected: str = ""
    _last_switch_rev: str = ""

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
        """Fill edit fields when selecting a different repo."""
        if event.select.id == "repo-select":
            RepositoryScreen._adding_new = False
            repo = self._get_selected_repo()
            if repo:
                self._fill_edit_fields(repo)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id

        if btn == "btn-branch-refresh":
            self._load_branches(force=True)
            return

        if btn == "btn-repo-log":
            self._open_commit_log()
            return

        if btn == "btn-download":
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

        elif btn == "btn-clone":
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
            if repo:
                mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)
                mgr.clean(log=self._log_sync)
                self._log_sync(f"[yellow]Cleaned {repo.name}[/yellow]")
                self._refresh_status()

        elif btn == "btn-patch":
            repo = self._get_selected_repo()
            if repo:
                # Collect selected patches
                selected = self._get_selected_patches()
                if not selected:
                    self.notify("No patches selected", severity="warning")
                    return
                self._log_sync(f"[bold]Applying {len(selected)} patches to {repo.name}...[/bold]")
                mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)

                def do_patch():
                    patches_dir = self.app.paths.patches_dir
                    success = mgr.apply_patches(patches_dir, log=self._log, selected_patches=selected)
                    if success and self.app.settings.patch_webif:
                        mgr.patch_webif(log=self._log)

                self._run_in_thread(do_patch)

    def do_add(self) -> None:
        """F10 Add — clear fields for a new repo (keep URL), open editor, focus Name."""
        RepositoryScreen._adding_new = True
        # Keep the current URL as a convenience starting point
        try:
            url = self.query_one("#edit-repo-url", Input).value
        except Exception:
            url = ""
        try:
            self.query_one("#edit-repo-name", Input).value = ""
            self.query_one("#edit-repo-url", Input).value = url
            self.query_one("#edit-repo-switch", Input).value = ""
            # Reset branch dropdown
            sel = self.query_one("#edit-repo-branch", Select)
            sel.set_options([(t("ph_repo_branch"), "")])
            sel.value = ""
        except Exception:
            pass
        # Expand the Edit/Add collapsible and focus the Name field
        try:
            col = self.query_one("#col-edit", Collapsible)
            col.collapsed = False
        except Exception:
            pass
        try:
            self.query_one("#edit-repo-name", Input).focus()
        except Exception:
            pass

    _remove_confirm_time: float = 0

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

    def _handle_save_repo(self, notify=True) -> bool:
        """Save edits to the currently selected repo. Returns True on success."""
        repo = self._get_selected_repo()
        if not repo:
            self.notify("No repo selected", severity="error")
            return False

        new_name = self.query_one("#edit-repo-name", Input).value.strip()
        url = self.query_one("#edit-repo-url", Input).value.strip()
        branch = self._get_branch_value()
        switch_rev = self.query_one("#edit-repo-switch", Input).value.strip()
        # Update the persisted Switch-to echo so _fill_edit_fields on
        # the next refresh shows whatever the user just typed, not a
        # stale value from a previous commit pick.
        RepositoryScreen._last_switch_rev = switch_rev

        if not new_name or not url:
            self.notify("Name and URL are required", severity="error")
            return False

        # Check name collision (if renamed)
        if new_name != repo.name and any(r.name == new_name for r in self._repos):
            self.notify(f"Repo '{new_name}' already exists", severity="error")
            return False

        # Rename checkout directory if name changed and repo exists on disk
        if new_name != repo.name:
            old_path = self.app.paths.repos_dir / f"oscam-{repo.name}"
            new_path = self.app.paths.repos_dir / f"oscam-{new_name}"
            if old_path.exists():
                old_path.rename(new_path)
                if notify:
                    self._log_sync(f"Renamed {old_path.name} → {new_path.name}")
            old_name = repo.name
            repo.name = new_name
            # Update all targets that reference the old name
            self._update_targets_source(old_name, new_name)
            if notify:
                self._log_sync(f"[green]Renamed '{old_name}' → '{new_name}'[/green]")

        # Update config
        old_branch = repo.branch
        old_url = repo.url
        repo.url = url
        repo.branch = branch
        # If the URL changed, drop the persisted vcs type so the next
        # clone re-detects it from the fresh URL/checkout — otherwise
        # switching e.g. a git entry to an SVN URL would still try git
        # clone.
        if url != old_url:
            repo.vcs = ""
        # For SVN: rewrite the URL to reflect the picked branch. SVN has
        # no separate branch concept — the branch IS the URL. We still
        # keep config.branch as a human-readable marker.
        vcs = self._current_vcs(repo)
        if vcs == "svn":
            from core.repo import RepoManager as _RM
            effective = _RM(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)._svn_effective_url()
            if effective != repo.url:
                repo.url = effective
                try:
                    self.query_one("#edit-repo-url", Input).value = effective
                except Exception:
                    pass
        save_repos(self._repos, self.app.paths.repos_config)
        if notify:
            self._log_sync(f"[green]Saved repo '{repo.name}'[/green]")
        # If the branch changed and the repo is already cloned, switch
        # the working copy too so the config and on-disk state stay in
        # sync. Runs in a worker — branch switch can be slow and may
        # fail on a dirty working copy.
        # Any async follow-up (branch switch or switch-to revision) runs
        # in a single worker so _full_refresh only fires once AFTER the
        # work is done — otherwise the refresh replaces the screen while
        # the worker is still updating the on-disk state and the new
        # instance reads a stale revision.
        mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)
        needs_branch_switch = branch != old_branch and mgr.state().exists
        needs_rev_switch = bool(switch_rev) and mgr.state().exists

        if needs_branch_switch or needs_rev_switch:
            if needs_branch_switch:
                self._log_sync(f"[bold]Switching to branch {branch or '(default)'}...[/bold]")
            if needs_rev_switch:
                self._log_sync(f"[bold]Switching to {switch_rev}...[/bold]")

            def do_async():
                if needs_branch_switch:
                    if mgr.ensure_config_branch(log=self._log):
                        self._log("[green]Branch switch complete[/green]")
                    else:
                        self._log("[red]Branch switch failed[/red]")
                if needs_rev_switch:
                    if mgr.switch_revision(switch_rev, log=self._log):
                        self._log("[green]Switch complete[/green]")
                    else:
                        self._log("[red]Switch failed[/red]")

            self._run_in_thread(do_async)
            return

        if switch_rev and not mgr.state().exists:
            self._log_sync("[yellow]Repo not checked out — clone first, then switch[/yellow]")

        self._full_refresh(new_name)
        self._refresh_status()

    def _handle_add_repo(self) -> None:
        """Add a new repo from edit fields."""
        name = self.query_one("#edit-repo-name", Input).value.strip()
        url = self.query_one("#edit-repo-url", Input).value.strip()
        branch = self._get_branch_value()

        if not name or not url:
            self.notify("Name and URL are required", severity="error")
            return
        if any(r.name == name for r in self._repos):
            self.notify(f"Repo '{name}' already exists", severity="error")
            return

        new_repo = RepoConfig(name=name, url=url, branch=branch)
        self._repos.append(new_repo)
        save_repos(self._repos, self.app.paths.repos_config)
        self._log_sync(f"[green]Added repo '{name}'[/green]")
        self._full_refresh(name)

    def is_dirty(self) -> bool:
        """True if the edit form differs from the selected repo entry."""
        try:
            repo = self._get_selected_repo()
            if not repo:
                return False
            name = self.query_one("#edit-repo-name", Input).value.strip()
            url = self.query_one("#edit-repo-url", Input).value.strip()
            branch = self._get_branch_value()
            return (name != repo.name) or (url != repo.url) or (branch != (repo.branch or ""))
        except Exception:
            return False

    def do_save(self) -> str:
        """Save current repo edits (called by App, no log output).

        In add-mode (after F10): always create a new repo; error if name
        already exists. Otherwise: update the selected repo.
        """
        name = self.query_one("#edit-repo-name", Input).value.strip()
        url = self.query_one("#edit-repo-url", Input).value.strip()
        if not name or not url:
            self.notify("Name and URL are required", severity="error")
            return ""
        if RepositoryScreen._adding_new:
            if any(r.name == name for r in self._repos):
                self.notify(f"Repo '{name}' already exists", severity="error")
                return ""
            self._handle_add_repo()
            RepositoryScreen._adding_new = False
            return f"Repository '{name}' added"
        else:
            if not self._handle_save_repo(notify=False):
                return ""
            return f"Repository '{name}' saved"

    def action_go_back(self) -> None:
        RepositoryScreen._log_history.clear()
        RepositoryScreen._patch_states.clear()
        self.app.pop_screen()
