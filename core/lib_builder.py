"""Library builder — downloads, configures, compiles and installs libraries into toolchain sysroot."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import yaml


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
    log: Optional[Callable[[str], None]] = None,
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
    # Load recipe
    recipe = _find_recipe(lib_name, version, libraries_yaml)
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
    extra = variant.get("extra", lib_defaults.get("extra", {}))

    if not source:
        if log:
            log(f"[red]No source URL for {lib_name} {actual_version}[/red]")
        return False

    # Determine paths
    sysroot_path = toolchain_dir / toolchain_sysroot if toolchain_sysroot else toolchain_dir
    # PREFIX: where libraries get installed (sysroot/usr or sysroot itself)
    prefix = _find_prefix(sysroot_path)
    target = _get_target(toolchain_dir, toolchain_prefix)
    cc = f"{toolchain_prefix}gcc"
    ranlib = f"{toolchain_prefix}ranlib"
    cpus = str(os.cpu_count() or 4)

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

    # Download source
    downloads_dir.mkdir(parents=True, exist_ok=True)
    filename = source.split("/")[-1].split("?")[0]
    filepath = downloads_dir / filename

    if log:
        log(f"Downloading {filename}...")
    if not _download(source, filepath, log):
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
                capture_output=True, text=True, timeout=120, check=True,
            )
            # Move contents up if extracted into subdirectory
            _flatten_single_subdir(build_dir)
        else:
            subprocess.run(
                ["tar", "-xf", str(filepath), "--strip-components=1", "-C", str(build_dir)],
                capture_output=True, text=True, timeout=120, check=True,
            )
    except subprocess.CalledProcessError as e:
        if log:
            log(f"[red]Extract failed: {e.stderr.strip() if e.stderr else e}[/red]")
        return False

    # Prepare environment
    env = os.environ.copy()
    env["PATH"] = f"{toolchain_dir}/bin:{toolchain_dir}/sysroots/x86_64-oesdk-linux/usr/bin:{env.get('PATH', '')}"
    if toolchain_cflags:
        env["CFLAGS"] = toolchain_cflags
    if toolchain_ldflags:
        env["LDFLAGS"] = toolchain_ldflags
    env["CC"] = cc
    env["RANLIB"] = ranlib

    # Apply extra commands based on toolchain traits
    if extra and isinstance(extra, dict):
        for trait, cmd in extra.items():
            if _matches_trait(trait, toolchain_prefix, toolchain_dir):
                if log:
                    log(f"Applying extra for {trait}...")
                cmd = _replace(cmd, replacements)
                _run_shell(cmd, build_dir, env, log)

    # Pre-configure
    if pre_configure:
        if log:
            log(f"Pre-configure: {pre_configure}")
        if not _run_shell(pre_configure, build_dir, env, log):
            return False

    # Configure
    if configure:
        if log:
            log(f"Configure...")
        if not _run_shell(configure, build_dir, env, log):
            return False

    # Make
    if make_cmd:
        if log:
            log(f"Building...")
        if not _run_shell(make_cmd, build_dir, env, log):
            return False

    # Install
    if install_cmd:
        if log:
            log(f"Installing...")
        if not _run_shell(install_cmd, build_dir, env, log):
            return False

    if log:
        log(f"[green]{lib_name} {actual_version} built successfully[/green]")

    # Cleanup build dir
    shutil.rmtree(build_dir, ignore_errors=True)
    return True


def _find_recipe(lib_name: str, version: str, libraries_yaml: Path) -> Optional[dict]:
    """Find the matching recipe and variant."""
    if not libraries_yaml.exists():
        return None
    data = yaml.safe_load(libraries_yaml.read_text(encoding="utf-8"))
    if not data or "libraries" not in data:
        return None

    for lib in data["libraries"]:
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
    """Get the target triplet from the compiler prefix."""
    # prefix is e.g. "arm-oe-linux-gnueabi-" → target "arm-oe-linux-gnueabi"
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
    if trait == "android" and ("android" in prefix_lower or (toolchain_dir / "sysroots").exists()):
        return False  # Android check needs more logic
    if trait == "android" and "android" in prefix_lower:
        return True
    if trait == "auto_bitslice":
        return True  # Always run bitslice detection
    return False


def _download(url: str, filepath: Path, log: Optional[Callable[[str], None]] = None) -> bool:
    """Download a file."""
    try:
        result = subprocess.run(
            ["wget", "-q", "-O", str(filepath), url],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0 and filepath.exists() and filepath.stat().st_size > 0:
            return True
    except FileNotFoundError:
        pass

    try:
        result = subprocess.run(
            ["curl", "-sL", "-o", str(filepath), url],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0 and filepath.exists() and filepath.stat().st_size > 0:
            return True
    except FileNotFoundError:
        pass

    if log:
        log("[red]Download failed[/red]")
    return False


def _run_shell(cmd: str, cwd: Path, env: dict, log: Optional[Callable[[str], None]] = None) -> bool:
    """Run a shell command."""
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in proc.stdout:
            if log:
                log(f"  {line.rstrip()}")
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
