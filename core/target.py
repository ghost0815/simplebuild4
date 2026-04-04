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

    # USE flags — Library
    use_libcrypto: bool = True
    use_ssl: bool = False
    use_libusb: bool = False
    use_pcsc: bool = False
    use_libdvbcsa: bool = False

    # USE flags — Hardware
    use_stapi: bool = False
    use_stapi5: bool = False
    use_coolapi: bool = False
    use_coolapi2: bool = False
    use_su980: bool = False
    use_azbox: bool = False

    # USE flags — Static linking
    use_static: bool = False
    static_libcrypto: bool = False
    static_ssl: bool = False
    static_libusb: bool = False
    static_pcsc: bool = False
    static_libdvbcsa: bool = False

    # USE flags — Other
    use_compress: bool = False
    use_oscamname: str = ""        # Custom binary name (empty = default)

    # Build options
    confdir: str = "/etc/tuxbox/config"
    make_vars: str = ""            # Raw make variable assignments


@dataclass
class Target:
    """A complete build target configuration."""

    name: str = ""
    description: str = ""
    sources: list[str] = field(default_factory=lambda: ["default"])
    toolchains: list[str] = field(default_factory=list)
    build: BuildSettings = field(default_factory=BuildSettings)
    upload: list[UploadConfig] = field(default_factory=list)

    def save(self, path: Path) -> None:
        """Save target to YAML file."""
        data = {
            "name": self.name,
            "description": self.description,
            "sources": self.sources,
            "toolchains": self.toolchains,
            "build": {
                "modules": self.build.modules,
                "protocols": self.build.protocols,
                "readers": self.build.readers,
                "card_readers": self.build.card_readers,
                "use_libcrypto": self.build.use_libcrypto,
                "use_ssl": self.build.use_ssl,
                "use_libusb": self.build.use_libusb,
                "use_pcsc": self.build.use_pcsc,
                "use_libdvbcsa": self.build.use_libdvbcsa,
                "use_stapi": self.build.use_stapi,
                "use_stapi5": self.build.use_stapi5,
                "use_coolapi": self.build.use_coolapi,
                "use_coolapi2": self.build.use_coolapi2,
                "use_su980": self.build.use_su980,
                "use_azbox": self.build.use_azbox,
                "use_static": self.build.use_static,
                "static_libcrypto": self.build.static_libcrypto,
                "static_ssl": self.build.static_ssl,
                "static_libusb": self.build.static_libusb,
                "static_pcsc": self.build.static_pcsc,
                "static_libdvbcsa": self.build.static_libdvbcsa,
                "use_compress": self.build.use_compress,
                "use_oscamname": self.build.use_oscamname,
                "confdir": self.build.confdir,
                "make_vars": self.build.make_vars,
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
            use_libcrypto=build_data.get("use_libcrypto", True),
            use_ssl=build_data.get("use_ssl", False),
            use_libusb=build_data.get("use_libusb", False),
            use_pcsc=build_data.get("use_pcsc", False),
            use_libdvbcsa=build_data.get("use_libdvbcsa", False),
            use_stapi=build_data.get("use_stapi", False),
            use_stapi5=build_data.get("use_stapi5", False),
            use_coolapi=build_data.get("use_coolapi", False),
            use_coolapi2=build_data.get("use_coolapi2", False),
            use_su980=build_data.get("use_su980", False),
            use_azbox=build_data.get("use_azbox", False),
            use_static=build_data.get("use_static", False),
            static_libcrypto=build_data.get("static_libcrypto", False),
            static_ssl=build_data.get("static_ssl", False),
            static_libusb=build_data.get("static_libusb", False),
            static_pcsc=build_data.get("static_pcsc", False),
            static_libdvbcsa=build_data.get("static_libdvbcsa", False),
            use_compress=build_data.get("use_compress", False),
            use_oscamname=build_data.get("use_oscamname", ""),
            confdir=build_data.get("confdir", "/etc/tuxbox/config"),
            make_vars=build_data.get("make_vars", ""),
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
            sources=data.get("sources", ["default"]),
            toolchains=data.get("toolchains", []),
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
