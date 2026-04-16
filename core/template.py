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
    category: str = ""             # common, stb, android, openwrt, …

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
    exclude_from_automation: bool = False

    # Internal — set when loaded
    yaml_path: Path | None = None
    is_local: bool = False         # True if from data/templates/ instead of defaults/

    @property
    def config_path(self) -> Path | None:
        """Absolute path to the builder's native config file."""
        if not self.config or not self.yaml_path:
            return None
        return self.yaml_path.parent / self.config


# ---------------------------------------------------------------------------
# OE-SDK config (.config as KEY=value pairs)
# ---------------------------------------------------------------------------

@dataclass
class OeSdkConfig:
    """Parsed OE-SDK builder configuration from a .config file."""

    machine: str = ""          # OESDK_MACHINE — representative build machine
    distro: str = ""           # OESDK_DISTRO — openatv, opendreambox, openpli
    repo_url: str = ""         # OESDK_REPO_URL — git clone URL
    repo_branch: str = ""      # OESDK_REPO_BRANCH — git branch (empty = must set via F6)
    build_dir: str = ""        # OESDK_BUILD_DIR — OE build environment location
    sdk_extras: str = ""       # OESDK_SDK_EXTRAS — extra packages for the SDK

    # Mapping between config file keys and dataclass fields.
    _KEY_MAP: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self):
        self._KEY_MAP = {
            "OESDK_MACHINE": "machine",
            "OESDK_DISTRO": "distro",
            "OESDK_REPO_URL": "repo_url",
            "OESDK_REPO_BRANCH": "repo_branch",
            "OESDK_BUILD_DIR": "build_dir",
            "OESDK_SDK_EXTRAS": "sdk_extras",
        }


def load_oesdk_config(config_path: Path) -> OeSdkConfig:
    """Parse an OE-SDK .config file (KEY=value, one per line).

    Lines starting with ``#`` are comments (preserved by save but not
    loaded as data). Quoting around values is stripped.
    """
    cfg = OeSdkConfig()
    if not config_path.exists():
        return cfg
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        attr = cfg._KEY_MAP.get(key)
        if attr:
            setattr(cfg, attr, value)
    return cfg


def save_oesdk_config(config_path: Path, cfg: OeSdkConfig, description: str = "") -> None:
    """Write an OE-SDK .config file.

    Preserves the header comment format used by both crosstool-ng and
    OE-SDK templates so the toolchain builder screen can display a
    consistent version/description.
    """
    lines = []
    if description:
        lines.append(f"#toolchain template: {description}")
    # Carry over the version line from an existing file if present
    if config_path.exists():
        for existing in config_path.read_text(encoding="utf-8").splitlines():
            if existing.startswith("#toolchain template version:"):
                lines.append(existing)
                break
        else:
            lines.append("#toolchain template version: 1")
    else:
        lines.append("#toolchain template version: 1")
    # Write data keys in a stable order
    inv = {v: k for k, v in cfg._KEY_MAP.items()}
    for attr in ("machine", "distro", "repo_url", "repo_branch", "build_dir", "sdk_extras"):
        key = inv.get(attr, attr.upper())
        lines.append(f"{key}={getattr(cfg, attr, '')}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Android NDK config (.config as KEY=value pairs)
# ---------------------------------------------------------------------------

# Arch → derived properties (host triplet, ABI, clang binary prefix)
ANDK_ARCH_MAP = {
    "arm":     {"host": "arm-linux-androideabi",  "abi": "armeabi-v7a",
                "clang_prefix": "armv7a-linux-androideabi",
                "prefix": "arm-linux-androideabi-"},
    "aarch64": {"host": "aarch64-linux-android",  "abi": "arm64-v8a",
                "clang_prefix": "aarch64-linux-android",
                "prefix": "aarch64-linux-android-"},
    "x86":     {"host": "i686-linux-android",     "abi": "x86",
                "clang_prefix": "i686-linux-android",
                "prefix": "i686-linux-android-"},
    "x86_64":  {"host": "x86_64-linux-android",   "abi": "x86_64",
                "clang_prefix": "x86_64-linux-android",
                "prefix": "x86_64-linux-android-"},
}

# Known NDK versions for the F6 dropdown
ANDK_KNOWN_VERSIONS = [
    ("r29 (latest)", "r29"),
    ("r28", "r28"),
    ("r27d (LTS)", "r27d"),
    ("r27c", "r27c"),
    ("r26d", "r26d"),
    ("r25c", "r25c"),
]

# API levels with Android version names
ANDK_API_LEVELS = [
    ("35 — Android 15", "35"),
    ("34 — Android 14", "34"),
    ("33 — Android 13", "33"),
    ("32 — Android 12L", "32"),
    ("31 — Android 12", "31"),
    ("30 — Android 11", "30"),
    ("29 — Android 10", "29"),
    ("28 — Android 9 Pie", "28"),
    ("27 — Android 8.1 Oreo", "27"),
    ("26 — Android 8.0 Oreo", "26"),
    ("25 — Android 7.1 Nougat", "25"),
    ("24 — Android 7.0 Nougat", "24"),
    ("23 — Android 6.0 Marshmallow", "23"),
    ("22 — Android 5.1 Lollipop", "22"),
    ("21 — Android 5.0 Lollipop", "21"),
]


@dataclass
class AndroidNdkConfig:
    """Parsed Android NDK builder configuration from a .config file."""

    api: str = ""            # ANDK_API — Android API level (21-35)
    arch: str = ""           # ANDK_ARCH — arm, aarch64, x86, x86_64
    ndk_version: str = ""    # ANDK_NDK_VERSION — r29, r27d, etc.

    _KEY_MAP: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self):
        self._KEY_MAP = {
            "ANDK_API": "api",
            "ANDK_ARCH": "arch",
            "ANDK_NDK_VERSION": "ndk_version",
        }

    @property
    def host(self) -> str:
        return ANDK_ARCH_MAP.get(self.arch, {}).get("host", "")

    @property
    def abi(self) -> str:
        return ANDK_ARCH_MAP.get(self.arch, {}).get("abi", "")

    @property
    def clang_prefix(self) -> str:
        return ANDK_ARCH_MAP.get(self.arch, {}).get("clang_prefix", "")

    @property
    def prefix(self) -> str:
        return ANDK_ARCH_MAP.get(self.arch, {}).get("prefix", "")

    @property
    def download_url(self) -> str:
        if not self.ndk_version:
            return ""
        return f"https://dl.google.com/android/repository/android-ndk-{self.ndk_version}-linux.zip"


def load_andk_config(config_path: Path) -> AndroidNdkConfig:
    """Parse an Android NDK .config file (KEY=value, one per line)."""
    cfg = AndroidNdkConfig()
    if not config_path.exists():
        return cfg
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        attr = cfg._KEY_MAP.get(key)
        if attr:
            setattr(cfg, attr, value)
    return cfg


def save_andk_config(config_path: Path, cfg: AndroidNdkConfig, description: str = "") -> None:
    """Write an Android NDK .config file."""
    lines = []
    if description:
        lines.append(f"#toolchain template: {description}")
    if config_path.exists():
        for existing in config_path.read_text(encoding="utf-8").splitlines():
            if existing.startswith("#toolchain template version:"):
                lines.append(existing)
                break
        else:
            lines.append("#toolchain template version: 1")
    else:
        lines.append("#toolchain template version: 1")
    inv = {v: k for k, v in cfg._KEY_MAP.items()}
    for attr in ("api", "arch", "ndk_version"):
        key = inv.get(attr, attr.upper())
        lines.append(f"{key}={getattr(cfg, attr, '')}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
        category=data.get("category", ""),
        prefix=data.get("prefix", ""),
        sysroot=data.get("sysroot", ""),
        cflags=data.get("cflags", ""),
        ldflags=data.get("ldflags", ""),
        machines=data.get("machines", ""),
        config=data.get("config", ""),
        download_location=data.get("download_location", ""),
        exclude_from_automation=bool(data.get("exclude_from_automation", False)),
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
