"""Library builder — downloads, configures, compiles and installs libraries into toolchain sysroot."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import yaml


def sort_by_dependencies(
    actions: list[tuple[str, str]],
    libraries_yaml: Path,
) -> list[tuple[str, str]]:
    """Sort build actions respecting library dependencies (topological sort).

    Args:
        actions: List of (lib_name, action_str) tuples
        libraries_yaml: Path to libraries.yaml

    Returns:
        Same list reordered so dependencies are built first.
    """
    # Load dependency graph
    dep_graph: dict[str, list[str]] = {}
    if libraries_yaml.exists():
        try:
            data = yaml.safe_load(libraries_yaml.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            data = None
        if data and "libraries" in data:
            for lib in data["libraries"]:
                dep_graph[lib["name"]] = lib.get("depends", [])

    # Build set of libs we're building
    action_libs = {name for name, _ in actions}
    action_map = {name: action for name, action in actions}

    # Topological sort (Kahn's algorithm)
    # Only consider dependencies that are also in our action list
    in_degree: dict[str, int] = {name: 0 for name in action_libs}
    for name in action_libs:
        for dep in dep_graph.get(name, []):
            if dep in action_libs:
                in_degree[name] = in_degree.get(name, 0) + 1

    queue = [name for name in action_libs if in_degree.get(name, 0) == 0]
    sorted_names = []

    while queue:
        # Pick alphabetically for deterministic order
        queue.sort()
        node = queue.pop(0)
        sorted_names.append(node)
        # For each lib that depends on node, reduce in_degree
        for name in action_libs:
            if node in dep_graph.get(name, []) and name not in sorted_names:
                in_degree[name] -= 1
                if in_degree[name] == 0:
                    queue.append(name)

    # Add any remaining (circular deps or missing)
    for name in action_libs:
        if name not in sorted_names:
            sorted_names.append(name)

    return [(name, action_map[name]) for name in sorted_names if name in action_map]


def build_library(
    lib_name: str,
    version: str,
    toolchain_dir: Path,
    toolchain_prefix: str,
    toolchain_sysroot: str,
    toolchain_cflags: str,
    toolchain_ldflags: str,
    libraries_yaml: Path,
    downloads_dir: Path,
    logs_dir: Optional[Path] = None,
    max_cpus: int = 0,
    native: bool = False,
    cancel_check: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
    libraries_local: Optional[Path] = None,
    organize_logs: bool = False,
) -> bool:
    """Build and install a library into the toolchain sysroot.

    Args:
        lib_name: Library name (e.g. "OpenSSL")
        version: Version to build (e.g. "3.5.5", "auto")
        toolchain_dir: Path to installed toolchain
        toolchain_prefix: Compiler prefix (e.g. "arm-oe-linux-gnueabi-")
        toolchain_sysroot: Sysroot relative path
        toolchain_cflags: Architecture C flags
        toolchain_ldflags: Linker flags
        libraries_yaml: Path to libraries.yaml
        downloads_dir: Path for downloaded sources
        log: Callback for log output

    Returns:
        True on success.
    """
    # Setup file logging
    log_file = None
    if logs_dir:
        from core.log_cleanup import resolve_log_dir
        _log_dir = resolve_log_dir(logs_dir, "lib", organize_logs)
        tc_name = toolchain_dir.name
        timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
        log_path = _log_dir / f"{timestamp}.{tc_name}.{lib_name}-{version}.log"
        log_file = open(log_path, "w", encoding="utf-8")

    original_log = log

    def _log(msg: str) -> None:
        # Strip Rich markup for file log
        import re
        plain = re.sub(r'\[/?[a-z_ ]+\]', '', msg)
        if log_file:
            log_file.write(plain + "\n")
            log_file.flush()
        if original_log:
            original_log(msg)

    log = _log
    log(f"Building {lib_name} {version} for {toolchain_dir.name}")
    log(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        result = _do_build(
            lib_name, version, toolchain_dir, toolchain_prefix,
            toolchain_sysroot, toolchain_cflags, toolchain_ldflags,
            libraries_yaml, downloads_dir, max_cpus, native, cancel_check, log,
            libraries_local,
        )
        log(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        if log_file:
            log(f"Log: {log_path}")
        return result
    except Exception as e:
        log(f"[red]Build crashed: {e}[/red]")
        import traceback
        log(traceback.format_exc())
        return False
    finally:
        if log_file:
            log_file.close()


def _do_build(
    lib_name: str,
    version: str,
    toolchain_dir: Path,
    toolchain_prefix: str,
    toolchain_sysroot: str,
    toolchain_cflags: str,
    toolchain_ldflags: str,
    libraries_yaml: Path,
    downloads_dir: Path,
    max_cpus: int,
    native: bool,
    cancel_check: Optional[Callable[[], bool]],
    log: Callable[[str], None],
    libraries_local: Optional[Path] = None,
) -> bool:
    """Internal build logic."""
    # Resolve @TOOLCHAIN@ placeholder in cflags/ldflags
    toolchain_cflags = (toolchain_cflags or "").replace("@TOOLCHAIN@", str(toolchain_dir))
    toolchain_ldflags = (toolchain_ldflags or "").replace("@TOOLCHAIN@", str(toolchain_dir))

    # Load recipe (local overrides defaults)
    recipe = _find_recipe(lib_name, version, libraries_yaml, libraries_local)
    if not recipe:
        if log:
            log(f"[red]No recipe found for {lib_name} {version}[/red]")
        return False

    variant = recipe["variant"]
    lib_defaults = recipe["defaults"]
    actual_version = variant.get("version", version)

    # Resolve fields: variant overrides defaults
    source = variant.get("source", lib_defaults.get("source", ""))
    pre_configure = variant.get("pre_configure", lib_defaults.get("pre_configure", ""))
    configure = variant.get("configure", lib_defaults.get("configure", ""))
    make_cmd = variant.get("make", lib_defaults.get("make", ""))
    install_cmd = variant.get("install", lib_defaults.get("install", ""))
    post_install = variant.get("post_install", lib_defaults.get("post_install", ""))
    # Merge extra commands: library-level defaults + variant overrides.
    # Variant extras override same-key library extras, but library-level
    # extras for other keys are preserved (e.g. csky patch + auto_bitslice).
    extra = dict(lib_defaults.get("extra", {}))
    extra.update(variant.get("extra", {}))

    def _cancelled() -> bool:
        return cancel_check() if cancel_check else False

    if not source:
        if log:
            log(f"[red]No source URL for {lib_name} {actual_version}[/red]")
        return False

    # Determine paths
    sysroot_path = toolchain_dir / toolchain_sysroot if toolchain_sysroot else toolchain_dir
    # PREFIX: where libraries get installed (sysroot/usr or sysroot itself)
    prefix = _find_prefix(sysroot_path)

    # Safety: never install outside the toolchain directory
    try:
        prefix.resolve().relative_to(toolchain_dir.resolve())
    except ValueError:
        log(f"[red]Refusing to install to {prefix} — outside toolchain directory[/red]")
        return False
    target = _get_target(toolchain_dir, toolchain_prefix)
    cc = f"{toolchain_prefix}gcc"
    ranlib = f"{toolchain_prefix}ranlib"
    cpus = str(max_cpus if max_cpus else os.cpu_count() or 4)

    # Replace placeholders
    replacements = {
        "@VERSION@": actual_version,
        "@PREFIX@": str(prefix),
        "@CROSS@": toolchain_prefix,
        "@TARGET@": target,
        "@CC@": cc,
        "@RANLIB@": ranlib,
        "@CFLAGS@": toolchain_cflags,
        "@LDFLAGS@": toolchain_ldflags,
        "@CPUS@": cpus,
        "@TOOLCHAIN@": str(toolchain_dir),
    }

    source = _replace(source, replacements)
    pre_configure = _replace(pre_configure, replacements)
    configure = _replace(configure, replacements)
    make_cmd = _replace(make_cmd, replacements)
    install_cmd = _replace(install_cmd, replacements)
    post_install = _replace(post_install, replacements)

    # Native builds: replace hardcoded cross-compile targets with auto-detection
    # e.g. OpenSSL's "./Configure linux-generic32" → "./Configure" (auto-detect host)
    if native and "linux-generic32" in configure:
        configure = configure.replace("linux-generic32 ", "").replace("linux-generic32", "")
        log(f"Native: removed linux-generic32 from configure (auto-detect host)")

    # Android NDK: replace linux-generic32 with the correct android-* OpenSSL
    # target and let OpenSSL find the compiler via ANDROID_NDK env var.
    _is_android = "android" in toolchain_prefix.lower()
    if _is_android and "linux-generic32" in configure:
        _android_ssl_targets = {
            "arm-linux-androideabi-": "android-arm",
            "aarch64-linux-android-": "android-arm64",
            "i686-linux-android-": "android-x86",
            "x86_64-linux-android-": "android-x86_64",
        }
        ssl_target = _android_ssl_targets.get(toolchain_prefix, "android-arm")
        configure = configure.replace("linux-generic32", ssl_target)
        log(f"Android: OpenSSL target → {ssl_target}")

    # Log resolved build commands
    log(f"--- Resolved Build Commands ---")
    log(f"Source:        {source}")
    log(f"Pre-configure: {pre_configure or '(none)'}")
    log(f"Configure:     {configure}")
    log(f"Make:          {make_cmd}")
    log(f"Install:       {install_cmd}")
    if post_install:
        log(f"Post-install:  {post_install[:80]}{'...' if len(post_install) > 80 else ''}")
    log(f"Prefix:        {prefix}")
    log(f"Target:        {target}")
    log(f"CC:            {cc}")
    log(f"CFLAGS:        {toolchain_cflags or '(none)'}")
    log(f"LDFLAGS:       {toolchain_ldflags or '(none)'}")
    log(f"CPUs:          {cpus}")
    if extra and isinstance(extra, dict):
        for trait, cmd in extra.items():
            log(f"Extra ({trait}): {_replace(cmd, replacements)}")
    log(f"-------------------------------")

    # Download source
    if _cancelled():
        log("[yellow]Cancelled[/yellow]")
        return False

    downloads_dir.mkdir(parents=True, exist_ok=True)
    filename = source.split("/")[-1].split("?")[0]
    filepath = downloads_dir / filename

    if source.startswith("file://"):
        # Local file — resolve relative to downloads_dir
        file_ref = source[7:]  # strip "file://"
        local_path = Path(file_ref) if file_ref.startswith("/") else downloads_dir / file_ref
        if not local_path.exists():
            if log:
                log(f"[red]Local file not found: {local_path}[/red]")
            return False
        filepath = local_path
        if log:
            log(f"Using local file: {filepath.name}")
    elif filepath.exists() and filepath.stat().st_size > 0:
        if log:
            log(f"Using cached {filename}")
    else:
        if log:
            log(f"Downloading {filename}...")
        if not _download(source, filepath, log):
            return False

    if _cancelled():
        log("[yellow]Cancelled[/yellow]")
        return False

    # Extract
    build_dir = downloads_dir / f"build-{lib_name}-{actual_version}"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir()

    if log:
        log(f"Extracting...")
    try:
        if filename.endswith(".zip"):
            subprocess.run(
                ["unzip", "-o", "-d", str(build_dir), str(filepath)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=True,
            )
            # Move contents up if extracted into subdirectory
            _flatten_single_subdir(build_dir)
        else:
            subprocess.run(
                ["tar", "-xf", str(filepath), "--strip-components=1", "-C", str(build_dir)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=True,
            )
    except subprocess.CalledProcessError as e:
        if log:
            log(f"[red]Extract failed: {e.stderr.strip() if e.stderr else e}[/red]")
        return False

    # Prepare environment — put /usr/bin BEFORE toolchain bin so host pkg-config is found first
    env = os.environ.copy()
    original_path = env.get('PATH', '')
    # OE-SDK: add the cross-compiler bin path (inside sysroots)
    oesdk_bin = toolchain_dir / "sysroots" / "x86_64-oesdk-linux" / "usr" / "bin"
    oesdk_cross_bins = list(oesdk_bin.glob(f"{target}*")) if oesdk_bin.is_dir() else []
    oesdk_cross_path = str(oesdk_cross_bins[0]) if oesdk_cross_bins else ""
    env["PATH"] = f"/usr/bin:{toolchain_dir}/bin:{toolchain_dir}:{oesdk_cross_path}:{oesdk_bin}:{original_path}"
    if toolchain_cflags:
        env["CFLAGS"] = toolchain_cflags
    if toolchain_ldflags:
        env["LDFLAGS"] = toolchain_ldflags
    # Embed CFLAGS into CC (like s3 for OE-SDK) — some build systems (OpenSSL 1.0.x)
    # ignore CFLAGS env but respect CC
    # Embed CFLAGS into CC (like s3 for OE-SDK) — some build systems (OpenSSL 1.0.x)
    # ignore CFLAGS env but respect CC
    env["CC"] = f"{cc} {toolchain_cflags}".strip() if toolchain_cflags else cc
    env["RANLIB"] = ranlib

    # Auto-inject --sysroot if compiler cannot find headers (same as s3 for OE-SDK)
    # Skip for Android — the NDK compiler has its own sysroot baked in.
    sysroot_inject = ""
    if not _is_android:
        try:
            gcc_path = shutil.which(cc, path=env["PATH"])
            if gcc_path:
                # Test if gcc can find system headers — if not, it needs --sysroot
                test_result = subprocess.run(
                    [gcc_path, "-c", "-x", "c", "-o", "/dev/null", "-"],
                    input="#include <stdio.h>\nint main(){return 0;}\n", text=True, encoding="utf-8", errors="replace",
                    capture_output=True, timeout=10, env=env,
                )
                if test_result.returncode != 0:
                    sysroot_inject = f"--sysroot={sysroot_path}"
                    env["CC"] = f"{cc} {toolchain_cflags} {sysroot_inject}".strip()
                    if toolchain_cflags and "--sysroot" not in toolchain_cflags:
                        env["CFLAGS"] = f"{env.get('CFLAGS', '')} {sysroot_inject}".strip()
                    elif not toolchain_cflags:
                        env["CFLAGS"] = sysroot_inject
                    log(f"Auto-injected: {sysroot_inject}")
        except Exception:
            pass

    # Update replacements with sysroot-injected CC so extra scripts (auto_bitslice) work
    replacements["@CC@"] = env.get("CC", cc)
    if env.get("CFLAGS"):
        replacements["@CFLAGS@"] = env["CFLAGS"]

    # Set PKG_CONFIG_PATH so libraries can find each other (e.g. ccid needs pcsclite)
    pkg_dirs = []
    for pc_dir in toolchain_dir.glob("**/pkgconfig"):
        pkg_dirs.append(str(pc_dir))
    if pkg_dirs:
        env["PKG_CONFIG_PATH"] = ":".join(pkg_dirs)
        if not native:
            env["PKG_CONFIG_LIBDIR"] = ":".join(pkg_dirs)
    # Build a combined shell script so environment variables persist between steps
    script_parts = ["set -e"]

    # Export PKG_CONFIG vars inside script so $(pkg-config ...) works in configure
    if pkg_dirs:
        pkg_path = ":".join(pkg_dirs)
        script_parts.append(f'export PKG_CONFIG_PATH="{pkg_path}"')
        if not native:
            script_parts.append(f'export PKG_CONFIG_LIBDIR="{pkg_path}"')
    if not native:
        # Use host pkg-config to avoid double sysroot from toolchain's pkg-config
        script_parts.append('export PKG_CONFIG_SYSROOT_DIR=""')
        script_parts.append('export PKG_CONFIG="/usr/bin/pkg-config"')

    # Apply extra commands based on toolchain traits
    if extra and isinstance(extra, dict):
        for trait, cmd in extra.items():
            if _matches_trait(trait, toolchain_prefix, toolchain_dir):
                cmd = _replace(cmd, replacements)
                script_parts.append(f"# extra: {trait}")
                script_parts.append(cmd)
                if log:
                    log(f"Extra ({trait}): applied")

    if pre_configure:
        script_parts.append(f"# pre_configure")
        script_parts.append(pre_configure)

    # Apply "post_preconfigure" extras — these run AFTER pre_configure
    # (which may run autoreconf that overwrites config.sub) but BEFORE
    # configure. Needed for patches like csky that modify config.sub.
    if extra and isinstance(extra, dict):
        for trait, cmd in extra.items():
            if trait.startswith("post:") and _matches_trait(trait[5:], toolchain_prefix, toolchain_dir):
                cmd = _replace(cmd, replacements)
                script_parts.append(f"# extra post: {trait[5:]}")
                script_parts.append(cmd)
                if log:
                    log(f"Extra post ({trait[5:]}): applied")

    if configure:
        script_parts.append(f"# configure")
        script_parts.append(configure)

    if make_cmd:
        script_parts.append(f"# make")
        script_parts.append(make_cmd)

    if install_cmd:
        script_parts.append(f"# install")
        script_parts.append(install_cmd)

    if post_install:
        script_parts.append(f"# post_install")
        script_parts.append(post_install)

    full_script = "\n".join(script_parts)

    if log:
        log("Running build script...")
    if not _run_shell(full_script, build_dir, env, log, cancel_check):
        shutil.rmtree(build_dir, ignore_errors=True)
        return False

    if log:
        log(f"[green]{lib_name} {actual_version} built successfully[/green]")

    # Cleanup build dir
    shutil.rmtree(build_dir, ignore_errors=True)
    return True


def _find_recipe(lib_name: str, version: str, libraries_yaml: Path,
                  libraries_local: Optional[Path] = None) -> Optional[dict]:
    """Find the matching recipe and variant. Local overrides defaults."""
    from core.toolchain import load_library_recipes
    recipes = load_library_recipes(libraries_yaml, libraries_local)

    for lib in recipes:
        if lib["name"] != lib_name:
            continue
        defaults = {k: v for k, v in lib.items() if k not in ("name", "pc_file", "version_check", "variants", "depends")}
        for v in lib.get("variants", []):
            if v.get("version") == version:
                return {"defaults": defaults, "variant": v}
        # If version not found, return first variant
        if lib.get("variants"):
            return {"defaults": defaults, "variant": lib["variants"][0]}
    return None


def _find_prefix(sysroot_path: Path) -> Path:
    """Find the best install prefix in the sysroot."""
    usr = sysroot_path / "usr"
    if usr.is_dir():
        return usr
    return sysroot_path


def _get_target(toolchain_dir: Path, prefix: str) -> str:
    """Get the target triplet, preferring ``gcc -dumpmachine`` over the
    raw prefix.  Some toolchains use a short prefix (e.g. ``csky-linux-``)
    that is not a valid GNU triple; ``-dumpmachine`` returns the real one
    (e.g. ``csky-unknown-linux-gnuabiv2``).
    """
    import subprocess, os
    gcc = f"{prefix}gcc"
    # Try gcc in the toolchain's bin/ directory first
    for bin_dir in (toolchain_dir / "bin", ):
        gcc_path = bin_dir / gcc
        if gcc_path.exists():
            try:
                env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
                triple = subprocess.check_output(
                    [str(gcc_path), "-dumpmachine"],
                    text=True, encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL, timeout=5, env=env,
                ).strip()
                if triple:
                    return triple
            except Exception:
                pass
    # Try gcc on PATH (e.g. native toolchain)
    try:
        triple = subprocess.check_output(
            [gcc, "-dumpmachine"],
            text=True, encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
        if triple:
            return triple
    except Exception:
        pass
    # Fallback: strip trailing dash from prefix
    return prefix.rstrip("-")


def _replace(text: str, replacements: dict) -> str:
    """Replace all placeholders in text."""
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _matches_trait(trait: str, prefix: str, toolchain_dir: Path) -> bool:
    """Check if a toolchain matches a trait like 'musl', 'uclibc', 'android'."""
    prefix_lower = prefix.lower()
    if trait == "musl" and "musl" in prefix_lower:
        return True
    if trait == "uclibc" and "uclibc" in prefix_lower:
        return True
    if trait == "android" and "android" in prefix_lower:
        return True
    if trait == "csky" and "csky" in prefix_lower:
        return True
    if trait in ("auto_bitslice", "resolve_libusb", "resolve_deps",
                 "fix_wirecheck", "fix_version", "fix_includes"):
        return True  # Always run these
    return False


def _download(url: str, filepath: Path, log: Optional[Callable[[str], None]] = None) -> bool:
    """Download a file."""
    try:
        result = subprocess.run(
            ["wget", "-q", "-O", str(filepath), url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        if result.returncode == 0 and filepath.exists() and filepath.stat().st_size > 0:
            return True
    except FileNotFoundError:
        pass

    try:
        result = subprocess.run(
            ["curl", "-sL", "-o", str(filepath), url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        if result.returncode == 0 and filepath.exists() and filepath.stat().st_size > 0:
            return True
    except FileNotFoundError:
        pass

    if log:
        log("[red]Download failed[/red]")
    return False


def _run_shell(
    cmd: str, cwd: Path, env: dict,
    log: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """Run a shell command. Kills process if cancel_check returns True."""
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        from rich.markup import escape as rich_escape
        for line in proc.stdout:
            if cancel_check and cancel_check():
                proc.kill()
                proc.wait()
                if log:
                    log("[yellow]Process killed — cancelled[/yellow]")
                return False
            if log:
                log(f"  {rich_escape(line.rstrip())}")
        proc.wait()
        if proc.returncode != 0:
            if log:
                log(f"[red]Command failed (exit {proc.returncode})[/red]")
            return False
        return True
    except Exception as e:
        if log:
            log(f"[red]Error: {e}[/red]")
        return False


def _flatten_single_subdir(directory: Path) -> None:
    """If directory contains a single subdirectory, move its contents up."""
    subdirs = [d for d in directory.iterdir() if d.is_dir()]
    if len(subdirs) == 1:
        subdir = subdirs[0]
        for item in subdir.iterdir():
            item.rename(directory / item.name)
        subdir.rmdir()
