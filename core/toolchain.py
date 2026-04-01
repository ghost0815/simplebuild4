"""Toolchain discovery and management."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass
class ToolchainInfo:
    """Parsed toolchain configuration."""

    name: str = ""
    description: str = ""
    compiler: str = ""
    sysroot: str = ""
    confdir_default: str = "/etc/tuxbox/config"
    machines: str = ""
    self_build: bool = False
    default_use: str = ""
    extra_cc: str = ""
    extra_ld: str = ""
    info_text: str = ""
    cfg_path: str = ""
    installed: bool = False


def parse_toolchain_cfg(path: Path) -> ToolchainInfo:
    """Parse a bash-style toolchain config file into ToolchainInfo."""
    tc = ToolchainInfo(cfg_path=str(path))
    text = path.read_text(encoding="utf-8", errors="replace")

    field_map = {
        "_toolchainname": "name",
        "_description": "description",
        "_compiler": "compiler",
        "_sysroot": "sysroot",
        "_oscamconfdir_default": "confdir_default",
        "_machines": "machines",
        "default_use": "default_use",
        "extra_cc": "extra_cc",
        "extra_ld": "extra_ld",
    }

    for line in text.splitlines():
        for bash_key, attr in field_map.items():
            m = re.match(rf'^{re.escape(bash_key)}="(.+?)";?$', line)
            if m:
                setattr(tc, attr, m.group(1))

        m = re.match(r'^_self_build="(.+?)";?$', line)
        if m:
            tc.self_build = m.group(1) == "yes"

        m = re.match(r'^_tc_info="(.+?)";?$', line)
        if m:
            tc.info_text = m.group(1).replace("\\n", "\n")

    return tc


def list_toolchains(cfg_dir: Path, install_dir: Path) -> list[ToolchainInfo]:
    """Discover all available toolchains from config directory."""
    toolchains = []
    if not cfg_dir.is_dir():
        return toolchains

    for cfg in sorted(cfg_dir.iterdir()):
        if cfg.is_file():
            tc = parse_toolchain_cfg(cfg)
            if tc.name:
                tc.installed = (install_dir / tc.name).is_dir()
                toolchains.append(tc)

    return toolchains
