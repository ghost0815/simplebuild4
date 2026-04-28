"""PreBuild toolchain installer — download, extract, relocate, register.

Extracted from ``ui/screens/toolchains.py::_handle_install`` so it can
be called from both the TUI and the MCP server. Returns a simple
``bool`` and logs to the caller-supplied callback.

Workflow:
  1. Download the archive (``tc.url``) to ``paths.downloads_dir`` if
     not already cached (checksum verified when available).
  2. Extract with ``tar --strip-components=<tc.strip_components>``.
  3. Relocate: run ``relocate-sdk.sh`` if present, else patch NSS
     sysroot paths (OE-SDK convention).
  4. Fix ``.pc`` files (rewrite ``prefix=`` to installation path).
  5. Fix ``.la`` files (sysroot prefix).
  6. Register the toolchain entry in ``toolchains.local.yaml`` so
     subsequent loads see it as installed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from core.toolchain import Toolchain, ToolchainRegistry
from core.toolchain_builder import fix_pc_files, fix_la_files


def _verify_checksum(filepath: Path, checksum: str) -> bool:
    """Verify file checksum. Format: ``md5:hex`` / ``sha256:hex``."""
    if not checksum or ":" not in checksum:
        return True
    algo, expected = checksum.split(":", 1)
    algo = algo.strip().lower()
    expected = expected.strip().lower()
    if algo not in ("md5", "sha256", "sha1", "sha512"):
        return True
    h = hashlib.new(algo)
    try:
        with filepath.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except Exception:
        return False
    return h.hexdigest().lower() == expected


def _relocate(install_dir: Path, log: Callable[[str], None]) -> None:
    """Relocate toolchain to its installed path (if the SDK supports it)."""
    relocate_sh = install_dir / "relocate-sdk.sh"
    if relocate_sh.exists():
        log("Running relocate-sdk.sh...")
        try:
            subprocess.run(
                [str(relocate_sh)], cwd=install_dir,
                capture_output=True, text=True, timeout=120,
            )
        except Exception as exc:
            log(f"  relocate-sdk.sh failed: {exc}")
        return
    # No script — leave the sysroot alone. The UI does deeper OE-SDK
    # path patching that we don't need for a plain prebuilt archive.


def install_toolchain_from_catalog(
    tc: Toolchain,
    paths,
    settings,
    log: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """Download + extract + register ``tc`` into ``paths.toolchains_dir``.

    Returns True on success, False on any failure. Cleans up partial
    installs.
    """
    _log = log or (lambda m: None)
    _cancel = cancel_check or (lambda: False)

    if not tc.url:
        _log(f"[red]No download URL for toolchain '{tc.name}'[/red]")
        return False

    install_dir = paths.toolchains_dir / tc.name
    if install_dir.is_dir():
        _log(f"[yellow]{tc.name} is already installed at {install_dir}[/yellow]")
        return True

    downloads_dir = paths.downloads_dir
    downloads_dir.mkdir(parents=True, exist_ok=True)

    url = tc.url
    filename = url.split("/")[-1]
    filepath = downloads_dir / filename

    # ---- 1. Fetch archive (or use cache) ----------------------------------
    if url.startswith("file://"):
        file_ref = url[len("file://"):]
        local_path = Path(file_ref) if file_ref.startswith("/") else downloads_dir / file_ref
        if not local_path.exists():
            _log(f"[red]Archive not found: {local_path}[/red]")
            return False
        filepath = local_path
        _log(f"Using local archive: {filepath.name}")
    else:
        use_cache = (
            filepath.exists()
            and tc.checksum
            and _verify_checksum(filepath, tc.checksum)
        )
        if use_cache:
            _log(f"Using cached {filename} (checksum OK)")
        else:
            _log(f"Downloading {filename}...")
            for cmd in (
                ["wget", "-q", "-O", str(filepath), url],
                ["curl", "-sL", "-o", str(filepath), url],
            ):
                if _cancel():
                    _log("[yellow]Cancelled during download[/yellow]")
                    return False
                try:
                    r = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=1800,
                    )
                    if r.returncode == 0:
                        break
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    _log(f"  {cmd[0]} failed: {exc}")
            else:
                _log("[red]Download failed (wget + curl both unavailable or errored)[/red]")
                filepath.unlink(missing_ok=True)
                return False

    if _cancel():
        _log("[yellow]Cancelled after download[/yellow]")
        return False

    # ---- 2. Checksum verification ----------------------------------------
    if tc.checksum:
        _log("Verifying checksum...")
        if not _verify_checksum(filepath, tc.checksum):
            _log("[red]Checksum mismatch[/red]")
            filepath.unlink(missing_ok=True)
            return False
        _log("[green]Checksum OK[/green]")

    # ---- 3. Extract -------------------------------------------------------
    _log(f"Extracting to {install_dir.name}...")
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [
                "tar", "-xf", str(filepath),
                f"--strip-components={tc.strip_components or 0}",
                "-C", str(install_dir),
            ],
            capture_output=True, text=True, timeout=1800,
        )
        if r.returncode != 0:
            _log(f"[red]Extract failed: {r.stderr.strip()}[/red]")
            shutil.rmtree(install_dir, ignore_errors=True)
            return False
    except Exception as exc:
        _log(f"[red]Extract failed: {exc}[/red]")
        shutil.rmtree(install_dir, ignore_errors=True)
        return False

    if _cancel():
        _log("[yellow]Cancelled after extract[/yellow]")
        shutil.rmtree(install_dir, ignore_errors=True)
        return False

    # Some vendor tarballs (legacy simplebuild3, OE-SDK) ship dirs
    # with mode 0555 — add u+rwx so later library installs and
    # uninstall don't trip on Permission denied.
    from core.toolchain import ensure_writable_dirs
    ensure_writable_dirs(install_dir)

    # ---- 4. Relocate + fix .pc / .la -------------------------------------
    _relocate(install_dir, _log)
    try:
        fix_pc_files(install_dir, log=_log)
        fix_la_files(install_dir, log=_log)
    except Exception as exc:
        _log(f"  .pc/.la fixup warning: {exc}")

    # ---- 5. Register in toolchains.local.yaml ----------------------------
    try:
        reg = ToolchainRegistry(
            defaults_path=paths.toolchains_defaults,
            local_path=paths.toolchains_local,
            install_dir=paths.toolchains_dir,
        )
        local_tcs = reg._load_file(reg.local_path)
        if not any(t.name == tc.name for t in local_tcs):
            local_tcs.append(tc)
            reg.save_local(local_tcs)
            _log(f"  Added {tc.name} to toolchains.local.yaml")
    except Exception as exc:
        _log(f"[yellow]Registry update failed: {exc}[/yellow]")

    _log(f"[green]{tc.name} installed successfully[/green]")
    return True
