"""Credential encryption and auth environment builder.

Passwords and tokens are stored in repos.yaml using a simple reversible
XOR encoding with a machine-specific key derived from hostname + login
name. Values are prefixed with ``enc:`` so plain-text legacy entries
are transparently supported (they pass through ``decrypt`` unchanged).

This is NOT cryptographic security — it prevents accidental exposure
(e.g. sharing repos.yaml, visible in git diffs) but not a determined
attacker with access to both the source code and the machine.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse


def _derive_key() -> bytes:
    """Derive a 32-byte key from the current hostname and login name.

    Uses getpass.getuser() which is consistent across TTY and non-TTY
    contexts (SSH, screen, systemd services). os.getlogin() reads from
    utmp/terminal and can return a different value or raise OSError in
    headless/reconnected sessions, causing spurious decrypt failures.
    """
    import getpass
    material = f"{socket.gethostname()}:{getpass.getuser()}".encode()
    return hashlib.sha256(material).digest()


def _derive_key_legacy() -> bytes | None:
    """Legacy key using os.getlogin() — used as decrypt fallback only.

    Passwords saved before the getpass.getuser() switch may have been
    encrypted with the os.getlogin() key. Returns None if getlogin()
    is unavailable (no TTY).
    """
    try:
        material = f"{socket.gethostname()}:{os.getlogin()}".encode()
        return hashlib.sha256(material).digest()
    except OSError:
        return None


def encrypt(plain: str) -> str:
    """Encrypt a string for storage. Returns ``enc:<base64>``."""
    if not plain:
        return ""
    key = _derive_key()
    data = plain.encode("utf-8")
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return "enc:" + base64.b64encode(xored).decode("ascii")


_decrypt_warnings: list[str | Callable[[], str]] = []
_decrypt_persistent: list[str | Callable[[], str]] = []


def decrypt(stored: str, label: str | Callable[[], str] = "", persistent: bool = False) -> str:
    """Decrypt a stored value. Plain text (no ``enc:`` prefix) passes through.

    Tries the primary key (getpass.getuser()) first, then falls back to
    the legacy key (os.getlogin()) for passwords saved before the key
    derivation was stabilised. Only appends a warning if both keys fail.

    label — optional human-readable description of the field. May be a
    plain string or a zero-argument callable (lambda) so that t() calls
    are deferred until the warning is actually displayed (after the UI
    and translations are fully initialised).
    """
    if not stored:
        return ""
    if not stored.startswith("enc:"):
        return stored  # legacy plain text
    try:
        raw = base64.b64decode(stored[4:])
    except Exception:
        _store_warning(label or stored[:20], persistent)
        return ""
    for key in filter(None, [_derive_key(), _derive_key_legacy()]):
        try:
            result = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw)).decode("utf-8")
            return result
        except (UnicodeDecodeError, ValueError):
            continue
    _store_warning(label or stored[:20], persistent)
    return ""


def _store_warning(label: str | Callable[[], str], persistent: bool) -> None:
    if persistent:
        _decrypt_persistent.append(label)
    else:
        _decrypt_warnings.append(label)


def pop_decrypt_warnings() -> list[str]:
    """Return and clear any decrypt warnings since last call.

    Callables are resolved here — at display time, after the UI and
    translations are fully initialised. Duplicate labels are collapsed
    (e.g. two deployments with the same host and no name).
    """
    seen: list[str] = []
    # Persistent warnings (settings, repos) — included every time, never cleared
    for w in _decrypt_persistent:
        try:
            label = w() if callable(w) else w
        except Exception as e:
            label = f"(label error: {e})"
        if label not in seen:
            seen.append(label)
    # Transient warnings (per-target) — cleared after each pop
    for w in _decrypt_warnings:
        try:
            label = w() if callable(w) else w
        except Exception as e:
            label = f"(label error: {e})"
        if label not in seen:
            seen.append(label)
    _decrypt_warnings.clear()
    return seen


def clear_persistent_warnings() -> None:
    """Clear persistent warnings — call after the user re-enters passwords."""
    _decrypt_persistent.clear()


def clear_transient_warnings() -> None:
    """Clear transient (per-target) warnings.

    Call before a full re-scan (e.g. ``_refresh_targets``) so the list
    reflects the *current* state of the world instead of accumulating
    every Target.load() call ever made.
    """
    _decrypt_warnings.clear()


_ASKPASS_PREFIX = "s4-askpass-"
_ASKPASS_SUFFIX = ".sh"
_askpass_cleanup_registered = False


def _cleanup_stale_askpass(tmp_dir: "Path | None" = None) -> None:
    """Best-effort removal of leftover ``s4-askpass-*.sh`` scripts.

    Called once on first ``build_git_env`` use (sweeps debris from
    earlier process crashes) and again on interpreter exit (atexit).
    Silent: a busy or read-only /tmp is not an error worth surfacing.
    """
    import os as _os
    import tempfile as _tempfile
    base = Path(tmp_dir) if tmp_dir else Path(_tempfile.gettempdir())
    if not base.is_dir():
        return
    for entry in base.iterdir():
        try:
            name = entry.name
            if (
                name.startswith(_ASKPASS_PREFIX)
                and name.endswith(_ASKPASS_SUFFIX)
                and entry.is_file()
                and entry.stat().st_uid == _os.getuid()
            ):
                entry.unlink(missing_ok=True)
        except Exception:
            continue


def build_git_env(auth_method: str, ssh_key: str = "",
                  auth_user: str = "", auth_pass: str = "") -> dict[str, str]:
    """Build extra environment variables for git subprocess calls.

    Returns a dict that should be merged into ``os.environ`` via
    ``env={**os.environ, **build_git_env(...)}``.

    For username+password auth, a temporary GIT_ASKPASS script is used
    so the password is never embedded in the URL (which breaks with
    special characters). The script is auto-cleaned on interpreter
    exit (atexit hook) and any stale leftovers from previous crashes
    are swept on first use of this function in the current process.
    """
    import tempfile, stat
    global _askpass_cleanup_registered
    env: dict[str, str] = {}
    if auth_method == "ssh_key" and ssh_key:
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o StrictHostKeyChecking=no"
    elif auth_method == "userpass" and auth_pass:
        if not _askpass_cleanup_registered:
            # First use this process: sweep stale scripts left over by
            # earlier crashes / kill -9 / OS shutdowns, and arm an
            # atexit hook so future ones go away on clean shutdown.
            import atexit
            _cleanup_stale_askpass()
            atexit.register(_cleanup_stale_askpass)
            _askpass_cleanup_registered = True
        # Create a temporary askpass script that returns the password.
        # GIT_ASKPASS is called by git with a prompt; we just echo the password.
        askpass = tempfile.NamedTemporaryFile(
            mode="w", prefix=_ASKPASS_PREFIX, suffix=_ASKPASS_SUFFIX, delete=False,
        )
        askpass.write(f"#!/bin/sh\necho '{auth_pass}'\n")
        askpass.close()
        import os as _os
        _os.chmod(askpass.name, stat.S_IRWXU)
        env["GIT_ASKPASS"] = askpass.name
        env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def apply_credentials_to_url(url: str, auth_method: str,
                             auth_user: str = "", auth_pass: str = "") -> str:
    """Embed only the username into an HTTPS URL if needed.

    The password is NOT embedded — it is supplied via GIT_ASKPASS
    (for git) or --password (for svn) to avoid URL-encoding issues
    with special characters.
    """
    if auth_method != "userpass" or not auth_user:
        return url
    p = urlparse(url)
    if p.scheme not in ("https", "http"):
        return url
    return urlunparse(p._replace(netloc=f"{auth_user}@{p.hostname}"
                                 + (f":{p.port}" if p.port else "")))


def build_svn_auth_args(auth_method: str, auth_user: str = "",
                        auth_pass: str = "") -> list[str]:
    """Build extra CLI arguments for svn subprocess calls.

    When a password is provided, ``--no-auth-cache`` is added to force
    using the supplied credentials. When only a username is set
    (password empty), no extra flags are added so SVN can prompt
    interactively and cache credentials for future use.
    ``--trust-server-cert`` is handled separately in RepoManager for
    all HTTPS URLs regardless of auth method.
    """
    args: list[str] = []
    if auth_method == "userpass":
        if auth_user:
            args += ["--username", auth_user]
        if auth_pass:
            args += ["--password", auth_pass, "--no-auth-cache"]
    return args


def svn_needs_interactive(auth_method: str, auth_pass: str = "") -> bool:
    """Return True if SVN should run without --non-interactive.

    When auth_method is userpass but password is empty, SVN needs to
    prompt the user interactively for credentials.
    """
    return auth_method == "userpass" and not auth_pass
