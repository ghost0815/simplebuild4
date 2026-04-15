"""Translation system for s4 — simple key-value YAML translations.

Supports an optional user overlay: any keys in
``data/translation/<lang>.yaml`` win over the bundled defaults in
``translation/<lang>.yaml``. Users only need to list the keys they want
to change; everything else falls back to the bundled translation, then
to English, then to the key itself. Adding a brand-new language works
the same way — drop ``data/translation/<code>.yaml`` and it shows up
in the language selector.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_translations: dict[str, str] = {}
_fallback: dict[str, str] = {}
_current_lang: str = "en"
_user_dir: Path | None = None


def _load_yaml(path: Path) -> dict[str, str]:
    """Load a YAML translation file as a dict. Returns {} on missing
    file, parse errors or non-dict content. Non-string values are
    filtered out so a stray ``key: null`` in a user override can't make
    ``t()`` return ``None``."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def load(language: str = "en", user_dir: Path | None = None) -> None:
    """Load translation files. English is always the fallback.

    ``user_dir`` (typically ``data/translation/``) is an optional
    per-installation overlay directory. Keys present in the user file
    override the bundled defaults; missing keys cascade through the
    normal default → English fallback chain.
    """
    global _translations, _fallback, _current_lang, _user_dir
    _current_lang = language
    _user_dir = user_dir
    trans_dir = Path(__file__).parent

    # English fallback: bundled default + optional user overlay.
    _fallback = _load_yaml(trans_dir / "en.yaml")
    if user_dir:
        _fallback.update(_load_yaml(user_dir / "en.yaml"))

    # Active language: bundled default + optional user overlay.
    if language == "en":
        _translations = _fallback
        return
    merged = _load_yaml(trans_dir / f"{language}.yaml")
    if user_dir:
        merged.update(_load_yaml(user_dir / f"{language}.yaml"))
    _translations = merged


def available_languages() -> list[tuple[str, str]]:
    """Scan the translation directories for YAML files and return
    (display_label, lang_code) pairs.

    Both the bundled ``translation/`` and the user overlay
    ``data/translation/`` are scanned; the overlay wins for the display
    name when both define ``language_name``. Lets users add a brand-new
    language by just dropping a file under ``data/translation/``.

    Display format: ``"<language_name> - <code>.yaml"``.
    Sorted alphabetically by language name, English always first.
    """
    trans_dir = Path(__file__).parent
    seen: dict[str, str] = {}  # code → display name

    def _add(directory: Path, prefer: bool) -> None:
        for f in sorted(directory.glob("*.yaml")):
            code = f.stem
            # README files are doc-only — never treat them as a language.
            if code.lower() in ("readme", "_readme"):
                continue
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                name = data.get("language_name", code) if isinstance(data, dict) else code
            except Exception:
                name = code
            if prefer or code not in seen:
                seen[code] = name

    _add(trans_dir, prefer=False)
    if _user_dir is not None and _user_dir.is_dir():
        _add(_user_dir, prefer=True)

    result = [(f"{name} - {code}.yaml", code) for code, name in seen.items()]
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
