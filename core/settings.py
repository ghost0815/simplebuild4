"""s4 settings — persistent application configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Settings:
    """All s4 application settings."""

    # General
    language: str = "en"
    theme: str = "textual-dark"    # Textual theme name (auto-saved)
    update_check: bool = True
    warn_unsaved_on_exit: bool = False  # Esc/F2 warn when a screen is dirty
    last_target: str = ""          # Last loaded target path (auto-saved)

    # Repository
    s4_url: str = "https://git.streamboard.tv/common/simplebuild4.git"

    # Build
    compiler_opt: str = "-O2 -ggdb -pipe -ffunction-sections -fdata-sections -fomit-frame-pointer -fno-schedule-insns"
    max_cpus: int = 16
    patch_webif: bool = True
    delete_debug: bool = True
    save_smargo: bool = False

    # Signing
    sign_cert: str = ""
    sign_key: str = ""

    # QEMU
    qemu_test: bool = False
    qemu_timeout: int = 5
    qemu_expect: str = "Server running"

    # Toolchain Builders
    ctng_dir: str = ""             # crosstool-ng install dir (empty = data/builders/crosstool-ng)

    def save(self, path: Path) -> None:
        """Save settings to YAML."""
        data = {
            "last_target": self.last_target,
            "language": self.language,
            "theme": self.theme,
            "update_check": self.update_check,
            "warn_unsaved_on_exit": self.warn_unsaved_on_exit,
            "s4_url": self.s4_url,
            "compiler_opt": self.compiler_opt,
            "max_cpus": self.max_cpus,
            "patch_webif": self.patch_webif,
            "delete_debug": self.delete_debug,
            "save_smargo": self.save_smargo,
            "sign_cert": self.sign_cert,
            "sign_key": self.sign_key,
            "qemu_test": self.qemu_test,
            "qemu_timeout": self.qemu_timeout,
            "qemu_expect": self.qemu_expect,
            "ctng_dir": self.ctng_dir,
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
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return cls()
        if not data:
            return cls()
        return cls(
            last_target=data.get("last_target", ""),
            language=data.get("language", "en"),
            theme=data.get("theme", "textual-dark"),
            update_check=data.get("update_check", True),
            warn_unsaved_on_exit=data.get("warn_unsaved_on_exit", False),
            s4_url=data.get("s4_url", cls.s4_url),
            compiler_opt=data.get("compiler_opt", cls.compiler_opt),
            max_cpus=data.get("max_cpus", 16),
            patch_webif=data.get("patch_webif", True),
            delete_debug=data.get("delete_debug", True),
            save_smargo=data.get("save_smargo", True),
            sign_cert=data.get("sign_cert", ""),
            sign_key=data.get("sign_key", ""),
            qemu_test=data.get("qemu_test", False),
            qemu_timeout=data.get("qemu_timeout", 5),
            qemu_expect=data.get("qemu_expect", "Server running"),
            ctng_dir=data.get("ctng_dir", ""),
        )
