"""Bootstrap a user-local Node.js install for the MCP inspector.

s4 doesn't depend on Node at runtime — it's only needed when the user
opens the ``@modelcontextprotocol/inspector`` from the F1 MCP Service
section's *Test* button. Instead of asking the user to ``sudo apt
install nodejs npm`` (which pollutes the system and needs root), we
download the official prebuilt Linux tarball from nodejs.org into
``$XDG_DATA_HOME/simplebuild4/node/`` and prepend its ``bin/`` to
``PATH`` when spawning ``npx``.

No sudo, no package manager, fully reversible (``rm -rf`` the dir).
Shared across multiple s4 checkouts on the same user account.
"""

from __future__ import annotations

import os
import platform
import shutil
import tarfile
import urllib.request
from pathlib import Path
from typing import Callable, Optional


# Pin a specific LTS release so repeated installs are deterministic.
# Bump this periodically.
NODE_VERSION = "v22.11.0"

_ARCH_MAP = {
    "x86_64":  "x64",
    "amd64":   "x64",
    "aarch64": "arm64",
    "arm64":   "arm64",
    "armv7l":  "armv7l",
}


def node_dir() -> Path:
    """Target location for the user-local Node install."""
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "simplebuild4" / "node"


def node_bin_dir() -> Optional[Path]:
    """``bin/`` of the local install, or None if not installed."""
    bd = node_dir() / "bin"
    return bd if (bd / "npx").exists() else None


def find_npx() -> Optional[Path]:
    """Return a usable ``npx`` path or None.

    Preference order:
      1. System ``PATH`` (respects whatever the user already has)
      2. User-local bootstrap install under ``node_dir()/bin/npx``
    """
    sys_npx = shutil.which("npx")
    if sys_npx:
        return Path(sys_npx)
    local = node_dir() / "bin" / "npx"
    if local.exists():
        return local
    return None


def _detect_arch() -> Optional[str]:
    return _ARCH_MAP.get(platform.machine().lower())


def install_node(
    log: Callable[[str], None] = lambda _m: None,
    version: str = NODE_VERSION,
) -> bool:
    """Download + extract a prebuilt Node into :func:`node_dir`.

    Streams progress through ``log``. Returns ``True`` on success.
    """
    if platform.system() != "Linux":
        log(f"[red]Node bootstrap only supports Linux (got {platform.system()}).[/red]")
        log("[red]Install node.js manually and make sure 'npx' is on PATH.[/red]")
        return False

    arch = _detect_arch()
    if not arch:
        log(f"[red]Unsupported CPU architecture: {platform.machine()}[/red]")
        return False

    target = node_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        log(f"Removing stale install at {target} …")
        shutil.rmtree(target, ignore_errors=True)

    tarname = f"node-{version}-linux-{arch}.tar.xz"
    url = f"https://nodejs.org/dist/{version}/{tarname}"
    tmp = target.parent / tarname

    log(f"Downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            chunk = 256 * 1024
            done = 0
            last_pct = -10
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if total:
                    pct = int(done * 100 / total)
                    if pct >= last_pct + 10:
                        log(f"  {pct:3d}%   {done//1024//1024} / {total//1024//1024} MB")
                        last_pct = pct
    except Exception as e:
        log(f"[red]Download failed: {e}[/red]")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    log("Extracting…")
    try:
        with tarfile.open(tmp, "r:xz") as tar:
            tar.extractall(target.parent)
        extracted = target.parent / f"node-{version}-linux-{arch}"
        if not extracted.is_dir():
            log(f"[red]Expected directory not found after extract: {extracted}[/red]")
            return False
        extracted.rename(target)
    except Exception as e:
        log(f"[red]Extraction failed: {e}[/red]")
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    npx = target / "bin" / "npx"
    if not npx.exists():
        log(f"[red]npx missing after install at {npx}[/red]")
        return False

    log(f"[green]Node {version} installed at {target}[/green]")
    return True
