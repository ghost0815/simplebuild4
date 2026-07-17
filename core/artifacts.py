"""Collect existing artifacts (binaries, libs zip, logs) that belong to a target."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from core.target import Target


@dataclass
class Artifact:
    kind: str  # "binary" | "lib" | "log"
    path: Path
    size: int
    mtime: float


def _tc_names(target: Target) -> list[str]:
    return [tc.name for tc in target.toolchains if tc.name]


def _binary_matches(name: str, tc_names: Iterable[str], hostname: str) -> bool:
    for tc in tc_names:
        if tc == "native":
            if hostname in name or "native" in name:
                return True
        elif tc and tc in name:
            return True
    return False


def list_target_artifacts(
    target: Target,
    binaries_dir: Path,
    logs_dir: Path,
) -> list[Artifact]:
    tcs = _tc_names(target)
    host = socket.gethostname()
    results: list[Artifact] = []

    if binaries_dir.is_dir():
        for f in binaries_dir.iterdir():
            if not f.is_file():
                continue
            n = f.name
            if n.endswith(".debug"):
                continue
            if n.endswith("-libs.zip"):
                tc = n[:-len("-libs.zip")]
                if tc in tcs:
                    st = f.stat()
                    results.append(Artifact("lib", f, st.st_size, st.st_mtime))
                continue
            if _binary_matches(n, tcs, host):
                st = f.stat()
                results.append(Artifact("binary", f, st.st_size, st.st_mtime))

    if logs_dir.is_dir():
        needle = f".{target.name}."
        for f in logs_dir.iterdir():
            if f.is_file() and needle in f.name:
                st = f.stat()
                results.append(Artifact("log", f, st.st_size, st.st_mtime))

    results.sort(key=lambda a: a.mtime, reverse=True)
    return results
