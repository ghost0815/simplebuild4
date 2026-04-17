"""Icon/symbol helper for ASCII-mode compatibility.

When ``ascii_mode`` is enabled in settings, emoji and special Unicode
characters are replaced with ASCII-safe alternatives so the TUI renders
correctly in terminals like PuTTY with limited font support.
"""

from __future__ import annotations

# Mapping: logical name → (unicode, ascii)
_ICONS = {
    "browse":   ("📂", ".."),
    "log":      ("📜", "log"),
    "refresh":  ("🔄", "ref"),
    "eye":      ("👁", "***"),
    "gear":     ("⚙", "cfg"),
    "reset":    ("↺", "def"),
    "marker":   ("▶", "►"),
    "copy":     ("📋", "cp"),
}

_ascii_mode: bool = False


def init(ascii_mode: bool) -> None:
    """Initialize icon mode from settings. Call once at app startup."""
    global _ascii_mode
    _ascii_mode = ascii_mode


def icon(name: str) -> str:
    """Return the icon for *name* — emoji or ASCII based on mode."""
    entry = _ICONS.get(name)
    if not entry:
        return name
    return entry[1] if _ascii_mode else entry[0]


def is_ascii() -> bool:
    """Return True if ASCII mode is active."""
    return _ascii_mode
