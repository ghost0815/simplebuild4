"""Repository edit screen — create or edit a repository configuration."""

from __future__ import annotations

import os
import threading

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Input, Button, Select, Checkbox
from textual.containers import Horizontal, Vertical, VerticalScroll

from core.repo import RepoConfig
from core.repos_config import load_repos, save_repos
from translation import t
from ui.header import S4Header
from ui.footer import S4Footer
from ui.icons import icon


class RepositoryEditScreen(Screen):

    BINDINGS = [
        Binding("escape", "esc", show=False),
    ]

    def __init__(self, repo: RepoConfig | None = None, is_new: bool = False) -> None:
        super().__init__()
        self._is_new = is_new or (repo is None)
        self._original_name = "" if self._is_new else (repo.name if repo else "")
        self._repo = repo or RepoConfig(name="")
        self._suppress_branch_changed = False
        self._all_repos: list[RepoConfig] = []

    def compose(self) -> ComposeResult:
        yield S4Header()

        with Vertical(id="content"):
            title = t("re_add") if self._is_new else t("re_edit")
            box = VerticalScroll(id="re-box")
            box.can_focus = True
            box.border_title = title
            with box:
                with Horizontal(classes="s-row"):
                    yield Static(f" {t('re_name')}", classes="s-label-tall")
                    yield Input(placeholder=t("ph_repo_name"), id="edit-repo-name",
                                value=self._repo.name)
                    yield Checkbox(t("re_disable_automation"),
                                   value=self._repo.exclude_from_automation,
                                   id="edit-repo-excl-auto")
                    yield Static("", classes="repo-row-spacer")

                with Horizontal(classes="s-row"):
                    yield Static(f" {t('re_auth_url')}", classes="s-label-tall")
                    yield Select(
                        options=[
                            (t("re_auth_none"), ""),
                            (t("re_auth_ssh_key"), "ssh_key"),
                            (t("re_auth_userpass"), "userpass"),
                        ],
                        value=self._repo.auth_method or "",
                        id="edit-repo-auth",
                        allow_blank=False,
                        classes="repo-auth-select",
                    )
                    yield Input(placeholder=t("ph_repo_url"), id="edit-repo-url",
                                value=self._repo.url)
                    yield Static("", classes="repo-row-spacer")

                ssh_browse = Button(icon("browse"), id="btn-ssh-browse", classes="tc-reset-btn")
                ssh_browse.tooltip = t("ph_browse_file")
                ssh_row = Horizontal(classes="s-row", id="auth-ssh-row")
                with ssh_row:
                    yield Static(f" {t('re_ssh_key')}", classes="s-label-tall")
                    yield Input(placeholder=t("ph_ssh_key_path"), id="edit-repo-ssh-key",
                                value=self._repo.ssh_key)
                    yield ssh_browse

                show_pw_btn = Button(icon("eye"), id="btn-toggle-pw", classes="tc-reset-btn")
                show_pw_btn.tooltip = t("tt_show_password")
                user_row = Horizontal(classes="s-row", id="auth-user-row")
                with user_row:
                    yield Static(f" {t('re_username')}", classes="s-label-tall")
                    yield Input(placeholder=t("ph_username"), id="edit-repo-user",
                                value=self._repo.auth_user)
                    yield Static(f" {t('re_password')}", classes="repo-label-short")
                    yield Input(placeholder=t("ph_password_token"), id="edit-repo-pass",
                                password=True, value=self._repo.auth_pass)
                    yield show_pw_btn

                with Horizontal(classes="s-row"):
                    yield Static(f" {t('re_branch')}", classes="s-label-tall")
                    yield Select(
                        options=[(t("ph_repo_branch"), "")],
                        id="edit-repo-branch",
                        allow_blank=False,
                        prompt=t("ph_repo_branch"),
                    )
                    refresh_btn = Button(icon("refresh"), id="btn-branch-refresh",
                                         classes="tc-reset-btn")
                    refresh_btn.tooltip = t("tt_branch_refresh")
                    yield refresh_btn

                log_btn = Button(icon("log"), id="btn-repo-log", classes="tc-reset-btn")
                log_btn.tooltip = t("tt_repo_log")
                with Horizontal(classes="s-row"):
                    yield Static(f" {t('re_switch_to')}", classes="s-label-tall")
                    yield Input(placeholder=t("ph_repo_switch"),
                                id="edit-repo-switch",
                                value=self._repo.switch_to)
                    yield log_btn

        yield S4Footer(active_screen="repository_edit")

    def on_mount(self) -> None:
        self._all_repos = load_repos(self.app.paths.repos_config)
        self._update_auth_visibility(self._repo.auth_method or "")
        try:
            from core.vcs_branches import _cache as _branch_cache, _cache_key
            key = _cache_key(self._repo.url, self._current_vcs()) if self._repo.url else ""
            if key and key in _branch_cache:
                self._apply_branch_options(_branch_cache[key], self._repo.branch)
            else:
                self._apply_branch_options([], self._repo.branch)
        except Exception:
            pass
        # `switch_to` is loaded straight from the persisted config into the
        # Input at compose time — no runtime pre-fill from the checkout.
        try:
            self.query_one("#edit-repo-name", Input).focus()
        except Exception:
            pass

    # ------------------------------------------------------------------ actions

    def action_esc(self) -> None:
        self.app.pop_screen()

    def do_save(self) -> str:
        name = self.query_one("#edit-repo-name", Input).value.strip()
        if not name:
            self.notify(t("re_name_required"), severity="error")
            return ""
        url = self.query_one("#edit-repo-url", Input).value.strip()
        auth = self._get_auth_fields()
        excl_auto = self.query_one("#edit-repo-excl-auto", Checkbox).value
        branch = self._get_branch_value()
        switch_to = self.query_one("#edit-repo-switch", Input).value.strip()

        for r in self._all_repos:
            if r.name == name and name != self._original_name:
                self.notify(t("re_name_exists", name=name), severity="error")
                return ""

        target = None
        if not self._is_new and self._original_name:
            for r in self._all_repos:
                if r.name == self._original_name:
                    target = r
                    break
        if target is None:
            target = RepoConfig(name=name)
            self._all_repos.append(target)

        target.name = name
        if url != target.url:
            target.vcs = ""  # let auto-detection re-run against the new URL
        target.url = url
        target.auth_method = auth["auth_method"]
        target.ssh_key = auth["ssh_key"]
        target.auth_user = auth["auth_user"]
        target.auth_pass = auth["auth_pass"]
        target.exclude_from_automation = excl_auto
        target.branch = branch
        target.switch_to = switch_to

        save_repos(self._all_repos, self.app.paths.repos_config)

        if excl_auto:
            try:
                from core.automation import remove_from_jobs
                remove_from_jobs(self.app.paths.automation_yaml, repo=target.name)
            except Exception:
                pass

        from ui.screens.repository import RepositoryScreen
        RepositoryScreen._last_selected = target.name

        return t("re_saved", name=target.name)

    # ------------------------------------------------------------------ auth

    def _update_auth_visibility(self, method: str) -> None:
        try:
            self.query_one("#auth-ssh-row").display = (method == "ssh_key")
            self.query_one("#auth-user-row").display = (method == "userpass")
        except Exception:
            pass

    def _get_auth_fields(self) -> dict:
        method = ""
        try:
            v = self.query_one("#edit-repo-auth", Select).value
            method = str(v) if v and v is not Select.BLANK else ""
        except Exception:
            pass
        try:
            ssh_key = self.query_one("#edit-repo-ssh-key", Input).value.strip()
        except Exception:
            ssh_key = ""
        try:
            user = self.query_one("#edit-repo-user", Input).value.strip()
        except Exception:
            user = ""
        try:
            pw = self.query_one("#edit-repo-pass", Input).value
        except Exception:
            pw = ""
        return {"auth_method": method, "ssh_key": ssh_key, "auth_user": user, "auth_pass": pw}

    # ------------------------------------------------------------------ branches

    def _current_vcs(self) -> str:
        from core.vcs_branches import detect_vcs
        clone = self.app.paths.repos_dir / f"oscam-{self._repo.name}"
        if self._repo.vcs:
            return self._repo.vcs
        try:
            url = self.query_one("#edit-repo-url", Input).value
        except Exception:
            url = self._repo.url
        return detect_vcs(url, clone_path=clone)

    def _apply_branch_options(self, branches: list, current: str) -> None:
        try:
            sel = self.query_one("#edit-repo-branch", Select)
            self._suppress_branch_changed = True
            try:
                merged = [b for b in (branches or []) if b != "."]
                if current and current not in merged and current != ".":
                    merged.append(current)
                merged.sort(key=str.lower)
                from core.vcs_branches import get_default_branch
                from rich.text import Text as _Text
                url = ""
                try:
                    url = self.query_one("#edit-repo-url", Input).value
                except Exception:
                    pass
                vcs = self._current_vcs()
                default = get_default_branch(url, vcs) if url else ""
                opts: list[tuple[object, str]] = []
                # (default) — empty saved value; git resolves to origin/HEAD,
                # svn to trunk. Shown on both VCS for symmetry.
                opts.append((t("ph_repo_branch"), ""))
                # SVN escape hatch for repositories that don't follow the
                # trunk/branches/tags convention — pass this to check out
                # config.url verbatim, without appending /trunk.
                if vcs == "svn":
                    opts.append((t("branch_url_as_is"), "."))
                for b in merged:
                    label = _Text(b, style="bold") if b == default else b
                    opts.append((label, b))
                sel.set_options(opts)
                sel.value = current if current else ""
            finally:
                self._suppress_branch_changed = False
        except Exception:
            pass

    def _get_branch_value(self) -> str:
        try:
            sel = self.query_one("#edit-repo-branch", Select)
            val = sel.value
            if val in ("__load__", "__loading__") or val is Select.BLANK:
                return ""
            return str(val) if val else ""
        except Exception:
            return ""

    def _load_branches(self, force: bool = False) -> None:
        try:
            url = self.query_one("#edit-repo-url", Input).value.strip()
        except Exception:
            url = ""
        if not url:
            self.notify(t("branch_no_url"), severity="warning")
            return
        vcs = self._current_vcs()
        current_branch = self._get_branch_value()
        auth = self._get_auth_fields()

        try:
            sel = self.query_one("#edit-repo-branch", Select)
            self._suppress_branch_changed = True
            try:
                # Keep the top entries consistent with the final options list:
                # (default) on both VCS, URL as-is only on SVN.
                placeholder: list[tuple[object, str]] = [
                    (t("ph_repo_branch"), ""),
                ]
                if vcs == "svn":
                    placeholder.append((t("branch_url_as_is"), "."))
                if current_branch and current_branch not in ("", "."):
                    placeholder.append((current_branch, current_branch))
                placeholder.append((t("branch_loading"), "__loading__"))
                sel.set_options(placeholder)
                sel.value = current_branch if current_branch else ""
            finally:
                self._suppress_branch_changed = False
        except Exception:
            pass

        def do_fetch():
            from core.vcs_branches import list_branches
            from core.credentials import build_git_env, build_svn_auth_args
            auth_env = None
            svn_args = None
            if auth["auth_method"]:
                auth_env = {**os.environ, **build_git_env(
                    auth["auth_method"], ssh_key=auth["ssh_key"],
                )}
                svn_args = build_svn_auth_args(
                    auth["auth_method"],
                    auth_user=auth["auth_user"], auth_pass=auth["auth_pass"],
                )
            err_holder: list[str] = []
            branches = list_branches(
                url, vcs, force_refresh=force,
                auth_env=auth_env, svn_auth_args=svn_args,
                on_error=err_holder.append,
            )

            def apply():
                try:
                    self._apply_branch_options(branches, current_branch)
                    if not branches:
                        reason = err_holder[0] if err_holder else ""
                        msg = t("branch_fetch_failed")
                        if reason:
                            msg = f"{msg}: {reason}"
                        self.notify(msg, severity="warning", timeout=8)
                    elif err_holder:
                        # Partial result — for SVN the "trunk"-only fallback
                        # still succeeds even when branches/ was unreachable.
                        # Tell the user why they don't see more.
                        self.notify(err_holder[0], severity="warning", timeout=8)
                except Exception:
                    pass

            self.app.call_from_thread(apply)

        threading.Thread(target=do_fetch, daemon=True).start()

    # ------------------------------------------------------------------ events

    def on_select_changed(self, event: "Select.Changed") -> None:
        if event.select.id == "edit-repo-auth":
            method = str(event.value) if event.value and event.value is not Select.BLANK else ""
            self._update_auth_visibility(method)

    def on_button_pressed(self, event) -> None:
        bid = event.button.id
        if bid == "btn-branch-refresh":
            self._load_branches(force=True)
            return
        if bid == "btn-toggle-pw":
            try:
                pw = self.query_one("#edit-repo-pass", Input)
                pw.password = not pw.password
            except Exception:
                pass
            return
        if bid == "btn-ssh-browse":
            self._open_ssh_key_picker()
            return
        if bid == "btn-repo-log":
            self._open_commit_log()
            return

    def _open_ssh_key_picker(self) -> None:
        from ui.screens.toolchains import FilePickerScreen
        from pathlib import Path as _P
        ssh_dir = _P.home() / ".ssh"
        start = str(ssh_dir) if ssh_dir.exists() else str(_P.home())
        self.app.push_screen(
            FilePickerScreen(start_path=start, title=t("re_ssh_key"), simple=True,
                             select_label=t("select_btn")),
            callback=self._on_ssh_key_selected,
        )

    def _on_ssh_key_selected(self, result) -> None:
        if not result:
            return
        path = result[0] if isinstance(result, tuple) else result
        if path:
            try:
                self.query_one("#edit-repo-ssh-key", Input).value = str(path)
            except Exception:
                pass

    def _open_commit_log(self) -> None:
        if not self._original_name or self._is_new:
            self.notify(t("log_not_cloned"), severity="warning")
            return
        from pathlib import Path as _P
        repo_path = self.app.paths.repos_dir / f"oscam-{self._original_name}"
        if not repo_path.exists():
            self.notify(t("log_not_cloned"), severity="warning")
            return
        vcs = self._current_vcs()
        # Prefer the branch actually selected in the dropdown; fall back to
        # the persisted repo.branch so a pinned repo (detached HEAD) still
        # shows the configured branch's history rather than only HEAD.
        branch = (self._get_branch_value() or self._repo.branch) if vcs == "git" else ""
        from ui.screens.commit_log import CommitLogModal
        from core.credentials import build_git_env
        auth = self._get_auth_fields()
        auth_env = (
            {**os.environ, **build_git_env(
                auth["auth_method"], ssh_key=auth["ssh_key"],
            )} if auth["auth_method"] else None
        )
        self.app.push_screen(
            CommitLogModal(repo_path=repo_path, vcs=vcs, branch=branch, auth_env=auth_env),
            callback=self._on_commit_picked,
        )

    def _on_commit_picked(self, rev: str | None) -> None:
        if not rev:
            return
        if rev.startswith("r") and rev[1:].isdigit():
            rev = rev[1:]
        try:
            self.query_one("#edit-repo-switch", Input).value = rev
        except Exception:
            pass
