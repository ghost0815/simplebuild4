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
    material = f"{socket.gethostname()}:{os.getlogin()}".encode()
    return hashlib.sha256(material).digest()


def encrypt(plain: str) -> str:
    """Encrypt a string for storage. Returns ``enc:<base64>``."""
    if not plain:
        return ""
    key = _derive_key()
    data = plain.encode("utf-8")
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return "enc:" + base64.b64encode(xored).decode("ascii")


def decrypt(stored: str) -> str:
    """Decrypt a stored value. Plain text (no ``enc:`` prefix) passes through."""
    if not stored:
        return ""
    if not stored.startswith("enc:"):
        return stored  # legacy plain text
    key = _derive_key()
    xored = base64.b64decode(stored[4:])
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(xored)).decode("utf-8")


def build_git_env(auth_method: str, ssh_key: str = "",
                  auth_token: str = "", auth_user: str = "",
                  auth_pass: str = "") -> dict[str, str]:
    """Build extra environment variables for git subprocess calls.

    Returns a dict that should be merged into ``os.environ`` via
    ``env={**os.environ, **build_git_env(...)}``.
    """
    env: dict[str, str] = {}
    if auth_method == "ssh_key" and ssh_key:
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o StrictHostKeyChecking=no"
    return env


def apply_credentials_to_url(url: str, auth_method: str,
                             auth_user: str = "", auth_pass: str = "") -> str:
    """Embed username/password into an HTTPS URL if needed.

    For git/svn HTTPS URLs with auth_method ``"userpass"``, the
    credentials are embedded as ``https://user:pass@host/path``.
    Returns the (possibly modified) URL.
    """
    if auth_method != "userpass" or not auth_user:
        return url
    p = urlparse(url)
    if p.scheme not in ("https", "http"):
        return url
    cred = f"{auth_user}:{auth_pass}" if auth_pass else auth_user
    return urlunparse(p._replace(netloc=f"{cred}@{p.hostname}"
                                 + (f":{p.port}" if p.port else "")))


def build_svn_auth_args(auth_method: str, auth_user: str = "",
                        auth_pass: str = "") -> list[str]:
    """Build extra CLI arguments for svn subprocess calls."""
    args: list[str] = []
    if auth_method == "userpass":
        if auth_user:
            args += ["--username", auth_user]
        if auth_pass:
            args += ["--password", auth_pass]
    return args
