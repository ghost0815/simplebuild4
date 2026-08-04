"""Toolchain self-build logic — UI-free core for building toolchains from templates.

Used by:
  - ui/screens/toolchains.py  (manual F5 build)
  - core/automation.py        (automated build-toolchains action)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from core.template import Template


def resolve_ctng_dir(settings, paths) -> Path:
    """Return the absolute crosstool-ng install dir from settings or default."""
    raw = (getattr(settings, "ctng_dir", "") or "").strip()
    if not raw:
        return paths.builders_dir / "crosstool-ng"
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = paths.root / p
    return p


def fix_pc_files(install_dir: Path, log=None) -> tuple[int, int]:
    """Fix prefix= paths in .pc files. Returns (total, fixed)."""
    def _log(msg):
        if log:
            log(msg)

    pc_files = list(install_dir.glob("**/pkgconfig/*.pc"))
    _log(f"  Checking {len(pc_files)} .pc files...")
    fixed = 0
    for pc_file in pc_files:
        try:
            content = pc_file.read_text(encoding="utf-8", errors="replace")
            old_prefix = ""
            for line in content.splitlines():
                if line.startswith("prefix="):
                    old_prefix = line.split("=", 1)[1].strip()
                    break
            if not old_prefix:
                continue
            new_prefix = str(pc_file.parent.parent.parent)
            if old_prefix != new_prefix:
                content = content.replace(old_prefix, new_prefix)
                pc_file.write_text(content, encoding="utf-8")
                _log(f"  Fixed {pc_file.name}")
                fixed += 1
        except Exception:
            pass
    return (len(pc_files), fixed)


def fix_la_files(install_dir: Path, log=None) -> tuple[int, int]:
    """Fix .la files: replace =/path with sysroot-relative path. Returns (total, fixed)."""
    def _log(msg):
        if log:
            log(msg)

    la_files = list(install_dir.glob("**/*.la"))
    if not la_files:
        return (0, 0)
    sysroot = ""
    for la in la_files:
        p = la.parent
        while p != install_dir and p != p.parent:
            if (p / "usr" / "lib").is_dir() or (p / "lib").is_dir():
                sysroot = str(p)
                break
            p = p.parent
        if sysroot:
            break
    if not sysroot:
        return (len(la_files), 0)

    fixed = 0
    for la_file in la_files:
        try:
            content = la_file.read_text(encoding="utf-8", errors="replace")
            if "dependency_libs" not in content or "=/" not in content:
                continue
            new_content = content.replace(" =/", f" {sysroot}/")
            if new_content != content:
                la_file.write_text(new_content, encoding="utf-8")
                fixed += 1
        except Exception:
            pass
    if fixed:
        _log(f"  Fixed {fixed} .la files (sysroot={sysroot})")
    return (len(la_files), fixed)


def register_built_toolchain(tpl: Template, paths, log=None) -> None:
    """Add or update the built toolchain in toolchains.local.yaml.

    Auto-detects prefix/sysroot from the built directory; falls back to
    template values. Merges into an existing local entry if one exists.

    Post-build: re-scans the sysroot and **replaces** ``libraries`` with
    whatever ``scan_libraries`` finds. The ``libraries`` dict is the
    authoritative record of what's actually in the sysroot:

    * crosstool-ng → empty dict (compiler + glibc only, no extra libs)
    * OE-SDK → whatever the BSP recipe bundled (OpenSSL, libusb, …)
    * Android NDK → typically empty (Bionic has no .pc-carrying libs)

    Callers that need to preserve the pre-rebuild library set for a
    later re-install step (e.g. the automation update-libraries
    action) must snapshot ``tc.libraries`` **before** calling
    ``build_toolchain`` — after this function returns, the dict is
    overwritten.
    """
    from core.toolchain import (
        Toolchain, ToolchainRegistry, detect_toolchain_props, scan_libraries,
    )

    def _log(msg):
        if log:
            log(msg)

    reg = ToolchainRegistry(paths.toolchains_defaults, paths.toolchains_local, paths.toolchains_dir)
    reg.load()
    local_tcs = reg._load_file(reg.local_path)

    install_dir = paths.toolchains_dir / tpl.name
    det_prefix, det_sysroot = detect_toolchain_props(install_dir)
    # OE-SDK's gcc --print-sysroot returns the build-time placeholder
    # "/not/exist" or an absolute path outside the toolchain dir — both
    # are useless. Only accept detected values that look sane.
    if det_sysroot and (det_sysroot.startswith("/") or "not/exist" in det_sysroot):
        det_sysroot = ""
    prefix = det_prefix or tpl.prefix
    sysroot = det_sysroot or tpl.sysroot
    if det_prefix or det_sysroot:
        _log(f"  [green]Auto-detected:[/green] prefix=[dim]{prefix or '-'}[/dim] sysroot=[dim]{sysroot or '-'}[/dim]")

    # Post-build library scan — reflects actual contents of the fresh sysroot.
    scanned_libs: dict[str, str] = {}
    try:
        infos = scan_libraries(install_dir, sysroot, native=False)
        scanned_libs = {
            info.name: info.version
            for info in infos
            if info.found and info.version
        }
    except Exception as e:
        _log(f"  [yellow]library scan failed: {e}[/yellow]")
    if scanned_libs:
        _log(
            f"  [green]Libraries detected:[/green] "
            + ", ".join(f"{k}={v}" for k, v in sorted(scanned_libs.items()))
        )
    else:
        _log("  [dim]No libraries detected in sysroot[/dim]")

    existing = next((tc for tc in local_tcs if tc.name == tpl.name), None)
    if existing is not None:
        existing.description = tpl.description or existing.description or tpl.name
        existing.arch = tpl.arch or existing.arch
        existing.machines = tpl.machines or existing.machines
        existing.prefix = prefix
        existing.sysroot = sysroot
        existing.cflags = tpl.cflags or existing.cflags
        existing.ldflags = tpl.ldflags or existing.ldflags
        existing.selfbuild = True
        existing.libraries = scanned_libs   # wholesale replace — scan result wins
        _log(f"  [green]Merged into existing local entry (category={existing.category or '(empty → Local)'} kept)[/green]")
    else:
        local_tcs.append(Toolchain(
            name=tpl.name,
            description=tpl.description or tpl.name,
            category="selfbuild",
            arch=tpl.arch,
            machines=tpl.machines,
            prefix=prefix,
            sysroot=sysroot,
            cflags=tpl.cflags,
            ldflags=tpl.ldflags,
            selfbuild=True,
            libraries=scanned_libs,
        ))
    reg.save_local(local_tcs)


def build_toolchain(
    tpl: Template,
    paths,
    settings,
    log: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    """Build a toolchain from a template. Returns True on success.

    Dispatches to the appropriate builder based on tpl.builder:
      - crosstool-ng: ct-ng build pipeline
      - oe-sdk: OE bitbake meta-toolchain pipeline

    Pure and UI-free — suitable for both manual (F5) and automated
    (build-toolchains action) use.
    """
    def _log(msg):
        if log:
            log(msg)

    if tpl.builder == "oe-sdk":
        return _build_oesdk(tpl, paths, settings, log=log, cancel_check=cancel_check)

    if tpl.builder == "android-ndk":
        return _build_android_ndk(tpl, paths, settings, log=log, cancel_check=cancel_check)

    if tpl.builder != "crosstool-ng":
        _log(f"[red]Builder '{tpl.builder}' is not supported[/red]")
        return False

    ctng_dir = resolve_ctng_dir(settings, paths)

    from core.builders.crosstool_ng import CrosstoolNG

    ctng = CrosstoolNG(
        ctng_dir=ctng_dir,
        toolchains_dir=paths.toolchains_dir,
        downloads_dir=paths.downloads_dir,
        max_cpus=settings.max_cpus,
        log=log,
        cancel_check=cancel_check,
    )

    if not ctng.is_installed():
        _log("[bold]Setting up crosstool-ng (one-time)...[/bold]")
        if not ctng.setup():
            _log("[red]crosstool-ng setup failed[/red]")
            return False

    if not ctng.build(tpl):
        _log(f"[red]Build of {tpl.name} failed[/red]")
        return False

    install_dir = paths.toolchains_dir / tpl.name
    if install_dir.exists():
        _log("[bold]Post-build: relocating paths...[/bold]")
        try:
            pc_total, pc_fixed = fix_pc_files(install_dir, log=log)
            if pc_total:
                _log(f"  [green]✓ .pc files: {pc_fixed}/{pc_total} updated[/green]")
        except Exception as e:
            _log(f"  [yellow]fix_pc_files failed: {e}[/yellow]")
        try:
            la_total, la_fixed = fix_la_files(install_dir, log=log)
            if la_total:
                _log(f"  [green]✓ .la files: {la_fixed}/{la_total} updated[/green]")
        except Exception as e:
            _log(f"  [yellow]fix_la_files failed: {e}[/yellow]")

    _log("[bold]Registering toolchain...[/bold]")
    register_built_toolchain(tpl, paths, log=log)
    _log(f"[green]{tpl.name} built and registered[/green]")
    return True


# ---------------------------------------------------------------------------
# OE-SDK builder
# ---------------------------------------------------------------------------

def _build_oesdk(
    tpl: Template,
    paths,
    settings,
    log: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    """Build an OE-SDK toolchain via bitbake meta-toolchain.

    Sequence (derived from s3 build-openembedded-toolchains.sh):
      1. Load OESDK_* config
      2. Ensure repo is cloned + up to date
      3. make init (MACHINE + DISTRO + DISTRO_TYPE=release)
      4. Find build directory
      5. Source OE environment
      6. Generate sdk-extras.conf (with BitBake syntax detection)
      7. bitbake meta-toolchain -R ./sdk-extras.conf
      8. Find + run SDK installer (.sh)
      9. Post-install (bin symlink, strip, relocate, fix .pc/.la)
     10. Auto-detect prefix/sysroot/cflags/ldflags
     11. Register toolchain
    """
    import shlex
    import shutil
    import subprocess

    def _log(msg):
        if log:
            log(msg)

    def _cancelled():
        return cancel_check() if cancel_check else False

    def _run(cmd, cwd=None, timeout=600, stream=False, env=None):
        """Run a command; optionally stream stdout line-by-line to _log."""
        _log(f"[dim]$ {' '.join(cmd) if isinstance(cmd, list) else cmd}[/dim]")
        if stream:
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=env,
            )
            for line in proc.stdout:
                if _cancelled():
                    proc.kill()
                    return None
                _log(line.rstrip())
            proc.wait()
            return proc
        else:
            return subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, env=env,
            )

    # --- Step 1: Load config ---
    from core.template import load_oesdk_config
    cfg_path = tpl.config_path
    if not cfg_path or not cfg_path.exists():
        _log("[red]OE-SDK config file not found[/red]")
        return False
    cfg = load_oesdk_config(cfg_path)
    if not cfg.repo_branch:
        _log("[red]No branch configured — run F6 Customize first[/red]")
        return False
    if not cfg.machine:
        _log("[red]No machine configured[/red]")
        return False

    build_dir = Path(cfg.build_dir)
    if not build_dir.is_absolute():
        build_dir = paths.root / cfg.build_dir
    tc_name = tpl.name
    tc_dir = paths.toolchains_dir / tc_name

    _log(f"[bold]Building OE-SDK toolchain: {tc_name}[/bold]")
    _log(f"  Machine:  {cfg.machine}")
    _log(f"  Distro:   {cfg.distro}")
    _log(f"  Branch:   {cfg.repo_branch}")
    _log(f"  Repo:     {cfg.repo_url}")
    _log(f"  Build:    {build_dir}")
    _log(f"  Install:  {tc_dir}")
    _log("")

    # --- Step 2: Ensure repo ---
    if not (build_dir / ".git").is_dir():
        _log("[bold]Cloning repository...[/bold]")
        build_dir.mkdir(parents=True, exist_ok=True)
        r = _run(["git", "clone", "-b", cfg.repo_branch, cfg.repo_url, str(build_dir)])
        if r is None or r.returncode != 0:
            _log(f"[red]Clone failed: {getattr(r, 'stderr', '') or ''}[/red]")
            return False
        _log("[bold]Running make update...[/bold]")
        r = _run(["make", "update"], cwd=str(build_dir), timeout=600)
        if r is None or r.returncode != 0:
            _log(f"[yellow]make update warning: {getattr(r, 'stderr', '')}[/yellow]")
    else:
        _log("[dim]Repository exists, skipping clone[/dim]")

    if _cancelled():
        return False

    # --- Step 3: make init ---
    _log("[bold]Running make init...[/bold]")
    init_env = {
        **os.environ,
        "MACHINE": cfg.machine,
        "DISTRO": cfg.distro or "openatv",
        "DISTRO_TYPE": "release",
    }
    r = _run(["make", "init"], cwd=str(build_dir), env=init_env, timeout=300)
    if r is None or r.returncode != 0:
        _log(f"[red]make init failed: {getattr(r, 'stderr', '')}[/red]")
        return False

    if _cancelled():
        return False

    # --- Step 4: Find build directory ---
    _log("[dim]Searching for build directory...[/dim]")
    bb_dir = _find_build_dir(build_dir, cfg.machine)
    if not bb_dir:
        _log("[red]Could not find OE build directory (conf/local.conf)[/red]")
        return False
    _log(f"  Build dir: {bb_dir}")

    # --- Step 5: Source environment + Step 6: sdk-extras.conf ---
    # We can't really "source" a bash script from Python. Instead, we
    # construct a shell script that sources the env and runs bitbake.
    _log("[bold]Generating sdk-extras.conf...[/bold]")
    extras_conf = _generate_sdk_extras(bb_dir, cfg.sdk_extras, build_dir)
    _log(f"  {extras_conf}")

    if _cancelled():
        return False

    # --- Step 7: bitbake meta-toolchain ---
    _log("[bold]Running bitbake meta-toolchain (this may take a while)...[/bold]")
    # Construct a shell command that sources the OE env and runs bitbake.
    # This is necessary because bitbake requires the sourced environment.
    source_cmd = _build_source_command(build_dir, bb_dir)
    shell_cmd = (
        f"cd {shlex.quote(str(build_dir))} && "
        f"{source_cmd} && "
        f"export MACHINE={shlex.quote(cfg.machine)} && "
        f"bitbake meta-toolchain -R {shlex.quote(str(extras_conf))}"
    )
    proc = _run(["bash", "-c", shell_cmd], cwd=str(build_dir), stream=True)
    if proc is None or proc.returncode != 0:
        _log("[red]bitbake meta-toolchain failed[/red]")
        return False

    if _cancelled():
        return False

    # --- Step 8: Find SDK installer ---
    _log("[bold]Searching for SDK installer...[/bold]")
    sdk_sh = _find_sdk_installer(bb_dir, cfg.machine)
    if not sdk_sh:
        _log("[red]SDK installer (.sh) not found in deploy/sdk/[/red]")
        return False
    _log(f"  Found: {sdk_sh.name}")

    # --- Step 9: Install SDK ---
    _log(f"[bold]Installing SDK to {tc_dir.name}...[/bold]")
    if tc_dir.exists():
        _log("  Removing existing toolchain directory...")
        shutil.rmtree(tc_dir, ignore_errors=True)
    r = _run([str(sdk_sh), "-d", str(tc_dir), "-y"], timeout=600)
    if r is None:
        _log("[red]SDK install failed (cancelled or timed out)[/red]")
        return False
    if r.returncode != 0:
        # Some SDKs fail in post-relocate scripts (e.g. meson-setup.py
        # missing OECORE_MESON_HOST_SYSTEM) but the actual SDK files are
        # installed fine. Continue if sysroots exist.
        if (tc_dir / "sysroots").is_dir():
            stderr = (getattr(r, 'stderr', '') or '').strip()
            _log(f"[yellow]SDK installer returned non-zero but sysroots exist — continuing[/yellow]")
            if stderr:
                _log(f"[dim]{stderr[-300:]}[/dim]")
        else:
            _log(f"[red]SDK install failed: {getattr(r, 'stderr', '')}[/red]")
            return False

    if _cancelled():
        return False

    # --- Step 10: Post-install ---
    _log("[bold]Post-install...[/bold]")

    # 10a: Parse environment-setup-* for metadata
    env_script = _find_env_setup(tc_dir)
    if env_script:
        _log(f"  Env script: {env_script.name}")
        env_data = _parse_env_setup(env_script)

        # Update template with auto-detected values
        if env_data.get("prefix"):
            tpl.prefix = env_data["prefix"]
        if env_data.get("sysroot"):
            tpl.sysroot = env_data["sysroot"]
        if env_data.get("cflags"):
            tpl.cflags = env_data["cflags"]
        if env_data.get("ldflags"):
            tpl.ldflags = env_data["ldflags"]

        _log(f"  prefix:  {tpl.prefix}")
        _log(f"  sysroot: {tpl.sysroot}")
        _log(f"  cflags:  {tpl.cflags}")
        _log(f"  ldflags: {tpl.ldflags}")
    else:
        _log("  [yellow]No environment-setup-* script found[/yellow]")

    # 10b: bin/ symlink
    _oesdk_create_bin_symlink(tc_dir, tpl.prefix, log=log)

    # 10c: Copy relocate_sdk.py
    _oesdk_copy_relocate(build_dir, tc_dir, log=log)

    # 10d: Strip SDK (remove unnecessary files)
    _oesdk_strip(tc_dir, log=log)

    # 10e: Fix .pc and .la files
    try:
        pc_total, pc_fixed = fix_pc_files(tc_dir, log=log)
        if pc_total:
            _log(f"  [green]✓ .pc files: {pc_fixed}/{pc_total} updated[/green]")
    except Exception as e:
        _log(f"  [yellow]fix_pc_files: {e}[/yellow]")
    try:
        la_total, la_fixed = fix_la_files(tc_dir, log=log)
        if la_total:
            _log(f"  [green]✓ .la files: {la_fixed}/{la_total} updated[/green]")
    except Exception as e:
        _log(f"  [yellow]fix_la_files: {e}[/yellow]")

    # 10f: Relocate SDK
    _oesdk_relocate(tc_dir, log=log)

    # --- Step 11: Register ---
    _log("[bold]Registering toolchain...[/bold]")
    register_built_toolchain(tpl, paths, log=log)
    _log(f"[green]{tc_name} built and registered[/green]")
    return True


# ---------------------------------------------------------------------------
# OE-SDK helpers
# ---------------------------------------------------------------------------

def _find_build_dir(repo_dir: Path, machine: str) -> Path | None:
    """Find the OE build directory (where conf/local.conf lives).

    Tries the three known patterns from oe-alliance, opendreambox, openpli.
    """
    candidates = [
        # oe-alliance: builds/<distro>/release/<machine>
        *sorted(repo_dir.glob(f"builds/*/release/{machine}")),
        # opendreambox / generic: build/<machine>
        repo_dir / "build" / machine,
        # fallback: build/
        repo_dir / "build",
    ]
    for d in candidates:
        if d.is_dir() and (d / "conf").is_dir():
            return d
    # Last resort: search for conf/local.conf
    for conf in repo_dir.rglob("conf/local.conf"):
        # Skip sstate/downloads/toolchain paths
        rel = str(conf.relative_to(repo_dir))
        if any(skip in rel for skip in ("sstate", "downloads", "toolchains")):
            continue
        return conf.parent.parent
    return None


def _build_source_command(repo_dir: Path, bb_dir: Path) -> str:
    """Build the bash source command for the OE environment.

    Tries three variants (same order as s3):
      1. oe-init-build-env + env.source
      2. bitbake.env
      3. oe-init-build-env only
    """
    import shlex
    oe_init = repo_dir / "openembedded-core" / "oe-init-build-env"
    env_source = bb_dir / "env.source"
    bitbake_env = repo_dir / "bitbake.env"

    if env_source.exists() and oe_init.exists():
        return (
            f"source {shlex.quote(str(oe_init))} {shlex.quote(str(bb_dir))} > /dev/null 2>&1 && "
            f"source {shlex.quote(str(env_source))} > /dev/null 2>&1"
        )
    if bitbake_env.exists():
        return (
            f"source {shlex.quote(str(bitbake_env))} > /dev/null 2>&1 && "
            f"cd {shlex.quote(str(bb_dir))}"
        )
    if oe_init.exists():
        return f"source {shlex.quote(str(oe_init))} {shlex.quote(str(bb_dir))} > /dev/null 2>&1"
    # Fallback: just add to PATH
    return (
        f"export PATH={shlex.quote(str(repo_dir / 'openembedded-core' / 'scripts'))}:"
        f"{shlex.quote(str(repo_dir / 'bitbake' / 'bin'))}:$PATH"
    )


def _generate_sdk_extras(bb_dir: Path, sdk_extras: str, repo_dir: Path) -> Path:
    """Generate sdk-extras.conf with BitBake-version-aware syntax.

    BitBake >= 2.0 uses ':' as override separator (e.g. :append),
    older versions use '_' (e.g. _append).
    """
    # Detect BitBake syntax
    import shlex
    sep = ":"
    try:
        import subprocess
        source_cmd = _build_source_command(repo_dir, bb_dir)
        r = subprocess.run(
            ["bash", "-c", f"{source_cmd} && bitbake --version"],
            capture_output=True, text=True, timeout=30, cwd=str(repo_dir),
        )
        version_str = r.stdout.strip() if r.returncode == 0 else ""
        # Extract major version number
        import re
        m = re.search(r"(\d+)\.", version_str)
        if m and int(m.group(1)) < 2:
            sep = "_"
    except Exception:
        pass

    lines = [f'TOOLCHAIN_TARGET_TASK{sep}append = " {sdk_extras}"']
    for pkg in sdk_extras.split():
        if pkg.endswith("-staticdev"):
            base = pkg[:-len("-staticdev")]
            lines.append(f'DISABLE_STATIC{sep}pn-{base} = ""')

    # Workaround: autoconf 2.73's .w man-page wrapper scripts use
    # exec "${PERL}" which breaks when PERL="/usr/bin/env perl"
    # (shell treats the space-containing string as a single path).
    # Override with a direct path so the wrapper can exec it.
    lines.append(f'EXTRA_OEMAKE{sep}append{sep}pn-nativesdk-autoconf = " PERL=/usr/bin/perl"')

    conf_path = bb_dir / "sdk-extras.conf"
    conf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return conf_path


def _find_sdk_installer(bb_dir: Path, machine: str) -> Path | None:
    """Find the SDK installer .sh in the deploy/sdk/ directory.

    Checks known tmp*/deploy/sdk/ locations directly first (fast), and
    only falls back to rglob if nothing is found there (slow, last resort).
    """
    # OE uses tmp-<libc>/deploy/sdk/ or tmp/deploy/sdk/ — check directly
    sdk_dirs = sorted(bb_dir.glob("tmp*/deploy/sdk/")) + [bb_dir / "tmp" / "deploy" / "sdk"]
    for pattern in (f"*-{machine}-*.sh", "*.sh"):
        for sdk_dir in sdk_dirs:
            candidates = list(sdk_dir.glob(pattern))
            if candidates:
                return max(candidates, key=lambda p: p.stat().st_mtime)
    # Fallback: full recursive search (slow, for non-standard layouts)
    for pattern in (f"*-{machine}-*.sh", "*.sh"):
        candidates = list(bb_dir.rglob(f"deploy/sdk/{pattern}"))
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime)
    return None


def _find_env_setup(tc_dir: Path) -> Path | None:
    """Find the environment-setup-* script in the installed SDK."""
    candidates = list(tc_dir.glob("environment-setup-*"))
    # Filter out multilib/lib32 variants
    candidates = [c for c in candidates if "lib32" not in c.name and "multilib" not in c.name]
    return candidates[0] if candidates else None


def _parse_env_setup(env_script: Path) -> dict[str, str]:
    """Extract toolchain metadata from the OE SDK environment-setup-* script."""
    result = {
        "prefix": "",
        "sysroot": "",
        "cflags": "",
        "ldflags": "",
    }
    content = env_script.read_text(encoding="utf-8", errors="replace")
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("export TARGET_PREFIX="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            # TARGET_PREFIX includes trailing dash (arm-oe-linux-gnueabi-)
            result["prefix"] = val if val.endswith("-") else val + "-"
        elif line.startswith("export SDKTARGETSYSROOT="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            # Make relative to tc_dir: .../sysroots/cortexa15hf-...-gnueabi
            if "/sysroots/" in val:
                result["sysroot"] = "sysroots/" + val.split("/sysroots/", 1)[1]
        elif line.startswith("export OECORE_TUNE_CCARGS="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'").strip()
            result["cflags"] = val
    # Detect dynamic linker for ldflags
    if result["sysroot"]:
        tc_dir = env_script.parent
        sysroot_abs = tc_dir / result["sysroot"]
        for ld in sysroot_abs.glob("lib/ld-*.so*"):
            if ld.is_file() and not ld.name.endswith(".p"):
                result["ldflags"] = f"-Wl,--dynamic-linker=/lib/{ld.name}"
                break
    return result


def _oesdk_create_bin_symlink(tc_dir: Path, prefix: str, log=None) -> None:
    """Create bin/ → sysroots/x86_64-oesdk-linux/usr/bin/<target> symlink."""
    if not prefix:
        return
    target_prefix = prefix.rstrip("-")
    host_bin = tc_dir / "sysroots" / "x86_64-oesdk-linux" / "usr" / "bin" / target_prefix
    if not host_bin.is_dir():
        # Try finding it
        for d in (tc_dir / "sysroots").glob("x86_64-*/usr/bin/*"):
            if d.is_dir() and any(f.name.endswith("-gcc") for f in d.iterdir()):
                host_bin = d
                break
    bin_link = tc_dir / "bin"
    if host_bin.is_dir():
        try:
            if bin_link.is_symlink() or bin_link.exists():
                bin_link.unlink()
            bin_link.symlink_to(host_bin.relative_to(tc_dir))
            if log:
                log(f"  bin/ → {host_bin.relative_to(tc_dir)}")
        except Exception as e:
            if log:
                log(f"  [yellow]bin symlink failed: {e}[/yellow]")


def _oesdk_copy_relocate(repo_dir: Path, tc_dir: Path, log=None) -> None:
    """Copy relocate_sdk.py from the OE build environment into the SDK."""
    import shutil
    for rpy in repo_dir.rglob("relocate_sdk.py"):
        try:
            shutil.copy2(rpy, tc_dir / "relocate_sdk.py")
            if log:
                log(f"  Copied relocate_sdk.py")
            return
        except Exception:
            pass


def _oesdk_strip(tc_dir: Path, log=None) -> None:
    """Strip unnecessary files from the SDK to reduce size (~50%).

    Removes locale, qemu, python, cmake, autoconf, automake, libtool,
    gettext, gdb, bison, perl, ssl certs, non-cross-compiler host binaries.
    Same list as s3 build-openembedded-toolchains.sh.
    """
    import shutil

    hsys = tc_dir / "sysroots"
    # Find the host sysroot (x86_64-*)
    host_sysroot = None
    for d in hsys.iterdir() if hsys.is_dir() else []:
        if d.is_dir() and d.name.startswith("x86_64"):
            host_sysroot = d
            break
    if not host_sysroot:
        return

    # Directories to remove
    strip_dirs = [
        "usr/lib/locale", "usr/share/qemu", "usr/share/cmake",
        "usr/share/autoconf", "usr/share/libtool", "usr/share/gettext",
        "usr/share/mime", "usr/share/gdb", "usr/share/bison",
        "usr/share/info", "usr/share/man", "usr/share/doc",
        "etc/ssl",
    ]
    # Glob patterns for dirs
    strip_globs = [
        "usr/lib/python*", "usr/share/automake*", "usr/share/aclocal*",
        "usr/lib/perl*",
    ]

    removed = 0
    for d in strip_dirs:
        target = host_sysroot / d
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed += 1
    for pattern in strip_globs:
        for target in host_sysroot.glob(pattern):
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                removed += 1

    # Remove non-cross-compiler host binaries
    host_bin = host_sysroot / "usr" / "bin"
    if host_bin.is_dir():
        for f in host_bin.iterdir():
            if f.is_file() and "-oe-linux-" not in f.name:
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass
            elif f.is_symlink() and "-oe-linux-" not in f.name:
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass

    if log and removed:
        # Calculate stripped size
        try:
            import subprocess
            r = subprocess.run(
                ["du", "-sh", str(tc_dir)], capture_output=True, text=True, timeout=30,
            )
            size = r.stdout.split()[0] if r.returncode == 0 else "?"
        except Exception:
            size = "?"
        log(f"  Stripped SDK ({removed} items removed, size: {size})")


def _oesdk_relocate(tc_dir: Path, log=None) -> None:
    """Run relocate_sdk.py to fix hardcoded paths in the SDK.

    The SDK installer hardcodes the install path; if we installed to a
    different location, all embedded paths need rewriting.
    """
    rpy = tc_dir / "relocate_sdk.py"
    if not rpy.exists():
        if log:
            log("  [dim]relocate_sdk.py not found, skipping relocation[/dim]")
        return
    import subprocess
    try:
        # Find the native sysroot (the host/build tools sysroot)
        native_sysroot = None
        sysroots = tc_dir / "sysroots"
        for d in sysroots.iterdir() if sysroots.is_dir() else []:
            if d.is_dir() and d.name.startswith("x86_64"):
                native_sysroot = d
                break
        if not native_sysroot:
            return
        # The relocate script needs the SDK install path
        r = subprocess.run(
            ["python3", str(rpy), str(tc_dir), str(native_sysroot)],
            capture_output=True, text=True, cwd=str(tc_dir), timeout=120,
        )
        if r.returncode == 0:
            if log:
                log("  [green]SDK relocated[/green]")
        else:
            if log:
                log(f"  [yellow]relocate_sdk.py: {(r.stderr or r.stdout or '').strip()[:200]}[/yellow]")
    except Exception as e:
        if log:
            log(f"  [yellow]relocate_sdk.py failed: {e}[/yellow]")


# ---------------------------------------------------------------------------
# Android NDK builder
# ---------------------------------------------------------------------------

def _andk_generate_cflags(ndk_dir: Path, arch: str, api: str) -> str:
    """Generate cflags dynamically from NDK metadata + clang probing.

    Reads meta/abis.json from the NDK to derive architecture-specific
    flags. Common Android flags (-DANDROID, -D__ANDROID_API__, -fpic,
    safety flags) are always included. Arch-specific flags (e.g.
    -march=armv7-a for ARM32, -mstackrealign for x86) are added based
    on the ABI info.
    """
    import json

    # Common flags for all Android architectures
    common = (
        f"-U__ANDROID_API__ -DANDROID -D__ANDROID_API__={api} "
        "-fpic -fno-addrsig -funwind-tables -fstack-protector-strong "
        "-no-canonical-prefixes"
    )

    # Try to read ABI info from NDK metadata
    arch_flags = ""
    abi_map = {"arm": "armeabi-v7a", "aarch64": "arm64-v8a",
               "x86": "x86", "x86_64": "x86_64"}
    abi_name = abi_map.get(arch, "")

    abis_file = ndk_dir / "meta" / "abis.json"
    if abis_file.exists() and abi_name:
        try:
            abis = json.loads(abis_file.read_text(encoding="utf-8"))
            abi_info = abis.get(abi_name, {})
            proc = abi_info.get("proc", "")  # e.g. "armv7-a", "aarch64", "i686"

            if arch == "arm":
                arch_flags = (
                    f"-march={proc} -mtune=cortex-a9 "
                    "-mfloat-abi=softfp -mfpu=vfpv3-d16"
                )
            elif arch == "x86":
                arch_flags = "-mstackrealign"
            # aarch64 and x86_64: clang handles everything via target triple
        except Exception:
            pass

    parts = [p for p in (arch_flags, common) if p]
    return " ".join(parts)


def _build_android_ndk(
    tpl: Template,
    paths,
    settings,
    log: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    """Build an Android NDK toolchain by downloading + extracting + cleaning up.

    No compilation needed — the NDK ships a prebuilt Clang toolchain.
    """
    import shutil
    import subprocess

    def _log(msg):
        if log:
            log(msg)

    def _cancelled():
        return cancel_check() if cancel_check else False

    # --- Step 1: Load config ---
    from core.template import load_andk_config, ANDK_ARCH_MAP
    cfg_path = tpl.config_path
    if not cfg_path or not cfg_path.exists():
        _log("[red]Android NDK config file not found[/red]")
        return False
    cfg = load_andk_config(cfg_path)
    if not cfg.api:
        _log("[red]No API level configured — run F6 Customize first[/red]")
        return False
    if not cfg.ndk_version:
        _log("[red]No NDK version configured — run F6 Customize first[/red]")
        return False

    arch_info = ANDK_ARCH_MAP.get(cfg.arch, {})
    if not arch_info:
        _log(f"[red]Unknown architecture: {cfg.arch}[/red]")
        return False

    tc_name = tpl.name
    tc_dir = paths.toolchains_dir / tc_name
    downloads_dir = paths.downloads_dir
    downloads_dir.mkdir(parents=True, exist_ok=True)

    _log(f"[bold]Building Android NDK toolchain: {tc_name}[/bold]")
    _log(f"  NDK:       {cfg.ndk_version}")
    _log(f"  API:       {cfg.api}")
    _log(f"  Arch:      {cfg.arch} ({arch_info.get('abi', '')})")
    _log(f"  Host:      {arch_info.get('host', '')}")
    _log(f"  Install:   {tc_dir}")
    _log("")

    # --- Step 2: Download NDK ZIP ---
    ndk_filename = f"android-ndk-{cfg.ndk_version}-linux.zip"
    ndk_zip = downloads_dir / ndk_filename
    download_url = cfg.download_url

    if ndk_zip.exists():
        _log(f"[dim]Using cached {ndk_filename}[/dim]")
    else:
        _log(f"[bold]Downloading {ndk_filename}...[/bold]")
        _log(f"  URL: {download_url}")
        try:
            r = subprocess.run(
                ["wget", "-q", "-O", str(ndk_zip), download_url],
                capture_output=True, text=True, timeout=1800,
            )
            if r.returncode != 0:
                r = subprocess.run(
                    ["curl", "-sL", "-o", str(ndk_zip), download_url],
                    capture_output=True, text=True, timeout=1800,
                )
            if r.returncode != 0:
                _log("[red]Download failed[/red]")
                ndk_zip.unlink(missing_ok=True)
                return False
        except Exception as e:
            _log(f"[red]Download failed: {e}[/red]")
            ndk_zip.unlink(missing_ok=True)
            return False
        _log(f"  Downloaded: {ndk_zip.stat().st_size / (1024*1024):.0f} MB")

    if _cancelled():
        return False

    # --- Step 3: Extract NDK (persistent under settings.andk_dir or data/builders/android-ndk/) ---
    _andk_base = (getattr(settings, "andk_dir", "") or "").strip()
    if _andk_base:
        builders_ndk_dir = Path(_andk_base) if Path(_andk_base).is_absolute() else paths.root / _andk_base
    else:
        builders_ndk_dir = paths.builders_dir / "android-ndk"
    builders_ndk_dir.mkdir(parents=True, exist_ok=True)
    ndk_extract_name = f"android-ndk-{cfg.ndk_version}"
    ndk_dir = builders_ndk_dir / ndk_extract_name

    if ndk_dir.is_dir():
        _log(f"[dim]Using existing NDK at {ndk_dir.relative_to(paths.root)}[/dim]")
    else:
        _log("[bold]Extracting NDK...[/bold]")
        r = subprocess.run(
            ["unzip", "-o", "-q", "-d", str(builders_ndk_dir), str(ndk_zip)],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            _log(f"[red]Unzip failed: {r.stderr}[/red]")
            return False
        # Find the extracted directory (may differ slightly from expected name)
        if not ndk_dir.is_dir():
            for d in builders_ndk_dir.iterdir():
                if d.is_dir() and d.name.startswith("android-ndk"):
                    ndk_dir = d
                    break
        if not ndk_dir.is_dir():
            _log("[red]Could not find extracted NDK directory[/red]")
            return False
        _log(f"  Extracted to {ndk_dir.relative_to(paths.root)}")

    if _cancelled():
        return False

    # --- Step 4: Copy prebuilt toolchain ---
    prebuilt = ndk_dir / "toolchains" / "llvm" / "prebuilt" / "linux-x86_64"
    if not prebuilt.is_dir():
        _log(f"[red]Prebuilt toolchain not found at toolchains/llvm/prebuilt/linux-x86_64/[/red]")
        return False

    _log(f"[bold]Copying prebuilt toolchain...[/bold]")
    if tc_dir.exists():
        _log("  Removing existing toolchain directory...")
        shutil.rmtree(tc_dir, ignore_errors=True)
    shutil.copytree(prebuilt, tc_dir, symlinks=True)

    if _cancelled():
        return False

    # --- Step 5: Cleanup ---
    _log("[bold]Cleaning up (removing unused archs/APIs)...[/bold]")
    _andk_cleanup(tc_dir, cfg.arch, cfg.api, log=log)

    # --- Step 6: Create symlinks ---
    _log("[bold]Creating compiler symlinks...[/bold]")
    _andk_create_symlinks(tc_dir, cfg, log=log)

    # --- Step 7: Generate cflags dynamically from NDK metadata ---
    cflags = _andk_generate_cflags(ndk_dir, cfg.arch, cfg.api)
    tpl.cflags = cflags
    tpl.prefix = arch_info.get("prefix", "")
    tpl.sysroot = "sysroot"
    _log(f"  Cflags: {cflags}")

    # --- Step 8: Fix .pc and .la files ---
    try:
        pc_total, pc_fixed = fix_pc_files(tc_dir, log=log)
        if pc_total:
            _log(f"  [green]✓ .pc files: {pc_fixed}/{pc_total} updated[/green]")
    except Exception as e:
        _log(f"  [yellow]fix_pc_files: {e}[/yellow]")
    try:
        la_total, la_fixed = fix_la_files(tc_dir, log=log)
        if la_total:
            _log(f"  [green]✓ .la files: {la_fixed}/{la_total} updated[/green]")
    except Exception as e:
        _log(f"  [yellow]fix_la_files: {e}[/yellow]")

    # --- Step 8b: Ensure builtins are findable via target-triple path ---
    # NDK r29+ (Clang 21+) looks for builtins at lib/clang/<ver>/lib/<triple>/
    # but they live in lib/clang/<ver>/lib/linux/. Create a symlink so both work.
    _andk_link_builtins(tc_dir, cfg, log=log)

    # --- Step 9: Register ---
    _log("[bold]Registering toolchain...[/bold]")
    register_built_toolchain(tpl, paths, log=log)

    # Android-specific: set make_vars so oscam's Makefile doesn't try
    # to link -lrt -lpthread (Bionic has these built-in).
    from core.toolchain import ToolchainRegistry
    reg = ToolchainRegistry(paths.toolchains_defaults, paths.toolchains_local, paths.toolchains_dir)
    local_tcs = reg._load_file(reg.local_path)
    for tc in local_tcs:
        if tc.name == tpl.name:
            tc.make_vars = "LIB_RT= LIB_PTHREAD="
            break
    reg.save_local(local_tcs)

    # Size
    try:
        r = subprocess.run(
            ["du", "-sh", str(tc_dir)], capture_output=True, text=True, timeout=30,
        )
        size = r.stdout.split()[0] if r.returncode == 0 else "?"
    except Exception:
        size = "?"
    _log(f"[green]{tc_name} built and registered (size: {size})[/green]")
    return True


def _andk_cleanup(tc_dir: Path, keep_arch: str, keep_api: str, log=None) -> None:
    """Remove unused architectures and API levels from the NDK toolchain.

    Targeted deletion instead of broad find-and-delete to avoid
    accidentally removing files for the active architecture (e.g.
    "arm" is a substring of "aarch64").
    """
    import shutil

    # --- Remove other architecture sysroots ---
    # The sysroot/usr/lib/ directory has per-triple subdirs
    sysroot_lib = tc_dir / "sysroot" / "usr" / "lib"
    if sysroot_lib.is_dir():
        from core.template import ANDK_ARCH_MAP
        keep_triple = ANDK_ARCH_MAP.get(keep_arch, {}).get("host", "")
        for d in list(sysroot_lib.iterdir()):
            if d.is_dir() and d.name != keep_triple:
                shutil.rmtree(d, ignore_errors=True)

    # --- Remove other architecture binaries from bin/ ---
    bin_dir = tc_dir / "bin"
    if bin_dir.is_dir():
        from core.template import ANDK_ARCH_MAP
        keep_prefixes = set()
        keep_info = ANDK_ARCH_MAP.get(keep_arch, {})
        if keep_info:
            keep_prefixes.add(keep_info.get("host", ""))
            keep_prefixes.add(keep_info.get("clang_prefix", ""))
        # Prefixes of other architectures to remove
        remove_prefixes = set()
        for arch, info in ANDK_ARCH_MAP.items():
            if arch != keep_arch:
                remove_prefixes.add(info["host"])
                remove_prefixes.add(info["clang_prefix"])
        for f in list(bin_dir.iterdir()):
            name = f.name
            # Keep generic tools (clang, llvm-*, ld.lld, etc.)
            if not any(name.startswith(p) for p in remove_prefixes):
                continue
            # Don't delete if it matches the keep arch
            if any(name.startswith(p) for p in keep_prefixes):
                continue
            try:
                if f.is_dir():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    f.unlink()
            except Exception:
                pass

    # --- Remove API-level-specific binaries for other APIs ---
    # bin/ contains clang wrappers named like armv7a-linux-androideabi21-clang
    if bin_dir.is_dir():
        for f in list(bin_dir.iterdir()):
            name = f.name
            # Match pattern: {prefix}{api}-clang or {prefix}{api}-clang++
            for api_num in range(21, 36):
                if str(api_num) == keep_api:
                    continue
                if f"{api_num}-clang" in name:
                    try:
                        f.unlink()
                    except Exception:
                        pass
                    break

    # --- Remove unnecessary top-level directories ---
    for d in ("include", "libexec", "share", "musl", "prebuilt_include"):
        target = tc_dir / d
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    # --- Remove host/musl/windows/baremetal libs from lib/ ---
    # Keep only lib/clang/<ver>/lib/linux/ (Android runtime libs)
    # and lib/clang/<ver>/include/ (builtins headers)
    lib_dir = tc_dir / "lib"
    if lib_dir.is_dir():
        # Remove non-Android lib subdirs (host toolchain libs)
        for d in list(lib_dir.iterdir()):
            if d.is_dir() and d.name != "clang":
                shutil.rmtree(d, ignore_errors=True)
        # Inside lib/clang/<ver>/lib/, keep only "linux" (Android)
        for clang_ver in lib_dir.glob("clang/*/lib"):
            for d in list(clang_ver.iterdir()):
                if d.is_dir() and d.name != "linux":
                    shutil.rmtree(d, ignore_errors=True)
            # Inside lib/clang/<ver>/lib/linux/, keep only the active arch
            _linux_arch_map = {"arm": "arm", "aarch64": "aarch64",
                               "x86": "i386", "x86_64": "x86_64"}
            keep_linux_dir = _linux_arch_map.get(keep_arch, keep_arch)
            linux_dir = clang_ver / "linux"
            if linux_dir.is_dir():
                # Remove subdirs for other archs
                for d in list(linux_dir.iterdir()):
                    if d.is_dir() and d.name != keep_linux_dir:
                        shutil.rmtree(d, ignore_errors=True)
                # Remove per-arch runtime libs (asan, tsan, ubsan, etc.)
                # that sit as flat files named *-{arch}-android.*
                # Runtime lib filenames use arch-android (e.g. arm-android,
                # aarch64-android) — NOT the full host triple.
                _arch_to_tag = {
                    "arm": "arm-android",
                    "aarch64": "aarch64-android",
                    "x86": "i686-android",
                    "x86_64": "x86_64-android",
                }
                _all_tags = set(_arch_to_tag.values()) | {"riscv64-android"}
                _keep_tag = _arch_to_tag.get(keep_arch, "")
                _remove_tags = _all_tags - {_keep_tag} if _keep_tag else set()
                # Remove files for other archs + sanitizer/fuzzer/profiler
                # libs we don't need for oscam. Keep only builtins.
                _keep_prefixes = ("libclang_rt.builtins-",)
                for f in list(linux_dir.iterdir()):
                    if f.is_dir():
                        continue
                    name = f.name
                    # Remove other arch files
                    if any(tag in name for tag in _remove_tags):
                        try:
                            f.unlink()
                        except Exception:
                            pass
                        continue
                    # Remove sanitizer/fuzzer/profiler libs for our arch too
                    if name.startswith("libclang_rt.") or name.startswith("liborc_rt"):
                        if not any(name.startswith(p) for p in _keep_prefixes):
                            try:
                                f.unlink()
                            except Exception:
                                pass

    # --- Remove python/cmake/test artifacts ---
    import subprocess
    for pattern in ("python3", "cmake", "__pycache__", "2to3", ".py"):
        try:
            subprocess.run(
                ["find", str(tc_dir), "-name", f"*{pattern}*",
                 "!", "-path", "*/sysroot/*",
                 "-exec", "rm", "-rf", "{}", ";"],
                capture_output=True, text=True, timeout=60,
            )
        except Exception:
            pass

    # --- Remove unnecessary binaries from bin/ ---
    # Keep only essential binaries — clang itself, linker, and the
    # llvm tools needed for cross-compilation. Remove dev tools like
    # clang-tidy (50M), clangd (43M), clang-check (71M), etc.
    _KEEP_BIN_NAMES = {
        "clang", "clang++", "clang-cpp",
        "ld", "ld.lld", "lld",
        "llvm-ar", "llvm-ranlib", "llvm-strip", "llvm-nm",
        "llvm-objdump", "llvm-readelf", "llvm-objcopy",
        "llvm-strings", "llvm-size", "llvm-addr2line",
    }
    if bin_dir.is_dir():
        from core.template import ANDK_ARCH_MAP
        keep_info = ANDK_ARCH_MAP.get(keep_arch, {})
        keep_host = keep_info.get("host", "")
        keep_clang = keep_info.get("clang_prefix", "")
        for f in list(bin_dir.iterdir()):
            name = f.name
            # Keep: our arch's target binaries (symlinks + clang wrappers)
            if keep_host and name.startswith(keep_host):
                continue
            if keep_clang and name.startswith(keep_clang):
                continue
            # Keep: clang main binary + version-suffixed (clang-18)
            # but NOT clang-tidy, clang-check, clang-scan-deps, clangd, etc.
            if name == "clang" or name == "clang++" or name == "clang-cpp":
                continue
            if name.startswith("clang-") and name.split("-", 1)[1].isdigit():
                # clang-18 (version binary)
                continue
            # Keep: essential tools by exact name
            if name in _KEEP_BIN_NAMES:
                continue
            # Remove everything else
            try:
                if f.is_dir():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    f.unlink()
            except Exception:
                pass

    if log:
        try:
            r = subprocess.run(
                ["du", "-sh", str(tc_dir)], capture_output=True, text=True, timeout=30,
            )
            size = r.stdout.split()[0] if r.returncode == 0 else "?"
        except Exception:
            size = "?"
        log(f"  Cleaned up (size after cleanup: {size})")


def _andk_link_builtins(tc_dir: Path, cfg, log=None) -> None:
    """Ensure libclang_rt.builtins is findable via the target-triple path.

    NDK r29+ (Clang 21+) may look for builtins at:
      lib/clang/<ver>/lib/<clang_prefix><api>-unknown-linux-android<api>/
    but they actually live at:
      lib/clang/<ver>/lib/linux/libclang_rt.builtins-<arch>-android.a

    Create a triple-named directory with a symlink to the actual file.
    """
    _arch_to_rt = {"arm": "arm", "aarch64": "aarch64", "x86": "i686", "x86_64": "x86_64"}
    rt_arch = _arch_to_rt.get(cfg.arch, cfg.arch)

    for clang_lib in tc_dir.glob("lib/clang/*/lib"):
        # Find the builtins file in linux/
        builtins = clang_lib / "linux" / f"libclang_rt.builtins-{rt_arch}-android.a"
        if not builtins.exists():
            continue
        # Clang may search with various triple formats; create dirs for common ones
        triple_patterns = [
            f"{cfg.clang_prefix}{cfg.api}-unknown-linux-android{cfg.api}",
            f"{cfg.host}{cfg.api}",
        ]
        for triple in triple_patterns:
            triple_dir = clang_lib / triple
            if not triple_dir.exists():
                triple_dir.mkdir(parents=True, exist_ok=True)
            link = triple_dir / builtins.name
            if not link.exists():
                try:
                    # Relative symlink: ../linux/libclang_rt.builtins-arm-android.a
                    link.symlink_to(Path("..") / "linux" / builtins.name)
                    if log:
                        log(f"  {triple}/{builtins.name} → linux/{builtins.name}")
                except Exception as e:
                    if log:
                        log(f"  [yellow]builtins symlink failed: {e}[/yellow]")


def _andk_create_symlinks(tc_dir: Path, cfg, log=None) -> None:
    """Create GCC-compatible symlinks to Clang binaries.

    Android NDK uses Clang but oscam's build system expects gcc/ar/ranlib/strip
    named as {host}-gcc, {host}-ar, etc.
    """
    bin_dir = tc_dir / "bin"
    if not bin_dir.is_dir():
        return

    host = cfg.host
    clang_prefix = cfg.clang_prefix
    api = cfg.api

    symlinks = {
        # GCC-compatible names (oscam Makefile uses CROSS=...gcc)
        f"{host}-gcc":     f"{clang_prefix}{api}-clang",
        f"{host}-g++":     f"{clang_prefix}{api}-clang++",
        # Clang names without API suffix (OpenSSL Configure android-* expects these)
        f"{host}-clang":   f"{clang_prefix}{api}-clang",
        f"{host}-clang++": f"{clang_prefix}{api}-clang++",
        # LLVM tools
        f"{host}-ar":      "llvm-ar",
        f"{host}-ranlib":  "llvm-ranlib",
        f"{host}-strip":   "llvm-strip",
        f"{host}-nm":      "llvm-nm",
        f"{host}-objdump": "llvm-objdump",
        f"{host}-readelf": "llvm-readelf",
    }

    for link_name, target_name in symlinks.items():
        link_path = bin_dir / link_name
        target_path = bin_dir / target_name
        if target_path.exists() or target_path.is_symlink():
            try:
                if link_path.exists() or link_path.is_symlink():
                    link_path.unlink()
                link_path.symlink_to(target_name)
                if log:
                    log(f"  {link_name} → {target_name}")
            except Exception as e:
                if log:
                    log(f"  [yellow]{link_name} → {target_name} failed: {e}[/yellow]")
        else:
            if log:
                log(f"  [dim]{target_name} not found, skipping {link_name}[/dim]")
