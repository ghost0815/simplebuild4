"""Git repository operations."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class RepoState:
    """Current state of the oscam source repository."""

    path: Path
    branch: str = ""
    commit: str = ""
    revision: int = 0
    clean: bool = True

    @classmethod
    def detect(cls, repo_path: Path) -> Optional["RepoState"]:
        """Detect repository state from a working directory."""
        if not (repo_path / ".git").exists():
            return None

        state = cls(path=repo_path)

        try:
            state.branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
            ).strip()

            state.commit = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
            ).strip()

            rev = subprocess.check_output(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
            ).strip()
            state.revision = int(rev)

            status = subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
            ).strip()
            state.clean = len(status) == 0

        except (subprocess.CalledProcessError, ValueError):
            pass

        return state
