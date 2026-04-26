"""Interface to oscam's config.sh — reads available options and manages config state."""

from __future__ import annotations

import subprocess
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class OscamOptions:
    """All available oscam options read from config.sh."""
    addons: list[str] = field(default_factory=list)
    protocols: list[str] = field(default_factory=list)
    readers: list[str] = field(default_factory=list)
    card_readers: list[str] = field(default_factory=list)

    # Default enabled state (from config.h defaults)
    default_enabled: dict[str, list[str]] = field(default_factory=dict)

    # Human-readable descriptions parsed from config.sh menu_* blocks
    # and Makefile USE_* help — keyed by option / flag name.
    descriptions: dict[str, str] = field(default_factory=dict)
    use_flag_descriptions: dict[str, str] = field(default_factory=dict)


def _parse_defconfig_defaults(config_sh: Path) -> dict[str, list[str]]:
    """Parse the ``$defconfig`` shell variable out of ``config.sh`` and
    return ``{category: [enabled_option_names]}`` for the four option
    categories.

    Unlike ``config.sh --show-enabled``, this reflects the oscam
    *defaults* regardless of how ``config.h`` may have been modified by
    a previous build. Pure text parsing, no side effects.
    """
    import re as _re

    try:
        text = config_sh.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}

    # Valid option names per category (from the shell variables at the
    # top of config.sh).
    cat_map: dict[str, set[str]] = {}
    for cat in ("addons", "protocols", "readers", "card_readers"):
        m = _re.search(rf'^{cat}="([^"]*)"', text, _re.MULTILINE)
        if m:
            cat_map[cat] = set(m.group(1).split())

    # The defconfig="..." heredoc-style variable.
    m = _re.search(r'defconfig="\s*\n(.*?)\n"', text, _re.DOTALL)
    if not m:
        return {cat: [] for cat in ("addons", "protocols", "readers", "card_readers")}
    block = m.group(1)

    enabled: set[str] = set()
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        if val.strip() != "y":
            continue
        name = key.strip()
        if name.startswith("CONFIG_"):
            name = name[len("CONFIG_"):]
        if name:
            enabled.add(name)

    result: dict[str, list[str]] = {c: [] for c in ("addons", "protocols", "readers", "card_readers")}
    for name in enabled:
        for cat, opts in cat_map.items():
            if name in opts:
                result[cat].append(name)
                break
    return result


def _parse_menu_descriptions(config_sh: Path) -> dict[str, str]:
    """Extract ``OPTION  "Description"`` pairs from every ``menu_*()``
    function in ``config.sh``. These are the human-readable labels
    oscam shows in its own ``make config`` dialog.

    Returns a flat dict — option names are unique across categories.
    """
    import re as _re
    try:
        text = config_sh.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    descs: dict[str, str] = {}
    # Capture each menu_<name>() { ... } block
    for block in _re.finditer(r"^menu_\w+\(\)\s*\{(.*?)^\}", text, _re.DOTALL | _re.MULTILINE):
        body = block.group(1)
        # Lines look like:  OPTION_NAME    "Some label"    $(check_test "OPTION_NAME") \
        for m in _re.finditer(r'^\s*([A-Z][A-Z0-9_]+)\s+"([^"]+)"\s+\$\(check_test', body, _re.MULTILINE):
            descs[m.group(1)] = m.group(2)
    return descs


def _parse_makefile_useflags(makefile: Path) -> dict[str, str]:
    """Extract ``USE_FOO=1 - description`` blocks from oscam's Makefile
    help text. Each entry spans multiple indented lines until the next
    blank line or the next ``USE_*=1`` block.

    Returns ``{"FOO": "Request linking with libfoo. ..."}`` (the
    ``USE_`` prefix is stripped to match s4's ``use_flags`` keys).
    """
    import re as _re
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    descs: dict[str, str] = {}
    # Match a header line like:  USE_SSL=1       - Request linking with libssl. ...
    # then continuation lines indented with spaces (until blank line or next flag).
    pat = _re.compile(
        r"^\s*USE_([A-Z0-9_]+)=1\s*-\s*(.*?)(?=\n\s*\n|\n\s*USE_[A-Z0-9_]+=1\s*-)",
        _re.DOTALL | _re.MULTILINE,
    )
    for m in pat.finditer(text):
        flag = m.group(1)
        # Collapse the multi-line continuation. The Makefile help is a
        # printf-style string: each source line ends with literal "\n\"
        # (backslash-n, backslash). Strip those, plus any plain trailing
        # backslashes, and drop empty lines.
        raw = m.group(2)
        cleaned = []
        for ln in raw.splitlines():
            ln = ln.strip()
            # Drop trailing line-continuation: "...text\n\" or "...text\"
            while ln.endswith("\\"):
                ln = ln[:-1].rstrip()
            if ln.endswith(r"\n"):
                ln = ln[:-2].rstrip()
            if ln:
                cleaned.append(ln)
        joined = " ".join(cleaned)
        # Drop the trailing example variable list (everything after the
        # first occurrence of "The variables that control") — keeps the
        # tooltip readable. If not present, keep full text.
        cut = joined.find("The variables that control")
        if cut > 0:
            joined = joined[:cut].rstrip(" .")
        descs[flag] = joined
    return descs


def read_options(repo_dir: Path) -> OscamOptions | None:
    """Read all available options from a cloned oscam repo's config.sh.

    Returns None if config.sh is not found or not executable.
    """
    config_sh = repo_dir / "config.sh"
    if not config_sh.exists():
        return None

    opts = OscamOptions()

    for category, attr in [
        ("addons", "addons"),
        ("protocols", "protocols"),
        ("readers", "readers"),
        ("card_readers", "card_readers"),
    ]:
        # Get all valid options for this category
        valid = _run_config_sh(config_sh, ["--show-valid", category])
        if valid is not None:
            setattr(opts, attr, valid.split())

    # Parse true oscam defaults directly from the defconfig variable in
    # config.sh (does not depend on the current state of config.h).
    defaults = _parse_defconfig_defaults(config_sh)
    for category in ("addons", "protocols", "readers", "card_readers"):
        opts.default_enabled[category] = defaults.get(category, [])

    # Human-readable labels — for tooltips in the Target Editor.
    opts.descriptions = _parse_menu_descriptions(config_sh)
    opts.use_flag_descriptions = _parse_makefile_useflags(repo_dir / "Makefile")

    return opts


def read_enabled(repo_dir: Path) -> dict[str, list[str]]:
    """Read currently enabled options from config.h via config.sh.

    Returns dict with keys: addons, protocols, readers, card_readers.
    """
    config_sh = repo_dir / "config.sh"
    if not config_sh.exists():
        return {}

    result = {}
    for category in ("addons", "protocols", "readers", "card_readers"):
        enabled = _run_config_sh(config_sh, ["--show-enabled", category])
        if enabled is not None:
            result[category] = enabled.split()
        else:
            result[category] = []
    return result


def apply_config(
    repo_dir: Path,
    enabled: dict[str, list[str]],
    all_options: OscamOptions,
    log: "Callable[[str], None] | None" = None,
) -> bool:
    """Apply a target configuration to config.h via config.sh.

    Flow (same as s3):
    1. Restore oscam defaults (config.sh -R)
    2. Disable ALL options (clean slate)
    3. Enable only the target's selected options
    4. Dependencies resolved by config.sh automatically (update_deps)

    Args:
        repo_dir: Path to cloned oscam repo
        enabled: Dict of category → list of enabled option names
        all_options: All available options (to know what to disable)
        log: Optional log callback

    Returns:
        True on success.
    """
    _log = log or (lambda msg: None)
    config_sh = repo_dir / "config.sh"
    if not config_sh.exists():
        _log(f"  [red]config.sh not found in {repo_dir}[/red]")
        return False

    # Step 1: Restore defaults
    _log("  Restoring oscam defaults...")
    _run_config_sh(config_sh, ["--restore"])

    # Step 2: Disable ALL options (clean slate)
    all_opts = []
    for attr in ("addons", "protocols", "readers", "card_readers"):
        all_opts.extend(getattr(all_options, attr))
    if all_opts:
        _log(f"  Disabling all {len(all_opts)} options...")
        _run_config_sh(config_sh, ["-D"] + all_opts)

    # Step 3: Enable only the target's selected options
    to_enable = []
    for category in ("addons", "protocols", "readers", "card_readers"):
        to_enable.extend(enabled.get(category, []))

    if to_enable:
        _log(f"  Enabling {len(to_enable)} options...")
        for opt in to_enable:
            _log(f"    [green]Enable[/green] {opt}")
        _run_config_sh(config_sh, ["-E"] + to_enable)

    # Step 4: Show final state
    for category, label in [("addons", "Addons"), ("protocols", "Protocols"),
                             ("readers", "Readers"), ("card_readers", "Card Readers")]:
        final = _run_config_sh(config_sh, ["--show-enabled", category])
        count = len(final.split()) if final else 0
        _log(f"  [bold]{label}[/bold]: [green]{count}[/green] enabled")

    return True


def merge_options(repo_dirs: list[Path]) -> OscamOptions:
    """Read options from multiple repos and merge them (union, unique).

    Args:
        repo_dirs: List of cloned oscam repo paths

    Returns:
        Merged OscamOptions with union of all options.
        Default enabled = union of all defaults.
    """
    merged = OscamOptions()

    for repo_dir in repo_dirs:
        opts = read_options(repo_dir)
        if not opts:
            continue
        for attr in ("addons", "protocols", "readers", "card_readers"):
            existing = set(getattr(merged, attr))
            for opt in getattr(opts, attr):
                if opt not in existing:
                    getattr(merged, attr).append(opt)
                    existing.add(opt)
        for cat, defaults in opts.default_enabled.items():
            if cat not in merged.default_enabled:
                merged.default_enabled[cat] = []
            existing_defaults = set(merged.default_enabled[cat])
            for opt in defaults:
                if opt not in existing_defaults:
                    merged.default_enabled[cat].append(opt)
                    existing_defaults.add(opt)
        # Descriptions: first repo wins (later repos don't overwrite),
        # so a stable forked oscam can't silently relabel options that
        # already exist in the canonical upstream entry.
        for k, v in opts.descriptions.items():
            merged.descriptions.setdefault(k, v)
        for k, v in opts.use_flag_descriptions.items():
            merged.use_flag_descriptions.setdefault(k, v)

    return merged


def get_version(repo_dir: Path) -> str:
    """Get oscam version string from the repo."""
    config_sh = repo_dir / "config.sh"
    if not config_sh.exists():
        return ""
    result = _run_config_sh(config_sh, ["--oscam-version"])
    return result.strip() if result else ""


# Internal Makefile flags that are NOT user-settable
_INTERNAL_FLAGS = frozenset({
    "CFLAGS", "FLAGS", "LDFLAGS", "LIBS", "COMPRESS",
    # Doc placeholder in oscam Makefile's prepare_use_flags macro
    # template comment: "Usage: $(eval $(call prepare_use_flags,FLAG_NAME,…))"
    "FLAG_NAME",
})


# USE_* flags that refer to hardware descrambler / vendor-API surfaces
# rather than ordinary third-party libraries. The target editor groups
# these under "Hardware" and the main-screen summary lists them under
# "Hardware flags" (everything else lands under "Libraries"). Keep in
# sync with any new descrambler-API addition.
HARDWARE_USE_FLAGS = frozenset({
    "STAPI", "STAPI5", "COOLAPI", "COOLAPI2", "SU980",
    "AZBOX", "AMSMC", "MCA",
    "WI", "GXAPI",
})


def parse_makefile_flags(repo_dirs: list[Path]) -> dict:
    """Parse USE_* and STATIC_* flags from oscam Makefile(s).

    Returns dict with:
        "use": sorted list of flag names (e.g. ["LIBCRYPTO", "SSL", "OPENSSL", ...])
        "static": sorted list of flag names (e.g. ["LIBDVBCSA"])
    Merges flags from multiple repos (union).
    Filters out internal flags (USE_CFLAGS, USE_FLAGS, etc.).
    """
    import re
    use_set: set[str] = set()
    static_set: set[str] = set()
    pattern = re.compile(r'\bUSE_([A-Z0-9_]+)\b|\bSTATIC_([A-Z0-9_]+)\b')
    # ``prepare_use_flags`` is oscam's Makefile macro that programmatically
    # defines ``USE_<NAME>`` — the generated USE_NAME may not appear as a
    # literal anywhere else in the Makefile (true e.g. for USE_GXAPI /
    # USE_WI that come in via optional patches). Pick the flag name out of
    # the macro invocation so those count too.
    prep_pattern = re.compile(r'\bprepare_use_flags,([A-Z0-9_]+)\b')

    for repo_dir in repo_dirs:
        makefile = repo_dir / "Makefile"
        if not makefile.exists():
            continue
        try:
            content = makefile.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in pattern.finditer(content):
            use_name = m.group(1)
            static_name = m.group(2)
            if use_name and use_name not in _INTERNAL_FLAGS:
                use_set.add(use_name)
            if static_name and static_name not in _INTERNAL_FLAGS:
                static_set.add(static_name)
        for m in prep_pattern.finditer(content):
            name = m.group(1)
            if name and name not in _INTERNAL_FLAGS:
                use_set.add(name)

    return {
        "use": sorted(use_set),
        "static": sorted(static_set),
    }


def _run_config_sh(config_sh: Path, args: list[str]) -> str | None:
    """Run config.sh with arguments and return stdout."""
    try:
        result = subprocess.run(
            ["bash", str(config_sh.resolve())] + args,
            capture_output=True, text=True, timeout=10,
            cwd=str(config_sh.parent.resolve()),
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return result.stdout.strip() if result.stdout.strip() else None
    except Exception:
        return None
