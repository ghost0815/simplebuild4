"""Export shared libraries to a per-toolchain ZIP file.

After each library build, copies the .so files into a ZIP archive
at data/binaries/<toolchain>-libs.zip. The ZIP contains one directory
per library (e.g. OpenSSL-3.6.1/, zlib-1.3.2/) with the shared
library files. Updated incrementally — existing entries for the
same library are replaced.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

def _resolve_lib_config(lib_name: str, libraries_yaml: Path,
                        libraries_local: Optional[Path] = None) -> tuple[str, list[str]]:
    """Look up pc_file and export extra_files for a library from libraries.yaml.

    Returns (pc_file, extra_files_patterns).
    """
    import yaml
    for yaml_path in [libraries_local, libraries_yaml]:
        if yaml_path and yaml_path.exists():
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                for lib in (data or {}).get("libraries", []):
                    if lib.get("name") == lib_name:
                        pc = lib.get("pc_file", "")
                        extra = lib.get("export", {}).get("extra_files", [])
                        return pc, extra
            except Exception:
                pass
    return "", []


def export_lib_to_zip(
    lib_name: str,
    toolchain_dir: Path,
    toolchain_sysroot: str,
    binaries_dir: Path,
    tc_name: str,
    libraries_yaml: Optional[Path] = None,
    libraries_local: Optional[Path] = None,
    log: Optional[Callable[[str], None]] = None,
) -> bool:
    """Export a library's .so files to <tc_name>-libs.zip.

    Finds the library's .so files in the toolchain sysroot via the .pc file
    and adds them to the ZIP under a <LibName>-<Version>/ directory.

    Returns True if export succeeded.
    """
    if log is None:
        log = lambda msg: None

    # Look up pc_file and export config from libraries.yaml (not hardcoded)
    pc_name, extra_patterns = _resolve_lib_config(lib_name, libraries_yaml, libraries_local) if libraries_yaml else ("", [])
    if not pc_name:
        return False

    # Find and read the .pc file to get version
    from core.toolchain import _read_pc_version, _parse_pc_libs
    pc_files = list(toolchain_dir.glob(f"**/pkgconfig/{pc_name}"))
    if not pc_files:
        return False

    version = _read_pc_version(pc_files[0])
    if not version:
        return False

    dir_name = f"{lib_name}-{version}"
    zip_path = (binaries_dir / f"{tc_name}-libs.zip").resolve()

    # Find .so files via .pc Libs: line
    # Also follow Requires: for sub-packages (e.g. openssl.pc → libssl + libcrypto)
    sysroot_path = toolchain_dir / toolchain_sysroot if toolchain_sysroot else toolchain_dir
    lib_names = []
    for pc in pc_files:
        lib_names.extend(_parse_pc_libs(pc))
        try:
            for line in pc.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Requires:"):
                    for req in line.split(":", 1)[1].split():
                        req = req.strip()
                        if req and not req.startswith("$"):
                            for rpc in toolchain_dir.glob(f"**/pkgconfig/{req}.pc"):
                                lib_names.extend(_parse_pc_libs(rpc))
        except Exception:
            pass

    # Fallback: derive from pc_file name
    if not lib_names:
        lib_names.append(pc_name.replace(".pc", "").replace("lib", ""))

    # Collect all .so files
    so_files = []
    for ln in lib_names:
        so_files.extend(sysroot_path.rglob(f"lib{ln}.so*"))

    # Collect extra files from export config (e.g. pcsc_scan, Info.plist)
    for pattern in extra_patterns:
        so_files.extend(sysroot_path.rglob(pattern))

    if not so_files:
        log(f"  [dim]No .so files found for {lib_name} — skipping zip export[/dim]")
        return False

    # Create temp directory with the library files
    with tempfile.TemporaryDirectory(prefix="s4-libexport-") as tmp:
        lib_dir = Path(tmp) / dir_name
        lib_dir.mkdir()
        import os, shutil
        for so in so_files:
            dest = lib_dir / so.name
            if so.is_symlink():
                # Preserve relative symlinks
                link_target = os.readlink(so)
                os.symlink(link_target, dest)
            elif so.is_file():
                shutil.copy2(str(so), str(dest))

        # Add to zip (update mode)
        binaries_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["zip", "-9yr", str(zip_path), dir_name],
            cwd=tmp, capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            size_kb = zip_path.stat().st_size // 1024
            log(f"  [dim]Exported {dir_name} → {zip_path.name} ({size_kb} KB)[/dim]")
            return True
        else:
            err = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            log(f"  [yellow]Zip export failed: {err}[/yellow]")
            return False
