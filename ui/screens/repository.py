"""Repository screen — manage oscam source repositories."""

from __future__ import annotations

import threading

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Input, Button, Select, Switch
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
            info.border_title = t("re_status")
            yield info

            # Output box
            with VerticalScroll(id="repo-output"):
                yield Static("", id="repo-output-text")

            # Patches
            patches_dir = self.app.paths.patches_dir
            available = sorted(patches_dir.glob("*.patch")) if patches_dir.is_dir() else []
            if available:
                with Grid(id="patch-grid"):
                    for p in available:
                        enabled = RepositoryScreen._patch_states.get(p.stem, True)
                        yield Horizontal(
                            Switch(value=enabled, id=f"patch-{_safe_id(p.stem)}"),
                            Static(f" {p.name}", classes="patch-name"),
                            classes="patch-item",
                        )
            else:
                yield Static(f"  [dim]no .patch files in {patches_dir}[/dim]", classes="patch-info")

            # Repositories actions
            patch_sources = load_patch_sources(self.app.paths.root / "data" / "patches.yaml")
            has_sources = len(patch_sources) > 0
            dl_tooltip = f"{len(patch_sources)} patch URL(s) configured in patches.yaml" if has_sources else "No patches.yaml found"

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

            # Edit / Add
            with Vertical(id="edit-box"):
                with Horizontal(classes="repo-row"):
                    yield Static(f" {t('re_name')}", classes="repo-label")
                    yield Input(placeholder=t("ph_repo_name"), id="edit-repo-name")

                with Horizontal(classes="repo-row"):
                    yield Static(f" {t('re_url')}", classes="repo-label")
                    yield Input(placeholder=t("ph_repo_url"), id="edit-repo-url")

                with Horizontal(classes="repo-row"):
                    yield Static(f" {t('re_branch')}", classes="repo-label")
                    yield Input(placeholder=t("ph_repo_branch"), id="edit-repo-branch")

                with Horizontal(classes="repo-row"):
                    yield Static(f" {t('re_switch_to')}", classes="repo-label")
                    yield Input(placeholder=t("ph_repo_switch"), id="edit-repo-switch")

        yield S4Footer(active_screen="repository")

    def on_mount(self) -> None:
        """Fill edit fields and restore log on mount."""
        if self._repos:
            self._fill_edit_fields(self._repos[0])
        # Set border titles
        self.query_one("#repo-output", VerticalScroll).border_title = t("output_title")
        try:
            self.query_one("#patch-grid", Grid).border_title = "Patches"
        except Exception:
            pass
        self.query_one("#repo-box", Vertical).border_title = t("repositories")
        self.query_one("#edit-box", Vertical).border_title = t("re_edit_add")
        # Restore log history
        if RepositoryScreen._log_history:
            self._update_output()
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
        self.query_one("#edit-repo-branch", Input).value = repo.branch
        self.query_one("#edit-repo-switch", Input).value = ""

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
        """Save current patch switch states before refresh."""
        patches_dir = self.app.paths.patches_dir
        if patches_dir.is_dir():
            for p in patches_dir.glob("*.patch"):
                try:
                    sw = self.query_one(f"#patch-{_safe_id(p.stem)}", Switch)
                    RepositoryScreen._patch_states[p.stem] = sw.value
                except Exception:
                    pass

    def _full_refresh(self, select_name: str = "") -> None:
        """Reload the screen by replacing it."""
        self._save_patch_states()
        self.app.pop_screen()
        self.app.push_screen(RepositoryScreen())

    def _log(self, msg: str) -> None:
        RepositoryScreen._log_history.append(msg)
        self.app.call_from_thread(self._update_output)

    def _log_sync(self, msg: str) -> None:
        RepositoryScreen._log_history.append(msg)
        self._update_output()

    def _update_output(self) -> None:
        """Update the output Static widget with all log lines."""
        try:
            text = "\n".join(RepositoryScreen._log_history)
            self.query_one("#repo-output-text", Static).update(text)
            # Auto-scroll to bottom
            scroll = self.query_one("#repo-output", VerticalScroll)
            scroll.scroll_end(animate=False)
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
            repo = self._get_selected_repo()
            if repo:
                self._fill_edit_fields(repo)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id

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
        """F10 Add — add new repo from edit fields."""
        self._handle_add_repo()

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
        now = time.time()
        if now - self._remove_confirm_time < 3:
            self._repos = [r for r in self._repos if r.name != repo.name]
            save_repos(self._repos, self.app.paths.repos_config)
            self._log_sync(f"[yellow]Removed repo '{repo.name}'[/yellow]")
            self._full_refresh()
            self._remove_confirm_time = 0
            return
        self._remove_confirm_time = now
        self.notify(t("confirm_remove_repo", name=repo.name), severity="warning", timeout=3)

    def _handle_save_repo(self, notify=True) -> None:
        """Save edits to the currently selected repo."""
        repo = self._get_selected_repo()
        if not repo:
            self.notify("No repo selected", severity="error")
            return

        new_name = self.query_one("#edit-repo-name", Input).value.strip()
        url = self.query_one("#edit-repo-url", Input).value.strip()
        branch = self.query_one("#edit-repo-branch", Input).value.strip()
        switch_rev = self.query_one("#edit-repo-switch", Input).value.strip()

        if not new_name or not url:
            self.notify("Name and URL are required", severity="error")
            return

        # Check name collision (if renamed)
        if new_name != repo.name and any(r.name == new_name for r in self._repos):
            self.notify(f"Repo '{new_name}' already exists", severity="error")
            return

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
        repo.url = url
        repo.branch = branch
        save_repos(self._repos, self.app.paths.repos_config)
        if notify:
            self._log_sync(f"[green]Saved repo '{repo.name}'[/green]")
        self._full_refresh(new_name)

        # Switch revision if specified
        if switch_rev:
            mgr = RepoManager(repo, self.app.paths.repos_dir, self.app.paths.backup_dir)
            if mgr.state().exists:
                self._log_sync(f"[bold]Switching to {switch_rev}...[/bold]")

                def do_switch():
                    success = mgr.switch_revision(switch_rev, log=self._log)
                    if success:
                        self._log("[green]Switch complete[/green]")
                    else:
                        self._log("[red]Switch failed[/red]")

                self._run_in_thread(do_switch)
            else:
                self._log_sync("[yellow]Repo not checked out — clone first, then switch[/yellow]")

        self._refresh_status()

    def _handle_add_repo(self) -> None:
        """Add a new repo from edit fields."""
        name = self.query_one("#edit-repo-name", Input).value.strip()
        url = self.query_one("#edit-repo-url", Input).value.strip()
        branch = self.query_one("#edit-repo-branch", Input).value.strip()

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
            branch = self.query_one("#edit-repo-branch", Input).value.strip()
            return (name != repo.name) or (url != repo.url) or (branch != (repo.branch or ""))
        except Exception:
            return False

    def do_save(self) -> str:
        """Save current repo edits (called by App, no log output)."""
        repo = self._get_selected_repo()
        self._handle_save_repo(notify=False)
        name = self.query_one("#edit-repo-name", Input).value.strip()
        return f"Repository '{name or (repo.name if repo else '')}' saved"

    def action_go_back(self) -> None:
        RepositoryScreen._log_history.clear()
        RepositoryScreen._patch_states.clear()
        self.app.pop_screen()
