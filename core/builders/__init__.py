"""Toolchain builder integrations (crosstool-ng, freetz-ng, android-ndk, oe-sdk)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class BuilderRecipe:
    """How to set up an external toolchain builder.

    Loaded from defaults/builders.yaml. Each recipe defines:
    - name: builder identifier (matches template's `builder` field)
    - description: human-readable name
    - repo: git URL to clone
    - binary: relative path to the executable that indicates "installed"
    - setup: list of command lists to execute sequentially
    - customize: single command list to launch the interactive configurator
    """
    name: str = ""
    description: str = ""
    repo: str = ""
    binary: str = ""
    setup: list = field(default_factory=list)
    customize: list = field(default_factory=list)


def load_builder_recipes(path: Path) -> dict:
    """Load builder recipes from defaults/builders.yaml.

    Returns a dict {name: BuilderRecipe}.
    """
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not data or "builders" not in data:
        return {}
    result = {}
    for entry in data["builders"]:
        if not isinstance(entry, dict):
            continue
        recipe = BuilderRecipe(
            name=entry.get("name", ""),
            description=entry.get("description", ""),
            repo=entry.get("repo", ""),
            binary=entry.get("binary", ""),
            setup=entry.get("setup", []),
            customize=entry.get("customize", []),
        )
        if recipe.name:
            result[recipe.name] = recipe
    return result


def expand_tokens(cmd: list, tokens: dict) -> list:
    """Replace @TOKEN@ placeholders in each argument of a command list."""
    result = []
    for arg in cmd:
        s = str(arg)
        for key, val in tokens.items():
            s = s.replace(f"@{key}@", str(val))
        result.append(s)
    return result
