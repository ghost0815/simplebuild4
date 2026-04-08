"""Repos configuration — manage named source identities."""

from __future__ import annotations

from pathlib import Path

import yaml

from core.repo import RepoConfig

DEFAULT_REPOS = [
    RepoConfig(
        name="default",
        url="https://git.streamboard.tv/common/oscam.git",
    ),
]


def load_repos(path: Path) -> list[RepoConfig]:
    """Load repos from YAML. Returns defaults if file missing."""
    if not path.exists():
        repos = list(DEFAULT_REPOS)
        save_repos(repos, path)
        return repos

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return list(DEFAULT_REPOS)
    if not data or "repos" not in data:
        return list(DEFAULT_REPOS)

    repos = []
    for entry in data["repos"]:
        repos.append(RepoConfig(
            name=entry.get("name", "default"),
            url=entry.get("url", ""),
            branch=entry.get("branch", ""),
            clone_depth=entry.get("clone_depth", 0),
            vcs=entry.get("vcs", ""),
        ))
    return repos


def save_repos(repos: list[RepoConfig], path: Path) -> None:
    """Save repos to YAML."""
    data = {
        "repos": [
            {k: v for k, v in {
                "name": r.name,
                "url": r.url,
                "branch": r.branch,
                "clone_depth": r.clone_depth,
                "vcs": r.vcs,
            }.items() if v not in ("", 0, None)}
            for r in repos
        ]
    }
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
