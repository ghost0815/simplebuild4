"""Toolchain builder templates.

A template describes how to build a toolchain via an external builder
(crosstool-ng, freetz-ng, android-ndk, oe-sdk).

Template format (YAML):

    name: qnap_x64
    description: QNAP x64 devices
    builder: crosstool-ng       # crosstool-ng | freetz-ng | android-ndk | oe-sdk
    arch: x86_64

    # Result properties (used to register the built toolchain)
    prefix: x86_64-qnap-linux-gnu-
    sysroot: x86_64-qnap-linux-gnu/sysroot
    cflags: ""
    ldflags: ""
    machines: ""

    # Builder-specific config file (relative to template yaml)
    config: qnap_x64.config

The native builder config (.config etc.) lives next to the YAML and is
opaque to s4 — only the builder itself reads/edits it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Template:
    """A toolchain builder template."""

    name: str = ""
    description: str = ""
    builder: str = ""              # crosstool-ng, freetz-ng, android-ndk, oe-sdk
    arch: str = ""

    # Result toolchain properties
    prefix: str = ""
    sysroot: str = ""
    cflags: str = ""
    ldflags: str = ""
    machines: str = ""

    # Builder-specific config file (relative to YAML location)
    config: str = ""

    # Optional: where the builder downloads its source tarballs.
    # Empty/missing → leave the builder's own setting (e.g. CT_LOCAL_TARBALLS_DIR
    # from the .config) untouched. Otherwise, paths are resolved relative to
    # the project root.
    download_location: str = ""

    # Internal — set when loaded
    yaml_path: Path | None = None
    is_local: bool = False         # True if from data/templates/ instead of defaults/

    @property
    def config_path(self) -> Path | None:
        """Absolute path to the builder's native config file."""
        if not self.config or not self.yaml_path:
            return None
        return self.yaml_path.parent / self.config


def load_template(yaml_path: Path) -> Template | None:
    """Load a single template from a YAML file."""
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not data or not isinstance(data, dict):
        return None
    return Template(
        name=data.get("name", yaml_path.stem),
        description=data.get("description", ""),
        builder=data.get("builder", ""),
        arch=data.get("arch", ""),
        prefix=data.get("prefix", ""),
        sysroot=data.get("sysroot", ""),
        cflags=data.get("cflags", ""),
        ldflags=data.get("ldflags", ""),
        machines=data.get("machines", ""),
        config=data.get("config", ""),
        download_location=data.get("download_location", ""),
        yaml_path=yaml_path,
    )


def load_templates(defaults_dir: Path, local_dir: Path) -> list[Template]:
    """Load all templates from defaults and local directories.

    Local templates are flagged with is_local=True for UI grouping.
    """
    templates: list[Template] = []
    for d, is_local in ((defaults_dir, False), (local_dir, True)):
        if not d.is_dir():
            continue
        for yaml_path in sorted(d.glob("*.yaml")):
            tpl = load_template(yaml_path)
            if tpl:
                tpl.is_local = is_local
                templates.append(tpl)
    return templates
