"""Translation system for s4 — simple key-value YAML translations."""

from __future__ import annotations

from pathlib import Path

import yaml

_translations: dict[str, str] = {}
_fallback: dict[str, str] = {}
_current_lang: str = "en"


def load(language: str = "en") -> None:
    """Load translation files. English is always the fallback."""
    global _translations, _fallback, _current_lang
    _current_lang = language
    trans_dir = Path(__file__).parent

    # Always load English as fallback
    en_path = trans_dir / "en.yaml"
    if en_path.exists():
        data = yaml.safe_load(en_path.read_text(encoding="utf-8"))
        _fallback = data if data else {}

    # Load requested language
    if language != "en":
        lang_path = trans_dir / f"{language}.yaml"
        if lang_path.exists():
            data = yaml.safe_load(lang_path.read_text(encoding="utf-8"))
            _translations = data if data else {}
        else:
            _translations = {}
    else:
        _translations = _fallback


def available_languages() -> list[tuple[str, str]]:
    """Scan translation directory for YAML files and return (display_label, lang_code) pairs.

    Display format: "English - en.yaml"
    Sorted alphabetically by language name, English always first.
    """
    trans_dir = Path(__file__).parent
    result = []
    for f in sorted(trans_dir.glob("*.yaml")):
        code = f.stem
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            name = data.get("language_name", code) if data else code
        except Exception:
            name = code
        result.append((f"{name} - {f.name}", code))
    # English first, rest alphabetical by display label
    result.sort(key=lambda x: (0 if x[1] == "en" else 1, x[0]))
    return result


def t(key: str, **kwargs) -> str:
    """Get translated string by key. Falls back to English, then to the key itself.

    Supports {placeholder} substitution via kwargs.
    """
    text = _translations.get(key) or _fallback.get(key) or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text
