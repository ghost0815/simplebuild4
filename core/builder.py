"""Build engine for oscam — manages the complete build pipeline."""

from __future__ import annotations

import os
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from core.target import Target, SourceConfig, ToolchainConfig


class BuildEngine:
    """Executes the oscam build pipeline for a target configuration.

    Each build runs through phases:
    - PHASE 1: Prepare Repository (update, reset)
    - PHASE 2: Apply Patches
    - PHASE 3: Configure (config.sh modules)
    - PHASE 4: Build + Post-Build (make distclean, make, save artifacts)
    - PHASE 5: Upload (SSH) — TODO
    """

    def __init__(
        self,
        target: Target,
        repos_dir: Path,
        backup_dir: Path,
        repos_config: Path,
        patches_dir: Path,
        logs_dir: Path,
        binaries_dir: Path,
        bin_dir: Path,
        toolchains_dir: Path,
        toolchains_local: Path,
        toolchains_defaults: Path,
        max_cpus: int = 0,
        compiler_opt: str = "",
        sign_cert: str = "",
        sign_key: str = "",
        delete_debug: bool = True,
        save_smargo: bool = True,
        patch_webif: bool = True,
        qemu_test: bool = False,
        qemu_timeout: int = 5,
        qemu_expect: str = "Server running",
        build_overrides: str = "",
        log: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ):
        self.target = target
        self.repos_dir = repos_dir
        self.backup_dir = backup_dir
        self.repos_config = repos_config
        self.patches_dir = patches_dir
        self.logs_dir = logs_dir
        self.binaries_dir = binaries_dir
        self.bin_dir = bin_dir
        self.toolchains_dir = toolchains_dir
        self.toolchains_local = toolchains_local
        self.toolchains_defaults = toolchains_defaults
        self.max_cpus = max_cpus or os.cpu_count() or 4
        self.compiler_opt = compiler_opt
        self.sign_cert = sign_cert
        self.sign_key = sign_key
        self.delete_debug = delete_debug
        self.save_smargo = save_smargo
        self.patch_webif = patch_webif
        self.qemu_test = qemu_test
        self.qemu_timeout = qemu_timeout
        self.qemu_expect = qemu_expect
        self.build_overrides = build_overrides
        self._log = log or (lambda msg: None)
        self._cancel = cancel_check or (lambda: False)
        self._log_file = None  # set in build()

    def _log_file_only(self, msg: str) -> None:
        """Write a line only to the log file, not to the GUI."""
        if self._log_file:
            self._log_file.write(msg + "\n")
            self._log_file.flush()

    def build(self) -> bool:
        """Run the full build pipeline for all source x toolchain combinations."""
        import re

        # Setup build log file
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
        bs = self.target.build.build_system or "make"
        log_path = self.logs_dir / f"{timestamp}.{self.target.name}.build-{bs}.log"
        self._log_file = open(log_path, "w", encoding="utf-8")
        original_log = self._log

        def _log_both(msg: str):
            # Strip Rich markup for file
            plain = re.sub(r'\[/?[a-z_ #0-9]+\]', '', msg)
            self._log_file.write(plain + "\n")
            self._log_file.flush()
            original_log(msg)

        self._log = _log_both

        try:
            return self._do_build(log_path)
        finally:
            self._close_log(log_path)

    def _setup_upx_symlink(self) -> None:
        """Create/update data/bin/upx symlink based on target's upx_binary setting."""
        import shutil as _shutil
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        upx_link = self.bin_dir / "upx"

        # Determine target path: custom or system default
        upx_target = self.target.build.upx_binary.strip() if self.target.build.upx_binary else ""
        if not upx_target:
            upx_target = _shutil.which("upx") or ""

        if not upx_target:
            # No UPX found — remove symlink if exists
            if upx_link.exists() or upx_link.is_symlink():
                upx_link.unlink()
            return

        # Create/update symlink
        upx_target_path = Path(upx_target).resolve()
        if upx_link.is_symlink() or upx_link.exists():
            if upx_link.resolve() == upx_target_path:
                return  # Already correct
            upx_link.unlink()
        upx_link.symlink_to(upx_target_path)

    def _do_build(self, log_path: Path) -> bool:
        """Internal build logic."""
        sources = self.target.sources
        toolchains = self.target.toolchains

        if not sources:
            self._log("[red]No sources configured[/red]")
            return False
        if not toolchains:
            self._log("[red]No toolchains configured[/red]")
            return False

        # Setup UPX symlink and prepend data/bin to PATH
        self._setup_upx_symlink()
        os.environ["PATH"] = f"{self.bin_dir}:{os.environ.get('PATH', '')}"

        total = len(sources) * len(toolchains)
        self._log(f"[bold]Building {self.target.name}: {len(sources)} source(s) x {len(toolchains)} toolchain(s) = {total} build(s)[/bold]")
        self._log("")

        # Resolve repo directories — auto-clone if missing
        repo_dirs: dict[str, Path] = {}
        for src in sources:
            repo_dir = self._find_repo_dir(src.name)
            if not repo_dir:
                # Try to clone the repo
                repo_dir = self._auto_clone_repo(src.name)
                if not repo_dir:
                    self._log(f"[red]Repository '{src.name}' not found — skipping[/red]")
            if repo_dir:
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

        # Signing: add certificate to repos if WITH_SIGNING enabled
        if self.sign_cert and self.sign_key:
            for src in sources:
                if src.name not in repo_dirs:
                    continue
                repo_dir = repo_dirs[src.name]
                config_sh = repo_dir / "config.sh"
                if config_sh.exists():
                    from core.oscam_config import _run_config_sh
                    is_signing = _run_config_sh(config_sh, ["--enabled", "WITH_SIGNING"])
                    if is_signing == "Y":
                        self._log(f"  Adding signing certificate to '{src.name}'...")
                        result = _run_config_sh(config_sh, ["--add-cert", self.sign_cert, self.sign_key])
                        if result is not None:
                            self._log(f"  [green]Certificate added[/green]")
                        else:
                            self._log(f"  [yellow]Warning: could not add certificate[/yellow]")

        # Load toolchain registry for PHASE 4
        from core.toolchain import ToolchainRegistry
        tc_registry = ToolchainRegistry(self.toolchains_defaults, self.toolchains_local, self.toolchains_dir)
        tc_registry.load()

        # PHASE 4+5: Build and Post-Build for each source x toolchain
        self._log("")
        self._log("[bold]PHASE 4: Build[/bold]")
        success_count = 0
        fail_count = 0
        for src in sources:
            if src.name not in repo_dirs:
                fail_count += len(toolchains)
                continue
            for tcc in toolchains:
                if self._cancel():
                    self._log("[yellow]Build cancelled[/yellow]")
                    return False
                tc = tc_registry.get(tcc.name)
                if not tc:
                    self._log(f"  [red]Toolchain '{tcc.name}' not found — skipping[/red]")
                    fail_count += 1
                    continue
                bin_path = self._phase4_build(src, repo_dirs[src.name], tcc, tc)
                if bin_path:
                    success_count += 1
                else:
                    fail_count += 1

        self._log(f"\n[bold]Build complete: {success_count} succeeded, {fail_count} failed[/bold]")
        return fail_count == 0

    # -------------------------------------------------------------------------
    # Post-Build (runs immediately after each make, before next distclean)
    # -------------------------------------------------------------------------

    def _postbuild_single(self, bin_path: Path, repo_dir: Path,
                          cross: str = "", sysroot: str = "", is_native: bool = True) -> None:
        """Post-build tasks for a single build — runs immediately after each make.

        Must happen before distclean of the next build, otherwise Distribution/
        artifacts (list_smargo, .debug) would be deleted.
        """
        # Delete debug binary
        debug_bin = Path(f"{bin_path}.debug")
        if self.delete_debug and debug_bin.exists():
            debug_bin.unlink()
            self._log(f"Removed: {debug_bin.name}")

        # Save list_smargo (built by make when USE_LIBUSB is active)
        if self.save_smargo:
            dist_dir = repo_dir / "Distribution"
            if dist_dir.is_dir():
                import shutil
                for smargo in list(dist_dir.glob("list_smargo*")):
                    dest = self.binaries_dir / smargo.name
                    shutil.move(str(smargo), str(dest))
                    self._log(f"Saved: {smargo.name}")

        # Smoke test: QEMU for cross, direct for native
        if self.qemu_test:
            if is_native:
                self._native_smoke_test(bin_path)
            elif cross:
                self._qemu_smoke_test(bin_path, cross, sysroot)

    def _native_smoke_test(self, bin_path: Path) -> None:
        """Run a smoke test on a native binary (direct execution)."""
        import time
        import threading

        timeout = self.qemu_timeout
        expect = self.qemu_expect

        self._log(f"[cyan]TEST -------> [/cyan]testing {bin_path.name} ([dark_orange]{timeout}s timeout[/dark_orange])")

        try:
            proc = subprocess.Popen(
                [str(bin_path)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                preexec_fn=os.setsid,
            )

            found = False

            def reader():
                nonlocal found
                for line in proc.stdout:
                    stripped = line.rstrip()
                    if stripped:
                        self._log(f"          {stripped}")
                    if expect and expect in stripped:
                        found = True
                        return

            t = threading.Thread(target=reader, daemon=True)
            t.start()

            start = time.time()
            while time.time() - start < timeout:
                if found or self._cancel():
                    break
                time.sleep(0.2)

            try:
                os.killpg(os.getpgid(proc.pid), 9)
                proc.wait(timeout=2)
            except Exception:
                pass

            if self._cancel():
                self._log("[yellow]TEST -------> CANCELLED[/yellow]")
            elif found:
                self._log(f"[green]TEST -------> PASS ({expect} found)[/green]")
            else:
                self._log(f"[red]TEST -------> FAIL ({expect} not found within {timeout}s)[/red]")

        except Exception as e:
            self._log(f"[red]TEST -------> ERROR: {e}[/red]")

    def _qemu_smoke_test(self, bin_path: Path, cross: str, sysroot: str) -> None:
        """Run a QEMU smoke test on a cross-compiled binary (like s3).

        Starts the binary under qemu-<arch>, waits for expected output string,
        kills after timeout. PASS if expected string found, FAIL otherwise.
        """
        import shutil as _shutil
        import time
        import threading

        # Determine qemu arch from cross-compiler's target
        _QEMU_ARCH_MAP = {
            "arm": "arm", "armv7": "arm", "armv7l": "arm", "armv7ve": "arm",
            "armv7a": "arm", "armv7ahf": "arm", "armv6": "arm", "armv5te": "arm",
            "aarch64": "aarch64", "arm64": "aarch64",
            "mipsel": "mipsel", "mips32el": "mipsel",
            "mips": "mips",
            "i686": "i386", "i386": "i386",
            "x86_64": "x86_64",
            "riscv64": "riscv64",
            "sh4": "sh4",
        }

        try:
            dumpmachine = subprocess.run(
                [f"{cross}gcc", "-dumpmachine"], capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            raw_arch = dumpmachine.split("-")[0]
            qemu_arch = _QEMU_ARCH_MAP.get(raw_arch, raw_arch)
        except Exception:
            self._log("[yellow]QEMU -------> SKIP (cannot determine arch)[/yellow]")
            return

        qemu_bin = f"qemu-{qemu_arch}"
        if not _shutil.which(qemu_bin):
            self._log(f"[yellow]QEMU -------> SKIP ({qemu_bin} not found)[/yellow]")
            return

        # Prepare sysroot for qemu -L (strip trailing /usr)
        qemu_sysroot = sysroot.rstrip("/")
        if qemu_sysroot.endswith("/usr"):
            qemu_sysroot = qemu_sysroot[:-4]

        timeout = self.qemu_timeout
        expect = self.qemu_expect

        self._log(f"[cyan]QEMU -------> [/cyan]testing {bin_path.name} ([dark_orange]{timeout}s timeout[/dark_orange])")

        try:
            proc = subprocess.Popen(
                [qemu_bin, "-L", qemu_sysroot, str(bin_path)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                preexec_fn=os.setsid,
            )

            found = False

            # Reader thread to avoid blocking on readline
            def reader():
                nonlocal found
                for line in proc.stdout:
                    stripped = line.rstrip()
                    if stripped and not stripped.startswith("qemu: Unsupported syscall"):
                        self._log(f"          {stripped}")
                    if expect and expect in stripped:
                        found = True
                        return

            t = threading.Thread(target=reader, daemon=True)
            t.start()

            # Wait for expect string or timeout
            start = time.time()
            while time.time() - start < timeout:
                if found or self._cancel():
                    break
                time.sleep(0.2)

            # Kill entire process group (oscam may fork)
            try:
                os.killpg(os.getpgid(proc.pid), 9)
                proc.wait(timeout=2)
            except Exception:
                pass

            if self._cancel():
                self._log("[yellow]QEMU -------> CANCELLED[/yellow]")
            elif found:
                self._log(f"[green]QEMU -------> PASS ({expect} found)[/green]")
            else:
                self._log(f"[red]QEMU -------> FAIL ({expect} not found within {timeout}s)[/red]")

        except Exception as e:
            self._log(f"[red]QEMU -------> ERROR: {e}[/red]")

    def _close_log(self, log_path: Path) -> None:
        """Close the build log file and show its path."""
        try:
            self._log(f"Log: {log_path}")
            self._log_file.close()
        except Exception:
            pass

    def _auto_clone_repo(self, source_name: str) -> Optional[Path]:
        """Auto-clone a repository if it doesn't exist yet."""
        try:
            from core.repos_config import load_repos
            from core.repo import RepoManager
            repos = load_repos(self.repos_config)
            for rc in repos:
                if rc.name == source_name:
                    mgr = RepoManager(rc, self.repos_dir, self.backup_dir)
                    self._log(f"[cyan]Cloning repository '{source_name}' from {rc.url}...[/cyan]")
                    if mgr.clone(log=self._log):
                        return mgr.repo_path
                    return None
        except Exception as e:
            self._log(f"[red]Auto-clone failed: {e}[/red]")
        return None

    # -------------------------------------------------------------------------
    # PHASE 1: Prepare Repository
    # -------------------------------------------------------------------------

    def _phase1_prepare_repo(self, src: SourceConfig, repo_dir: Path) -> bool:
        """PHASE 1: Prepare the source repository.

        If update_before_build:
          1. Restore repo from backup (clean state, removes patches)
             Fallback: git checkout + clean if no backup exists
          2. Pull latest changes
          3. Create new backup
        If not update_before_build:
          Don't touch the repo at all
        """
        if not src.update_before_build:
            self._log(f"[dim]PHASE 1: Skipping repo update for '{src.name}'[/dim]")
            return True

        self._log(f"[bold]PHASE 1: Preparing repository '{src.name}'[/bold]")

        # Get RepoManager for this source
        repo_mgr = self._get_repo_manager(src.name)

        if repo_mgr:
            # Restore from backup (fast, guaranteed clean state)
            backups = repo_mgr.list_backups()
            if backups:
                self._log("  Restoring from backup...")
                if not repo_mgr.restore(log=self._log):
                    self._log("[red]  Restore failed — falling back to git reset[/red]")
                    self._git_reset(repo_dir)
            else:
                self._log("  No backup found — using git reset")
                self._git_reset(repo_dir)

            # Pull latest
            self._log("  Pulling latest changes...")
            is_git = (repo_dir / ".git").exists()
            if is_git:
                if not self._run(["git", "pull", "--ff-only"], cwd=repo_dir):
                    self._log("[red]  Failed to pull latest changes[/red]")
                    return False
            else:
                if not self._run(["svn", "update"], cwd=repo_dir):
                    self._log("[red]  Failed to update repository[/red]")
                    return False

            # Show current state
            self._log_repo_state(repo_dir)

            # Create new backup only if revision changed
            current_rev = self._run_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir) if (repo_dir / ".git").exists() else self._run_output(["svnversion"], cwd=repo_dir)
            if current_rev and current_rev not in repo_mgr.list_backups():
                repo_mgr._backup(log=self._log)

        else:
            # No RepoManager available — fallback to git reset + pull
            self._log("  [yellow]No repo config found — using git fallback[/yellow]")
            self._git_reset(repo_dir)

            self._log("  Pulling latest changes...")
            if not self._run(["git", "pull", "--ff-only"], cwd=repo_dir):
                self._log("[red]  Failed to pull latest changes[/red]")
                return False

            self._log_repo_state(repo_dir)

        return True

    def _get_repo_manager(self, source_name: str):
        """Get a RepoManager for the given source name."""
        try:
            from core.repos_config import load_repos
            from core.repo import RepoManager
            repos = load_repos(self.repos_config)
            for rc in repos:
                if rc.name == source_name:
                    return RepoManager(rc, self.repos_dir, self.backup_dir)
        except Exception:
            pass
        return None

    def _git_reset(self, repo_dir: Path) -> None:
        """Fallback: reset repo via git checkout + clean."""
        is_git = (repo_dir / ".git").exists()
        if is_git:
            self._run(["git", "checkout", "."], cwd=repo_dir)
            self._run(["git", "clean", "-fd"], cwd=repo_dir)
        else:
            self._run(["svn", "revert", "-R", "."], cwd=repo_dir)

    def _log_repo_state(self, repo_dir: Path) -> None:
        """Log current repository revision info."""
        is_git = (repo_dir / ".git").exists()
        if is_git:
            rev = self._run_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir)
            branch = self._run_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
            count = self._run_output(["git", "rev-list", "--count", "HEAD"], cwd=repo_dir)
            self._log(f"  Repository: {branch} @ {rev} (r{count})")
        else:
            rev = self._run_output(["svnversion"], cwd=repo_dir)
            self._log(f"  Repository: r{rev}")

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

        # Patch WebIF with build/patch info (if setting enabled and patches applied)
        if applied > 0 and self.patch_webif:
            self._patch_webif(repo_dir, src.patches)

        return failed == 0

    def _patch_webif(self, repo_dir: Path, patches: list[str]) -> None:
        """Inject patch info into oscam source so WebIF shows which patches are applied."""
        config_h = repo_dir / "config.h"
        oscam_c = repo_dir / "oscam.c"

        if not config_h.exists() or not oscam_c.exists():
            return

        try:
            # Build patch info string
            patch_names = "\\n\\t\\t".join(patches)
            patch_str = f"yes\\n\\t\\t{patch_names}"

            # Modify config.h: add #define S4PATCHED before #endif
            content = config_h.read_text(encoding="utf-8", errors="replace")
            # Remove previous S4PATCHED if exists
            content = "\n".join(line for line in content.splitlines() if "S4PATCHED" not in line)
            # Add before final #endif
            content = content.replace(
                "#endif //OSCAM_CONFIG_H_",
                f'#define S4PATCHED "{patch_str}"\n#endif //OSCAM_CONFIG_H_',
            )
            config_h.write_text(content, encoding="utf-8")

            # Modify oscam.c: add fprintf for patch info after WebifPort line
            content = oscam_c.read_text(encoding="utf-8", errors="replace")
            # Remove previous S4PATCHED block if exists
            lines = content.splitlines()
            filtered = []
            skip = False
            for line in lines:
                if "#ifdef S4PATCHED" in line:
                    skip = True
                    continue
                if skip and "#endif" in line:
                    skip = False
                    continue
                if skip:
                    continue
                filtered.append(line)
            content = "\n".join(filtered)

            # Insert after WebifPort fprintf
            content = content.replace(
                'fprintf(fp, "WebifPort:\\t%s\\n", WebifPort);',
                'fprintf(fp, "WebifPort:\\t%s\\n", WebifPort);\n'
                '#ifdef S4PATCHED\n'
                '\tfprintf(fp, "Patched:\\t%s\\n", S4PATCHED);\n'
                '#endif',
            )
            oscam_c.write_text(content, encoding="utf-8")

            self._log(f"  WebIF patched with build info")
        except Exception as e:
            self._log(f"  [yellow]WebIF patch failed: {e}[/yellow]")

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

        # Apply build overrides (e.g. "USE_SSL USE_PCSC_off MODULE_GBOX_off")
        if self.build_overrides:
            from core.oscam_config import _run_config_sh
            config_sh = repo_dir / "config.sh"
            self._log(f"  Applying build overrides...")
            for token in self.build_overrides.split():
                if token.endswith("_off"):
                    opt = token[:-4]  # strip _off
                    _run_config_sh(config_sh, ["-D", opt])
                    self._log(f"    [red]Disable[/red] {opt}")
                else:
                    _run_config_sh(config_sh, ["-E", token])
                    self._log(f"    [green]Enable[/green] {token}")

        return True

    # -------------------------------------------------------------------------
    # PHASE 4: Build (make)
    # -------------------------------------------------------------------------

    def _phase4_build(self, src: SourceConfig, repo_dir: Path, tcc: ToolchainConfig, tc) -> bool:
        """PHASE 4: Execute make for each source x toolchain combination."""
        self._log(f"\n  [bold]Building {src.name} x {tcc.name}[/bold]")

        b = self.target.build
        is_native = tc.native

        # --- Toolchain paths ---
        tc_dir = self.toolchains_dir / tc.name
        if is_native:
            cross = ""
            sysroot = ""
        else:
            cross = str(tc_dir / "bin" / tc.prefix)
            sysroot_path = tc_dir / tc.sysroot if tc.sysroot else tc_dir
            sysroot = str(sysroot_path)

        # --- Auto-inject --sysroot if compiler's built-in sysroot is invalid ---
        # Like s3: override CC= so every gcc invocation (including Makefile's
        # internal $(CC) -print-sysroot) sees the correct sysroot.
        cross_sysroot = ""
        if not is_native and cross:
            import shutil as _shutil
            gcc_path = _shutil.which(f"{tc.prefix}gcc", path=f"{tc_dir}/bin:{os.environ.get('PATH', '')}")
            if gcc_path:
                try:
                    gcc_sysroot = subprocess.run(
                        [gcc_path, "-print-sysroot"], capture_output=True, text=True, timeout=5,
                    ).stdout.strip()
                except Exception:
                    gcc_sysroot = ""
                if not gcc_sysroot or not Path(gcc_sysroot).is_dir():
                    cross_sysroot = f"--sysroot={sysroot}"
                    self._log(f"  Auto-injected: {cross_sysroot}")

        # --- CC_OPTS, CFLAGS, LDFLAGS ---
        # s4 settings = base, toolchain appends, target overrides toolchain
        tc_cc = tcc.cc_opts if tcc.cc_opts else (tc.cc_opts or "")
        cc_opts = f"{self.compiler_opt} {tc_cc}".strip() if tc_cc else (self.compiler_opt or "")
        cflags = tcc.cflags if tcc.cflags else (tc.cflags or "")
        if cross_sysroot and "--sysroot" not in cflags:
            cflags = f"{cflags} {cross_sysroot}".strip()
        ldflags = tcc.ldflags if tcc.ldflags else (tc.ldflags or "")
        extra_make = tcc.make_vars if tcc.make_vars else (tc.make_vars or "")

        # --- USE_FLAGS ---
        use_flags = []
        if b.use_libcrypto: use_flags.append("USE_LIBCRYPTO=1")
        if b.use_ssl: use_flags.append("USE_SSL=1")
        if b.use_libusb: use_flags.append("USE_LIBUSB=1")
        if b.use_pcsc: use_flags.append("USE_PCSC=1")
        if b.use_libdvbcsa: use_flags.append("USE_LIBDVBCSA=1")
        if b.use_stapi: use_flags.append("USE_STAPI=1")
        if b.use_stapi5: use_flags.append("USE_STAPI5=1")
        if b.use_coolapi: use_flags.append("USE_COOLAPI=1")
        if b.use_coolapi2: use_flags.append("USE_COOLAPI2=1")
        if b.use_su980: use_flags.append("USE_SU980=1")
        if b.use_azbox: use_flags.append("USE_AZBOX=1")
        if b.use_amsmc: use_flags.append("USE_AMSMC=1")
        if b.use_mca: use_flags.append("USE_MCA=1")
        if b.use_compress:
            use_flags.append("USE_COMPRESS=1")
            if b.compress_level and b.compress_level != "--best":
                use_flags.append(f"COMP_LEVEL={b.compress_level}")

        # --- Apply build overrides to USE_FLAGS ---
        if self.build_overrides:
            for token in self.build_overrides.split():
                if token.startswith("USE_"):
                    if token.endswith("_off"):
                        # Remove: USE_SSL_off → remove USE_SSL=1
                        flag = token[:-4] + "=1"
                        use_flags = [f for f in use_flags if f != flag]
                    else:
                        # Add: USE_STAPI → add USE_STAPI=1
                        flag = token + "=1"
                        if flag not in use_flags:
                            use_flags.append(flag)

        # --- Static library detection ---
        static_libs = {}
        if is_native:
            # Search system paths + native toolchain local libs
            search_dirs = self._get_native_lib_paths(tc_dir)
        else:
            search_dirs = [sysroot] if sysroot else [str(tc_dir)]

        lib_map = {
            "LIBCRYPTO": ("libcrypto.a", b.use_libcrypto, b.static_libcrypto),
            "SSL": ("libssl.a", b.use_ssl, b.static_ssl),
            "LIBUSB": ("libusb-1.0.a", b.use_libusb, b.static_libusb),
            "PCSC": ("libpcsclite.a", b.use_pcsc, b.static_pcsc),
            "LIBDVBCSA": ("libdvbcsa.a", b.use_libdvbcsa, b.static_libdvbcsa),
        }
        for key, (lib_file, use_flag, static_flag) in lib_map.items():
            if use_flag and static_flag:
                found = self._find_static_lib(lib_file, search_dirs)
                if found:
                    static_libs[f"{key}_LIB"] = found
                    self._log(f"  Static: {key}_LIB={found}")
                else:
                    self._log(f"  [yellow]Static {lib_file} not found[/yellow]")

        # --- STAPI library ---
        stapi_lib = ""
        if b.use_stapi or b.use_stapi5:
            stapi_val = b.stapi_lib if b.stapi_lib else (tc.stapi_lib or "")
            # Fallback: use shipped stapi libs (like s3's support/stapi/)
            if not stapi_val:
                stapi_dir = self.repos_dir.parent.parent / "support" / "stapi"
                if b.use_stapi5:
                    # STAPI5: try platform-specific libs
                    for candidate in ("liboscam_stapi5_UFS916.a", "liboscam_stapi5_OPENBOX.a"):
                        if (stapi_dir / candidate).exists():
                            stapi_val = str(stapi_dir / candidate)
                            break
                else:
                    default_lib = stapi_dir / "liboscam_stapi.a"
                    if default_lib.exists():
                        stapi_val = str(default_lib)
            if stapi_val:
                stapi_key = "STAPI5_LIB" if b.use_stapi5 else "STAPI_LIB"
                stapi_lib = f"{stapi_key}={stapi_val}"
                self._log(f"  {stapi_lib}")
            else:
                self._log(f"  [yellow]Warning: STAPI enabled but no library found[/yellow]")

        # --- CONF_DIR ---
        confdir = b.confdir or "/usr/local/etc"

        # --- Binary name ---
        oscam_name = self._generate_binary_name(repo_dir, tcc.name, is_native, b)

        # --- Output path ---
        self.binaries_dir.mkdir(parents=True, exist_ok=True)
        bin_path = self.binaries_dir / oscam_name

        # --- Log common settings ---
        self._log(f"  Build system: {b.build_system}")
        self._log(f"  Output: {bin_path.name}")
        self._log(f"  CROSS: {cross or '(native)'}")
        if cross_sysroot:
            self._log(f"  CC: {cross}gcc {cross_sysroot}")
        self._log(f"  CC_OPTS: {cc_opts}")
        if cflags:
            self._log(f"  EXTRA_CFLAGS: {cflags}")
        if ldflags:
            self._log(f"  EXTRA_LDFLAGS: {ldflags}")
        self._log(f"  CONF_DIR: {confdir}")
        self._log(f"  CPUs: {self.max_cpus}")
        self._log("")

        # --- Dispatch to make or cmake ---
        started = datetime.now()
        if b.build_system == "cmake":
            ok, bin_path = self._build_cmake(
                repo_dir, bin_path, cross, sysroot, cross_sysroot,
                cc_opts, cflags, ldflags, confdir, use_flags, static_libs,
                stapi_lib, is_native, tc, tcc,
            )
        else:
            ok, bin_path = self._build_make(
                repo_dir, bin_path, cross, cross_sysroot,
                cc_opts, cflags, ldflags, extra_make, confdir,
                use_flags, static_libs, stapi_lib,
            )
        elapsed = (datetime.now() - started).total_seconds()

        if ok and bin_path and bin_path.exists():
            size_kb = bin_path.stat().st_size / 1024
            self._log("")
            self._log(f"[green]OK: {bin_path.name} ({size_kb:.0f} KB) in {elapsed:.0f}s[/green]")

            # Post-build: immediately after each build (before distclean of next build)
            self._postbuild_single(bin_path, repo_dir, cross, sysroot, is_native)

            return bin_path
        else:
            self._log(f"[red]Build FAILED after {elapsed:.0f}s[/red]")
            return None

    def _build_make(self, repo_dir, bin_path, cross, cross_sysroot,
                    cc_opts, cflags, ldflags, extra_make, confdir,
                    use_flags, static_libs, stapi_lib):
        """Build using make (default)."""
        make_cmd = ["make", f"-j{self.max_cpus}"]
        make_cmd.append(f"CONF_DIR={confdir}")
        make_cmd.append(f"OSCAM_BIN={bin_path}")
        if cc_opts:
            make_cmd.append(f"CC_OPTS={cc_opts}")
        if cflags:
            make_cmd.append(f"EXTRA_CFLAGS={cflags}")
        if ldflags:
            make_cmd.append(f"EXTRA_LDFLAGS={ldflags}")
        if cross:
            make_cmd.append(f"CROSS={cross}")
        if cross_sysroot:
            make_cmd.append(f"CC={cross}gcc {cross_sysroot}")
        for flag in use_flags:
            make_cmd.append(flag)
        for key, val in static_libs.items():
            make_cmd.append(f"{key}={val}")
        if stapi_lib:
            make_cmd.append(stapi_lib)
        if extra_make:
            make_cmd.extend(extra_make.split())

        # Clean
        self._log("  Cleaning...")
        self._run(["make", "distclean"], cwd=repo_dir, colorize=True)

        # Build
        self._log("  Compiling...")
        ok = self._run(make_cmd, cwd=repo_dir, colorize=True)
        return ok, bin_path

    def _build_cmake(self, repo_dir, bin_path, cross, sysroot, cross_sysroot,
                     cc_opts, cflags, ldflags, confdir, use_flags, static_libs,
                     stapi_lib, is_native, tc, tcc):
        """Build using cmake."""
        import shutil as _shutil

        build_dir = repo_dir / "build_s4"

        # Clean previous cmake build
        self._log("  Cleaning...")
        if build_dir.exists():
            _shutil.rmtree(build_dir)
        # Also distclean make artifacts
        self._run(["make", "distclean"], cwd=repo_dir, colorize=True)

        build_dir.mkdir(parents=True, exist_ok=True)

        # --- Generate cmake toolchain file for cross-compilation ---
        toolchain_file = None
        if not is_native and cross:
            toolchain_file = build_dir / "s4-toolchain.cmake"
            gcc_path = f"{cross}gcc"
            gxx_path = f"{cross}g++"
            if cross_sysroot:
                gcc_path = f"{gcc_path} {cross_sysroot}"
                gxx_path = f"{gxx_path} {cross_sysroot}"

            # Combine all C flags for cmake toolchain
            all_cflags = f"{cc_opts} {cflags}".strip()

            tc_content = f"""# Auto-generated by s4 BuildEngine
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR {tc.arch or 'arm'})
set(CMAKE_C_COMPILER {cross}gcc)
set(CMAKE_CXX_COMPILER {cross}g++)
set(CMAKE_C_FLAGS_INIT "{all_cflags}")
set(CMAKE_CXX_FLAGS_INIT "{all_cflags}")
set(CMAKE_EXE_LINKER_FLAGS_INIT "{ldflags}")
set(CMAKE_FIND_ROOT_PATH "{sysroot}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
"""
            if cross_sysroot:
                tc_content += f'set(CMAKE_SYSROOT "{sysroot}")\n'

            toolchain_file.write_text(tc_content)
            self._log(f"  Generated toolchain file: {toolchain_file.name}")

        # --- cmake configure ---
        cmake_cmd = ["cmake", str(repo_dir)]

        # Toolchain
        if toolchain_file:
            cmake_cmd.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file}")

        # Compiler flags (only for native builds — cross uses toolchain file)
        if not toolchain_file:
            all_cflags = f"{cc_opts} {cflags}".strip()
            if all_cflags:
                cmake_cmd.append(f"-DCMAKE_C_FLAGS={all_cflags}")
                cmake_cmd.append(f"-DCMAKE_CXX_FLAGS={all_cflags}")
            if ldflags:
                cmake_cmd.append(f"-DCMAKE_EXE_LINKER_FLAGS={ldflags}")

        # Config dir
        cmake_cmd.append(f"-DCS_CONFDIR={confdir}")

        # USE_FLAGS as cmake -D variables (translated to cmake names)
        _CMAKE_FLAG_MAP = {
            "USE_SSL": "WITH_SSL",
            "USE_LIBCRYPTO": None,         # auto via WITH_SSL in cmake
            "USE_LIBUSB": "HAVE_LIBUSB",
            "USE_PCSC": "HAVE_PCSC",
            "USE_LIBDVBCSA": "HAVE_LIBDVBCSA",
            "USE_STAPI": "WITH_STAPI",
            "USE_STAPI5": "WITH_STAPI5",
            "USE_COMPRESS": "USE_COMPRESS",  # same name
            "USE_COOLAPI": "WITH_COOLAPI",
            "USE_COOLAPI2": "WITH_COOLAPI2",
            "USE_SU980": "WITH_SU980",
            "USE_AZBOX": "WITH_AZBOX",
            "USE_AMSMC": "WITH_AMSMC",
            "USE_MCA": "WITH_MCA",
        }
        for flag in use_flags:
            if "=" in flag:
                key, val = flag.split("=", 1)
                cmake_key = _CMAKE_FLAG_MAP.get(key, key)
                if cmake_key is not None:
                    cmake_cmd.append(f"-D{cmake_key}={val}")

        # Static libraries — cmake variable names differ from make
        b = self.target.build
        # OpenSSL static: set OPENSSL_*_LIBRARY (singular = cache hint for find_package)
        if b.static_ssl and "SSL_LIB" in static_libs:
            cmake_cmd.append(f"-DOPENSSL_SSL_LIBRARY={static_libs['SSL_LIB']}")
        if b.static_libcrypto and "LIBCRYPTO_LIB" in static_libs:
            cmake_cmd.append(f"-DOPENSSL_CRYPTO_LIBRARY={static_libs['LIBCRYPTO_LIB']}")
        if b.static_libusb: cmake_cmd.append("-DSTATIC_LIBUSB=1")
        if b.static_libdvbcsa: cmake_cmd.append("-DSTATIC_LIBDVBCSA=1")
        # Note: STATIC_PCSC not supported by oscam's CMakeLists.txt

        # STAPI
        if stapi_lib and "=" in stapi_lib:
            key, val = stapi_lib.split("=", 1)
            cmake_cmd.append(f"-D{key}={val}")

        # Build type
        cmake_cmd.append("-DCMAKE_BUILD_TYPE=Release")

        self._log("  Configuring (cmake)...")
        ok = self._run(cmake_cmd, cwd=build_dir)
        if not ok:
            self._log("[red]cmake configure FAILED[/red]")
            return False, None

        # --- cmake build ---
        self._log("  Compiling...")
        build_cmd = ["cmake", "--build", ".", "--", f"-j{self.max_cpus}"]
        ok = self._run(build_cmd, cwd=build_dir, colorize=True)

        if not ok:
            return False, None

        # --- Find and copy the built binary ---
        # cmake builds to build_dir/oscam or build_dir/oscam-upx
        cmake_bin = None
        for candidate in ("oscam-upx", "oscam"):
            p = build_dir / candidate
            if p.exists():
                cmake_bin = p
                break

        if not cmake_bin:
            self._log("[red]cmake binary not found in build directory[/red]")
            return False, None

        # Copy to our standard binaries location with proper name
        import shutil
        shutil.copy2(str(cmake_bin), str(bin_path))
        return True, bin_path

    def _generate_binary_name(self, repo_dir: Path, tc_name: str, is_native: bool, b) -> str:
        """Generate the output binary name.

        Format: oscam-git<rev>@<commit>-<toolchain>-<backend>-<features>-<linking>
        """
        # Custom name overrides everything
        if b.use_oscamname:
            return b.use_oscamname

        # Revision info — detect VCS type
        # Like s3: use config.sh --oscam-revision for the revision number
        is_git = (repo_dir / ".git").exists()
        is_svn = (repo_dir / ".svn").exists()
        config_sh = repo_dir / "config.sh"

        if is_git:
            vcs = "git"
            # Use config.sh --oscam-revision like s3
            rev = ""
            if config_sh.exists():
                from core.oscam_config import _run_config_sh
                rev = _run_config_sh(config_sh, ["--oscam-revision"]) or ""
                if not rev:
                    ver = _run_config_sh(config_sh, ["--oscam-version"]) or ""
                    rev = ver.split("-", 1)[1] if "-" in ver else ver
            if not rev:
                rev = self._run_output(["git", "rev-list", "--count", "HEAD"], cwd=repo_dir) or "0"
            commit = self._run_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir) or "unknown"
        elif is_svn:
            vcs = "svn"
            # Use config.sh --oscam-revision like s3 (cleaner than svnversion)
            if config_sh.exists():
                from core.oscam_config import _run_config_sh
                rev = _run_config_sh(config_sh, ["--oscam-revision"]) or ""
                if not rev:
                    # Fallback: config.sh --oscam-version | cut revision part
                    ver = _run_config_sh(config_sh, ["--oscam-version"]) or ""
                    rev = ver.split("-", 1)[1] if "-" in ver else ver
            if not rev:
                rev = self._run_output(["svnversion"], cwd=repo_dir) or "0"
            commit = ""
        else:
            vcs = "git"
            rev = "0"
            commit = "unknown"

        # Toolchain part
        if is_native:
            tc_part = f"{socket.gethostname().split('.')[0]}-{os.uname().machine}"
        else:
            tc_part = tc_name

        # SSL backend (openssl vs mbedtls) — only if Makefile supports USE_OPENSSL
        backend = ""
        makefile = repo_dir / "Makefile"
        if makefile.exists():
            try:
                mk_content = makefile.read_text(encoding="utf-8", errors="replace")
                if "USE_OPENSSL" in mk_content:
                    # New oscam with dual backend support
                    backend = "-openssl"  # TODO: add USE_OPENSSL flag to target config
            except Exception:
                pass

        # Feature suffixes (alphabetically sorted)
        features = []
        from core.oscam_config import _run_config_sh
        config_sh = repo_dir / "config.sh"
        if config_sh.exists():
            checks = [
                ("HAVE_DVBAPI", "dvbapi"),
                ("WITH_EMU", "emu"),
                ("CARDREADER_GXAPI", "gxapi"),
                ("MODULE_STREAMRELAY", "icam"),
                ("IPV6SUPPORT", "ipv6"),
                ("WITH_ARM_NEON", "neon"),
                ("WITH_SIGNING", "signed"),
                ("WITH_SSL", "ssl"),
                ("WEBIF", "webif"),
            ]
            for option, suffix in checks:
                result = _run_config_sh(config_sh, ["--enabled", option])
                if result == "Y":
                    features.append(suffix)

        # USE_FLAGS based features
        if b.use_libdvbcsa:
            features.append("libdvbcsa") if "libdvbcsa" not in features else None
        if b.use_libusb:
            features.append("libusb")
        if b.use_pcsc:
            features.append("pcsc")
        if b.use_stapi:
            features.append("stapi")
        if b.use_stapi5:
            features.append("stapi5")
        if b.use_compress:
            features.append("upx")

        features.sort()

        # Linking type
        lib_count = sum(1 for x in [b.use_libcrypto, b.use_ssl, b.use_libusb, b.use_pcsc, b.use_libdvbcsa] if x)
        stat_count = sum(1 for x in [b.static_libcrypto, b.static_ssl, b.static_libusb, b.static_pcsc, b.static_libdvbcsa] if x)
        if stat_count > 0 and stat_count >= lib_count:
            linking = "-static"
        elif stat_count > 0:
            linking = "-mixed"
        else:
            linking = ""

        # Assemble
        feat_str = "-" + "-".join(features) if features else ""
        # Git: oscam-git10864@6c8324e0-toolchain-features
        # SVN: oscam-svn2039-toolchain-features (no @commit)
        if commit:
            return f"oscam-{vcs}{rev}@{commit}-{tc_part}{backend}{feat_str}{linking}"
        else:
            return f"oscam-{vcs}{rev}-{tc_part}{backend}{feat_str}{linking}"

    def _get_native_lib_paths(self, tc_dir: Path) -> list[str]:
        """Get library search paths for native toolchain.

        Includes native toolchain local libs (data/toolchains/native/)
        AND system library paths from ldconfig.
        """
        paths = []
        # Native toolchain local libs first (higher priority)
        for d in tc_dir.glob("**/lib"):
            if d.is_dir():
                paths.append(str(d))
        # System paths
        try:
            result = subprocess.run(
                ["ldconfig", "-v"],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "LC_ALL": "C"},
            )
            for line in result.stderr.splitlines() + result.stdout.splitlines():
                if line and not line.startswith("\t") and ":" in line:
                    path = line.split(":")[0].strip()
                    if path and Path(path).is_dir():
                        paths.append(path)
        except Exception:
            paths.extend(["/usr/lib", "/usr/local/lib", "/lib"])
        return paths

    def _find_static_lib(self, lib_name: str, search_dirs: list[str]) -> str:
        """Find a static library (.a) file in search directories."""
        for search_dir in search_dirs:
            for root, dirs, files in os.walk(search_dir):
                if lib_name in files:
                    return os.path.join(root, lib_name)
        return ""

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

    def _run(self, cmd: list[str], cwd: Path, colorize: bool = False) -> bool:
        """Run a command and log output. Returns True on success.

        If colorize=True, applies s3-style coloring to make/build output.
        Filtered lines still go to the log file (plain), just not to the GUI.
        """
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
                stripped = line.rstrip()
                if colorize:
                    colored = self._colorize_build_line(stripped)
                    if colored is not None:
                        self._log(f"    {colored}")
                    else:
                        # Filtered from GUI but still write to log file
                        self._log_file_only(f"    {stripped}")
                else:
                    self._log(f"    {rich_escape(stripped)}")
            proc.wait()
            return proc.returncode == 0
        except Exception as e:
            self._log(f"  [red]Error: {e}[/red]")
            return False

    # Build output coloring (like s3)
    _BUILD_COLORS = [
        # (prefix, rich color, label)       — order matters, first match wins
        ("HOSTCC",  "green",        "BUILD ------> "),
        ("CC\t",    "green",        "BUILD ------> "),
        ("BUILD\t", "green",        "BUILD ------> "),
        ("GEN\t",   "cyan",         "GEN --------> "),
        ("WIKI\t",  "cyan",         "WIKI -------> "),
        ("CONF\t",  "cyan",         "CONFIG -----> "),
        ("LINK\t",  "magenta",      "LINK -------> "),
        ("STRIP\t", "magenta",      "STRIP ------> "),
        ("RM\t",    "red",          "REMOVE -----> "),
        ("UPX\t",   "bright_green", "UPX --------> "),
        ("UPX ",    "bright_green", "UPX --------> "),
        ("SIGN\t",  "bright_yellow","SIGN -------> "),
        ("SIGN ",   "bright_yellow","SIGN -------> "),
    ]

    def _colorize_build_line(self, line: str) -> str | None:
        """Colorize a make/cmake output line like s3. Returns None to skip the line."""
        from rich.markup import escape as rich_escape

        stripped = line.lstrip()

        # Info block lines (| ...) → yellow
        if stripped.startswith("|") or stripped.startswith("+---"):
            return f"[yellow]{rich_escape(stripped)}[/yellow]"

        # Submodule lines
        if "submodule" in stripped.lower():
            return f"[cyan]{rich_escape(stripped)}[/cyan]"

        # Known make prefixes
        for prefix, color, label in self._BUILD_COLORS:
            if stripped.startswith(prefix):
                rest = stripped[len(prefix):].strip()
                return f"[{color}]{label}[/{color}]{rich_escape(rest)}"

        # cmake output: [  5%] Building C object ..., Linking C ..., Built target ...
        if stripped.startswith("[") and "%]" in stripped[:8]:
            rest = stripped.split("]", 1)[1].strip() if "]" in stripped else stripped
            pct = stripped.split("]")[0].strip("[ %")
            if "Linking" in rest:
                return f"[magenta]LINK -------> \\[{pct}%][/magenta] {rich_escape(rest)}"
            elif "Built target" in rest:
                return f"[green]DONE -------> \\[{pct}%][/green] {rich_escape(rest)}"
            elif "Building" in rest:
                # Shorten: "Building C object CMakeFiles/csreaders.dir/reader-nagra.c.o" → "reader-nagra.c"
                short = rest
                if ".dir/" in rest:
                    short = rest.split(".dir/")[-1].replace(".o", "")
                return f"[green]BUILD ------> \\[{pct}%][/green] {rich_escape(short)}"
            else:
                return f"    \\[{pct}%] {rich_escape(rest)}"

        # cmake status messages (-- ...)
        if stripped.startswith("-- "):
            return f"[cyan]{rich_escape(stripped)}[/cyan]"

        # cmake [SIGN] [UPX] markers
        if stripped.startswith("[SIGN]"):
            return f"[bright_yellow]SIGN -------> [/bright_yellow]{rich_escape(stripped[6:].strip())}"
        if stripped.startswith("[ UPX]") or stripped.startswith("[UPX]"):
            rest = stripped.split("]", 1)[1].strip()
            return f"[bright_green]UPX --------> [/bright_green]{rich_escape(rest)}"

        # Error/warning lines
        if any(kw in stripped.lower() for kw in ("error", "warning", "fatal", "undefined")):
            return f"[red]{rich_escape(stripped)}[/red]"

        # Skip unknown lines (noise from compiler, BFD warnings, etc.)
        return None

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
