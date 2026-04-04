"""Build engine for oscam — manages the complete build pipeline."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from core.target import Target, SourceConfig, ToolchainConfig


class BuildEngine:
    """Executes the oscam build pipeline for a target configuration.

    Each build runs through phases:
    - PHASE 1: Prepare Repository (update, reset)
    - PHASE 2: Apply Patches
    - PHASE 3: Configure (config.sh modules) — TODO
    - PHASE 4: Build (make) — TODO
    - PHASE 5: Post-Build (UPX, copy binary) — TODO
    - PHASE 6: Upload (SSH) — TODO
    """

    def __init__(
        self,
        target: Target,
        repos_dir: Path,
        patches_dir: Path,
        logs_dir: Path,
        log: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ):
        self.target = target
        self.repos_dir = repos_dir
        self.patches_dir = patches_dir
        self.logs_dir = logs_dir
        self._log = log or (lambda msg: None)
        self._cancel = cancel_check or (lambda: False)

    def build(self) -> bool:
        """Run the full build pipeline for all source x toolchain combinations."""
        sources = self.target.sources
        toolchains = self.target.toolchains

        if not sources:
            self._log("[red]No sources configured[/red]")
            return False
        if not toolchains:
            self._log("[red]No toolchains configured[/red]")
            return False

        total = len(sources) * len(toolchains)
        self._log(f"[bold]Building {self.target.name}: {len(sources)} source(s) x {len(toolchains)} toolchain(s) = {total} build(s)[/bold]")
        self._log("")

        # Resolve repo directories
        repo_dirs: dict[str, Path] = {}
        for src in sources:
            repo_dir = self._find_repo_dir(src.name)
            if not repo_dir:
                self._log(f"[red]Repository '{src.name}' not found — skipping[/red]")
            else:
                repo_dirs[src.name] = repo_dir

        if not repo_dirs:
            self._log("[red]No repositories found[/red]")
            return False

        # PHASE 1: Prepare all repositories
        self._log("[bold]PHASE 1: Prepare Repositories[/bold]")
        phase1_ok = True
        for src in sources:
            if src.name not in repo_dirs:
                continue
            if not self._phase1_prepare_repo(src, repo_dirs[src.name]):
                phase1_ok = False
            if self._cancel():
                self._log("[yellow]Build cancelled[/yellow]")
                return False

        if not phase1_ok:
            self._log("[red]PHASE 1 failed — aborting[/red]")
            return False

        # PHASE 2: Apply patches to all repositories
        self._log("")
        self._log("[bold]PHASE 2: Apply Patches[/bold]")
        phase2_ok = True
        for src in sources:
            if src.name not in repo_dirs:
                continue
            if not self._phase2_apply_patches(src, repo_dirs[src.name]):
                phase2_ok = False
            if self._cancel():
                self._log("[yellow]Build cancelled[/yellow]")
                return False

        if not phase2_ok:
            self._log("[red]PHASE 2 failed — aborting[/red]")
            return False

        # PHASE 3: Configure oscam for all repositories
        self._log("")
        self._log("[bold]PHASE 3: Configure oscam[/bold]")
        phase3_ok = True
        for src in sources:
            if src.name not in repo_dirs:
                continue
            if not self._phase3_configure(src, repo_dirs[src.name]):
                phase3_ok = False
            if self._cancel():
                self._log("[yellow]Build cancelled[/yellow]")
                return False

        if not phase3_ok:
            self._log("[red]PHASE 3 failed — aborting[/red]")
            return False

        # Future phases per source x toolchain
        success_count = 0
        fail_count = 0
        for src in sources:
            if src.name not in repo_dirs:
                fail_count += len(toolchains)
                continue
            for tcc in toolchains:
                self._log(f"\n[bold]--- {src.name} x {tcc.name} ---[/bold]")
                # PHASE 4-6 TODO
                self._log(f"[dim]Build phases 4-6 not yet implemented[/dim]")
                success_count += 1

        self._log(f"\n[bold]Build complete: {success_count} succeeded, {fail_count} failed[/bold]")
        return fail_count == 0

    # -------------------------------------------------------------------------
    # PHASE 1: Prepare Repository
    # -------------------------------------------------------------------------

    def _phase1_prepare_repo(self, src: SourceConfig, repo_dir: Path) -> bool:
        """PHASE 1: Prepare the source repository.

        If update_before_build:
          1. Reset repo (remove patches, local changes)
          2. Pull latest changes
        If not update_before_build:
          Don't touch the repo
        """
        if not src.update_before_build:
            self._log(f"[dim]PHASE 1: Skipping repo update for '{src.name}'[/dim]")
            return True

        self._log(f"[bold]PHASE 1: Preparing repository '{src.name}'[/bold]")

        is_git = (repo_dir / ".git").exists()
        is_svn = (repo_dir / ".svn").exists()

        if is_git:
            # Reset to clean state (removes patches, local modifications)
            self._log("  Resetting repository...")
            if not self._run(["git", "checkout", "."], cwd=repo_dir):
                self._log("[red]  Failed to reset repository[/red]")
                return False
            self._run(["git", "clean", "-fd"], cwd=repo_dir)

            # Pull latest
            self._log("  Pulling latest changes...")
            if not self._run(["git", "pull", "--ff-only"], cwd=repo_dir):
                self._log("[red]  Failed to pull latest changes[/red]")
                return False

            # Show current state
            rev = self._run_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir)
            branch = self._run_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
            count = self._run_output(["git", "rev-list", "--count", "HEAD"], cwd=repo_dir)
            self._log(f"  Repository: {branch} @ {rev} (r{count})")

        elif is_svn:
            self._log("  Reverting local changes...")
            self._run(["svn", "revert", "-R", "."], cwd=repo_dir)

            self._log("  Updating...")
            if not self._run(["svn", "update"], cwd=repo_dir):
                self._log("[red]  Failed to update repository[/red]")
                return False

            rev = self._run_output(["svnversion"], cwd=repo_dir)
            self._log(f"  Repository: r{rev}")

        else:
            self._log(f"[yellow]  Unknown VCS in {repo_dir} - skipping update[/yellow]")

        return True

    # -------------------------------------------------------------------------
    # PHASE 2: Apply Patches
    # -------------------------------------------------------------------------

    def _phase2_apply_patches(self, src: SourceConfig, repo_dir: Path) -> bool:
        """PHASE 2: Apply patches to the source repository.

        If patch_before_build:
          - If update_before_build was active: repo is already clean
          - If update_before_build was NOT active: reset repo first, then apply
          - Apply all configured patches
        If not patch_before_build:
          Don't touch the repo
        """
        if not src.patch_before_build:
            self._log(f"[dim]PHASE 2: Skipping patches for '{src.name}'[/dim]")
            return True

        if not src.patches:
            self._log(f"[dim]PHASE 2: No patches selected for '{src.name}'[/dim]")
            return True

        self._log(f"[bold]PHASE 2: Applying patches to '{src.name}'[/bold]")

        is_git = (repo_dir / ".git").exists()

        # If repo was NOT updated, we need to ensure a clean state first
        if not src.update_before_build:
            if is_git:
                self._log("  Cleaning repo for patching...")
                self._run(["git", "checkout", "."], cwd=repo_dir)
                self._run(["git", "clean", "-fd"], cwd=repo_dir)
            else:
                self._run(["svn", "revert", "-R", "."], cwd=repo_dir)

        # Apply each patch
        applied = 0
        failed = 0
        for patch_name in src.patches:
            patch_file = self.patches_dir / patch_name
            if not patch_file.exists():
                self._log(f"  [red]Patch not found: {patch_name}[/red]")
                failed += 1
                continue

            self._log(f"  Applying: {patch_name}")
            if is_git:
                ok = self._run(
                    ["git", "apply", "--verbose", str(patch_file)],
                    cwd=repo_dir,
                )
            else:
                ok = self._run(
                    ["patch", "-p1", "-i", str(patch_file)],
                    cwd=repo_dir,
                )

            if ok:
                applied += 1
                self._log(f"  [green]OK: {patch_name}[/green]")
            else:
                failed += 1
                self._log(f"  [red]FAILED: {patch_name}[/red]")

        self._log(f"  Patches: {applied} applied, {failed} failed")
        return failed == 0

    # -------------------------------------------------------------------------
    # PHASE 3: Configure oscam
    # -------------------------------------------------------------------------

    def _phase3_configure(self, src: SourceConfig, repo_dir: Path) -> bool:
        """PHASE 3: Configure oscam modules via config.sh.

        Flow:
        1. Restore oscam defaults (config.sh -R)
        2. Disable ALL options (clean slate)
        3. Enable only the target's selected options
        4. config.sh resolves dependencies automatically
        """
        rel_path = repo_dir.relative_to(repo_dir.parent.parent.parent) if repo_dir.parent.parent.parent.exists() else repo_dir
        self._log(f"  Configuring '{src.name}' @ {rel_path}/...")

        from core.oscam_config import read_options, apply_config

        # Read available options from this repo
        opts = read_options(repo_dir)
        if not opts:
            self._log(f"  [red]Cannot read config.sh from {repo_dir}[/red]")
            return False

        # Build the enabled dict from target's build settings
        b = self.target.build
        enabled = {
            "addons": b.modules,
            "protocols": b.protocols,
            "readers": b.readers,
            "card_readers": b.card_readers,
        }

        # Apply configuration
        ok = apply_config(repo_dir, enabled, opts, log=self._log)
        if not ok:
            self._log(f"  [red]Failed to configure '{src.name}'[/red]")
            return False

        return True

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _find_repo_dir(self, name: str) -> Optional[Path]:
        """Find the repository directory for a given source name."""
        candidates = [
            self.repos_dir / name,
            self.repos_dir / f"oscam-{name}",
        ]
        if self.repos_dir.is_dir():
            for d in self.repos_dir.iterdir():
                if d.is_dir() and name in d.name and d not in candidates:
                    candidates.append(d)
        for d in candidates:
            if d.is_dir():
                return d
        return None

    def _run(self, cmd: list[str], cwd: Path) -> bool:
        """Run a command and log output. Returns True on success."""
        try:
            from rich.markup import escape as rich_escape
            proc = subprocess.Popen(
                cmd, cwd=str(cwd),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in proc.stdout:
                if self._cancel():
                    proc.kill()
                    proc.wait()
                    return False
                self._log(f"    {rich_escape(line.rstrip())}")
            proc.wait()
            return proc.returncode == 0
        except Exception as e:
            self._log(f"  [red]Error: {e}[/red]")
            return False

    def _run_output(self, cmd: list[str], cwd: Path) -> str:
        """Run a command and return stdout (stripped)."""
        try:
            result = subprocess.run(
                cmd, cwd=str(cwd),
                capture_output=True, text=True, timeout=30,
            )
            return result.stdout.strip()
        except Exception:
            return ""
