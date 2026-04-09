"""Patch download management — download patches from configured URLs."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml


@dataclass
class PatchSource:
    """A patch download source."""
    name: str
    url: str


def load_patch_sources(path: Path) -> list[PatchSource]:
    """Load patch sources from patches.yaml."""
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return []
    if not data or not data.get("patches"):
        return []
    return [
        PatchSource(name=p.get("name", ""), url=p.get("url", ""))
        for p in data["patches"]
        if p.get("name") and p.get("url")
    ]


def save_patch_sources(sources: list[PatchSource], path: Path) -> None:
    """Save patch sources to patches.yaml."""
    data = {
        "patches": [
            {"name": s.name, "url": s.url}
            for s in sources
        ]
    }
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def download_patches(
    sources: list[PatchSource],
    patches_dir: Path,
    log: Optional[Callable[[str], None]] = None,
) -> int:
    """Download patches from configured URLs.

    Args:
        sources: List of patch sources to download.
        patches_dir: Directory to save patches to.
        log: Optional callback for log output.

    Returns:
        Number of successfully downloaded patches.
    """
    patches_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for source in sources:
        if not source.name or not source.url:
            continue

        dest = patches_dir / source.name
        if log:
            log(f"Downloading {source.name}...")

        try:
            result = subprocess.run(
                ["wget", "-q", "-O", str(dest), source.url],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                if log:
                    log(f"  [green]ok[/green] ({dest.stat().st_size} bytes)")
                count += 1
            else:
                if log:
                    log(f"  [red]failed[/red]")
                if dest.exists():
                    dest.unlink()
        except FileNotFoundError:
            # wget not available, try curl
            try:
                result = subprocess.run(
                    ["curl", "-sL", "-o", str(dest), source.url],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                    if log:
                        log(f"  [green]ok[/green] ({dest.stat().st_size} bytes)")
                    count += 1
                else:
                    if log:
                        log(f"  [red]failed[/red]")
                    if dest.exists():
                        dest.unlink()
            except Exception as e:
                if log:
                    log(f"  [red]failed: {e}[/red]")
        except Exception as e:
            if log:
                log(f"  [red]failed: {e}[/red]")
            if dest.exists():
                dest.unlink()

    return count
