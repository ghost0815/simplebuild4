"""Log retention cleanup for all logs written to ``logs_dir``.

Log filenames share the shape ``{timestamp}.{rest}.log`` where the
timestamp is ``YYYY-MM-DD.HH-MM-SS`` (two dot-separated tokens). The
remainder is classified into *logical groups* (buckets) so that the
retention rules are applied **per bucket**, not across unrelated log
types:

    build:<target>        {ts}.{target}.build-{bs}.log
    lib:<toolchain>       {ts}.{toolchain}.{lib}-{version}.log
    selfbuild:<template>  {ts}.{template}.selfbuild.log
    backup:<toolchain>    {ts}.{toolchain}.backup.log
    deploy:<target>       {ts}.{target}.{dests}.deployment-check.log
    webservice            {ts}.webservice.log
    __s4__                {ts}.s4-update.log

Within each bucket the newest file is always preserved so the user
never loses the most recent log of that kind. Rule values of ``0``
disable the corresponding rule; if all three are ``0`` nothing is
touched.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


def resolve_log_dir(logs_dir: Path, log_type: str, organize: bool = False) -> Path:
    """Return the directory for a log file, creating it if needed.

    When organize=True, returns logs_dir/<type>/ (e.g. logs/build/).
    When organize=False, returns logs_dir/ (flat).
    """
    if organize and log_type:
        d = logs_dir / log_type
        d.mkdir(parents=True, exist_ok=True)
        return d
    return logs_dir


def _classify(name: str) -> Tuple[str, str]:
    """Return ``(kind, bucket_key)`` for a log filename."""
    if not name.endswith(".log"):
        return ("other", f"other:{name}")
    base = name[:-4]
    parts = base.split(".")
    # Timestamp occupies parts[0] + parts[1] ("YYYY-MM-DD" + "HH-MM-SS").
    if len(parts) < 3:
        return ("other", f"other:{base}")
    rest = parts[2:]

    if len(rest) == 1 and rest[0] == "s4-update":
        return ("s4", "__s4__")
    if len(rest) == 1 and rest[0] == "webservice":
        return ("webservice", "webservice")
    if len(rest) >= 2 and rest[-1] == "selfbuild":
        tpl = ".".join(rest[:-1])
        return ("selfbuild", f"selfbuild:{tpl}")
    if len(rest) >= 2 and rest[-1] == "backup":
        tc = ".".join(rest[:-1])
        return ("backup", f"backup:{tc}")
    if len(rest) >= 2 and rest[-1].startswith("build-"):
        tgt = ".".join(rest[:-1])
        return ("build", f"build:{tgt}")
    if len(rest) >= 2 and rest[-1] in ("upload-check", "deployment-check"):
        tgt = ".".join(rest[:-1])
        return ("deploy", f"deploy:{tgt}")
    if len(rest) >= 2:
        # Library build log: {toolchain}.{lib}-{version}
        return ("lib", f"lib:{rest[0]}")
    return ("other", f"other:{base}")


def cleanup_logs(
    logs_dir: Path,
    *,
    max_age_days: int = 0,
    max_count: int = 0,
    max_size_mb: int = 0,
) -> Tuple[int, int]:
    """Apply retention rules to every log bucket under ``logs_dir``.

    Returns ``(removed_count, freed_bytes)``. Rule values of ``0``
    disable the respective rule; if all three rules are disabled, no
    files are touched.
    """
    if not logs_dir.exists():
        return (0, 0)
    if not (max_age_days or max_count or max_size_mb):
        return (0, 0)

    buckets: Dict[str, List[Path]] = {}
    # Scan both flat logs and subdirectory logs
    for p in logs_dir.rglob("*.log"):
        if not p.is_file():
            continue
        _, key = _classify(p.name)
        buckets.setdefault(key, []).append(p)

    total_removed = 0
    total_freed = 0

    for group in buckets.values():
        if len(group) <= 1:
            continue
        group.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        protected = {group[0]}  # newest per bucket is always kept
        removed: set[Path] = set()
        freed = 0

        def _drop(f: Path) -> None:
            nonlocal freed
            if f in protected or f in removed:
                return
            try:
                freed += f.stat().st_size
                f.unlink()
                removed.add(f)
            except OSError:
                pass

        # 1) Age
        if max_age_days > 0:
            cutoff = time.time() - max_age_days * 86400
            for f in group:
                try:
                    if f.stat().st_mtime < cutoff:
                        _drop(f)
                except OSError:
                    pass

        # 2) Count
        if max_count > 0:
            survivors = [f for f in group if f not in removed]
            for f in survivors[max_count:]:
                _drop(f)

        # 3) Size
        if max_size_mb > 0:
            limit = max_size_mb * 1024 * 1024
            survivors = [f for f in group if f not in removed]
            total = 0
            for f in survivors:
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
            for f in reversed(survivors):
                if total <= limit:
                    break
                if f in protected:
                    continue
                try:
                    total -= f.stat().st_size
                except OSError:
                    pass
                _drop(f)

        total_removed += len(removed)
        total_freed += freed

    return (total_removed, total_freed)


def run_retention(
    logs_dir: Path,
    settings,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Convenience wrapper: read retention limits from a ``Settings``
    instance and report the result via ``log``. Errors are swallowed so
    retention never breaks an ongoing operation.
    """
    try:
        removed, freed = cleanup_logs(
            logs_dir,
            max_age_days=getattr(settings, "log_max_age_days", 0),
            max_count=getattr(settings, "log_max_count", 0),
            max_size_mb=getattr(settings, "log_max_size_mb", 0),
        )
        if removed and log:
            mb = freed / (1024 * 1024)
            log(f"Log retention: removed {removed} old log(s), freed {mb:.2f} MB")
    except Exception:
        pass
