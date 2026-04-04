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
        binaries_dir: Path,
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
        log: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ):
        self.target = target
        self.repos_dir = repos_dir
        self.patches_dir = patches_dir
        self.logs_dir = logs_dir
        self.binaries_dir = binaries_dir
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
        self._log = log or (lambda msg: None)
        self._cancel = cancel_check or (lambda: False)

    def build(self) -> bool:
        """Run the full build pipeline for all source x toolchain combinations."""
        import re

        # Setup build log file
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
        log_path = self.logs_dir / f"{timestamp}.{self.target.name}.build.log"
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
        built_binaries = []
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
                    built_binaries.append((bin_path, repo_dirs[src.name]))
                    success_count += 1
                else:
                    fail_count += 1

        # PHASE 5: Post-Build
        if built_binaries:
            self._log("")
            self._log("[bold]PHASE 5: Post-Build[/bold]")
            for bin_path, repo_dir in built_binaries:
                self._phase5_postbuild(bin_path, repo_dir)

        self._log(f"\n[bold]Build complete: {success_count} succeeded, {fail_count} failed[/bold]")
        return fail_count == 0

    # -------------------------------------------------------------------------
    # PHASE 5: Post-Build
    # -------------------------------------------------------------------------

    def _phase5_postbuild(self, bin_path: Path, repo_dir: Path) -> None:
        """PHASE 5: Post-build tasks for a successfully built binary."""
        self._log(f"  Post-build: {bin_path.name}")

        # Delete debug binary
        debug_bin = Path(f"{bin_path}.debug")
        if self.delete_debug and debug_bin.exists():
            debug_bin.unlink()
            self._log(f"  Removed: {debug_bin.name}")

        # Save list_smargo
        if self.save_smargo:
            dist_dir = repo_dir / "Distribution"
            if dist_dir.is_dir():
                for smargo in dist_dir.glob("list_smargo*"):
                    import shutil
                    dest_name = f"{bin_path.stem}-list_smargo"
                    dest = self.binaries_dir / dest_name
                    shutil.move(str(smargo), str(dest))
                    self._log(f"  Saved: {dest_name}")

    def _close_log(self, log_path: Path) -> None:
        """Close the build log file and show its path."""
        try:
            self._log(f"Log: {log_path}")
            self._log_file.close()
        except Exception:
            pass

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
        sysroot_inject = ""
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
                    sysroot_inject = f"--sysroot={sysroot}"
                    self._log(f"  Auto-injected: {sysroot_inject}")

        # --- CC_OPTS, CFLAGS, LDFLAGS ---
        # s4 settings = base, toolchain appends, target overrides toolchain
        tc_cc = tcc.cc_opts if tcc.cc_opts else (tc.cc_opts or "")
        cc_opts = f"{self.compiler_opt} {tc_cc}".strip() if tc_cc else (self.compiler_opt or "")
        cflags = tcc.cflags if tcc.cflags else (tc.cflags or "")
        if sysroot_inject and "--sysroot" not in cflags:
            cflags = f"{cflags} {sysroot_inject}".strip()
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
            if stapi_val:
                stapi_key = "STAPI5_LIB" if b.use_stapi5 else "STAPI_LIB"
                stapi_lib = f"{stapi_key}={stapi_val}"
                self._log(f"  {stapi_lib}")

        # --- CONF_DIR ---
        confdir = b.confdir or "/usr/local/etc"

        # --- Binary name ---
        oscam_name = self._generate_binary_name(repo_dir, tcc.name, is_native, b)

        # --- Output path ---
        self.binaries_dir.mkdir(parents=True, exist_ok=True)
        bin_path = self.binaries_dir / oscam_name

        # --- Construct make command ---
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
        for flag in use_flags:
            make_cmd.append(flag)
        for key, val in static_libs.items():
            make_cmd.append(f"{key}={val}")
        if stapi_lib:
            make_cmd.append(stapi_lib)
        if extra_make:
            make_cmd.extend(extra_make.split())

        # --- Log the command ---
        self._log(f"  Output: {bin_path.name}")
        self._log(f"  CROSS: {cross or '(native)'}")
        self._log(f"  CC_OPTS: {cc_opts}")
        if cflags:
            self._log(f"  EXTRA_CFLAGS: {cflags}")
        if ldflags:
            self._log(f"  EXTRA_LDFLAGS: {ldflags}")
        self._log(f"  CONF_DIR: {confdir}")
        self._log(f"  CPUs: {self.max_cpus}")
        self._log("")

        # --- Clean before build ---
        self._log("  Cleaning...")
        self._run(["make", "clean"], cwd=repo_dir)

        # --- Execute make ---
        self._log("  Compiling...")
        started = datetime.now()
        ok = self._run(make_cmd, cwd=repo_dir)
        elapsed = (datetime.now() - started).total_seconds()

        if ok and bin_path.exists():
            size_kb = bin_path.stat().st_size / 1024
            self._log(f"  [green]OK: {bin_path.name} ({size_kb:.0f} KB) in {elapsed:.0f}s[/green]")
            return bin_path
        else:
            self._log(f"  [red]Build FAILED after {elapsed:.0f}s[/red]")
            return None

    def _generate_binary_name(self, repo_dir: Path, tc_name: str, is_native: bool, b) -> str:
        """Generate the output binary name.

        Format: oscam-git<rev>@<commit>-<toolchain>-<backend>-<features>-<linking>
        """
        # Custom name overrides everything
        if b.use_oscamname:
            return b.use_oscamname

        # Revision info
        rev = self._run_output(["git", "rev-list", "--count", "HEAD"], cwd=repo_dir) or "0"
        commit = self._run_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir) or "unknown"

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
        return f"oscam-git{rev}@{commit}-{tc_part}{backend}{feat_str}{linking}"

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
