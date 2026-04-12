"""Repository management — clone, update, backup, restore oscam sources.

Supports Git and SVN with auto-detection based on URL format.
Multiple named source identities can coexist in parallel.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

from rich.markup import escape as rich_escape


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class RepoConfig:
    """A named source identity — defines where to get oscam source."""

    name: str = "default"
    url: str = "https://git.streamboard.tv/common/oscam.git"
    branch: str = ""           # empty = default branch
    clone_depth: int = 0       # 0 = full clone
    vcs: str = ""              # "git" | "svn" | "" (auto-detect)
    # Authentication
    auth_method: str = ""      # "" | "ssh_key" | "userpass"
    ssh_key: str = ""          # path to private key file
    auth_user: str = ""        # username (plain)
    auth_pass: str = ""        # encrypted password or token


@dataclass
class RepoState:
    """Current on-disk state of a checked-out repository."""

    path: Path
    exists: bool = False
    vcs: str = ""              # "git" or "svn"
    branch: str = ""
    commit: str = ""
    revision: int = 0
    clean: bool = True
    tag: str = ""              # git tag on current commit (if any)
    patched: bool = False
    has_config_sh: bool = False

    @classmethod
    def detect(cls, repo_path: Path) -> "RepoState":
        """Detect repository state from a working directory."""
        state = cls(path=repo_path)

        if not repo_path.is_dir():
            return state

        state.exists = True
        state.has_config_sh = (repo_path / "config.sh").exists()

        # Git?
        if (repo_path / ".git").exists():
            state.vcs = "git"
            try:
                state.branch = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
                if state.branch == "HEAD":
                    # Detached HEAD — find the branch whose tip contains HEAD
                    try:
                        refs = _git(repo_path, "branch", "--contains", "HEAD", "--format=%(refname:short)")
                        for line in refs.splitlines():
                            name = line.strip()
                            if name and name != "(HEAD" and not name.startswith("("):
                                state.branch = name
                                break
                    except subprocess.CalledProcessError:
                        pass
                state.commit = _git(repo_path, "rev-parse", "--short", "HEAD")
                rev = _git(repo_path, "rev-list", "--count", "HEAD")
                state.revision = int(rev)
                status = _git(repo_path, "status", "--porcelain")
                state.clean = len(status) == 0
                try:
                    desc = _git(repo_path, "describe", "--tags")
                    # Extract just the tag name, strip distance and hash suffix
                    if "-g" in desc:
                        state.tag = desc.rsplit("-", 2)[0]
                    else:
                        state.tag = desc
                except subprocess.CalledProcessError:
                    state.tag = ""
            except (subprocess.CalledProcessError, ValueError):
                pass

        # SVN?
        elif (repo_path / ".svn").exists():
            state.vcs = "svn"
            try:
                info = subprocess.check_output(
                    ["svn", "info"], cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
                )
                # svn info exposes two revision fields:
                #   Revision         → global HEAD the WC was updated to
                #   Last Changed Rev → rev at which THIS path was last
                #                      modified — that's the per-branch
                #                      number we actually want to show
                last_changed = 0
                global_rev = 0
                for line in info.splitlines():
                    if line.startswith("Revision:"):
                        try:
                            global_rev = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            pass
                    elif line.startswith("Last Changed Rev:"):
                        try:
                            last_changed = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            pass
                    elif line.startswith("URL:"):
                        state.branch = line.split(":", 1)[1].strip()
                state.revision = last_changed or global_rev
            except (subprocess.CalledProcessError, ValueError):
                pass

        # Patch state
        # Convention: a file named IS-PATCHED in the repo parent
        patch_marker = repo_path.parent / f"{repo_path.name}-IS-PATCHED"
        state.patched = patch_marker.exists()

        return state


# ---------------------------------------------------------------------------
# Repository manager
# ---------------------------------------------------------------------------

class RepoManager:
    """Manages all repository operations for a given source identity."""

    def __init__(self, config: RepoConfig, repos_dir: Path, backup_dir: Path):
        self.config = config
        self.repo_path = repos_dir / f"oscam-{config.name}"
        self.backup_dir = backup_dir
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        # Pre-compute auth environment and URL for subprocess calls.
        from core.credentials import build_git_env, apply_credentials_to_url, build_svn_auth_args, svn_needs_interactive
        self._auth_env = {**os.environ, **build_git_env(
            config.auth_method, ssh_key=config.ssh_key,
            auth_user=config.auth_user, auth_pass=config.auth_pass)}
        self._auth_url = apply_credentials_to_url(
            config.url, config.auth_method,
            auth_user=config.auth_user, auth_pass=config.auth_pass)
        self._svn_auth_args = build_svn_auth_args(
            config.auth_method, auth_user=config.auth_user,
            auth_pass=config.auth_pass)
        self._svn_interactive = svn_needs_interactive(
            config.auth_method, auth_pass=config.auth_pass)

    def _log_auth(self, log: Optional[Callable[[str], None]]) -> None:
        """Log which auth method is being used, if any."""
        if not log or not self.config.auth_method:
            return
        info = {"ssh_key": f"SSH key ({self.config.ssh_key})",
                "userpass": f"user '{self.config.auth_user}'"}
        log(f"  [dim]Auth: {info.get(self.config.auth_method, self.config.auth_method)}[/dim]")

    def _run_git(self, *args: str) -> str:
        """Run git with auth environment. Convenience wrapper."""
        return _git(self.repo_path, *args, env=self._auth_env)

    def _svn_cmd(self, base_cmd: list[str]) -> list[str]:
        """Append SVN auth args. Remove --non-interactive when SVN needs
        to prompt for credentials. Add --trust-server-cert for HTTPS
        only when --non-interactive is present (SVN requires it)."""
        cmd = list(base_cmd)
        if self._svn_interactive:
            cmd = [a for a in cmd if a != "--non-interactive"]
        elif self.config.url.startswith("https://") and "--trust-server-cert" not in cmd:
            cmd.append("--trust-server-cert")
        cmd += self._svn_auth_args
        return cmd

    @property
    def is_git(self) -> bool:
        """Detect VCS type: explicit config.vcs wins, otherwise URL heuristic."""
        if self.config.vcs:
            return self.config.vcs == "git"
        url = self.config.url
        # Obvious SVN markers override the default git assumption
        if "/svn/" in url or url.rstrip("/").endswith("/trunk") or "/trunk/" in url:
            return False
        return url.startswith("git@") or url.endswith(".git")

    @property
    def vcs_type(self) -> str:
        return "git" if self.is_git else "svn"

    def state(self) -> RepoState:
        """Get current state of the repository."""
        return RepoState.detect(self.repo_path)

    # -- Clone / Checkout --------------------------------------------------

    def clone(self, log: Optional[Callable[[str], None]] = None) -> bool:
        """Clone the repository. Returns True on success."""
        if self.repo_path.exists():
            if log:
                log(f"Repository already exists at {self.repo_path}")
            return True

        self.repo_path.parent.mkdir(parents=True, exist_ok=True)

        if self.is_git:
            return self._git_clone(log)
        else:
            return self._svn_checkout(log)

    def _git_clone(self, log: Optional[Callable[[str], None]] = None) -> bool:
        cmd = ["git", "clone"]
        if self.config.clone_depth > 0:
            cmd += ["--depth", str(self.config.clone_depth)]
        if self.config.branch:
            cmd += ["--branch", self.config.branch]
        cmd += [self._auth_url, str(self.repo_path)]

        if log:
            # Log command without embedded credentials
            log(f"$ {' '.join(cmd).replace(self._auth_url, self.config.url)}")
            self._log_auth(log)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env=self._auth_env,
            )
            for line in proc.stdout:
                if log:
                    log(rich_escape(line.rstrip()))
            proc.wait()
            if proc.returncode == 0:
                self._backup(log)
                return True
            return False
        except Exception as e:
            if log:
                log(f"Clone failed: {rich_escape(str(e))}")
            return False

    def _svn_effective_url(self) -> str:
        """Compute the real checkout URL for the configured branch.

        SVN does NOT support credentials in the URL (unlike git).
        Authentication is handled exclusively via --username/--password.

        config.url is treated as the root (typically ending in
        `/trunk`, `/branches/<x>` or `/tags/<x>`); config.branch
        overrides which path within the standard layout to use.

        - empty branch on a standard-layout URL → `<base>/trunk`
        - empty branch on a non-standard URL    → url as-is
        - 'trunk'                               → `<base>/trunk`
        - 'branches/x' / 'tags/x'               → `<base>/<x>`
        - anything else                         → `<base>/branches/<x>`
        """
        from core.vcs_branches import svn_base_url
        url = self.config.url
        target = (self.config.branch or "").strip()
        base = svn_base_url(url)
        # Standard layout is detected when svn_base_url actually stripped
        # a trunk/branches/tags suffix off the configured URL.
        is_standard = base.rstrip("/") != url.rstrip("/")
        if not target:
            return f"{base}/trunk" if is_standard else url
        if target == "trunk":
            return f"{base}/trunk"
        if target.startswith("branches/") or target.startswith("tags/"):
            return f"{base}/{target}"
        return f"{base}/branches/{target}"

    def _svn_checkout(self, log: Optional[Callable[[str], None]] = None) -> bool:
        # Build the effective checkout URL. config.url is the base/trunk
        # URL; config.branch (if set) selects an alternate location in
        # the standard trunk/branches/tags layout.
        checkout_url = self._svn_effective_url()

        # Interactive SVN auth cannot work inside the TUI (Textual
        # captures stdin). Show the manual command instead.
        if self._svn_interactive:
            manual_cmd = f"svn checkout --username {self.config.auth_user} {checkout_url} {self.repo_path}"
            if log:
                log(f"[yellow]This SVN server requires interactive login.[/yellow]")
                log(f"[yellow]Please run this command once on the command line:[/yellow]")
                log(f"")
                log(f"  [bold]{manual_cmd}[/bold]")
                log(f"")
                log(f"[yellow]After entering your password, SVN will cache the credentials.[/yellow]")
                log(f"[yellow]Then retry Clone in s4 with Auth set to None.[/yellow]")
            return False

        cmd = ["svn", "checkout", "--non-interactive"]
        if self.config.url.startswith("https://"):
            cmd.append("--trust-server-cert")
        cmd += self._svn_auth_args + [checkout_url, str(self.repo_path)]
        if log:
            log(f"$ svn checkout {checkout_url} {self.repo_path}")
            self._log_auth(log)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in proc.stdout:
                if log:
                    log(rich_escape(line.rstrip()))
            proc.wait()
            if proc.returncode == 0:
                self._backup(log)
                return True
            return False
        except Exception as e:
            if log:
                log(f"Checkout failed: {rich_escape(str(e))}")
            return False

    # -- Update -------------------------------------------------------------

    def update(self, log: Optional[Callable[[str], None]] = None) -> bool:
        """Update the repository. Returns True on success."""
        if not self.repo_path.exists():
            return self.clone(log)

        if self.is_git:
            return self._git_update(log)
        else:
            return self._svn_update(log)

    def _git_update(self, log: Optional[Callable[[str], None]] = None) -> bool:
        try:
            self._log_auth(log)
            # If the config has a branch set and the working copy is on a
            # different branch, fetch + checkout + pull — so that a simple
            # Update also applies a branch change saved in the editor.
            target = (self.config.branch or "").strip()
            if target:
                try:
                    current = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
                except subprocess.CalledProcessError:
                    current = ""
                if current != target:
                    if log:
                        log("$ git reset --hard")
                    self._run_git("reset", "--hard")
                    if log:
                        log(f"$ git fetch origin {target}")
                    self._run_git("fetch", "origin", target)
                    if log:
                        log(f"$ git checkout -B {target} origin/{target}")
                    out = self._run_git("checkout", "-B", target, f"origin/{target}")
                    if log and out:
                        log(out)
            if log:
                log("$ git pull")
            output = self._run_git("pull")
            if log and output:
                log(output)
            self._backup(log)
            return True
        except subprocess.CalledProcessError as e:
            if log:
                log(f"Update failed: {e}")
            return False

    def _svn_update(self, log: Optional[Callable[[str], None]] = None) -> bool:
        try:
            self._log_auth(log)
            # Revert local changes (patches) before update to avoid conflicts
            if log:
                log("$ svn revert -R .")
            subprocess.run(
                self._svn_cmd(["svn", "revert", "-R", "."]),
                cwd=self.repo_path, capture_output=True, text=True, timeout=120,
            )
            target = (self.config.branch or "").strip()
            if target:
                try:
                    info = subprocess.check_output(
                        self._svn_cmd(["svn", "info", "--show-item", "url", "--non-interactive"]),
                        cwd=self.repo_path, text=True, stderr=subprocess.DEVNULL,
                    ).strip()
                except Exception:
                    info = ""
                target_url = self._svn_effective_url()
                if info and info.rstrip("/") != target_url.rstrip("/"):
                    if log:
                        log(f"$ svn switch {target_url}")
                    out = subprocess.check_output(
                        self._svn_cmd(["svn", "switch", "--non-interactive", "--accept", "postpone", target_url]),
                        cwd=self.repo_path, text=True, stderr=subprocess.STDOUT,
                    )
                    if log and out:
                        log(out)
            if log:
                log("$ svn update")
            output = subprocess.check_output(
                self._svn_cmd(["svn", "update", "--non-interactive"]),
                cwd=self.repo_path, text=True, stderr=subprocess.STDOUT,
            )
            if log and output:
                log(output)
            self._backup(log)
            return True
        except subprocess.CalledProcessError as e:
            if log:
                log(f"Update failed: {e}")
            return False

    def ensure_config_branch(self, log: Optional[Callable[[str], None]] = None) -> bool:
        """If the working copy is not on config.branch, switch to it.

        No fetch, no pull, no update — just the minimal checkout/switch
        needed to move the working copy to the configured branch.
        Returns True if already in sync or switch succeeded.
        """
        if not self.repo_path.exists():
            return True
        target = (self.config.branch or "").strip()
        if self.is_git:
            try:
                current = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
            except subprocess.CalledProcessError:
                return False
            # Empty branch for git → resolve to the remote default
            # (what origin/HEAD points at). Falls back to "master".
            if not target:
                try:
                    # Ask the remote so we pick the *current* default
                    # even if the local symref is stale.
                    out = subprocess.check_output(
                        ["git", "ls-remote", "--symref", self.config.url, "HEAD"],
                        text=True, stderr=subprocess.DEVNULL, timeout=10,
                        env=self._auth_env,
                    )
                    target = ""
                    for line in out.splitlines():
                        if line.startswith("ref:"):
                            import re as _re
                            m = _re.match(r'^ref:\s+refs/heads/(\S+)', line)
                            if m:
                                target = m.group(1)
                                break
                    if not target:
                        target = "master"
                except Exception:
                    target = "master"
            if current == target:
                return True
            try:
                # Drop local modifications (typically applied patches) so
                # the checkout can proceed without a dirty WC refusal.
                if log:
                    log("$ git reset --hard")
                self._run_git("reset", "--hard")
                if log:
                    log(f"$ git fetch origin {target}")
                self._run_git("fetch", "origin", target)
                # Prefer the remote-tracking ref so the checkout works
                # even if no local branch exists yet.
                if log:
                    log(f"$ git checkout -B {target} origin/{target}")
                out = self._run_git("checkout", "-B", target, f"origin/{target}")
                if log and out:
                    log(out)
                return True
            except subprocess.CalledProcessError as e:
                if log:
                    log(f"Branch switch failed: {e}")
                return False
        else:
            # SVN — translate the branch value to a URL and svn switch
            try:
                info = subprocess.check_output(
                    self._svn_cmd(["svn", "info", "--show-item", "url", "--non-interactive"]),
                    cwd=self.repo_path, text=True, stderr=subprocess.DEVNULL,
                ).strip()
            except Exception:
                info = ""
            target_url = self._svn_effective_url()
            if info.rstrip("/") == target_url.rstrip("/"):
                return True
            try:
                if log:
                    log("$ svn revert -R .")
                subprocess.run(
                    self._svn_cmd(["svn", "revert", "-R", "."]),
                    cwd=self.repo_path, capture_output=True, text=True, timeout=120,
                )
                if log:
                    log(f"$ svn switch {target_url}")
                out = subprocess.check_output(
                    self._svn_cmd(["svn", "switch", "--non-interactive", "--accept", "postpone", "--ignore-ancestry", target_url]),
                    cwd=self.repo_path, text=True, stderr=subprocess.STDOUT,
                )
                if log and out:
                    log(out)
                if log:
                    log("$ svn update")
                out = subprocess.check_output(
                    self._svn_cmd(["svn", "update", "--non-interactive"]),
                    cwd=self.repo_path, text=True, stderr=subprocess.STDOUT,
                )
                if log and out:
                    log(out)
                return True
            except subprocess.CalledProcessError as e:
                if log:
                    log(f"Branch switch failed: {e}")
                return False

    # -- Switch revision/branch ---------------------------------------------

    def switch_revision(self, rev: str, log: Optional[Callable[[str], None]] = None) -> bool:
        """Switch to a specific revision, branch, or tag."""
        if not self.repo_path.exists():
            if log:
                log("Repository not checked out")
            return False

        if self.is_git:
            try:
                if log:
                    log(f"$ git checkout {rev}")
                output = self._run_git("checkout", rev)
                if log and output:
                    log(output)
                return True
            except subprocess.CalledProcessError as e:
                if log:
                    log(f"Switch failed: {e}")
                return False
        else:
            try:
                if log:
                    log(f"$ svn update -r {rev}")
                output = subprocess.check_output(
                    self._svn_cmd(["svn", "update", "--non-interactive", "--accept", "postpone", f"-r{rev}"]),
                    cwd=self.repo_path, text=True, stderr=subprocess.STDOUT,
                )
                if log and output:
                    log(output)
                return True
            except subprocess.CalledProcessError as e:
                if log:
                    log(f"Switch failed: {e}")
                return False

    # -- Backup / Restore ---------------------------------------------------

    def _backup(self, log: Optional[Callable[[str], None]] = None) -> None:
        """Create a tar.gz backup of the current repo."""
        if not self.repo_path.exists():
            return

        state = self.state()
        rev = state.commit if state.vcs == "git" else str(state.revision)
        name = self.config.name
        filename = f"{rev}-oscam-{name}.tar.gz"
        backup_path = self.backup_dir / filename
        latest_link = self.backup_dir / f"last-oscam-{name}.tar.gz"

        try:
            if log:
                log(f"Creating backup: {filename}")
            with tarfile.open(backup_path, "w:gz") as tar:
                tar.add(self.repo_path, arcname=self.repo_path.name)
            # Update latest symlink
            if latest_link.exists() or latest_link.is_symlink():
                latest_link.unlink()
            latest_link.symlink_to(backup_path.name)
        except Exception as e:
            if log:
                log(f"Backup failed: {e}")

    def restore(self, revision: str = "", log: Optional[Callable[[str], None]] = None) -> bool:
        """Restore from backup. Empty revision = latest."""
        name = self.config.name

        if revision:
            backup_path = self.backup_dir / f"{revision}-oscam-{name}.tar.gz"
        else:
            backup_path = self.backup_dir / f"last-oscam-{name}.tar.gz"
            if backup_path.is_symlink():
                backup_path = backup_path.resolve()

        if not backup_path.exists():
            if log:
                log(f"Backup not found: {backup_path}")
            return False

        # Remove existing checkout
        if self.repo_path.exists():
            shutil.rmtree(self.repo_path)

        # Remove patch marker
        patch_marker = self.repo_path.parent / f"{self.repo_path.name}-IS-PATCHED"
        if patch_marker.exists():
            patch_marker.unlink()

        try:
            if log:
                log(f"Restoring from: {backup_path.name}")
            with tarfile.open(backup_path, "r:gz") as tar:
                tar.extractall(self.repo_path.parent)
            return True
        except Exception as e:
            if log:
                log(f"Restore failed: {e}")
            return False

    def list_backups(self) -> list[str]:
        """List available backup revisions."""
        name = self.config.name
        pattern = f"*-oscam-{name}.tar.gz"
        backups = []
        for f in sorted(self.backup_dir.glob(pattern), reverse=True):
            if not f.is_symlink():
                rev = f.name.split("-oscam-")[0]
                backups.append(rev)
        return backups

    # -- Clean --------------------------------------------------------------

    def clean(self, log: Optional[Callable[[str], None]] = None) -> None:
        """Remove the checked-out repository."""
        if self.repo_path.exists():
            if log:
                log(f"Removing {self.repo_path}")
            shutil.rmtree(self.repo_path)
        patch_marker = self.repo_path.parent / f"{self.repo_path.name}-IS-PATCHED"
        if patch_marker.exists():
            patch_marker.unlink()

    # -- Patch management ---------------------------------------------------

    @property
    def patch_marker(self) -> Path:
        return self.repo_path.parent / f"{self.repo_path.name}-IS-PATCHED"

    @property
    def is_patched(self) -> bool:
        return self.patch_marker.exists()

    def apply_patches(self, patches_dir: Path, log: Optional[Callable[[str], None]] = None,
                       selected_patches: Optional[list[str]] = None) -> bool:
        """Apply .patch files from patches_dir to the repo.

        Args:
            patches_dir: Directory containing .patch files.
            log: Callback for log output.
            selected_patches: If given, only apply these filenames. Otherwise apply all.

        Returns True if all patches applied successfully.
        """
        if not self.repo_path.exists():
            if log:
                log("Repository not checked out")
            return False

        if selected_patches:
            patch_files = [patches_dir / name for name in selected_patches if (patches_dir / name).exists()]
        else:
            patch_files = sorted(patches_dir.glob("*.patch"))
        if not patch_files:
            if log:
                log("No patch files found")
            return True

        # If already patched, restore first
        if self.is_patched:
            if log:
                log("Already patched — restoring from backup first")
            if not self.restore(log=log):
                if log:
                    log("Restore failed — cannot apply patches")
                return False

        applied = []
        had_warnings = False
        had_failures = False

        for patch_file in patch_files:
            if log:
                log(f"PATCH: applying {patch_file.name}")

            # Detect patch style (-p0 or -p1)
            style = self._detect_patch_style(patch_file)

            # Detect git binary patch
            is_git_binary = "GIT binary patch" in patch_file.read_text(
                encoding="utf-8", errors="replace"
            )

            try:
                if is_git_binary:
                    if log:
                        log(f"PATCH: binary patch, using git apply")
                    output = subprocess.check_output(
                        ["git", "apply", "--verbose", style, str(patch_file)],
                        cwd=self.repo_path, text=True, stderr=subprocess.STDOUT,
                    )
                else:
                    output = subprocess.check_output(
                        ["patch", "-f", style],
                        input=patch_file.read_text(encoding="utf-8", errors="replace"),
                        cwd=self.repo_path, text=True, stderr=subprocess.STDOUT,
                    )

                if log and output:
                    for line in output.splitlines():
                        log(f"  {rich_escape(line)}")

                # Check for hunks/failures
                if "FAILED" in (output or ""):
                    had_failures = True
                    if log:
                        log(f"PATCH: [red]FAILED[/red] {patch_file.name}")
                elif "Hunk" in (output or ""):
                    had_warnings = True
                    if log:
                        log(f"PATCH: [yellow]applied with hunks[/yellow] {patch_file.name}")
                else:
                    if log:
                        log(f"PATCH: [green]ok[/green] {patch_file.name}")

                applied.append(patch_file.name)

            except subprocess.CalledProcessError as e:
                had_failures = True
                if log:
                    log(f"PATCH: [red]FAILED[/red] {patch_file.name}")
                    if e.output:
                        for line in e.output.splitlines():
                            log(f"  {rich_escape(line)}")

                # On failure, restore from backup
                if log:
                    log("PATCH: failure detected — restoring from backup")
                self.restore(log=log)
                return False

        # Mark as patched
        self.patch_marker.write_text(
            "\n".join(applied), encoding="utf-8"
        )

        if had_failures:
            if log:
                log("PATCH: [red]completed with failures[/red]")
            return False
        elif had_warnings:
            if log:
                log("PATCH: [yellow]completed with warnings[/yellow]")
            return True
        else:
            if log:
                log("PATCH: [green]all patches applied successfully[/green]")
            return True

    def unpatch(self, log: Optional[Callable[[str], None]] = None) -> bool:
        """Remove patches by restoring from backup."""
        if not self.is_patched:
            if log:
                log("Not patched")
            return True
        if log:
            log("Restoring unpatched state from backup")
        return self.restore(log=log)

    def list_applied_patches(self) -> list[str]:
        """Return list of applied patch names."""
        if not self.patch_marker.exists():
            return []
        return [
            line.strip() for line in
            self.patch_marker.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _detect_patch_style(self, patch_file: Path) -> str:
        """Detect if patch uses -p0 or -p1 style."""
        content = patch_file.read_text(encoding="utf-8", errors="replace")
        patched_files = set()
        for line in content.splitlines():
            if line.startswith("--- ") or line.startswith("+++ "):
                parts = line.split()
                if len(parts) >= 2 and parts[1] != "/dev/null":
                    patched_files.add(parts[1])

        # Check if any file exists with -p0 (direct path)
        for f in patched_files:
            if (self.repo_path / f).exists():
                return "-p0"
        return "-p1"

    def patch_webif(self, log: Optional[Callable[[str], None]] = None) -> None:
        """Inject patch info into oscam source for WebIf display."""
        config_h = self.repo_path / "config.h"
        oscam_c = self.repo_path / "oscam.c"

        if not config_h.exists() or not oscam_c.exists():
            return

        patches = self.list_applied_patches()
        if not patches:
            return

        patches_str = "yes\\n\\t\\t" + "\\n\\t\\t".join(patches)

        # Patch config.h
        config_text = config_h.read_text(encoding="utf-8", errors="replace")
        # Remove old define
        lines = [l for l in config_text.splitlines() if not l.startswith("#define S3PATCHED")]
        # Add before #endif
        new_lines = []
        for line in lines:
            if line.startswith("#endif //OSCAM_CONFIG_H_") or line.startswith("#endif /* OSCAM_CONFIG_H_"):
                new_lines.append(f'#define S3PATCHED "{patches_str}"')
            new_lines.append(line)
        config_h.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        if log:
            log(f"WebIf: injected patch info ({len(patches)} patches)")

    # -- Query oscam config.sh ----------------------------------------------

    def query_config(self, category: str) -> list[str]:
        """Query oscam modules via config.sh -s <category>.

        Categories: addons, protocols, readers, card_readers
        Returns list of module names.
        """
        config_sh = self.repo_path / "config.sh"
        if not config_sh.exists():
            return []
        try:
            output = subprocess.check_output(
                [str(config_sh), "-s", category],
                cwd=self.repo_path, text=True, stderr=subprocess.DEVNULL,
            )
            return [line.strip() for line in output.splitlines() if line.strip()]
        except (subprocess.CalledProcessError, PermissionError):
            return []

    def query_all_modules(self) -> dict[str, list[str]]:
        """Query all module categories. Returns dict of category -> module list."""
        return {
            "addons": self.query_config("addons"),
            "protocols": self.query_config("protocols"),
            "readers": self.query_config("readers"),
            "card_readers": self.query_config("card_readers"),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo_path: Path, *args: str, env: Optional[dict] = None) -> str:
    """Run a git command and return stripped stdout."""
    return subprocess.check_output(
        ["git"] + list(args),
        cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
        env=env,
    ).strip()
