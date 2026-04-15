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
    """
    from core.toolchain import Toolchain, ToolchainRegistry, detect_toolchain_props

    def _log(msg):
        if log:
            log(msg)

    reg = ToolchainRegistry(paths.toolchains_defaults, paths.toolchains_local, paths.toolchains_dir)
    reg.load()
    local_tcs = reg._load_file(reg.local_path)

    install_dir = paths.toolchains_dir / tpl.name
    det_prefix, det_sysroot = detect_toolchain_props(install_dir)
    prefix = det_prefix or tpl.prefix
    sysroot = det_sysroot or tpl.sysroot
    if det_prefix or det_sysroot:
        _log(f"  [green]Auto-detected:[/green] prefix=[dim]{prefix or '-'}[/dim] sysroot=[dim]{sysroot or '-'}[/dim]")

    existing = next((tc for tc in local_tcs if tc.name == tpl.name), None)
    if existing is not None:
        existing.description = tpl.description or existing.description or tpl.name
        existing.category = "selfbuild"
        existing.arch = tpl.arch or existing.arch
        existing.machines = tpl.machines or existing.machines
        existing.prefix = prefix
        existing.sysroot = sysroot
        existing.cflags = tpl.cflags or existing.cflags
        existing.ldflags = tpl.ldflags or existing.ldflags
        existing.selfbuild = True
        _log("  [green]Merged into existing local entry → category=selfbuild[/green]")
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

    Covers: builder setup (one-time), ct-ng build, post-build path fixups,
    and toolchain registration. Pure and UI-free — suitable for both
    manual (F5) and automated (build-toolchains action) use.
    """
    def _log(msg):
        if log:
            log(msg)

    if tpl.builder != "crosstool-ng":
        _log(f"[red]Builder '{tpl.builder}' is not supported (only crosstool-ng)[/red]")
        return False

    ctng_dir = resolve_ctng_dir(settings, paths)

    from core.builders import load_builder_recipes
    from core.builders.crosstool_ng import CrosstoolNG

    recipes = load_builder_recipes(paths.builders_recipes)
    recipe = recipes.get(tpl.builder)
    if not recipe:
        _log(f"[red]No recipe for builder '{tpl.builder}' in builders.yaml[/red]")
        return False

    ctng = CrosstoolNG(
        ctng_dir=ctng_dir,
        toolchains_dir=paths.toolchains_dir,
        downloads_dir=paths.downloads_dir,
        recipe=recipe,
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
