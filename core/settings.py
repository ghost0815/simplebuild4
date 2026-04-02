"""S4 settings — persistent application configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Settings:
    """All s4 application settings."""

    # General
    language: str = "en"
    update_check: bool = True
    verbose: bool = False

    # Repository
    s4_url: str = "https://github.com/gorgone/s3_releases.git"
    repo_autoupdate: bool = True
    url_check: bool = True

    # Build
    compiler_opt: str = "-O2 -ggdb -pipe -ffunction-sections -fdata-sections -fomit-frame-pointer -fno-schedule-insns"
    max_cpus: int = 16
    patch_webif: bool = True
    delete_debug: bool = True
    targz: bool = False
    save_smargo: bool = True

    # Compression
    upx_option: str = "--best"

    # Signing
    sign_cert: str = ""
    sign_key: str = ""

    # QEMU
    qemu_test: bool = False
    qemu_timeout: int = 5
    qemu_expect: str = "Server running"

    def save(self, path: Path) -> None:
        """Save settings to YAML."""
        data = {
            "language": self.language,
            "update_check": self.update_check,
            "verbose": self.verbose,
            "s4_url": self.s4_url,
            "repo_autoupdate": self.repo_autoupdate,
            "url_check": self.url_check,
            "compiler_opt": self.compiler_opt,
            "max_cpus": self.max_cpus,
            "patch_webif": self.patch_webif,
            "delete_debug": self.delete_debug,
            "targz": self.targz,
            "save_smargo": self.save_smargo,
            "upx_option": self.upx_option,
            "sign_cert": self.sign_cert,
            "sign_key": self.sign_key,
            "qemu_test": self.qemu_test,
            "qemu_timeout": self.qemu_timeout,
            "qemu_expect": self.qemu_expect,
        }
        path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "Settings":
        """Load settings from YAML. Returns defaults if file missing."""
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not data:
            return cls()
        return cls(
            language=data.get("language", "en"),
            update_check=data.get("update_check", True),
            verbose=data.get("verbose", False),
            s4_url=data.get("s4_url", cls.s4_url),
            repo_autoupdate=data.get("repo_autoupdate", True),
            url_check=data.get("url_check", True),
            compiler_opt=data.get("compiler_opt", "-O2"),
            max_cpus=data.get("max_cpus", 16),
            patch_webif=data.get("patch_webif", True),
            delete_debug=data.get("delete_debug", True),
            targz=data.get("targz", False),
            save_smargo=data.get("save_smargo", True),
            upx_option=data.get("upx_option", "--best"),
            sign_cert=data.get("sign_cert", ""),
            sign_key=data.get("sign_key", ""),
            qemu_test=data.get("qemu_test", False),
            qemu_timeout=data.get("qemu_timeout", 5),
            qemu_expect=data.get("qemu_expect", "Server running"),
        )
