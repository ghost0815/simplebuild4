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


def apply_config(repo_dir: Path, enabled: dict[str, list[str]], all_options: OscamOptions) -> bool:
    """Apply a target configuration to config.h via config.sh.

    1. Restores defaults
    2. Enables/disables options based on the target config
    3. Dependencies are resolved by config.sh automatically

    Args:
        repo_dir: Path to cloned oscam repo
        enabled: Dict of category → list of enabled option names
        all_options: All available options (to know what to disable)

    Returns:
        True on success.
    """
    config_sh = repo_dir / "config.sh"
    if not config_sh.exists():
        return False

    # Restore defaults first
    _run_config_sh(config_sh, ["--restore"])

    # Build enable/disable lists
    to_enable = []
    to_disable = []

    for category, attr in [
        ("addons", "addons"),
        ("protocols", "protocols"),
        ("readers", "readers"),
        ("card_readers", "card_readers"),
    ]:
        all_opts = getattr(all_options, attr)
        target_enabled = enabled.get(category, [])
        for opt in all_opts:
            if opt in target_enabled:
                to_enable.append(opt)
            else:
                to_disable.append(opt)

    # Apply enables first, then disables (config.sh resolves deps)
    if to_enable:
        _run_config_sh(config_sh, ["-E"] + to_enable)
    if to_disable:
        _run_config_sh(config_sh, ["-D"] + to_disable)

    return True


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
