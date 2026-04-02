"""Toolchain registry and management.

All known toolchains are defined in a single YAML file (data/toolchains.yaml).
Installed toolchains live in data/toolchains/<name>/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class Toolchain:
    """A toolchain definition with all metadata and build defaults."""

    # Identity
    name: str = ""
    description: str = ""
    arch: str = ""
    machines: str = ""

    # Compiler
    prefix: str = ""               # Compiler prefix (e.g. aarch64-oe-linux-)
    sysroot: str = ""              # Sysroot path relative to toolchain dir
    confdir: str = "/etc/tuxbox/config"  # Default oscam config directory

    # Build defaults (copied to target on assignment)
    useflags: str = ""             # USE flags (e.g. "USE_LIBCRYPTO USE_EXTRA")
    cc_opts: str = ""              # Compiler options → CC_OPTS
    cflags: str = ""               # Architecture C flags → EXTRA_CFLAGS
    ldflags: str = ""              # Linker flags → EXTRA_LDFLAGS
    make_vars: str = ""            # Raw make variable assignments

    # STAPI (empty = not supported)
    stapi_lib: str = ""            # Custom STAPI lib name, "default" = liboscam_stapi.a

    # Download / Install
    url: str = ""                  # Download URL
    strip_components: int = 1      # tar --strip-components

    # Runtime state (not persisted)
    installed: bool = False

    def build_defaults(self) -> dict[str, str]:
        """Return the build defaults that get copied into a target."""
        return {
            "useflags": self.useflags,
            "cc_opts": self.cc_opts,
            "cflags": self.cflags,
            "ldflags": self.ldflags,
            "make_vars": self.make_vars,
            "confdir": self.confdir,
            "stapi_lib": self.stapi_lib,
        }


class ToolchainRegistry:
    """Manages the toolchain registry and installed toolchains.

    Loads from two sources:
    - defaults_path: shipped with s4, git-managed (defaults/toolchains.yaml)
    - local_path: user additions, gitignored (data/toolchains.local.yaml)

    Local entries override defaults with the same name.
    """

    def __init__(self, defaults_path: Path, local_path: Path, install_dir: Path):
        self.defaults_path = defaults_path
        self.local_path = local_path
        self.install_dir = install_dir
        self._toolchains: list[Toolchain] = []

    def load(self) -> None:
        """Load toolchain registry from both YAML files."""
        by_name: dict[str, Toolchain] = {}

        # Load defaults first
        for tc in self._load_file(self.defaults_path):
            by_name[tc.name] = tc

        # Local overrides/additions
        for tc in self._load_file(self.local_path):
            by_name[tc.name] = tc

        # Check installed state
        for tc in by_name.values():
            tc.installed = (self.install_dir / tc.name).is_dir()

        self._toolchains = sorted(by_name.values(), key=lambda t: t.name)

    def _load_file(self, path: Path) -> list[Toolchain]:
        """Load toolchains from a single YAML file."""
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not data or "toolchains" not in data:
            return []
        result = []
        for entry in data["toolchains"]:
            tc = Toolchain(
                name=entry.get("name", ""),
                description=entry.get("description", ""),
                arch=entry.get("arch", ""),
                machines=entry.get("machines", ""),
                prefix=entry.get("prefix", ""),
                sysroot=entry.get("sysroot", ""),
                confdir=entry.get("confdir", "/etc/tuxbox/config"),
                useflags=entry.get("useflags", ""),
                cc_opts=entry.get("cc_opts", ""),
                cflags=entry.get("cflags", ""),
                ldflags=entry.get("ldflags", ""),
                make_vars=entry.get("make_vars", ""),
                stapi_lib=entry.get("stapi_lib", ""),
                url=entry.get("url", ""),
                strip_components=entry.get("strip_components", 1),
            )
            if tc.name:
                result.append(tc)
        return result

    def save_local(self, toolchains: list[Toolchain] | None = None) -> None:
        """Save to the local (user) registry file."""
        tcs = toolchains if toolchains is not None else self._get_local_only()
        self._save_file(self.local_path, tcs)

    def _save_file(self, path: Path, toolchains: list[Toolchain]) -> None:
        """Save toolchains to a YAML file."""
        data = {
            "toolchains": [
                {
                    "name": tc.name,
                    "description": tc.description,
                    "arch": tc.arch,
                    "machines": tc.machines,
                    "prefix": tc.prefix,
                    "sysroot": tc.sysroot,
                    "confdir": tc.confdir,
                    "useflags": tc.useflags,
                    "cc_opts": tc.cc_opts,
                    "cflags": tc.cflags,
                    "ldflags": tc.ldflags,
                    "make_vars": tc.make_vars,
                    "stapi_lib": tc.stapi_lib,
                    "url": tc.url,
                    "strip_components": tc.strip_components,
                }
                for tc in toolchains
            ]
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    def _get_local_only(self) -> list[Toolchain]:
        """Get toolchains that are only in the local file (not in defaults)."""
        default_names = {tc.name for tc in self._load_file(self.defaults_path)}
        return [tc for tc in self._toolchains if tc.name not in default_names]

    @property
    def toolchains(self) -> list[Toolchain]:
        return list(self._toolchains)

    def get(self, name: str) -> Optional[Toolchain]:
        """Get a toolchain by name."""
        for tc in self._toolchains:
            if tc.name == name:
                return tc
        return None

    def installed_toolchains(self) -> list[Toolchain]:
        """Return only installed toolchains."""
        return [tc for tc in self._toolchains if tc.installed]

    def available_toolchains(self) -> list[Toolchain]:
        """Return toolchains that are not yet installed but have a download URL."""
        return [tc for tc in self._toolchains if not tc.installed and tc.url]

    def add(self, tc: Toolchain) -> None:
        """Add a toolchain to the registry."""
        # Replace if exists
        self._toolchains = [t for t in self._toolchains if t.name != tc.name]
        self._toolchains.append(tc)

    def remove(self, name: str) -> None:
        """Remove a toolchain from the registry."""
        self._toolchains = [t for t in self._toolchains if t.name != name]

    def refresh_installed(self) -> None:
        """Re-check which toolchains are installed on disk."""
        for tc in self._toolchains:
            tc.installed = (self.install_dir / tc.name).is_dir()

    def get_select_options(self, include_native: bool = True) -> list[tuple[str, str]]:
        """Return (display_name, value) tuples for a Select widget."""
        options = []
        if include_native:
            options.append(("native", "native"))
        for tc in self._toolchains:
            if tc.installed:
                options.append((f"{tc.name} — {tc.description}", tc.name))
        return options
