"""
Toolchain backup — pure, UI-free backup routine.

Used by both the F4-Screen backup button (ui/screens/toolchains.py)
and the automation update-libraries action (core/automation.py).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from core.env import deliver_link

if TYPE_CHECKING:
    from core.paths import Paths
    from core.toolchain import Toolchain, ToolchainRegistry


def backup_toolchain(
    tc: "Toolchain",
    paths: "Paths",
    registry: "ToolchainRegistry",
    settings,
    log: Callable[[str], None] | None = None,
    write_log_file: bool = True,
) -> bool:
    """Backup a single installed toolchain to a .tar.xz archive.

    Creates the archive in paths.downloads_dir, writes a timestamped log
    under paths.logs_dir/backup/, and — for local (file://) toolchains —
    updates the checksum and URL in the local registry YAML.

    Args:
        tc:        Toolchain object (must be installed and non-native).
        paths:     Paths instance (provides toolchains_dir, downloads_dir, logs_dir).
        registry:  ToolchainRegistry instance (used for local YAML update).
        settings:  Settings instance (needs .organize_logs for log directory layout).
        log:       Optional rich-markup-capable log callback; prints plain text if None.
        write_log_file: When False, skip creating a separate backup.log
                        file. Used by the automation update-libraries
                        action so the full output ends up only in the
                        single job log instead of being duplicated.

    Returns:
        True on success, False on failure.
    """
    from core.log_cleanup import resolve_log_dir, run_retention
    from rich.markup import escape as rich_escape

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)
        else:
            print(re.sub(r"\[/?[a-z_ ]+\]", "", msg))

    install_dir = paths.toolchains_dir / tc.name
    backup_dir = paths.downloads_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_file = backup_dir / f"Toolchain-{tc.name}.tar.xz"

    log_file = None
    log_path = None
    if write_log_file:
        _log_dir = resolve_log_dir(paths.logs_dir, "backup", settings.organize_logs)
        timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
        log_path = _log_dir / f"{timestamp}.{tc.name}.backup.log"
        log_file = open(log_path, "w", encoding="utf-8")

    def blog(msg: str) -> None:
        if log_file is not None:
            plain = re.sub(r"\[/?[a-z_ ]+\]", "", msg)
            log_file.write(plain + "\n")
            log_file.flush()
        _log(msg)

    new_checksum: str | None = None
    try:
        blog(f"Source: {install_dir}")
        blog(f"Target: {backup_file}")
        blog(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        blog("")

        proc = subprocess.Popen(
            ["tar", "-cJvf", str(backup_file), "-C", str(paths.toolchains_dir), tc.name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env={**os.environ, "XZ_OPT": "-e9"},
        )
        for line in proc.stdout:
            blog(f"  {rich_escape(line.rstrip())}")
        proc.wait()

        if proc.returncode != 0:
            blog(f"[red]Backup failed (exit {proc.returncode})[/red]")
            return False

        size_mb = backup_file.stat().st_size / (1024 * 1024)

        # Always update url + checksum in toolchains.local.yaml —
        # regardless of what was there before (upstream ``https://``,
        # previous ``file://`` pin, or empty). The local entry is the
        # user-editable copy of a defaults/ catalog entry and the
        # restore path reads from the local entry first. After a
        # backup the locally stored archive IS the source of truth.
        # Original upstream info stays preserved in
        # ``defaults/toolchains.yaml`` — if the user later wants to
        # reset to upstream: delete the install dir + remove the
        # local.yaml entry and the catalog URL becomes active again.
        h = hashlib.sha256()
        with open(backup_file, "rb") as bf:
            for chunk in iter(lambda: bf.read(8192), b""):
                h.update(chunk)
        new_checksum = f"sha256:{h.hexdigest()}"

        local_tcs = registry._load_file(registry.local_path)
        found = False
        for ltc in local_tcs:
            if ltc.name == tc.name:
                ltc.url = f"file://{backup_file.name}"
                ltc.checksum = new_checksum
                found = True
                break
        if not found:
            # Defensive fallback — if the local copy somehow disappeared
            # between install and backup, re-create it from the in-memory
            # tc so the backup is actually usable for a restore.
            tc.url = f"file://{backup_file.name}"
            tc.checksum = new_checksum
            local_tcs.append(tc)
        registry.save_local(local_tcs)

        blog("")
        blog(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        blog(f"Backup: {deliver_link(backup_file)}")
        blog(f"Size: {size_mb:.1f} MB")
        if new_checksum:
            blog(f"Checksum: {new_checksum}")
        if log_path is not None:
            _log("")
            _log(f"Log: {deliver_link(log_path)}")
        blog(f"[green]{tc.name} backed up to {backup_file.name}[/green]")
        return True

    except Exception as e:
        blog(f"[red]Backup failed: {e}[/red]")
        return False

    finally:
        if log_file is not None:
            log_file.close()
        try:
            run_retention(paths.logs_dir, settings, log=_log)
        except Exception:
            pass
