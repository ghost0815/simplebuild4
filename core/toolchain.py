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
    exclude_from_automation: bool = False  # hide from F7 automation dropdowns; skip on "all"

    # Libraries integrated into this toolchain's sysroot. Name → version.
    # Persistent record of "what should be in here" — survives a full
    # rebuild of the toolchain (build-toolchains action) so that the
    # update-libraries automation action can re-install missing libs.
    # Only meaningful on installed toolchains; written to toolchains.local.yaml.
    libraries: dict[str, str] = field(default_factory=dict)

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


def ensure_writable_dirs(root: Path) -> None:
    """Add ``u+rwx`` to every directory under ``root``.

    Several vendor toolchain tarballs ship directories with mode 0555
    (read-only). After extraction, that blocks any later operation
    that needs to add or remove an entry inside such a directory:
    library installs into the sysroot, ``rmtree`` on uninstall, fixup
    of pkg-config files, etc. Files are left untouched so executable
    bits on binaries stay intact.

    No-op when ``root`` doesn't exist or isn't a directory.
    """
    import os, stat
    if not root.is_dir():
        return
    for dirpath, _dirs, _files in os.walk(root):
        try:
            st = os.stat(dirpath).st_mode
            if (st & stat.S_IRWXU) != stat.S_IRWXU:
                os.chmod(dirpath, st | stat.S_IRWXU)
        except OSError:
            # Best effort — don't crash an install over one stubborn
            # entry; ``rmtree_force`` has the same retry logic and
            # will sweep up if needed.
            pass


def rmtree_force(path: Path) -> None:
    """``shutil.rmtree`` that survives read-only directory entries.

    Many vendor toolchain tarballs (crosstool-ng, OE-SDK, simplebuild3
    legacy …) are packed with mode 0555 on every directory, so a plain
    ``rmtree`` fails with ``Permission denied`` because removing an
    inode requires write+execute on its parent dir.

    We attach an ``onerror`` callback that chmods the offending path
    to 0700 and retries — only triggered when needed, so the common
    case (writable tree) costs nothing.
    """
    import os, stat

    def _on_error(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IRWXU)
        except OSError:
            pass
        try:
            func(p)
        except OSError:
            pass

    if not Path(path).exists():
        return
    shutil.rmtree(path, onerror=_on_error)


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


def detect_toolchain_props(install_dir: Path) -> tuple[str, str]:
    """Auto-detect (prefix, sysroot) from a freshly built toolchain dir.

    Walks the install dir for a `*-gcc` binary and asks it for its sysroot
    via `-print-sysroot`. Returns ("", "") if nothing usable is found.
    The sysroot is returned as a path relative to install_dir if possible,
    otherwise the absolute path.
    """
    if not install_dir or not install_dir.is_dir():
        return "", ""
    gcc = None
    prefix = ""
    # Look for a cross gcc binary anywhere under install_dir/**/bin/
    for bd in install_dir.rglob("bin"):
        if not bd.is_dir():
            continue
        try:
            entries = list(bd.iterdir())
        except Exception:
            continue
        for f in entries:
            n = f.name
            if not n.endswith("-gcc"):
                continue
            if n.endswith(".br_real"):
                continue
            stem = n[: -len("-gcc")]
            if "-" not in stem:
                continue
            try:
                if (f.is_file() or f.is_symlink()) and os.access(f, os.X_OK):
                    gcc = f
                    prefix = stem + "-"
                    break
            except Exception:
                continue
        if gcc:
            break
    if not gcc:
        return "", ""
    sysroot_rel = ""
    try:
        sr = subprocess.check_output(
            [str(gcc), "-print-sysroot"], text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
    except Exception:
        sr = ""
    if sr:
        try:
            sysroot_rel = str(Path(sr).resolve().relative_to(install_dir.resolve()))
        except Exception:
            sysroot_rel = sr
    return prefix, sysroot_rel


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


def _parse_pc_libs(pc_file: Path) -> list[str]:
    """Parse Libs: line from .pc file and return library base names (e.g. ['crypto', 'ssl'])."""
    try:
        for line in pc_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Libs:"):
                return [t[2:] for t in line.split() if t.startswith("-l")]
    except Exception:
        pass
    return []


def _parse_pc_include_dir(pc_file: Path) -> str:
    """Parse Cflags: line from .pc and resolve the include directory path."""
    try:
        # First read prefix= and includedir= variables
        variables = {}
        content = pc_file.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            if "=" in line and not line.startswith("Name") and not line.startswith("Description"):
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if not key.startswith("#"):
                    variables[key] = val
        # Resolve ${var} references
        def _resolve(s: str) -> str:
            for k, v in variables.items():
                s = s.replace(f"${{{k}}}", v)
            return s
        for line in content.splitlines():
            if line.startswith("Cflags:"):
                for token in line.split():
                    if token.startswith("-I"):
                        return _resolve(token[2:])
    except Exception:
        pass
    return ""


def remove_library(install_dir: Path, sysroot: str, lib_name: str,
                   libraries_yaml: Path = None, libraries_local: Path = None,
                   log=None) -> bool:
    """Remove an installed library from a toolchain sysroot.

    Parses the .pc file to find library files (.a/.so) and include dirs,
    then deletes them. Returns True if anything was removed.
    """
    import shutil
    import yaml as _yaml

    if log is None:
        log = lambda msg: None

    # Find the library's pc_file name from KNOWN_LIBS
    pc_name = ""
    static_name = ""
    for kl_name, kl_pc, kl_static in KNOWN_LIBS:
        if kl_name == lib_name:
            pc_name = kl_pc
            static_name = kl_static or ""
            break
    if not pc_name:
        log(f"  [red]Unknown library: {lib_name}[/red]")
        return False

    # Load removal config from libraries.yaml if available
    removal_config = {}
    for yaml_path in [libraries_local, libraries_yaml]:
        if yaml_path and yaml_path.exists():
            try:
                data = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                for lib in (data or {}).get("libraries", []):
                    if lib.get("name") == lib_name and "removal" in lib:
                        removal_config = lib["removal"]
                        break
            except Exception:
                pass
            if removal_config:
                break

    removed_count = 0

    def _delete(path: Path, label: str = ""):
        nonlocal removed_count
        try:
            if path.is_dir():
                shutil.rmtree(path)
                log(f"  Removed dir:  {label or path}")
                removed_count += 1
            elif path.exists():
                path.unlink()
                log(f"  Removed file: {label or path}")
                removed_count += 1
        except Exception as e:
            log(f"  [red]Failed to remove {path}: {e}[/red]")

    # Find .pc file(s)
    pc_files = list(install_dir.glob(f"**/pkgconfig/{pc_name}"))
    lib_names = []
    include_dir = ""

    # Parse .pc before deleting
    for pc in pc_files:
        lib_names.extend(_parse_pc_libs(pc))
        if not include_dir:
            include_dir = _parse_pc_include_dir(pc)

    # Delete .pc files
    for pc in pc_files:
        _delete(pc, f"pkgconfig/{pc_name}")
    # Also delete related .pc files (e.g. openssl has libcrypto.pc + libssl.pc + openssl.pc)
    if lib_names:
        for ln in lib_names:
            for extra_pc in install_dir.glob(f"**/pkgconfig/lib{ln}.pc"):
                if extra_pc not in pc_files:
                    _delete(extra_pc, f"pkgconfig/{extra_pc.name}")

    # Delete static libraries (.a)
    if static_name:
        for f in install_dir.glob(f"**/{static_name}"):
            _delete(f, static_name)
    for ln in lib_names:
        for f in install_dir.glob(f"**/lib{ln}.a"):
            _delete(f, f"lib{ln}.a")

    # Delete dynamic libraries (.so*)
    for ln in lib_names:
        for f in install_dir.glob(f"**/lib{ln}.so*"):
            _delete(f, f.name)

    # Delete include directory (from Cflags -I)
    # Safety: never delete a top-level include/ dir (e.g. sysroot/usr/include)
    # — only delete subdirectories like include/openssl/, include/curl/ etc.
    if include_dir:
        inc_path = Path(include_dir)
        if inc_path.is_dir() and str(inc_path).startswith(str(install_dir)):
            # Check if this is a top-level include dir (parent is 'usr' or sysroot root)
            if inc_path.name == "include":
                log(f"  [yellow]Skipped: {inc_path} (top-level include dir)[/yellow]")
            else:
                _delete(inc_path, f"include/{inc_path.name}/")

    # Delete individual include files from removal config
    for inc_file in removal_config.get("include_files", []):
        for f in install_dir.glob(f"**/include/{inc_file}"):
            _delete(f, f"include/{inc_file}")

    # Delete extra directories from removal config
    for extra_dir in removal_config.get("extra_dirs", []):
        d = install_dir / extra_dir
        if d.is_dir():
            _delete(d, extra_dir + "/")

    if removed_count:
        log(f"  [green]Removed {removed_count} file(s)/dir(s)[/green]")
    else:
        log(f"  [yellow]Nothing to remove[/yellow]")
    return removed_count > 0


def check_remove_allowed(lib_name: str, installed_libs: list[LibraryInfo],
                         libraries_yaml: Path,
                         libraries_local: Path = None) -> list[str]:
    """Check if a library can be removed (no installed dependents).

    Returns list of installed library names that depend on lib_name.
    Empty list = removal allowed.
    """
    import yaml as _yaml

    # Load dependency graph from libraries.yaml
    deps_map: dict[str, list[str]] = {}  # lib_name → [depends_on...]
    for yaml_path in [libraries_local, libraries_yaml]:
        if yaml_path and yaml_path.exists():
            try:
                data = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                for lib in (data or {}).get("libraries", []):
                    name = lib.get("name", "")
                    depends = lib.get("depends", [])
                    if name and depends:
                        deps_map[name] = depends
            except Exception:
                pass

    # Find installed libs that depend on the target lib
    installed_names = {lib.name for lib in installed_libs if lib.found}
    dependents = []
    for dep_name, dep_list in deps_map.items():
        if lib_name in dep_list and dep_name in installed_names:
            dependents.append(dep_name)

    return sorted(dependents)


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



def load_library_recipes(libraries_path: Path, local_path: Path | None = None) -> list[dict]:
    """Load library recipes from libraries.yaml, with optional local overrides.

    Local entries with the same 'name' supersede the default entry completely.
    New entries in local are appended.
    """
    if not libraries_path.exists():
        return []
    try:
        data = yaml.safe_load(libraries_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return []
    if not data or "libraries" not in data:
        return []
    recipes = data["libraries"]

    # Merge local overrides
    if local_path and local_path.exists():
        try:
            local_data = yaml.safe_load(local_path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            local_data = None
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

        # Skip pre-release versions for recommendations
        _prerelease = ("alpha", "beta", "preview", "rc", "dev")
        stable = [v for v in available if not any(tag in v.get("version", "").lower() for tag in _prerelease)]

        # Latest available version (first stable, or first overall if all are pre-release)
        lib.latest_version = (stable[0] if stable else available[0]).get("version", "")

        if not lib.found or not lib.version:
            # Not installed — recommend latest stable
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
        self.yaml_error: str = ""

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

        # One-shot migration: back-fill the libraries dict for installed
        # toolchains that don't have it yet. update-libraries automation
        # needs this record to re-install libs after a toolchain rebuild.
        self._backfill_libraries(self._local_tcs)

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
        if self.yaml_error:
            return  # parse error — never overwrite a broken file
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
        """Load toolchains from a single YAML file. Returns empty list on parse errors."""
        if not path.exists():
            return []
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            self.yaml_error = f"{path.name}: {str(e).splitlines()[0]}"
            return []
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
                strip_components=entry.get("strip_components", 0),
                selfbuild=entry.get("selfbuild", False),
                native=entry.get("native", False),
                exclude_from_automation=entry.get("exclude_from_automation", False),
                libraries=(
                    {str(k): str(v) for k, v in entry["libraries"].items()}
                    if isinstance(entry.get("libraries"), dict) else {}
                ),
            )
            if tc.name:
                result.append(tc)
        return result

    def save_local(self, toolchains: list[Toolchain] | None = None) -> None:
        """Save to the local (user) registry file. Refuses to save if parse error detected."""
        if self.yaml_error:
            return  # never overwrite a file we couldn't parse
        tcs = toolchains if toolchains is not None else self._get_local_only()
        self._save_file(self.local_path, tcs)

    def _backfill_libraries(self, local_tcs: list[Toolchain]) -> None:
        """One-shot migration: populate ``libraries`` for installed
        toolchains that have no entry yet, by scanning the sysroot via
        scan_libraries(). Only touches the file when something changed
        and parsing of toolchains.local.yaml succeeded."""
        if self.yaml_error:
            return
        changed = False
        for tc in local_tcs:
            if tc.libraries:
                continue
            if tc.native or not tc.installed:
                continue
            try:
                tc_dir = self.install_dir / tc.name
                libs = scan_libraries(tc_dir, tc.sysroot, native=tc.native)
            except Exception:
                continue
            detected = {lib.name: lib.version for lib in libs if lib.found and lib.version}
            if detected:
                tc.libraries = detected
                changed = True
        if changed:
            try:
                self._save_file(self.local_path, local_tcs)
            except Exception:
                pass

    def record_library(self, toolchain_name: str, lib_name: str, version: str) -> None:
        """Record a lib install/update in toolchains.local.yaml. Persistent
        record used by the update-libraries automation action to re-install
        libraries after a toolchain rebuild."""
        if self.yaml_error:
            return
        local_tcs = self._load_file(self.local_path)
        if self.yaml_error:
            return
        found = False
        for tc in local_tcs:
            if tc.name == toolchain_name:
                tc.libraries[lib_name] = version
                found = True
                break
        if found:
            self._save_file(self.local_path, local_tcs)

    def forget_library(self, toolchain_name: str, lib_name: str) -> None:
        """Remove a lib from the toolchain's persistent libraries record."""
        if self.yaml_error:
            return
        local_tcs = self._load_file(self.local_path)
        if self.yaml_error:
            return
        for tc in local_tcs:
            if tc.name == toolchain_name and lib_name in tc.libraries:
                del tc.libraries[lib_name]
                self._save_file(self.local_path, local_tcs)
                return

    def _save_file(self, path: Path, toolchains: list[Toolchain]) -> None:
        """Save toolchains to a YAML file. Omits empty/default fields."""
        defaults = Toolchain()
        entries = []
        for tc in toolchains:
            entry = {k: v for k, v in {
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
            entry["exclude_from_automation"] = tc.exclude_from_automation  # always write
            if tc.libraries:
                entry["libraries"] = dict(tc.libraries)
            entries.append(entry)
        data = {"toolchains": entries}
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

    def rescan_installed(self, log=None) -> int:
        """Rebuild toolchains.local.yaml from installed directories.

        For each directory in install_dir:
        - If defined in defaults: copy all fields from defaults
        - If not in defaults: auto-detect prefix/sysroot, set category=custom
        - Native toolchain is handled separately by _ensure_native()

        Returns number of recovered toolchains.
        """
        if log is None:
            log = lambda msg: None

        defaults_by_name = {tc.name: tc for tc in self._load_file(self.defaults_path)}
        existing_local = {tc.name: tc for tc in self._load_file(self.local_path)}
        recovered = []

        if not self.install_dir.is_dir():
            return 0

        for d in sorted(self.install_dir.iterdir()):
            if not d.is_dir() or d.name == "native":
                continue
            name = d.name
            # Already in local yaml
            if name in existing_local:
                continue

            if name in defaults_by_name:
                # Known toolchain from defaults — copy all fields
                tc = defaults_by_name[name]
                # Verify prefix/sysroot against actual installation
                # (defaults may be outdated or toolchain may have been rebuilt)
                actual_prefix, actual_sysroot = detect_toolchain_props(d)
                if actual_prefix and actual_prefix != tc.prefix:
                    log(f"  [yellow]  prefix: {tc.prefix} → {actual_prefix}[/yellow]")
                    tc.prefix = actual_prefix
                if actual_sysroot and actual_sysroot != tc.sysroot:
                    log(f"  [yellow]  sysroot: {tc.sysroot} → {actual_sysroot}[/yellow]")
                    tc.sysroot = actual_sysroot
                tc.installed = True
                recovered.append(tc)
                log(f"  [green]Restored from defaults:[/green] {name} ({tc.category})")
            else:
                # Unknown toolchain — auto-detect prefix/sysroot
                prefix, sysroot = detect_toolchain_props(d)
                if not prefix:
                    log(f"  [yellow]Skipped (no gcc found):[/yellow] {name}")
                    continue
                # Derive arch from prefix
                arch = ""
                _prefix_lower = prefix.lower()
                for _arch_key in ("aarch64", "armv7", "arm", "mipsel", "mips", "sh4", "csky", "x86_64", "i686"):
                    if _arch_key in _prefix_lower:
                        arch = _arch_key
                        break
                tc = Toolchain(
                    name=name,
                    description=f"Recovered: {name}",
                    category="local",
                    arch=arch,
                    prefix=prefix,
                    sysroot=sysroot,
                )
                tc.installed = True
                recovered.append(tc)
                log(f"  [cyan]Auto-detected:[/cyan] {name} (prefix={prefix}, arch={arch})")

        if not recovered:
            log("  No toolchains to recover")
            return 0

        # Merge with existing local entries and save
        local_tcs = self._load_file(self.local_path)
        local_tcs.extend(recovered)
        self.save_local(local_tcs)
        log(f"  [bold green]Recovered {len(recovered)} toolchain(s)[/bold green]")
        return len(recovered)

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
