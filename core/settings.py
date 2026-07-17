"""s4 settings — persistent application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.credentials import encrypt as _encrypt, decrypt as _decrypt
from translation import t


@dataclass
class Settings:
    """All s4 application settings."""

    # General
    language: str = "en"
    theme: str = "textual-dark"    # Textual theme name (auto-saved)
    update_check: bool = True
    warn_unsaved_on_exit: bool = False  # Esc/F2 warn when a screen is dirty
    confirm_dangerous: bool = True     # require double-press for destructive actions
    ascii_mode: bool = False           # replace emoji/unicode with ASCII-safe alternatives
    last_target: str = ""          # Last loaded target path (auto-saved)
    last_repo: str = ""            # Last selected repository (auto-saved)
    last_toolchain: str = ""       # Last selected toolchain (auto-saved)

    # Repository
    s4_url: str = "https://git.streamboard.tv/common/simplebuild4.git"

    # Build
    compiler_opt: str = "-O2 -ggdb -pipe -ffunction-sections -fdata-sections -fomit-frame-pointer -fno-schedule-insns"
    # 0 = auto; effective value comes from the max_cpus property below.
    _max_cpus: int = 0
    patch_webif: bool = True
    delete_debug: bool = True
    save_smargo: bool = False
    export_libs_zip: bool = False      # export shared libs to <toolchain>-libs.zip after lib build
    organize_logs: bool = False        # sort logs into subdirs by type (build/lib/deploy/...)

    # Signing
    sign_cert: str = ""
    sign_key: str = ""

    # QEMU
    qemu_test: bool = False
    qemu_timeout: int = 5
    qemu_expect: str = "Server running"
    # When true, s4 invokes oscam's ``make tests`` target after a
    # successful make-build and runs the resulting ``tests.bin`` via
    # qemu-user (or directly on native) — picks up oscam's own config-
    # parser unit tests. Requires the oscam repo to carry the
    # build-isolate-test-artifacts + test-runner-exit-status commits
    # so the test build lands in ``build-tests/`` (no collision with
    # the next regular build) and ``tests.bin`` returns a non-zero
    # exit code on failure. cmake builds aren't supported yet.
    qemu_run_unit_tests: bool = False

    # Web Service
    web_enabled: bool = False      # serve TUI in browser via textual-serve
    web_host: str = "0.0.0.0"     # bind address
    web_port: int = 8080          # listen port
    web_dns_name: str = ""        # DNS name (empty = localhost or bind address)
    web_ssl: bool = False          # enable HTTPS
    web_ssl_cert: str = ""         # path to SSL certificate (empty = auto-generate)
    web_ssl_key: str = ""          # path to SSL private key (empty = auto-generate)
    web_auth_user: str = ""        # Basic Auth username (empty = no auth)
    web_auth_password: str = ""    # Basic Auth password (empty = no auth)

    # MCP Service (Model Context Protocol — LLM-driven automation)
    mcp_enabled: bool = False      # embedded test mode: spawn server as subprocess
    mcp_host: str = "0.0.0.0"     # bind address
    mcp_port: int = 8181
    mcp_dns_name: str = ""         # DNS name (empty = localhost or bind address)
    mcp_token: str = ""            # bearer token (empty = no auth, local only!)
    mcp_allow_actions: bool = False  # enable build/install/run-job tools
    mcp_ssl: bool = False          # enable HTTPS
    mcp_ssl_cert: str = ""         # path to SSL certificate (empty = auto-generate)
    mcp_ssl_key: str = ""          # path to SSL private key (empty = auto-generate)
    # Transport protocol: "sse" (legacy HTTP+SSE, endpoints /sse + /messages/,
    # spec rev 2024-11-05) or "http" (Streamable HTTP, single /mcp endpoint,
    # spec rev 2025-03-26). Default is "http" — current MCP spec.
    mcp_transport: str = "http"
    # True once the user has picked a transport in the Setup modal. Needed
    # to distinguish an intentional choice from a value that got persisted
    # implicitly by a generic Settings.save() call — any stored mcp_transport
    # without this flag set is treated as unset and falls back to the class
    # default. Set to True by McpServiceSetupModal on Install.
    mcp_transport_explicit: bool = False
    mcp_root_path: str = ""        # ASGI root_path prefix when behind a
                                   # reverse proxy (e.g. '/s4-mcp')

    # Toolchain Builders
    ctng_dir: str = ""             # crosstool-ng install dir (empty = data/builders/crosstool-ng)
    oesdk_dir: str = ""            # OE-SDK build dir (empty = data/builders/oe-sdk)
    andk_dir: str = ""             # Android NDK dir (empty = data/builders/android-ndk)

    # Build / Deployment defaults (used for new targets, editable in settings.yaml)
    default_compress_level: str = "--lzma"
    default_confdir: str = "/etc/tuxbox/config"
    default_build_system: str = "make"
    default_deploy_user: str = "root"
    default_deploy_port: int = 22
    default_deploy_path: str = "/usr/bin/oscam"

    # Log retention (applied per target to build logs in logs_dir).
    # 0 = unlimited for that rule. The newest log per target is always kept.
    log_max_age_days: int = 30
    log_max_count: int = 5
    log_max_size_mb: int = 0

    @property
    def max_cpus(self) -> int:
        return self._max_cpus if self._max_cpus > 0 else (os.cpu_count() or 4)

    @max_cpus.setter
    def max_cpus(self, value: int) -> None:
        self._max_cpus = value

    def save(self, path: Path) -> None:
        """Save settings to YAML."""
        data = {
            "last_target": self.last_target,
            "last_repo": self.last_repo,
            "last_toolchain": self.last_toolchain,
            "language": self.language,
            "theme": self.theme,
            "update_check": self.update_check,
            "warn_unsaved_on_exit": self.warn_unsaved_on_exit,
            "confirm_dangerous": self.confirm_dangerous,
            "ascii_mode": self.ascii_mode,
            "s4_url": self.s4_url,
            "compiler_opt": self.compiler_opt,
            "max_cpus": self._max_cpus,
            "patch_webif": self.patch_webif,
            "delete_debug": self.delete_debug,
            "save_smargo": self.save_smargo,
            "export_libs_zip": self.export_libs_zip,
            "organize_logs": self.organize_logs,
            "sign_cert": self.sign_cert,
            "sign_key": self.sign_key,
            "qemu_test": self.qemu_test,
            "qemu_timeout": self.qemu_timeout,
            "qemu_expect": self.qemu_expect,
            "qemu_run_unit_tests": self.qemu_run_unit_tests,
            "web_enabled": self.web_enabled,
            "web_host": self.web_host,
            "web_port": self.web_port,
            "web_dns_name": self.web_dns_name,
            "web_ssl": self.web_ssl,
            "web_ssl_cert": self.web_ssl_cert,
            "web_ssl_key": self.web_ssl_key,
            "web_auth_user": self.web_auth_user,
            "web_auth_password": _encrypt(self.web_auth_password),
            "mcp_enabled": self.mcp_enabled,
            "mcp_host": self.mcp_host,
            "mcp_port": self.mcp_port,
            "mcp_dns_name": self.mcp_dns_name,
            "mcp_token": _encrypt(self.mcp_token),
            "mcp_allow_actions": self.mcp_allow_actions,
            "mcp_ssl": self.mcp_ssl,
            "mcp_ssl_cert": self.mcp_ssl_cert,
            "mcp_ssl_key": self.mcp_ssl_key,
            "mcp_transport": self.mcp_transport,
            "mcp_transport_explicit": self.mcp_transport_explicit,
            "mcp_root_path": self.mcp_root_path,
            "default_compress_level": self.default_compress_level,
            "default_confdir": self.default_confdir,
            "default_build_system": self.default_build_system,
            "default_deploy_user": self.default_deploy_user,
            "default_deploy_port": self.default_deploy_port,
            "default_deploy_path": self.default_deploy_path,
            "ctng_dir": self.ctng_dir,
            "oesdk_dir": self.oesdk_dir,
            "andk_dir": self.andk_dir,
            "log_max_age_days": self.log_max_age_days,
            "log_max_count": self.log_max_count,
            "log_max_size_mb": self.log_max_size_mb,
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
            last_repo=data.get("last_repo", ""),
            last_toolchain=data.get("last_toolchain", ""),
            language=data.get("language", "en"),
            theme=data.get("theme", "textual-dark"),
            update_check=data.get("update_check", True),
            warn_unsaved_on_exit=data.get("warn_unsaved_on_exit", False),
            confirm_dangerous=data.get("confirm_dangerous", True),
            ascii_mode=data.get("ascii_mode", False),
            s4_url=data.get("s4_url", cls.s4_url),
            compiler_opt=data.get("compiler_opt", cls.compiler_opt),
            _max_cpus=data.get("max_cpus", 0),
            patch_webif=data.get("patch_webif", True),
            delete_debug=data.get("delete_debug", True),
            save_smargo=data.get("save_smargo", True),
            export_libs_zip=data.get("export_libs_zip", False),
            organize_logs=data.get("organize_logs", False),
            sign_cert=data.get("sign_cert", ""),
            sign_key=data.get("sign_key", ""),
            qemu_test=data.get("qemu_test", False),
            qemu_timeout=data.get("qemu_timeout", 5),
            qemu_expect=data.get("qemu_expect", "Server running"),
            qemu_run_unit_tests=data.get("qemu_run_unit_tests", False),
            web_enabled=data.get("web_enabled", False),
            web_host=data.get("web_host", "0.0.0.0"),
            web_port=data.get("web_port", 8080),
            web_dns_name=data.get("web_dns_name", ""),
            web_ssl=data.get("web_ssl", False),
            web_ssl_cert=data.get("web_ssl_cert", ""),
            web_ssl_key=data.get("web_ssl_key", ""),
            web_auth_user=data.get("web_auth_user", ""),
            web_auth_password=_decrypt(data.get("web_auth_password", ""), label=lambda: f"{t('web_auth_pass_ph')} ({t('settings_title')})", persistent=True),
            mcp_enabled=data.get("mcp_enabled", False),
            mcp_host=data.get("mcp_host", cls.mcp_host),
            mcp_port=data.get("mcp_port", cls.mcp_port),
            mcp_dns_name=data.get("mcp_dns_name", ""),
            mcp_token=_decrypt(data.get("mcp_token", ""), label=lambda: f"MCP token ({t('settings_title')})", persistent=True),
            mcp_allow_actions=data.get("mcp_allow_actions", False),
            mcp_ssl=data.get("mcp_ssl", False),
            mcp_ssl_cert=data.get("mcp_ssl_cert", ""),
            mcp_ssl_key=data.get("mcp_ssl_key", ""),
            # Only honor a stored transport when the user explicitly picked
            # it in the Setup modal. Anything else (including values
            # persisted by a generic Settings.save() that dumped the
            # in-memory default) falls back to the class default — which
            # is "http", the current MCP spec.
            mcp_transport=(
                data["mcp_transport"]
                if (
                    data.get("mcp_transport_explicit")
                    and data.get("mcp_transport") in ("sse", "http")
                )
                else cls.mcp_transport
            ),
            mcp_transport_explicit=bool(data.get("mcp_transport_explicit", False)),
            mcp_root_path=data.get("mcp_root_path", ""),
            default_compress_level=data.get("default_compress_level", cls.default_compress_level),
            default_confdir=data.get("default_confdir", cls.default_confdir),
            default_build_system=data.get("default_build_system", cls.default_build_system),
            default_deploy_user=data.get("default_deploy_user", cls.default_deploy_user),
            default_deploy_port=data.get("default_deploy_port", cls.default_deploy_port),
            default_deploy_path=data.get("default_deploy_path", cls.default_deploy_path),
            ctng_dir=data.get("ctng_dir", ""),
            oesdk_dir=data.get("oesdk_dir", ""),
            andk_dir=data.get("andk_dir", ""),
            log_max_age_days=data.get("log_max_age_days", 30),
            log_max_count=data.get("log_max_count", 5),
            log_max_size_mb=data.get("log_max_size_mb", 0),
        )
