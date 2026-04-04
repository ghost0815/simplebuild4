"""Interface to oscam's config.sh — reads available options and manages config state."""

from __future__ import annotations

import subprocess
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class OscamOptions:
    """All available oscam options read from config.sh."""
    addons: list[str] = field(default_factory=list)
    protocols: list[str] = field(default_factory=list)
    readers: list[str] = field(default_factory=list)
    card_readers: list[str] = field(default_factory=list)

    # Default enabled state (from config.h defaults)
    default_enabled: dict[str, list[str]] = field(default_factory=dict)


def read_options(repo_dir: Path) -> OscamOptions | None:
    """Read all available options from a cloned oscam repo's config.sh.

    Returns None if config.sh is not found or not executable.
    """
    config_sh = repo_dir / "config.sh"
    if not config_sh.exists():
        return None

    opts = OscamOptions()

    for category, attr in [
        ("addons", "addons"),
        ("protocols", "protocols"),
        ("readers", "readers"),
        ("card_readers", "card_readers"),
    ]:
        # Get all valid options for this category
        valid = _run_config_sh(config_sh, ["--show-valid", category])
        if valid is not None:
            setattr(opts, attr, valid.split())

        # Get default enabled options
        enabled = _run_config_sh(config_sh, ["--show-enabled", category])
        if enabled is not None:
            opts.default_enabled[category] = enabled.split()

    return opts


def read_enabled(repo_dir: Path) -> dict[str, list[str]]:
    """Read currently enabled options from config.h via config.sh.

    Returns dict with keys: addons, protocols, readers, card_readers.
    """
    config_sh = repo_dir / "config.sh"
    if not config_sh.exists():
        return {}

    result = {}
    for category in ("addons", "protocols", "readers", "card_readers"):
        enabled = _run_config_sh(config_sh, ["--show-enabled", category])
        if enabled is not None:
            result[category] = enabled.split()
        else:
            result[category] = []
    return result


def apply_config(
    repo_dir: Path,
    enabled: dict[str, list[str]],
    all_options: OscamOptions,
    log: "Callable[[str], None] | None" = None,
) -> bool:
    """Apply a target configuration to config.h via config.sh.

    Flow (same as s3):
    1. Restore oscam defaults (config.sh -R)
    2. Disable ALL options (clean slate)
    3. Enable only the target's selected options
    4. Dependencies resolved by config.sh automatically (update_deps)

    Args:
        repo_dir: Path to cloned oscam repo
        enabled: Dict of category → list of enabled option names
        all_options: All available options (to know what to disable)
        log: Optional log callback

    Returns:
        True on success.
    """
    _log = log or (lambda msg: None)
    config_sh = repo_dir / "config.sh"
    if not config_sh.exists():
        _log(f"  [red]config.sh not found in {repo_dir}[/red]")
        return False

    # Step 1: Restore defaults
    _log("  Restoring oscam defaults...")
    _run_config_sh(config_sh, ["--restore"])

    # Step 2: Disable ALL options (clean slate)
    all_opts = []
    for attr in ("addons", "protocols", "readers", "card_readers"):
        all_opts.extend(getattr(all_options, attr))
    if all_opts:
        _log(f"  Disabling all {len(all_opts)} options...")
        _run_config_sh(config_sh, ["-D"] + all_opts)

    # Step 3: Enable only the target's selected options
    to_enable = []
    for category in ("addons", "protocols", "readers", "card_readers"):
        to_enable.extend(enabled.get(category, []))

    if to_enable:
        _log(f"  Enabling {len(to_enable)} options...")
        result = _run_config_sh(config_sh, ["-E"] + to_enable)
        if result:
            for line in result.splitlines():
                if line.strip():
                    _log(f"    {line.strip()}")

    # Step 4: Show final state
    for category, label in [("addons", "Addons"), ("protocols", "Protocols"),
                             ("readers", "Readers"), ("card_readers", "Card Readers")]:
        final = _run_config_sh(config_sh, ["--show-enabled", category])
        count = len(final.split()) if final else 0
        _log(f"  {label}: {count} enabled")

    return True


def merge_options(repo_dirs: list[Path]) -> OscamOptions:
    """Read options from multiple repos and merge them (union, unique).

    Args:
        repo_dirs: List of cloned oscam repo paths

    Returns:
        Merged OscamOptions with union of all options.
        Default enabled = union of all defaults.
    """
    merged = OscamOptions()

    for repo_dir in repo_dirs:
        opts = read_options(repo_dir)
        if not opts:
            continue
        for attr in ("addons", "protocols", "readers", "card_readers"):
            existing = set(getattr(merged, attr))
            for opt in getattr(opts, attr):
                if opt not in existing:
                    getattr(merged, attr).append(opt)
                    existing.add(opt)
        for cat, defaults in opts.default_enabled.items():
            if cat not in merged.default_enabled:
                merged.default_enabled[cat] = []
            existing_defaults = set(merged.default_enabled[cat])
            for opt in defaults:
                if opt not in existing_defaults:
                    merged.default_enabled[cat].append(opt)
                    existing_defaults.add(opt)

    return merged


def get_version(repo_dir: Path) -> str:
    """Get oscam version string from the repo."""
    config_sh = repo_dir / "config.sh"
    if not config_sh.exists():
        return ""
    result = _run_config_sh(config_sh, ["--oscam-version"])
    return result.strip() if result else ""


def _run_config_sh(config_sh: Path, args: list[str]) -> str | None:
    """Run config.sh with arguments and return stdout."""
    try:
        result = subprocess.run(
            ["bash", str(config_sh.resolve())] + args,
            capture_output=True, text=True, timeout=10,
            cwd=str(config_sh.parent.resolve()),
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return result.stdout.strip() if result.stdout.strip() else None
    except Exception:
        return None
