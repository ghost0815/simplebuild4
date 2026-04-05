"""Toolchain registry and management.

All known toolchains are defined in a single YAML file (data/toolchains.yaml).
Installed toolchains live in data/toolchains/<name>/.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
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
    category: str = ""             # common, stb, android, openwrt, router, nas, minipc, tv, legacy, custom
    arch: str = ""
    machines: str = ""

    # Compiler
    prefix: str = ""               # Compiler prefix (e.g. aarch64-oe-linux-)
    sysroot: str = ""              # Sysroot path relative to toolchain dir

    # Build defaults (copied to target on assignment)
    cc_opts: str = ""              # Compiler options → CC_OPTS
    cflags: str = ""               # Architecture C flags → EXTRA_CFLAGS
    ldflags: str = ""              # Linker flags → EXTRA_LDFLAGS
    make_vars: str = ""            # Raw make variable assignments

    # STAPI (empty = not supported)
    stapi_lib: str = ""            # Custom STAPI lib name, "default" = liboscam_stapi.a

    # Download / Install
    url: str = ""                  # Download URL
    checksum: str = ""             # Format: "md5:hash" or "sha256:hash"
    strip_components: int = 0      # tar --strip-components (0 = most STB toolchains)
    selfbuild: bool = False        # True if toolchain was built locally (not downloaded)
    native: bool = False           # True if this is the host system compiler

    # Runtime state (not persisted, set at load time)
    installed: bool = False

    def build_defaults(self) -> dict[str, str]:
        """Return the build defaults that get copied into a target."""
        return {
            "cc_opts": self.cc_opts,
            "cflags": self.cflags,
            "ldflags": self.ldflags,
            "make_vars": self.make_vars,
            "stapi_lib": self.stapi_lib,
        }


# ---------------------------------------------------------------------------
# Library detection via pkg-config
# ---------------------------------------------------------------------------

# Libraries we care about: (display_name, pc_file, static_lib)
KNOWN_LIBS = [
    ("OpenSSL",    "openssl.pc",       "libcrypto.a"),
    ("libcurl",    "libcurl.pc",       "libcurl.a"),
    ("libusb",     "libusb-1.0.pc",    "libusb-1.0.a"),
    ("pcsclite",   "libpcsclite.pc",   "libpcsclite.a"),
    ("ccid",       "libccid.pc",       None),
    ("libdvbcsa",  "libdvbcsa.pc",     "libdvbcsa.a"),
    ("zlib",       "zlib.pc",          None),
]


@dataclass
class LibraryInfo:
    """Detected library in a toolchain sysroot."""

    name: str = ""
    version: str = ""
    pc_file: str = ""        # path to .pc file
    static_lib: str = ""     # path to .a file (empty = not found)
    dynamic_lib: str = ""    # path to .so file (empty = not found)
    found: bool = False
    # Update info (filled by check_library_updates)
    update_version: str = ""     # recommended update within same branch
    latest_version: str = ""     # newest available version overall
    up_to_date: bool = False


def scan_libraries(install_dir: Path, sysroot: str, native: bool = False) -> list[LibraryInfo]:
    """Scan a toolchain's sysroot for known libraries via pkg-config files.

    Args:
        install_dir: Path to installed toolchain (e.g. data/toolchains/armv7_universal)
        sysroot: Sysroot relative path from toolchain config
        native: If True, use system pkg-config to detect libraries

    Returns:
        List of LibraryInfo for each known library (found or not).
    """
    results = []

    if native:
        # Native: first scan system libs, then overlay with toolchain-local builds
        sys_map: dict[str, LibraryInfo] = {}
        for lib_name, pc_name, static_name in KNOWN_LIBS:
            info = LibraryInfo(name=lib_name)
            mod_name = pc_name.replace(".pc", "")
            try:
                version = subprocess.check_output(
                    ["pkg-config", "--modversion", mod_name],
                    text=True, stderr=subprocess.DEVNULL, timeout=5,
                ).strip()
                if version:
                    info.found = True
                    info.version = version
                    info.pc_file = "system"  # marker: from system
            except Exception:
                pass
            sys_map[lib_name] = info

        # Now scan toolchain-local builds (data/toolchains/native/**/pkgconfig)
        local_pc_dirs = list(install_dir.glob("**/pkgconfig"))
        local_pc_map: dict[str, Path] = {}
        for pc_dir in local_pc_dirs:
            for pc_file in pc_dir.glob("*.pc"):
                local_pc_map[pc_file.name] = pc_file

        for lib_name, pc_name, static_name in KNOWN_LIBS:
            if pc_name in local_pc_map:
                local_ver = _read_pc_version(local_pc_map[pc_name])
                if local_ver:
                    info = sys_map[lib_name]
                    info.found = True
                    info.version = local_ver
                    info.pc_file = str(local_pc_map[pc_name])

        return list(sys_map.values())

    # Find pkgconfig directories in the sysroot
    sysroot_path = install_dir / sysroot if sysroot else install_dir
    pc_dirs = list(install_dir.glob("**/pkgconfig"))

    # Build a map of available .pc files
    pc_map: dict[str, Path] = {}
    for pc_dir in pc_dirs:
        for pc_file in pc_dir.glob("*.pc"):
            pc_map[pc_file.name] = pc_file

    for lib_name, pc_name, static_name in KNOWN_LIBS:
        info = LibraryInfo(name=lib_name)

        # Check .pc file (also check legacy alternatives like libusb.pc)
        if pc_name in pc_map:
            info.found = True
            info.pc_file = str(pc_map[pc_name])
            info.version = _read_pc_version(pc_map[pc_name])
        elif lib_name == "libusb" and "libusb.pc" in pc_map:
            # Fallback: libusb 0.1 installed
            info.found = True
            info.pc_file = str(pc_map["libusb.pc"])
            info.version = _read_pc_version(pc_map["libusb.pc"])

        # Check static library
        if static_name:
            static_files = list(install_dir.glob(f"**/{static_name}"))
            if static_files:
                info.static_lib = str(static_files[0])
                if not info.found:
                    info.found = True

        # Check dynamic library (derive .so name from .a name or pc name)
        so_name = ""
        if static_name:
            so_name = static_name.replace(".a", ".so")
        elif pc_name:
            so_name = "lib" + pc_name.replace(".pc", ".so")
        if so_name:
            so_files = list(install_dir.glob(f"**/{so_name}*"))
            if so_files:
                info.dynamic_lib = str(so_files[0])
                if not info.found:
                    info.found = True

        results.append(info)

    return results


def _read_pc_version(pc_file: Path) -> str:
    """Read Version: field from a .pc file."""
    try:
        for line in pc_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


def _get_version_branch(version: str) -> str:
    """Extract the version branch for update comparison.

    OpenSSL has special branching:
      3.5.5 → "3.5", 3.0.19 → "3.0"
      1.1.1w → "1.1.1", 1.1.0l → "1.1.0"
      1.0.2u → "1.0.2", 1.0.0t → "1.0.0"
      0.9.8zh → "0.9.8"
    Other libs use major.minor:
      1.0.29 → "1.0", 8.19.0 → "8"
    """
    if not version:
        return ""
    parts = version.split(".")
    if len(parts) >= 3:
        # Check if 3rd part starts with a digit (e.g. "3.5.5") vs letter (e.g. "1.1.1w")
        third = parts[2]
        if third and third[0].isdigit():
            # Normal versioning: branch = major.minor (e.g. 3.5, 1.0)
            return f"{parts[0]}.{parts[1]}"
        else:
            # OpenSSL legacy: branch = major.minor.patch_prefix (e.g. 1.1.1, 1.0.2)
            return f"{parts[0]}.{parts[1]}.{parts[2].rstrip('abcdefghijklmnopqrstuvwxyz')}"
    elif len(parts) == 2:
        return parts[0]
    return version


def _version_tuple(version: str) -> tuple:
    """Convert version string to comparable tuple."""
    import re
    parts = re.split(r'[.\-]', version)
    result = []
    for p in parts:
        m = re.match(r'^(\d+)(.*)', p)
        if m:
            result.append((int(m.group(1)), m.group(2)))
        else:
            result.append((0, p))
    return tuple(result)


def load_library_recipes(libraries_path: Path, local_path: Path | None = None) -> list[dict]:
    """Load library recipes from libraries.yaml, with optional local overrides.

    Local entries with the same 'name' supersede the default entry completely.
    New entries in local are appended.
    """
    if not libraries_path.exists():
        return []
    data = yaml.safe_load(libraries_path.read_text(encoding="utf-8"))
    if not data or "libraries" not in data:
        return []
    recipes = data["libraries"]

    # Merge local overrides
    if local_path and local_path.exists():
        local_data = yaml.safe_load(local_path.read_text(encoding="utf-8"))
        if local_data and "libraries" in local_data:
            local_by_name = {lib["name"]: lib for lib in local_data["libraries"]}
            # Replace defaults with local overrides
            merged = []
            seen = set()
            for lib in recipes:
                name = lib["name"]
                if name in local_by_name:
                    merged.append(local_by_name[name])
                else:
                    merged.append(lib)
                seen.add(name)
            # Append new local-only entries
            for lib in local_data["libraries"]:
                if lib["name"] not in seen:
                    merged.append(lib)
            recipes = merged

    return recipes


def check_library_updates(
    libs: list[LibraryInfo],
    recipes: list[dict],
    has_c11: bool = True,
) -> list[LibraryInfo]:
    """Enrich LibraryInfo with update recommendations.

    Args:
        libs: Scanned libraries from scan_libraries()
        recipes: Loaded from libraries.yaml
        has_c11: Whether the toolchain supports C11

    Returns:
        Same list with update_version, latest_version, up_to_date filled in.
    """
    # Build recipe lookup by name
    recipe_map: dict[str, dict] = {}
    for r in recipes:
        recipe_map[r["name"]] = r

    for lib in libs:
        recipe = recipe_map.get(lib.name)
        if not recipe:
            continue

        variants = recipe.get("variants", [])
        if not variants:
            continue

        # Filter variants by C11 capability
        available = []
        for v in variants:
            if v.get("requires") == "c11" and not has_c11:
                continue
            available.append(v)

        if not available:
            continue

        # Latest available version (first in list after filtering)
        lib.latest_version = available[0].get("version", "")

        if not lib.found or not lib.version:
            # Not installed — recommend latest
            lib.update_version = lib.latest_version
            continue

        # Check if installed version matches a variant
        exact_match = False
        has_auto = any(v.get("version") == "auto" for v in available)
        for v in available:
            v_version = v.get("version", "")
            if v_version == lib.version:
                exact_match = True
                break

        # For "auto" variants: installed "1.1.0-128-neon" or "1.1.0-128" matches
        # if it starts with any variant's base version (e.g. "1.1.0-")
        if not exact_match and has_auto and "-" in lib.version:
            for v in available:
                v_version = v.get("version", "")
                if v_version not in ("auto", "master") and "-" in v_version:
                    variant_base = v_version.split("-")[0]  # "1.1.0-64" → "1.1.0"
                    if lib.version.startswith(variant_base + "-"):
                        exact_match = True
                        break

        # For "auto" variants: plain version like "1.1.0" (no batchsize) should
        # recommend "auto" to get proper bitslice optimization
        if not exact_match and has_auto and "-" not in lib.version:
            for v in available:
                v_version = v.get("version", "")
                if v_version not in ("auto", "master") and v_version.startswith(lib.version + "-"):
                    # Installed "1.1.0" matches base of "1.1.0-64" → recommend auto
                    lib.update_version = "auto"
                    lib.latest_version = "auto"
                    lib.up_to_date = False
                    break
            continue

        if exact_match:
            lib.update_version = lib.version
            lib.up_to_date = True
            continue

        # Find update within same branch
        installed_branch = _get_version_branch(lib.version)

        best_in_branch = ""
        for v in available:
            v_version = v.get("version", "")
            if v_version in ("auto", "master"):
                continue
            v_branch = _get_version_branch(v_version)
            if v_branch == installed_branch:
                if not best_in_branch:
                    best_in_branch = v_version

        if best_in_branch:
            lib.update_version = best_in_branch
            lib.up_to_date = (lib.version == best_in_branch)
        else:
            # No match in same branch — suggest latest
            lib.update_version = lib.latest_version
            lib.up_to_date = (lib.version == lib.latest_version)

    return libs


class ToolchainRegistry:
    """Manages the toolchain registry and installed toolchains.

    Two independent sources:
    - defaults_path: PreBuild catalog (defaults/toolchains.yaml) — browse & install
    - local_path: installed/imported toolchains (data/toolchains.local.yaml) — single source of truth

    Install copies an entry from defaults → local. Installed = in local + dir exists.
    """

    def __init__(self, defaults_path: Path, local_path: Path, install_dir: Path):
        self.defaults_path = defaults_path
        self.local_path = local_path
        self.install_dir = install_dir
        self._toolchains: list[Toolchain] = []

    def load(self) -> None:
        """Load toolchain registry from both YAML files."""
        # Ensure native toolchain exists in local file
        self._ensure_native()

        # Load local = installed/imported toolchains (single source of truth)
        self._local_tcs = self._load_file(self.local_path)
        local_names = {tc.name for tc in self._local_tcs}

        for tc in self._local_tcs:
            if tc.native:
                tc.installed = shutil.which("gcc") is not None
            else:
                tc.installed = (self.install_dir / tc.name).is_dir()

        # Load defaults = PreBuild catalog
        self._default_tcs = self._load_file(self.defaults_path)
        for tc in self._default_tcs:
            # Mark as installed if same name exists in local and dir exists
            tc.installed = tc.name in local_names and (self.install_dir / tc.name).is_dir()

        # Combined list: local entries override defaults with same name
        by_name: dict[str, Toolchain] = {}
        for tc in self._default_tcs:
            by_name[tc.name] = tc
        for tc in self._local_tcs:
            by_name[tc.name] = tc  # local overrides defaults for installed config
        self._toolchains = sorted(by_name.values(), key=lambda t: t.name)

    def _ensure_native(self) -> None:
        """Auto-detect and register the native (host) toolchain in local yaml."""
        # Check if native already exists in local file AND directory exists
        local_tcs = self._load_file(self.local_path)
        native_dir = self.install_dir / "native" / "bin"
        has_entry = any(tc.native for tc in local_tcs)
        has_dir = native_dir.is_dir() and any(native_dir.iterdir())
        if has_entry and has_dir:
            return
        # Remove stale native entry if dir is missing
        if has_entry and not has_dir:
            local_tcs = [tc for tc in local_tcs if not tc.native]

        # Detect system gcc
        gcc = shutil.which("gcc")
        if not gcc:
            return

        try:
            dumpmachine = subprocess.check_output(
                [gcc, "-dumpmachine"], text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
        except Exception:
            return

        try:
            machine = platform.machine()  # e.g. x86_64, aarch64
        except Exception:
            machine = "unknown"

        try:
            hostname = socket.gethostname().split(".")[0]
        except Exception:
            hostname = "host"

        prefix = f"{dumpmachine}-" if dumpmachine else ""

        native_tc = Toolchain(
            name="native",
            description=f"Native ({hostname}-{machine})",
            category="native",
            arch=machine,
            prefix=prefix,
            sysroot="",
            native=True,
        )

        # Setup native toolchain directory with symlinks
        native_dir = self.install_dir / "native" / "bin"
        native_dir.mkdir(parents=True, exist_ok=True)

        # Create symlinks: prefix-gcc → system gcc, etc.
        for tool in ("gcc", "g++", "strip", "ar", "ranlib", "objcopy", "objdump", "nm", "ld"):
            sys_tool = shutil.which(tool)
            if sys_tool:
                for name in (f"{prefix}{tool}", tool):
                    dest = native_dir / name
                    if not dest.exists():
                        try:
                            dest.symlink_to(sys_tool)
                        except Exception:
                            pass

        # Save to local yaml
        local_tcs.append(native_tc)
        self.save_local(local_tcs)

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
                category=entry.get("category", ""),
                arch=entry.get("arch", ""),
                machines=entry.get("machines", ""),
                prefix=entry.get("prefix", ""),
                sysroot=entry.get("sysroot", ""),
                cc_opts=entry.get("cc_opts", ""),
                cflags=entry.get("cflags", ""),
                ldflags=entry.get("ldflags", ""),
                make_vars=entry.get("make_vars", ""),
                stapi_lib=entry.get("stapi_lib", ""),
                url=entry.get("url", ""),
                checksum=entry.get("checksum", ""),
                strip_components=entry.get("strip_components", 1),
                selfbuild=entry.get("selfbuild", False),
                native=entry.get("native", False),
            )
            if tc.name:
                result.append(tc)
        return result

    def save_local(self, toolchains: list[Toolchain] | None = None) -> None:
        """Save to the local (user) registry file."""
        tcs = toolchains if toolchains is not None else self._get_local_only()
        self._save_file(self.local_path, tcs)

    def _save_file(self, path: Path, toolchains: list[Toolchain]) -> None:
        """Save toolchains to a YAML file. Omits empty/default fields."""
        defaults = Toolchain()
        data = {
            "toolchains": [
                {k: v for k, v in {
                    "name": tc.name,
                    "description": tc.description,
                    "category": tc.category,
                    "arch": tc.arch,
                    "machines": tc.machines,
                    "prefix": tc.prefix,
                    "sysroot": tc.sysroot,
                    "cc_opts": tc.cc_opts,
                    "cflags": tc.cflags,
                    "ldflags": tc.ldflags,
                    "make_vars": tc.make_vars,
                    "stapi_lib": tc.stapi_lib,
                    "url": tc.url,
                    "checksum": tc.checksum,
                    "strip_components": tc.strip_components,
                    "selfbuild": tc.selfbuild,
                    "native": tc.native,
                }.items() if v != getattr(defaults, k)}
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

    def get_from_defaults(self, name: str) -> Optional[Toolchain]:
        """Get a toolchain from the defaults catalog (not local)."""
        for tc in self._load_file(self.defaults_path):
            if tc.name == name:
                return tc
        return None

    def is_local(self, name: str) -> bool:
        """Check if a toolchain is in the local yaml."""
        return any(tc.name == name for tc in self._load_file(self.local_path))

    def installed_toolchains(self) -> list[Toolchain]:
        """Return only installed toolchains (from local yaml), native first."""
        installed = [tc for tc in self._local_tcs if tc.installed]
        return sorted(installed, key=lambda t: (not t.native, t.name))

    def catalog_toolchains(self) -> list[Toolchain]:
        """Return the PreBuild catalog. Local entries override defaults with same name."""
        local_by_name = {tc.name: tc for tc in self._local_tcs}
        result = []
        for tc in self._default_tcs:
            if tc.name in local_by_name:
                result.append(local_by_name[tc.name])  # local supersedes defaults
            else:
                result.append(tc)
        return result

    def local_toolchains(self) -> list[Toolchain]:
        """Return toolchains from local yaml that don't exist in defaults."""
        default_names = {tc.name for tc in self._default_tcs}
        return [tc for tc in self._local_tcs if tc.name not in default_names]

    def available_toolchains(self) -> list[Toolchain]:
        """Return toolchains that are not yet installed but have a download URL."""
        return [tc for tc in self._default_tcs if not tc.installed and tc.url]

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
