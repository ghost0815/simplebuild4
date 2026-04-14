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
    """Derive a 32-byte key from the current hostname and login name."""
    try:
        user = os.getlogin()
    except OSError:
        import getpass
        user = getpass.getuser()
    material = f"{socket.gethostname()}:{user}".encode()
    return hashlib.sha256(material).digest()


def encrypt(plain: str) -> str:
    """Encrypt a string for storage. Returns ``enc:<base64>``."""
    if not plain:
        return ""
    key = _derive_key()
    data = plain.encode("utf-8")
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return "enc:" + base64.b64encode(xored).decode("ascii")


_decrypt_warnings: list[str] = []


def decrypt(stored: str) -> str:
    """Decrypt a stored value. Plain text (no ``enc:`` prefix) passes through.

    Returns empty string if decryption fails (e.g. config moved to a
    different machine where the derived key differs). A warning is
    appended to _decrypt_warnings for the app to display.
    """
    if not stored:
        return ""
    if not stored.startswith("enc:"):
        return stored  # legacy plain text
    try:
        key = _derive_key()
        xored = base64.b64decode(stored[4:])
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(xored)).decode("utf-8")
    except (UnicodeDecodeError, Exception):
        _decrypt_warnings.append(stored[:20])
        return ""


def pop_decrypt_warnings() -> list[str]:
    """Return and clear any decrypt warnings since last call."""
    warnings = list(_decrypt_warnings)
    _decrypt_warnings.clear()
    return warnings


def build_git_env(auth_method: str, ssh_key: str = "",
                  auth_user: str = "", auth_pass: str = "") -> dict[str, str]:
    """Build extra environment variables for git subprocess calls.

    Returns a dict that should be merged into ``os.environ`` via
    ``env={**os.environ, **build_git_env(...)}``.

    For username+password auth, a temporary GIT_ASKPASS script is used
    so the password is never embedded in the URL (which breaks with
    special characters).
    """
    import tempfile, stat
    env: dict[str, str] = {}
    if auth_method == "ssh_key" and ssh_key:
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o StrictHostKeyChecking=no"
    elif auth_method == "userpass" and auth_pass:
        # Create a temporary askpass script that returns the password.
        # GIT_ASKPASS is called by git with a prompt; we just echo the password.
        askpass = tempfile.NamedTemporaryFile(
            mode="w", prefix="s4-askpass-", suffix=".sh", delete=False,
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
