"""Toolchain builder integrations (crosstool-ng, oe-sdk, android-ndk)."""

from __future__ import annotations


def expand_tokens(cmd: list, tokens: dict) -> list:
    """Replace @TOKEN@ placeholders in each argument of a command list."""
    result = []
    for arg in cmd:
        s = str(arg)
        for key, val in tokens.items():
            s = s.replace(f"@{key}@", str(val))
        result.append(s)
    return result
