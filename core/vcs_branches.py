"""List branches for a repo URL (git or svn).

Session-lived cache keyed on URL. Fetch is network-bound, so callers
should run it in a worker thread.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional


# URL → list[str] (ordered, unique)
_cache: dict[str, list[str]] = {}
# URL → default branch name
_default_cache: dict[str, str] = {}


def detect_vcs(url: str, clone_path: Optional[Path] = None) -> str:
    """Return 'git' or 'svn' for the given URL.

    If a local clone path is provided and exists, that wins. Otherwise
    fall back to URL heuristics: strong git markers first, then a broader
    net for SVN (host prefix, layout hints), git as final default.
    """
    if clone_path is not None and clone_path.is_dir():
        if (clone_path / ".git").exists():
            return "git"
        if (clone_path / ".svn").exists():
            return "svn"
    u = (url or "").strip()
    lower = u.lower()
    # Git-specific: ssh-style URL or an explicit ".git" suffix.
    if u.startswith("git@") or lower.endswith(".git"):
        return "git"
    # SVN heuristics (in order of strength):
    #  - dedicated svn subdomain (svn.apache.org, svn.code.sourceforge.net, …)
    #  - explicit /svn/ path segment
    #  - standard-layout markers anywhere in the path
    if (
        "://svn." in lower
        or "/svn/" in lower
        or "/trunk" in lower
        or "/branches/" in lower
        or "/tags/" in lower
    ):
        return "svn"
    return "git"


def svn_base_url(url: str) -> str:
    """Return the base URL of an SVN repository (parent of trunk/branches/…)."""
    u = (url or "").rstrip("/")
    # Strip /trunk, /branches/<x>, /tags/<x>
    m = re.match(r'^(.*?)(?:/trunk|/branches/[^/]+|/tags/[^/]+)/?$', u)
    if m:
        return m.group(1)
    return u


def _cache_key(url: str, vcs: str) -> str:
    """Normalize the cache key.

    For SVN we key on the base URL (stripped of any trunk/branches/tags
    suffix) so that switching between branches of the same repo does
    not invalidate the fetched list.
    """
    if vcs == "svn":
        return svn_base_url(url)
    return url


def list_branches(url: str, vcs: str, force_refresh: bool = False, timeout: int = 30,
                  auth_env: dict | None = None, svn_auth_args: list | None = None,
                  on_error=None) -> list[str]:
    """Return the list of branches for a repo URL.

    Uses a session-lived in-memory cache. Git keys on the full URL,
    SVN on the base URL. Pass force_refresh=True to bypass. Network
    failures return an empty list; when `on_error` is given it is
    called with a short human-readable reason so callers can surface it.
    """
    if not url:
        return []
    key = _cache_key(url, vcs)
    if not force_refresh and key in _cache:
        return list(_cache[key])

    branches: list[str] = []
    try:
        if vcs == "git":
            branches = _list_git_branches(url, timeout=timeout, env=auth_env)
        elif vcs == "svn":
            branches = _list_svn_branches(
                url, timeout=timeout, extra_args=svn_auth_args, on_error=on_error,
            )
    except subprocess.TimeoutExpired:
        if on_error:
            on_error(f"timeout after {timeout}s")
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip().splitlines()[-1:] if e.stderr else []
        msg = detail[0] if detail else f"exit {e.returncode}"
        if on_error:
            on_error(msg)
    except FileNotFoundError as e:
        if on_error:
            on_error(f"{e.filename} not found")
    except Exception as e:
        if on_error:
            on_error(f"{type(e).__name__}: {e}")

    if branches:
        _cache[key] = list(branches)
    return list(branches)


def _list_git_branches(url: str, timeout: int = 30, env: dict | None = None) -> list[str]:
    """`git ls-remote --symref --heads <url>` → list of branches + default."""
    out = subprocess.check_output(
        ["git", "ls-remote", "--symref", url, "HEAD", "refs/heads/*"],
        text=True, stderr=subprocess.PIPE, timeout=timeout,
        env=env,
    )
    branches: list[str] = []
    default = ""
    for line in out.splitlines():
        # "ref: refs/heads/master\tHEAD"
        if line.startswith("ref:"):
            m = re.match(r'^ref:\s+refs/heads/(\S+)', line)
            if m:
                default = m.group(1)
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        if ref.startswith("refs/heads/"):
            branches.append(ref[len("refs/heads/"):])
    branches.sort()
    if default:
        _default_cache[url] = default
    return branches


def _list_svn_branches(url: str, timeout: int = 30, extra_args: list | None = None,
                       on_error=None) -> list[str]:
    """`svn list <base>/branches/` → list starting with 'trunk'.

    When the ``branches/`` subdir is missing (repos that don't follow the
    standard trunk/branches/tags layout, or a URL that already points into
    a subproject) `on_error` is called with a short reason so the caller
    can tell the user why only ``trunk`` came back.
    """
    base = svn_base_url(url)
    result: list[str] = ["trunk"]
    _default_cache[base] = "trunk"
    try:
        cmd = ["svn", "list", "--non-interactive", f"{base}/branches/"] + (extra_args or [])
        out = subprocess.check_output(
            cmd,
            text=True, stderr=subprocess.PIPE, timeout=timeout,
        )
        for line in out.splitlines():
            name = line.strip().rstrip("/")
            if name:
                result.append(f"branches/{name}")
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip().splitlines()[-1:] if e.stderr else []
        msg = detail[0] if detail else f"exit {e.returncode}"
        if on_error:
            on_error(f"{base}/branches/: {msg}")
    except subprocess.TimeoutExpired:
        if on_error:
            on_error(f"{base}/branches/: timeout after {timeout}s")
    except Exception as e:
        if on_error:
            on_error(f"{base}/branches/: {type(e).__name__}: {e}")
    return result


def get_default_branch(url: str, vcs: str = "") -> str:
    """Return the cached default branch name for a URL, or empty.

    SVN normalises on the base URL so that the same repo shares its
    default across every branch/tag variant.
    """
    if vcs == "svn":
        return _default_cache.get(svn_base_url(url), "")
    return _default_cache.get(url, "")


def clear_cache(url: Optional[str] = None) -> None:
    """Drop the cache for one URL or all of them."""
    if url is None:
        _cache.clear()
        _default_cache.clear()
    else:
        _cache.pop(url, None)
        _default_cache.pop(url, None)
