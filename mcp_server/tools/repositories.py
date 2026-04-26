"""Repository-related MCP tools.

Read
  list_repositories      — all configured repos (oscam source identities)
  get_repository         — details of a single repo incl. on-disk state
  get_repository_commits — recent git log of a cloned repo

Write (need --allow-actions)
  create_repository      — add a new RepoConfig to data/repos.yaml
  update_repository      — patch fields of an existing repo
  delete_repository      — remove a repo (refuses to delete 'default')
  clone_repository       — start git clone / svn checkout as async job
  update_repository_sources — fetch + pull / svn update as async job
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from mcp.server import Server
from mcp.types import Tool

from core.repo import RepoConfig, RepoManager, RepoState
from core.repos_config import load_repos, save_repos
from core.patches import PatchSource, load_patch_sources, save_patch_sources, download_patches
from core import jobs as _jobs

from mcp_server.context import McpContext


_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_name(name: str) -> None:
    if not name:
        raise ValueError("repository name is required")
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid repository name '{name}' — only letters, digits, '_', '-', '.' allowed"
        )


def _repo_summary(cfg: RepoConfig, paths, include_secret: bool = False) -> dict[str, Any]:
    """JSON-friendly view of a repo config + its on-disk state."""
    manager = RepoManager(cfg, paths.repos_dir, paths.backup_dir)
    state = manager.state()
    data: dict[str, Any] = {
        "name": cfg.name,
        "url": cfg.url,
        "branch": cfg.branch,
        "clone_depth": cfg.clone_depth,
        "vcs": cfg.vcs or manager.vcs_type,
        "auth_method": cfg.auth_method,
        "ssh_key": cfg.ssh_key,
        "auth_user": cfg.auth_user,
        "auth_pass": "<redacted>" if cfg.auth_pass else "",
        "exclude_from_automation": bool(cfg.exclude_from_automation),
        "state": {
            "path": str(state.path),
            "exists": state.exists,
            "vcs": state.vcs,
            "branch": state.branch,
            "commit": state.commit,
            "revision": state.revision,
            "clean": state.clean,
            "tag": state.tag,
            "patched": state.patched,
            "has_config_sh": state.has_config_sh,
        },
    }
    if include_secret:
        data["auth_pass"] = cfg.auth_pass
    return data


# ------------------------------------------------------------------ read


def _list_repositories_impl(ctx: McpContext) -> list[dict[str, Any]]:
    repos = load_repos(ctx.paths.repos_config)
    return [_repo_summary(r, ctx.paths) for r in repos]


def _get_repository_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    repos = load_repos(ctx.paths.repos_config)
    for r in repos:
        if r.name == name:
            return _repo_summary(r, ctx.paths)
    raise ValueError(f"Unknown repository: {name}")


# ------------------------------------------------------------- commit log


def _git_log(cwd, limit: int, path: str | None) -> list[dict[str, Any]]:
    """Run ``git log`` in ``cwd`` and parse into a list of commits.

    Uses a unit-separator / record-separator scheme so commit bodies
    may span newlines without breaking the parser.
    """
    import subprocess
    if not cwd or not cwd.is_dir() or not (cwd / ".git").exists():
        raise ValueError(f"not a git repository: {cwd}")
    limit = max(1, min(int(limit or 20), 200))
    _FMT = "%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b%x1e"
    cmd = [
        "git", "-C", str(cwd), "log",
        f"-n{limit}", f"--pretty=format:{_FMT}",
    ]
    if path:
        cmd += ["--", str(path)]
    try:
        out = subprocess.check_output(
            cmd, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except subprocess.CalledProcessError as e:
        raise ValueError(f"git log failed: {e}") from e
    except subprocess.TimeoutExpired:
        raise ValueError("git log timed out (15s)")
    commits: list[dict[str, Any]] = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 7:
            continue
        commits.append({
            "hash": parts[0],
            "short_hash": parts[1],
            "author_name": parts[2],
            "author_email": parts[3],
            "date": parts[4],
            "subject": parts[5],
            "body": parts[6].rstrip("\n"),
        })
    return commits


def _get_oscam_options_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    """Read the oscam build options + USE flags + descriptions from a
    cloned repo's config.sh and Makefile.

    Returned shape (per category and for use_flags):

        {
          "repo": "default",
          "clone_path": "data/repos/oscam-default",
          "categories": {
            "addons":       [{"name": "WEBIF", "label": "Web Interface", "default": true}, ...],
            "protocols":    [...],
            "readers":      [...],
            "card_readers": [...]
          },
          "use_flags": [
            {"name": "SSL", "description": "Request linking with libssl. ..."},
            ...
          ]
        }

    The LLM should call this **before** create_target / update_target
    when it needs to know which addon / protocol / reader / use_flag
    names are valid for a given source repo, and what each one means.
    """
    from core.oscam_config import read_options
    repos = load_repos(ctx.paths.repos_config)
    cfg = next((r for r in repos if r.name == name), None)
    if not cfg:
        raise ValueError(f"Unknown repository: {name}")
    clone_path = ctx.paths.repos_dir / f"oscam-{name}"
    if not clone_path.exists():
        raise ValueError(
            f"Repository '{name}' is not cloned yet "
            f"(expected at {clone_path}) — run clone_repository first"
        )
    opts = read_options(clone_path)
    if opts is None:
        raise ValueError(
            f"config.sh not found in {clone_path} — repo may be incomplete"
        )
    out: dict[str, Any] = {
        "repo": name,
        "clone_path": str(clone_path),
        "categories": {},
        "use_flags": [],
    }
    for cat in ("addons", "protocols", "readers", "card_readers"):
        defaults_set = set(opts.default_enabled.get(cat, []))
        out["categories"][cat] = [
            {
                "name": opt,
                "label": opts.descriptions.get(opt, ""),
                "default": opt in defaults_set,
            }
            for opt in getattr(opts, cat)
        ]
    for flag, desc in sorted(opts.use_flag_descriptions.items()):
        out["use_flags"].append({"name": flag, "description": desc})
    return out


def _get_repository_commits_impl(
    ctx: McpContext, name: str, limit: int = 20, path: str | None = None,
) -> dict[str, Any]:
    repos = load_repos(ctx.paths.repos_config)
    cfg = next((r for r in repos if r.name == name), None)
    if not cfg:
        raise ValueError(f"Unknown repository: {name}")
    clone_path = ctx.paths.repos_dir / f"oscam-{name}"
    if not clone_path.exists():
        raise ValueError(
            f"Repository '{name}' is not cloned yet "
            f"(expected at {clone_path})"
        )
    if (cfg.vcs or "git") != "git":
        raise ValueError(
            f"get_repository_commits only supports git repos; "
            f"'{name}' is '{cfg.vcs}'"
        )
    commits = _git_log(clone_path, limit, path)
    return {
        "repo": name,
        "clone_path": str(clone_path),
        "path_filter": path,
        "count": len(commits),
        "commits": commits,
    }


# ------------------------------------------------------------------ write


def _apply_repo_patch(cfg: RepoConfig, patch: dict[str, Any]) -> None:
    """Write allowed fields from ``patch`` onto ``cfg`` in place.

    Security: ``auth_pass`` is intentionally NOT in the allowlist. Credentials
    must be set via the TUI (Settings → Repositories) only — an LLM must not
    be able to inject or rotate repo passwords via ``update_repository``.
    """
    _allowed = {
        "url", "branch", "clone_depth", "vcs",
        "auth_method", "ssh_key", "auth_user",
        "exclude_from_automation",
    }
    for key in _allowed:
        if key in patch:
            value = patch[key]
            if key == "clone_depth":
                value = int(value or 0)
            elif key == "exclude_from_automation":
                value = bool(value)
            else:
                value = "" if value is None else str(value)
            setattr(cfg, key, value)


def _create_repository_impl(
    ctx: McpContext, name: str, url: str, spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "create_repository is disabled — start the MCP server with --allow-actions"
        )
    _validate_name(name)
    if not url:
        raise ValueError("url is required")
    repos = load_repos(ctx.paths.repos_config)
    if any(r.name == name for r in repos):
        raise ValueError(f"Repository '{name}' already exists")
    new = RepoConfig(name=name, url=url)
    if spec:
        _apply_repo_patch(new, spec)
    repos.append(new)
    save_repos(repos, ctx.paths.repos_config)
    return {"name": name, "url": new.url, "created": True}


def _update_repository_impl(
    ctx: McpContext, name: str, patch: dict[str, Any],
) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "update_repository is disabled — start the MCP server with --allow-actions"
        )
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    repos = load_repos(ctx.paths.repos_config)
    for r in repos:
        if r.name == name:
            _apply_repo_patch(r, patch)
            save_repos(repos, ctx.paths.repos_config)
            return {"name": name, "saved": True, "applied_keys": sorted(patch.keys())}
    raise ValueError(f"Unknown repository: {name}")


def _delete_repository_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "delete_repository is disabled — start the MCP server with --allow-actions"
        )
    if name == "default":
        raise ValueError("Cannot delete the 'default' repository")
    repos = load_repos(ctx.paths.repos_config)
    new = [r for r in repos if r.name != name]
    if len(new) == len(repos):
        raise ValueError(f"Unknown repository: {name}")
    save_repos(new, ctx.paths.repos_config)
    return {"name": name, "deleted": True}


# ------------------------------------------------------------------ actions (async jobs)


def _clone_repository_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "clone_repository is disabled — start the MCP server with --allow-actions"
        )
    ctx.refresh_settings()
    handle = _jobs.start_repo_job(ctx.paths, ctx.settings, name, "clone")
    return {
        "job_id": handle.job_id,
        "log_path": str(handle.log_path),
        "status": "running",
        "tip": "Poll with get_build_status(job_id).",
    }


def _update_repository_sources_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "update_repository_sources is disabled — start the MCP server with --allow-actions"
        )
    ctx.refresh_settings()
    handle = _jobs.start_repo_job(ctx.paths, ctx.settings, name, "update")
    return {
        "job_id": handle.job_id,
        "log_path": str(handle.log_path),
        "status": "running",
        "tip": "Poll with get_build_status(job_id).",
    }


# ------------------------------------------------------------------ patches


def _list_patches_impl(ctx: McpContext) -> dict[str, Any]:
    """List patch sources (from data/patches.yaml) and actual files on disk."""
    patches_yaml = ctx.paths.repos_config.parent / "patches.yaml"
    sources = load_patch_sources(patches_yaml)
    downloaded = []
    if ctx.paths.patches_dir.is_dir():
        for f in sorted(ctx.paths.patches_dir.glob("*.patch")):
            downloaded.append({
                "name": f.name,
                "size": f.stat().st_size,
                "path": str(f),
            })
    return {
        "sources": [{"name": s.name, "url": s.url} for s in sources],
        "downloaded": downloaded,
    }


def _add_patch_source_impl(ctx: McpContext, name: str, url: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "add_patch_source is disabled — start the MCP server with --allow-actions"
        )
    _validate_name(name)
    if not url:
        raise ValueError("url is required")
    patches_yaml = ctx.paths.repos_config.parent / "patches.yaml"
    sources = load_patch_sources(patches_yaml)
    if any(s.name == name for s in sources):
        raise ValueError(f"Patch source '{name}' already exists")
    sources.append(PatchSource(name=name, url=url))
    save_patch_sources(sources, patches_yaml)
    return {"name": name, "url": url, "added": True}


def _remove_patch_source_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "remove_patch_source is disabled — start the MCP server with --allow-actions"
        )
    patches_yaml = ctx.paths.repos_config.parent / "patches.yaml"
    sources = load_patch_sources(patches_yaml)
    new = [s for s in sources if s.name != name]
    if len(new) == len(sources):
        raise ValueError(f"Unknown patch source: {name}")
    save_patch_sources(new, patches_yaml)
    return {"name": name, "removed": True}


def _download_patches_impl(ctx: McpContext) -> dict[str, Any]:
    """Download all configured patches synchronously (each is small + quick)."""
    if not ctx.allow_actions:
        raise PermissionError(
            "download_patches is disabled — start the MCP server with --allow-actions"
        )
    patches_yaml = ctx.paths.repos_config.parent / "patches.yaml"
    sources = load_patch_sources(patches_yaml)
    if not sources:
        return {"downloaded": 0, "total": 0, "note": "no patch sources configured"}
    lines: list[str] = []
    n = download_patches(sources, ctx.paths.patches_dir, log=lambda m: lines.append(m))
    return {"downloaded": n, "total": len(sources), "log": lines}


def _apply_repository_patches_impl(
    ctx: McpContext, repo: str, patches: list[str] | None = None,
) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "apply_repository_patches is disabled — start the MCP server with --allow-actions"
        )
    ctx.refresh_settings()
    handle = _jobs.start_patch_job(
        ctx.paths, ctx.settings, repo, selected_patches=patches or None,
    )
    return {
        "job_id": handle.job_id,
        "log_path": str(handle.log_path),
        "status": "running",
        "tip": "Poll with get_build_status(job_id).",
    }


# ------------------------------------------------------------------ register


def register(server: Server, ctx: McpContext) -> list[Tool]:
    tools: list[Tool] = [
        Tool(
            name="list_repositories",
            description=(
                "List all configured oscam source repositories (git/svn). "
                "Each entry includes on-disk state: clone path, current "
                "branch/commit, dirty flag, patched flag."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="get_repository",
            description="Return full configuration + on-disk state of one repository.",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="get_oscam_options",
            description=(
                "Return all available oscam build options for a repo's "
                "config.sh — addons, protocols, readers, card_readers — "
                "each with its human-readable label and default-enabled "
                "flag. Also returns USE_* build flags parsed from the "
                "Makefile help with full descriptions. "
                "Call this before create_target / update_target when "
                "you need to know which option names are valid for a "
                "given source repo and what they mean. The repo must "
                "already be cloned (run clone_repository first)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Repository name (e.g. 'default').",
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="get_repository_commits",
            description=(
                "Return the recent git commit log of a cloned repository "
                "(data/repos/oscam-<name>/). Useful for correlating build "
                "changes with upstream oscam history. Git repos only; "
                "SVN is not supported."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "limit": {
                        "type": "integer", "minimum": 1, "maximum": 200,
                        "default": 20,
                        "description": "Max commits to return (default 20, cap 200).",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional path filter (relative to the repo "
                            "root) — return only commits touching that file "
                            "or directory."
                        ),
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
    ]
    if ctx.allow_actions:
        tools.extend([
            Tool(
                name="create_repository",
                description=(
                    "Add a new named repository (oscam source identity) to "
                    "data/repos.yaml. 'spec' accepts optional extra fields: "
                    "branch, clone_depth, vcs, auth_method, ssh_key, "
                    "auth_user, exclude_from_automation. "
                    "(auth_pass is TUI-only and cannot be set via MCP.)"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "url": {"type": "string"},
                        "spec": {"type": "object"},
                    },
                    "required": ["name", "url"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="update_repository",
                description=(
                    "Patch fields of an existing repository config. "
                    "Allowed keys: url, branch, clone_depth, vcs, "
                    "auth_method, ssh_key, auth_user, "
                    "exclude_from_automation. "
                    "(auth_pass is TUI-only and cannot be set via MCP.)"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "patch": {"type": "object"},
                    },
                    "required": ["name", "patch"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="delete_repository",
                description="Remove a repository config. The 'default' repo cannot be deleted.",
                inputSchema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="clone_repository",
                description=(
                    "Clone a repository into data/repos/oscam-<name>/ if it "
                    "doesn't exist yet. Returns a job_id — poll with "
                    "get_build_status."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="update_repository_sources",
                description=(
                    "Run git fetch + pull (or svn update) for a repository. "
                    "Returns a job_id — poll with get_build_status."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="add_patch_source",
                description=(
                    "Add a patch download URL to data/patches.yaml. "
                    "download_patches will fetch it into data/patches/."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "File name (e.g. 'extra.patch')"},
                        "url": {"type": "string", "description": "Download URL"},
                    },
                    "required": ["name", "url"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="remove_patch_source",
                description="Remove a patch source entry from data/patches.yaml (does not delete the downloaded file).",
                inputSchema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="download_patches",
                description=(
                    "Download all configured patch sources into data/patches/. "
                    "Synchronous (patches are small files). Returns download "
                    "counts and per-patch log lines."
                ),
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            Tool(
                name="apply_repository_patches",
                description=(
                    "Apply .patch files to a checked-out repository. If "
                    "``patches`` is omitted, all *.patch files from "
                    "data/patches/ are applied. Returns a job_id — poll "
                    "with get_build_status."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name"},
                        "patches": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filenames to apply (omit for all)",
                        },
                    },
                    "required": ["repo"],
                    "additionalProperties": False,
                },
            ),
        ])
    # list_patches is read-only → always on
    tools.append(Tool(
        name="list_patches",
        description=(
            "List configured patch sources (data/patches.yaml) and the "
            "patch files currently present on disk (data/patches/)."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ))
    return tools


def dispatch(ctx: McpContext, name: str, args: dict[str, Any]) -> Any:
    if name == "list_repositories":
        return _list_repositories_impl(ctx)
    if name == "get_repository":
        return _get_repository_impl(ctx, args["name"])
    if name == "get_oscam_options":
        return _get_oscam_options_impl(ctx, args["name"])
    if name == "get_repository_commits":
        return _get_repository_commits_impl(
            ctx, args["name"],
            limit=args.get("limit", 20),
            path=args.get("path"),
        )
    if name == "create_repository":
        return _create_repository_impl(ctx, args["name"], args["url"], args.get("spec") or {})
    if name == "update_repository":
        return _update_repository_impl(ctx, args["name"], args["patch"])
    if name == "delete_repository":
        return _delete_repository_impl(ctx, args["name"])
    if name == "clone_repository":
        return _clone_repository_impl(ctx, args["name"])
    if name == "update_repository_sources":
        return _update_repository_sources_impl(ctx, args["name"])
    if name == "list_patches":
        return _list_patches_impl(ctx)
    if name == "add_patch_source":
        return _add_patch_source_impl(ctx, args["name"], args["url"])
    if name == "remove_patch_source":
        return _remove_patch_source_impl(ctx, args["name"])
    if name == "download_patches":
        return _download_patches_impl(ctx)
    if name == "apply_repository_patches":
        return _apply_repository_patches_impl(
            ctx, args["repo"], patches=args.get("patches"),
        )
    raise KeyError(name)


TOOL_NAMES = {
    "list_repositories", "get_repository", "get_oscam_options",
    "get_repository_commits",
    "create_repository", "update_repository", "delete_repository",
    "clone_repository", "update_repository_sources",
    "list_patches", "add_patch_source", "remove_patch_source",
    "download_patches", "apply_repository_patches",
}
