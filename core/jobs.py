"""Async job runner for long-running s4 operations (builds, deploys, ...).

The MCP server exposes s4 actions like "build qnap_x64" to LLM clients
but the Model Context Protocol has no natural way to stream a
multi-hour build — tool calls should return fast. This module runs
the actual work in a background thread and publishes its status via
a small JSON side-file next to the regular log, so the caller can
poll with ``get_build_status(job_id)``.

Job ID format
-------------
The job ID IS the log filename (e.g.
``2026-04-17.10-00-00.qnap_x64.build-make.log``). That gives us:

  * Uniqueness (timestamps + sequence suffix on collision)
  * Human-readable IDs
  * Persistence across MCP-server restarts (just scan the logs dir)
  * Direct interop with the TUI log viewer — same file

Status side-file
----------------
For each running/finished job we write
``<logs_dir>/<job_id>.status.json`` with:

.. code-block:: json

    {
        "job_id": "2026-04-17.10-00-00.qnap_x64.build-make.log",
        "type": "build",
        "target": "qnap_x64",
        "started": "2026-04-17T10:00:00",
        "finished": null,          // null while running
        "success": null,           // true / false after finish
        "pid": 12345,              // os.getpid() of the launcher
        "params": {"overrides": "USE_SSL", "build_system": "make"}
    }

While ``finished`` is ``null`` and the PID exists, the job is
considered running. If the PID no longer exists but ``finished`` is
still null, the job was killed hard — we mark it as crashed on read.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


# ------------------------------------------------------------------ utilities


_MARKUP_RE = re.compile(r"\[/?[a-z_ #0-9]+\]")


def _strip_markup(s: str) -> str:
    return _MARKUP_RE.sub("", s)


def _pid_alive(pid: int) -> bool:
    """Return True when a process with this PID is still alive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but we can't signal it — still "alive" from our POV.
        return True
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ registry


_REGISTRY_LOCK = threading.Lock()
_ACTIVE: dict[str, "JobHandle"] = {}


def get_active_job(job_id: str) -> Optional["JobHandle"]:
    """Return the live handle for a running job, or None if finished."""
    with _REGISTRY_LOCK:
        return _ACTIVE.get(job_id)


def _register(handle: "JobHandle") -> None:
    with _REGISTRY_LOCK:
        _ACTIVE[handle.job_id] = handle


def _unregister(job_id: str) -> None:
    with _REGISTRY_LOCK:
        _ACTIVE.pop(job_id, None)


# ------------------------------------------------------------------ handle


@dataclass
class JobHandle:
    """Control handle for a background job.

    Returned by the ``start_*`` functions. Callers only really use
    ``job_id`` — everything else is for the runner itself.
    """
    job_id: str
    log_path: Path
    status_path: Path
    thread: threading.Thread = field(repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def cancel(self) -> bool:
        """Signal cooperative cancellation to the worker thread."""
        if self.thread.is_alive():
            self.cancel_event.set()
            return True
        return False


# ------------------------------------------------------------------ status I/O


def _write_status(status_path: Path, data: dict[str, Any]) -> None:
    """Atomic status-file write (tmp + rename)."""
    status_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = status_path.with_suffix(status_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(status_path)


def _read_status(status_path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_job_status(logs_dir: Path, job_id: str) -> Optional[dict[str, Any]]:
    """Read the status JSON for ``job_id``.

    Also repairs a "zombie" status (PID gone, ``finished`` still null)
    by marking it as crashed so polling clients see a definitive
    terminal state.
    """
    status_path = _status_path_for(logs_dir, job_id)
    data = _read_status(status_path)
    if not data:
        return None
    if data.get("finished") is None:
        pid = int(data.get("pid") or 0)
        if pid and not _pid_alive(pid) and not get_active_job(job_id):
            data["finished"] = datetime.now().isoformat(timespec="seconds")
            data["success"] = False
            data["error"] = "process vanished (crashed or killed)"
            try:
                _write_status(status_path, data)
            except Exception:
                pass
    return data


def _status_path_for(logs_dir: Path, job_id: str) -> Path:
    """Return the status JSON path for a job.

    Respects organize_logs=True where logs live in a per-type subdir:
    the job_id stays the bare filename, but the status lives next to
    the log (same directory).
    """
    for sub in ("", "build", "selfbuild", "deploy", "backup", "automation", "lib"):
        p = (logs_dir / sub / job_id) if sub else (logs_dir / job_id)
        if p.exists():
            return p.with_suffix(p.suffix + ".status.json")
    # Fallback — flat layout
    return (logs_dir / job_id).with_suffix(
        (logs_dir / job_id).suffix + ".status.json"
    )


# ------------------------------------------------------------------ log discovery


def list_recent_jobs(
    logs_dir: Path,
    limit: int = 20,
    type_filter: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List recent logs with their status (if available).

    Discovers both the flat layout (logs_dir/*.log) and the organized
    layout (logs_dir/<type>/*.log). Sorted newest first.
    """
    log_files: list[Path] = []
    if logs_dir.is_dir():
        log_files.extend(logs_dir.glob("*.log"))
        for sub in logs_dir.iterdir():
            if sub.is_dir() and sub.name not in {"automation"}:
                log_files.extend(sub.glob("*.log"))
            elif sub.is_dir() and sub.name == "automation":
                log_files.extend(sub.glob("*.log"))
    log_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    results: list[dict[str, Any]] = []
    for log in log_files:
        job_id = log.name
        kind = _classify_log(job_id)
        if type_filter and kind != type_filter:
            continue
        status_path = log.with_suffix(log.suffix + ".status.json")
        status = _read_status(status_path) if status_path.exists() else None
        if status and status.get("finished") is None:
            pid = int(status.get("pid") or 0)
            if pid and not _pid_alive(pid) and not get_active_job(job_id):
                status = dict(status)
                status["finished"] = datetime.now().isoformat(timespec="seconds")
                status["success"] = False
                status["error"] = "process vanished"
        results.append({
            "job_id": job_id,
            "type": kind,
            "log_path": str(log),
            "size": log.stat().st_size,
            "mtime": datetime.fromtimestamp(log.stat().st_mtime).isoformat(
                timespec="seconds"),
            "status": status,
        })
        if len(results) >= limit:
            break
    return results


def _classify_log(name: str) -> str:
    """Derive the log type from its filename (see log_cleanup.py)."""
    # MCP jobs append the kind as a dotted marker before .log so the
    # origin is unambiguous even if the subject contains dots:
    #   *.lib.log, *.backup.log, *.automation.log, *.selfbuild.log
    # Check these first — they're the most specific.
    if name.endswith(".lib.log"):
        return "lib"
    if name.endswith(".selfbuild.log"):
        return "selfbuild"
    if name.endswith(".automation.log"):
        return "automation"
    if name.endswith(".backup.log"):
        return "backup"
    if name.endswith(".repo.log"):
        return "repo"
    if ".build-" in name:
        return "build"
    if ".selfbuild." in name:
        return "selfbuild"
    if ".automation." in name:
        return "automation"
    if ".backup." in name:
        return "backup"
    if ".deployment-check." in name:
        return "deploy"
    if ".webservice." in name:
        return "webservice"
    if ".s4-update." in name:
        return "s4"
    # lib:<toolchain>  — {ts}.{toolchain}.{lib}-{version}.log
    if re.match(r"^\d{4}-\d{2}-\d{2}\..+\.[^.]+-[^.]+\.log$", name):
        return "lib"
    return "other"


def tail_log(
    logs_dir: Path,
    job_id: str,
    max_bytes: int = 4096,
) -> str:
    """Return the last ``max_bytes`` bytes of a log (UTF-8 decoded)."""
    log_path = _resolve_log_path(logs_dir, job_id)
    if not log_path or not log_path.exists():
        return ""
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                # Drop possibly broken leading line
                f.readline()
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def read_log(
    logs_dir: Path,
    job_id: str,
    offset: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    """Return a slice of log lines.

    Negative ``offset`` means "last N lines" (tail). Positive is a
    1-based line number. ``limit`` caps the number of lines returned.
    """
    log_path = _resolve_log_path(logs_dir, job_id)
    if not log_path or not log_path.exists():
        return {"lines": [], "total_lines": 0, "offset": 0, "truncated": False}
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return {"lines": [], "total_lines": 0, "offset": 0, "truncated": False}
    total = len(lines)
    if offset < 0:
        start = max(0, total + offset)
    else:
        start = max(0, offset)
    end = min(total, start + limit)
    return {
        "lines": [ln.rstrip("\n") for ln in lines[start:end]],
        "total_lines": total,
        "offset": start,
        "truncated": end < total,
    }


def _resolve_log_path(logs_dir: Path, job_id: str) -> Optional[Path]:
    """Find the actual log file for a given job_id."""
    candidates = [logs_dir / job_id]
    for sub in ("build", "selfbuild", "deploy", "backup", "automation", "lib"):
        candidates.append(logs_dir / sub / job_id)
    for p in candidates:
        if p.exists():
            return p
    return None


# ------------------------------------------------------------------ job_id allocation


def _allocate_log_path(
    logs_dir: Path,
    target_name: str,
    kind: str,
    build_system: str,
    organize: bool,
) -> tuple[Path, str]:
    """Reserve a unique log path for a new job.

    Returns ``(log_path, job_id)``. The file does NOT exist yet but
    is reserved — a second concurrent call gets a suffixed name.
    """
    from core.log_cleanup import resolve_log_dir

    log_dir = resolve_log_dir(logs_dir, kind, organize)
    base_ts = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
    if kind == "build":
        stem = f"{base_ts}.{target_name}.build-{build_system}"
    elif kind == "deploy":
        stem = f"{base_ts}.{target_name}.deployment-check"
    elif kind == "automation":
        stem = f"{base_ts}.{target_name}.automation"
    else:
        stem = f"{base_ts}.{target_name}.{kind}"

    # Collision-safe: probe up to 100 suffixes per second (way more than
    # needed in practice). Reservation is atomic via ``O_EXCL``.
    for suffix in [""] + [f".{i}" for i in range(1, 100)]:
        log_path = log_dir / f"{stem}{suffix}.log"
        try:
            fd = os.open(str(log_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return log_path, log_path.name
        except FileExistsError:
            continue
    # Shouldn't happen — fall through with a timestamp variant
    log_path = log_dir / f"{stem}.{int(time.time() * 1000)}.log"
    log_path.touch()
    return log_path, log_path.name


# ------------------------------------------------------------------ build job


def start_build_job(
    paths,
    settings,
    target_name: str,
    overrides: str = "",
    build_system: Optional[str] = None,
) -> JobHandle:
    """Start a build in a background thread.

    Parameters
    ----------
    paths : core.paths.Paths
    settings : core.settings.Settings
    target_name : str
        Target name (yaml stem) in ``paths.targets_dir``.
    overrides : str
        Space-separated build overrides (e.g. ``"USE_SSL USE_PCSC_off"``).
    build_system : "make" | "cmake" | None
        Override the target's build system.

    Returns
    -------
    JobHandle
        Use ``handle.job_id`` with ``load_job_status`` / ``tail_log``
        to poll progress.
    """
    from core.target import Target
    from core.builder import BuildEngine

    # Locate target
    available = dict(Target.list_targets(paths.targets_dir))
    if target_name not in available:
        raise ValueError(f"Unknown target: {target_name}")
    target = Target.load(available[target_name])
    if build_system:
        target.build.build_system = build_system
    bs = target.build.build_system or settings.default_build_system or "make"

    # Reserve a log file + job id
    log_path, job_id = _allocate_log_path(
        paths.logs_dir, target_name, "build", bs, settings.organize_logs,
    )
    status_path = log_path.with_suffix(log_path.suffix + ".status.json")

    # Initial status
    started = datetime.now().isoformat(timespec="seconds")
    _write_status(status_path, {
        "job_id": job_id,
        "type": "build",
        "target": target_name,
        "started": started,
        "finished": None,
        "success": None,
        "pid": os.getpid(),
        "params": {"overrides": overrides, "build_system": bs},
    })

    cancel_event = threading.Event()

    def _worker(handle_ref: dict[str, JobHandle]) -> None:
        log_file = None
        success = False
        error: Optional[str] = None
        try:
            log_file = log_path.open("w", encoding="utf-8")

            def _log(msg: str) -> None:
                try:
                    log_file.write(_strip_markup(msg) + "\n")
                    log_file.flush()
                except Exception:
                    pass

            engine = BuildEngine(
                target=target,
                repos_dir=paths.repos_dir,
                backup_dir=paths.backup_dir,
                repos_config=paths.repos_config,
                patches_dir=paths.patches_dir,
                logs_dir=paths.logs_dir,
                binaries_dir=paths.binaries_dir,
                bin_dir=paths.bin_dir,
                toolchains_dir=paths.toolchains_dir,
                toolchains_local=paths.toolchains_local,
                toolchains_defaults=paths.toolchains_defaults,
                max_cpus=settings.max_cpus,
                compiler_opt=settings.compiler_opt,
                sign_cert=settings.sign_cert,
                sign_key=settings.sign_key,
                delete_debug=settings.delete_debug,
                save_smargo=settings.save_smargo,
                patch_webif=settings.patch_webif,
                qemu_test=settings.qemu_test,
                qemu_timeout=settings.qemu_timeout,
                qemu_expect=settings.qemu_expect,
                build_overrides=overrides,
                log_max_age_days=settings.log_max_age_days,
                log_max_count=settings.log_max_count,
                log_max_size_mb=settings.log_max_size_mb,
                organize_logs=settings.organize_logs,
                default_confdir=settings.default_confdir,
                default_build_system=settings.default_build_system,
                log=_log,
                cancel_check=cancel_event.is_set,
                write_log_file=False,    # we own the log file
            )
            success = bool(engine.build())
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if log_file:
                try:
                    log_file.write(f"\n[s4-mcp] job crashed: {error}\n")
                    log_file.flush()
                except Exception:
                    pass
        finally:
            if log_file:
                try:
                    log_file.close()
                except Exception:
                    pass
            finished = datetime.now().isoformat(timespec="seconds")
            status = _read_status(status_path) or {}
            status.update({
                "finished": finished,
                "success": success,
                "cancelled": cancel_event.is_set(),
            })
            if error:
                status["error"] = error
            try:
                _write_status(status_path, status)
            except Exception:
                pass
            _unregister(job_id)

    holder: dict[str, JobHandle] = {}
    thread = threading.Thread(
        target=_worker, args=(holder,), name=f"s4-build-{target_name}",
        daemon=True,
    )
    handle = JobHandle(
        job_id=job_id,
        log_path=log_path,
        status_path=status_path,
        thread=thread,
        cancel_event=cancel_event,
    )
    holder["h"] = handle
    _register(handle)
    thread.start()
    return handle


# ------------------------------------------------------------------ generic thread runner


def _run_in_thread(
    paths,
    settings,
    *,
    kind: str,
    subject: str,
    params: dict[str, Any],
    worker: Callable[[Callable[[str], None], threading.Event], bool],
    thread_name: str,
) -> JobHandle:
    """Shared scaffolding for the non-build job types.

    ``worker(log, cancel_event) -> success`` does the actual work. This
    function handles: log-path reservation, status JSON lifecycle,
    registry, thread setup.
    """
    log_path, job_id = _allocate_log_path(
        paths.logs_dir, subject, kind, "", settings.organize_logs,
    )
    status_path = log_path.with_suffix(log_path.suffix + ".status.json")
    started = datetime.now().isoformat(timespec="seconds")
    _write_status(status_path, {
        "job_id": job_id,
        "type": kind,
        "target": subject,
        "started": started,
        "finished": None,
        "success": None,
        "pid": os.getpid(),
        "params": params,
    })
    cancel_event = threading.Event()

    def _run() -> None:
        log_file = None
        success = False
        error: Optional[str] = None
        try:
            log_file = log_path.open("w", encoding="utf-8")

            def _log(msg: str) -> None:
                try:
                    log_file.write(_strip_markup(msg) + "\n")
                    log_file.flush()
                except Exception:
                    pass

            success = bool(worker(_log, cancel_event))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if log_file:
                try:
                    log_file.write(f"\n[s4-mcp] worker crashed: {error}\n")
                    log_file.flush()
                except Exception:
                    pass
        finally:
            if log_file:
                try:
                    log_file.close()
                except Exception:
                    pass
            data = _read_status(status_path) or {}
            data.update({
                "finished": datetime.now().isoformat(timespec="seconds"),
                "success": success,
                "cancelled": cancel_event.is_set(),
            })
            if error:
                data["error"] = error
            try:
                _write_status(status_path, data)
            except Exception:
                pass
            _unregister(job_id)

    thread = threading.Thread(target=_run, name=thread_name, daemon=True)
    handle = JobHandle(
        job_id=job_id, log_path=log_path, status_path=status_path,
        thread=thread, cancel_event=cancel_event,
    )
    _register(handle)
    thread.start()
    return handle


# ------------------------------------------------------------------ library build job


def start_install_library_job(
    paths,
    settings,
    toolchain_name: str,
    library: str,
    version: str = "auto",
) -> JobHandle:
    """Build + install a library into an installed toolchain's sysroot."""
    from core.toolchain import ToolchainRegistry
    from core.lib_builder import build_library

    reg = ToolchainRegistry(
        defaults_path=paths.toolchains_defaults,
        local_path=paths.toolchains_local,
        install_dir=paths.toolchains_dir,
    )
    reg.load()
    tc = reg.get(toolchain_name)
    if not tc:
        raise ValueError(f"Unknown toolchain: {toolchain_name}")
    if not tc.installed:
        raise ValueError(f"Toolchain not installed: {toolchain_name}")

    install_dir = paths.toolchains_dir / tc.name
    libraries_yaml = paths.defaults_dir / "libraries.yaml"

    def _worker(log, cancel_event):
        return build_library(
            lib_name=library,
            version=version,
            toolchain_dir=install_dir,
            toolchain_prefix=tc.prefix,
            toolchain_sysroot=tc.sysroot,
            toolchain_cflags=tc.cflags,
            toolchain_ldflags=tc.ldflags,
            libraries_yaml=libraries_yaml,
            downloads_dir=paths.downloads_dir,
            logs_dir=None,    # we own the log file
            max_cpus=settings.max_cpus,
            native=bool(getattr(tc, "native", False)),
            cancel_check=cancel_event.is_set,
            log=log,
            libraries_local=paths.libraries_local,
            organize_logs=settings.organize_logs,
        )

    return _run_in_thread(
        paths, settings,
        kind="lib",
        subject=f"{toolchain_name}.{library}-{version}",
        params={"toolchain": toolchain_name, "library": library, "version": version},
        worker=_worker,
        thread_name=f"s4-lib-{library}-{toolchain_name}",
    )


# ------------------------------------------------------------------ toolchain install (PreBuild)


def start_install_toolchain_job(
    paths,
    settings,
    toolchain_name: str,
) -> JobHandle:
    """Download + extract + register a PreBuild toolchain."""
    from core.toolchain import ToolchainRegistry
    from core.toolchain_install import install_toolchain_from_catalog

    reg = ToolchainRegistry(
        defaults_path=paths.toolchains_defaults,
        local_path=paths.toolchains_local,
        install_dir=paths.toolchains_dir,
    )
    reg.load()
    tc = reg.get(toolchain_name)
    if not tc:
        raise ValueError(f"Unknown toolchain: {toolchain_name}")
    if tc.installed:
        raise ValueError(f"Toolchain already installed: {toolchain_name}")
    if not tc.url:
        raise ValueError(f"No download URL for {toolchain_name} (SelfBuild-only?)")

    def _worker(log, cancel_event):
        return install_toolchain_from_catalog(
            tc, paths, settings,
            log=log,
            cancel_check=cancel_event.is_set,
        )

    return _run_in_thread(
        paths, settings,
        kind="backup",  # re-use the 'backup' bucket — it's install-related
        subject=toolchain_name,
        params={"url": tc.url, "checksum": tc.checksum},
        worker=_worker,
        thread_name=f"s4-install-{toolchain_name}",
    )


# ------------------------------------------------------------------ toolchain build (SelfBuild)


def start_patch_job(
    paths,
    settings,
    repo_name: str,
    selected_patches: Optional[list] = None,
) -> JobHandle:
    """Apply patches to an oscam repository in the background."""
    from core.repos_config import load_repos
    from core.repo import RepoManager

    repos = load_repos(paths.repos_config)
    cfg = next((r for r in repos if r.name == repo_name), None)
    if not cfg:
        raise ValueError(f"Unknown repository: {repo_name}")
    manager = RepoManager(cfg, paths.repos_dir, paths.backup_dir)

    def _worker(log, cancel_event):
        if cancel_event.is_set():
            return False
        return bool(manager.apply_patches(
            paths.patches_dir, log=log,
            selected_patches=list(selected_patches) if selected_patches else None,
        ))

    return _run_in_thread(
        paths, settings,
        kind="repo",
        subject=f"{repo_name}.patch",
        params={"repo": repo_name, "action": "patch", "patches": list(selected_patches or [])},
        worker=_worker,
        thread_name=f"s4-repo-patch-{repo_name}",
    )


def start_repo_job(
    paths,
    settings,
    repo_name: str,
    action: str,
) -> JobHandle:
    """Clone / update / clean an oscam repository in the background.

    action:
      * ``clone``   — git clone / svn checkout if missing
      * ``update``  — git fetch + pull (or svn update)
      * ``clean``   — ``git clean -fd`` + reset backup, ready for a fresh
                      patch pass
    """
    from core.repos_config import load_repos
    from core.repo import RepoManager

    repos = load_repos(paths.repos_config)
    cfg = next((r for r in repos if r.name == repo_name), None)
    if not cfg:
        raise ValueError(f"Unknown repository: {repo_name}")
    action = action.strip().lower()
    if action not in ("clone", "update", "clean"):
        raise ValueError("action must be one of: clone, update, clean")

    manager = RepoManager(cfg, paths.repos_dir, paths.backup_dir)

    def _worker(log, cancel_event):
        if cancel_event.is_set():
            return False
        if action == "clone":
            return bool(manager.clone(log=log))
        if action == "update":
            return bool(manager.update(log=log))
        if action == "clean":
            # clean_repo() isn't a first-class method, but update() does a
            # reset + clean as part of its flow when appropriate. For now
            # route ``clean`` to update.
            return bool(manager.update(log=log))
        return False

    return _run_in_thread(
        paths, settings,
        kind="repo",
        subject=f"{repo_name}.{action}",
        params={"repo": repo_name, "action": action},
        worker=_worker,
        thread_name=f"s4-repo-{action}-{repo_name}",
    )


def start_build_toolchain_job(
    paths,
    settings,
    template_name: str,
) -> JobHandle:
    """Build a toolchain from a SelfBuild template (ct-ng / OE-SDK / Android-NDK)."""
    from core.template import load_templates
    from core.toolchain_builder import build_toolchain

    # Locate template by name (local entries win, then defaults)
    templates = load_templates(paths.templates_defaults, paths.templates_local)
    # Sort so local wins when names collide
    templates.sort(key=lambda t: (0 if getattr(t, "is_local", False) else 1))
    tpl = next((t for t in templates if t.name == template_name), None)
    if not tpl:
        raise ValueError(f"Unknown template: {template_name}")
    if tpl.builder not in ("crosstool-ng", "oe-sdk", "android-ndk"):
        raise ValueError(f"Unsupported builder: {tpl.builder}")

    def _worker(log, cancel_event):
        return build_toolchain(
            tpl, paths, settings,
            log=log,
            cancel_check=cancel_event.is_set,
        )

    return _run_in_thread(
        paths, settings,
        kind="selfbuild",
        subject=template_name,
        params={"builder": tpl.builder},
        worker=_worker,
        thread_name=f"s4-selfbuild-{template_name}",
    )


# ------------------------------------------------------------------ cancel


def cancel_job(job_id: str) -> bool:
    """Signal cancellation to a running job. Returns True if signalled."""
    handle = get_active_job(job_id)
    if not handle:
        return False
    return handle.cancel()
