"""Target configuration — the central data model of s4.

A Target bundles everything needed to build oscam for a specific device:
toolchain selection, build settings (modules/protocols/readers), and
optional upload destinations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class UploadConfig:
    """SSH upload destination."""

    host: str = ""
    user: str = "root"
    port: int = 22
    key: str = ""
    target_path: str = "/usr/bin/oscam"
    pre_cmd: str = ""
    post_cmd: str = ""


@dataclass
class BuildSettings:
    """Oscam build configuration."""

    # Module selection
    modules: list[str] = field(default_factory=list)
    protocols: list[str] = field(default_factory=list)
    readers: list[str] = field(default_factory=list)
    card_readers: list[str] = field(default_factory=list)

    # Build flags (defaults from toolchain, editable per target)
    useflags: str = ""             # USE flags (e.g. "USE_LIBCRYPTO USE_EXTRA")
    cc_opts: str = ""              # Compiler options → CC_OPTS
    cflags: str = ""               # Architecture C flags → EXTRA_CFLAGS
    ldflags: str = ""              # Linker flags → EXTRA_LDFLAGS
    make_vars: str = ""            # Raw make variable assignments
    confdir: str = "/etc/tuxbox/config"
    stapi_lib: str = ""            # STAPI library (empty = not used)


@dataclass
class Target:
    """A complete build target configuration."""

    name: str = ""
    description: str = ""
    source: str = "default"
    toolchain: str = ""
    build: BuildSettings = field(default_factory=BuildSettings)
    upload: list[UploadConfig] = field(default_factory=list)

    def save(self, path: Path) -> None:
        """Save target to YAML file."""
        data = {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "toolchain": self.toolchain,
            "build": {
                "modules": self.build.modules,
                "protocols": self.build.protocols,
                "readers": self.build.readers,
                "card_readers": self.build.card_readers,
                "useflags": self.build.useflags,
                "cc_opts": self.build.cc_opts,
                "cflags": self.build.cflags,
                "ldflags": self.build.ldflags,
                "make_vars": self.build.make_vars,
                "confdir": self.build.confdir,
                "stapi_lib": self.build.stapi_lib,
            },
            "upload": [
                {
                    "host": u.host,
                    "user": u.user,
                    "port": u.port,
                    "key": u.key,
                    "target_path": u.target_path,
                    "pre_cmd": u.pre_cmd,
                    "post_cmd": u.post_cmd,
                }
                for u in self.upload
            ],
        }
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Target":
        """Load target from YAML file."""
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not data:
            return cls()

        build_data = data.get("build", {})
        build = BuildSettings(
            modules=build_data.get("modules", []),
            protocols=build_data.get("protocols", []),
            readers=build_data.get("readers", []),
            card_readers=build_data.get("card_readers", []),
            useflags=build_data.get("useflags", ""),
            cc_opts=build_data.get("cc_opts", ""),
            cflags=build_data.get("cflags", ""),
            ldflags=build_data.get("ldflags", ""),
            make_vars=build_data.get("make_vars", ""),
            confdir=build_data.get("confdir", "/etc/tuxbox/config"),
            stapi_lib=build_data.get("stapi_lib", ""),
        )

        uploads = []
        for u in data.get("upload", []):
            uploads.append(UploadConfig(
                host=u.get("host", ""),
                user=u.get("user", "root"),
                port=u.get("port", 22),
                key=u.get("key", ""),
                target_path=u.get("target_path", "/usr/bin/oscam"),
                pre_cmd=u.get("pre_cmd", ""),
                post_cmd=u.get("post_cmd", ""),
            ))

        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            source=data.get("source", "default"),
            toolchain=data.get("toolchain", ""),
            build=build,
            upload=uploads,
        )

    @classmethod
    def list_targets(cls, targets_dir: Path) -> list[tuple[str, Path]]:
        """List all saved targets. Returns [(name, path), ...]."""
        targets = []
        if targets_dir.is_dir():
            for f in sorted(targets_dir.glob("*.yaml")):
                targets.append((f.stem, f))
        return targets
