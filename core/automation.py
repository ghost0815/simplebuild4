"""s4 automation — cron-based job automation via system crontab.

Jobs are defined in data/automation.yaml and executed via CLI:
  ./s4 run-job <name>

Crontab entries are managed per s4 installation using marker comments
so multiple s4 instances don't interfere with each other.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import yaml


# ---------------------------------------------------------------------------
# Human-readable cron description
# ---------------------------------------------------------------------------

_DOW_NAMES = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday",
              4: "Thursday", 5: "Friday", 6: "Saturday"}


def _parse_cron_field(expr: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of allowed values."""
    result: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                step = 1
            part = base
        if part == "*":
            lo, hi = min_val, max_val
        elif "-" in part:
            try:
                lo, hi = (int(x) for x in part.split("-", 1))
            except ValueError:
                continue
        else:
            try:
                lo = hi = int(part)
            except ValueError:
                continue
        for v in range(lo, hi + 1, step):
            if min_val <= v <= max_val:
                result.add(v)
    return result


def cron_next_run(expr: str, from_dt: "datetime | None" = None, max_years: int = 2) -> "datetime | None":
    """Compute the next matching datetime for a 5-field cron expression.

    Returns None if the expression is invalid or no match within max_years.
    """
    try:
        fields = expr.strip().split()
        if len(fields) != 5:
            return None
        mins = _parse_cron_field(fields[0], 0, 59)
        hours = _parse_cron_field(fields[1], 0, 23)
        doms = _parse_cron_field(fields[2], 1, 31)
        months = _parse_cron_field(fields[3], 1, 12)
        dows = _parse_cron_field(fields[4], 0, 6)  # 0=Sunday
        if not all([mins, hours, doms, months, dows]):
            return None
    except Exception:
        return None

    now = (from_dt or datetime.now()).replace(second=0, microsecond=0)
    # Start at next minute
    from datetime import timedelta
    candidate = now + timedelta(minutes=1)
    end = now.replace(year=now.year + max_years)
    while candidate <= end:
        if (candidate.month in months and
            candidate.day in doms and
            candidate.hour in hours and
            candidate.minute in mins and
            (candidate.isoweekday() % 7) in dows):
            return candidate
        candidate += timedelta(minutes=1)
        # Fast-forward optimisations
        if candidate.minute not in mins:
            # Jump to next allowed minute in same hour if possible
            future_mins = [m for m in mins if m > candidate.minute]
            if future_mins:
                candidate = candidate.replace(minute=min(future_mins))
            else:
                candidate = (candidate.replace(minute=0) + timedelta(hours=1))
    return None


def cron_to_human(expr: str) -> str:
    """Convert a 5-field cron expression to a human-readable description."""
    try:
        fields = expr.strip().split()
        if len(fields) != 5:
            return ""

        minute, hour, dom, month, dow = fields

        # Accept: *  */N  N  N-M  N-M/S  and comma-separated lists of these
        _cron_re = re.compile(
            r'^(\*(/\d+)?|\d+(-\d+)?(/\d+)?(,\d+(-\d+)?(/\d+)?)*)$'
        )
        for f in fields:
            if not _cron_re.match(f):
                return ""

        parts = []

        if minute.startswith("*/") and hour == "*":
            parts.append(f"Every {minute[2:]} minutes")
        elif minute == "*" and hour.startswith("*/"):
            parts.append(f"Every {hour[2:]} hours")
        elif minute == "*" and hour == "*":
            parts.append("Every minute")
        elif hour == "*":
            parts.append(f"Every hour at :{minute.zfill(2)}")
        elif minute.startswith("*/"):
            parts.append(f"Every {minute[2:]} minutes during hour {hour}")
        else:
            parts.append(f"At {hour.zfill(2)}:{minute.zfill(2)}")

        if dom != "*":
            if "," in dom:
                parts.append(f"on days {dom}")
            elif "-" in dom:
                parts.append(f"on days {dom}")
            elif dom.startswith("*/"):
                parts.append(f"every {dom[2:]} days")
            else:
                parts.append(f"on day {dom}")

        _month_names = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
                        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
        if month != "*":
            if "," in month:
                names = [_month_names.get(int(m), m) for m in month.split(",")]
                parts.append(f"in {', '.join(names)}")
            elif "-" in month:
                lo, hi = month.split("-", 1)
                parts.append(f"from {_month_names.get(int(lo), lo)} to {_month_names.get(int(hi), hi)}")
            else:
                parts.append(f"in {_month_names.get(int(month), month)}")

        if dow != "*":
            if dow == "1-5":
                parts.append("on weekdays")
            elif dow == "0,6":
                parts.append("on weekends")
            elif "," in dow:
                names = [_DOW_NAMES.get(int(d), d) for d in dow.split(",")]
                parts.append(f"on {', '.join(names)}")
            elif "-" in dow:
                lo, hi = dow.split("-", 1)
                parts.append(f"from {_DOW_NAMES.get(int(lo), lo)} to {_DOW_NAMES.get(int(hi), hi)}")
            else:
                parts.append(f"on {_DOW_NAMES[int(dow)]}")

        return ", ".join(parts)
    except (ValueError, KeyError, IndexError):
        return ""


# ---------------------------------------------------------------------------
# Schedule config
# ---------------------------------------------------------------------------

@dataclass
class JobConfig:
    """A single automation job."""
    name: str = ""
    enabled: bool = True
    cron: str = ""           # 5-field cron expression
    actions: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    toolchains: list[str] = field(default_factory=list)


_ACTION_MIGRATION = {
    "build": "build-targets-force",
    "poll_repos": "update-repositories",
    "poll_and_build": "update-repositories",
    "update_libs": "update-libraries",
}


def _migrate_actions(actions: list[str]) -> list[str]:
    return [_ACTION_MIGRATION.get(a, a) for a in actions]


def load_jobs(path: Path) -> list[JobConfig]:
    """Load jobs from YAML."""
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return []
    if not data or "jobs" not in data:
        return []
    jobs = []
    for entry in data["jobs"]:
        jobs.append(JobConfig(
            name=entry.get("name", ""),
            enabled=entry.get("enabled", True),
            cron=entry.get("cron", ""),
            actions=_migrate_actions(entry.get("actions", []) or ([entry["action"]] if entry.get("action") else [])),
            targets=entry.get("targets", []) or [],
            repos=entry.get("repos", []) or [],
            toolchains=entry.get("toolchains", []) or [],
        ))
    return jobs


def save_jobs(jobs: list[JobConfig], path: Path) -> None:
    """Save jobs to YAML."""
    data = {
        "jobs": [
            {k: v for k, v in {
                "name": s.name,
                "enabled": s.enabled,
                "cron": s.cron,
                "actions": s.actions or None,
                "targets": s.targets or None,
                "repos": s.repos or None,
                "toolchains": s.toolchains or None,
            }.items() if v is not None}
            for s in jobs
        ]
    }
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Schedule state (last_run, last_result per job)
# ---------------------------------------------------------------------------

@dataclass
class JobState:
    """Runtime state for a single job."""
    last_run: str = ""
    last_result: str = ""    # "ok" | "fail" | ""
    running: bool = False    # True while a job instance is currently running
    pid: int = 0             # PID of the running process (0 if not running)
    started_at: str = ""     # ISO timestamp when the current run started
    duration_sec: int = 0    # Duration of the last completed run in seconds


def _pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = just check existence
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def load_state(path: Path) -> dict[str, JobState]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    if not data:
        return {}
    states = {}
    for name, entry in data.items():
        s = JobState(
            last_run=entry.get("last_run", ""),
            last_result=entry.get("last_result", ""),
            running=entry.get("running", False),
            pid=entry.get("pid", 0),
            started_at=entry.get("started_at", ""),
            duration_sec=int(entry.get("duration_sec", 0) or 0),
        )
        # Crash recovery: if marked running but PID is dead, clear it
        if s.running and not _pid_alive(s.pid):
            s.running = False
            s.pid = 0
            s.started_at = ""
        states[name] = s
    return states


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via temp file + rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def save_state(states: dict[str, JobState], path: Path) -> None:
    """Save state atomically with file lock to prevent concurrent writes."""
    import fcntl
    data = {
        name: {
            "last_run": s.last_run,
            "last_result": s.last_result,
            "running": s.running,
            "pid": s.pid,
            "started_at": s.started_at,
            "duration_sec": s.duration_sec,
        }
        for name, s in states.items()
    }
    content = yaml.dump(data, default_flow_style=False, sort_keys=False)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_f:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
        except Exception:
            pass  # flock may not be available on all filesystems
        _atomic_write(path, content)


def update_state(path: Path, job_name: str, **updates) -> JobState:
    """Read-modify-write a single job's state with file lock.

    Returns the updated JobState.
    """
    import fcntl
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_f:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
        except Exception:
            pass
        states = load_state(path)
        state = states.setdefault(job_name, JobState())
        for key, value in updates.items():
            setattr(state, key, value)
        # Write (reusing save_state would double-lock; do inline)
        data = {
            name: {
                "last_run": s.last_run,
                "last_result": s.last_result,
                "running": s.running,
                "pid": s.pid,
                "started_at": s.started_at,
                "duration_sec": s.duration_sec,
            }
            for name, s in states.items()
        }
        _atomic_write(path, yaml.dump(data, default_flow_style=False, sort_keys=False))
        return state


def try_acquire_run(path: Path, job_name: str, pid: int, started_at: str) -> bool:
    """Atomically claim the 'running' slot for a job.

    Returns True if successful (job was not already running), False otherwise.
    Held under the state file lock to prevent concurrent claims.
    """
    import fcntl
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_f:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
        except Exception:
            pass
        states = load_state(path)  # load_state already clears dead PIDs
        state = states.setdefault(job_name, JobState())
        if state.running:
            return False  # another live instance holds the slot
        state.running = True
        state.pid = pid
        state.started_at = started_at
        data = {
            name: {
                "last_run": s.last_run,
                "last_result": s.last_result,
                "running": s.running,
                "pid": s.pid,
                "started_at": s.started_at,
                "duration_sec": s.duration_sec,
            }
            for name, s in states.items()
        }
        _atomic_write(path, yaml.dump(data, default_flow_style=False, sort_keys=False))
        return True


# ---------------------------------------------------------------------------
# Helper: find targets that use a given repo
# ---------------------------------------------------------------------------

def find_targets_for_repo(repo_name: str, targets_dir: Path) -> list[str]:
    from core.target import Target
    matches = []
    for name, path in Target.list_targets(targets_dir):
        try:
            t = Target.load(path)
            for src in t.sources:
                if src.name == repo_name:
                    matches.append(name)
                    break
        except Exception:
            continue
    return matches


# ---------------------------------------------------------------------------
# Crontab management
# ---------------------------------------------------------------------------

def _crontab_marker(root: Path) -> str:
    """Unique marker identifying this s4 installation in crontab."""
    return f"s4-auto[{root}]"


def install_crontab(jobs: list[JobConfig], root: Path, organize_logs: bool = False) -> str:
    """Install enabled jobs into user crontab.

    Only touches entries belonging to this s4 installation (identified by
    root path marker). Other entries and other s4 installations are preserved.

    Returns status message.
    """
    from core.log_cleanup import resolve_log_dir
    marker = _crontab_marker(root)
    s4_cmd = root / "s4"
    if not s4_cmd.exists():
        s4_cmd = root / "s4.py"
    logs_dir = resolve_log_dir(root / "data" / "logs", "automation", organize_logs)

    # Read existing crontab
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        existing = result.stdout if result.returncode == 0 else ""
    except Exception:
        existing = ""

    # Remove our old entries (between markers)
    cleaned_lines = []
    skip = False
    for line in existing.splitlines():
        if line.strip().startswith(f"# {marker}"):
            skip = True
            continue
        if skip and line.strip() == "# s4-auto-end":
            skip = False
            continue
        if not skip:
            cleaned_lines.append(line)

    # Build new entries — let run_job handle the per-run logfile itself
    import shlex
    new_entries = []
    enabled_count = 0
    for sched in jobs:
        if not sched.enabled or not sched.cron or not sched.name:
            continue
        enabled_count += 1
        quoted_name = shlex.quote(sched.name)
        new_entries.append(f"# {marker} {sched.name}")
        new_entries.append(
            f"{sched.cron} cd {shlex.quote(str(root))} && ./s4 run-job {quoted_name}"
        )
        new_entries.append("# s4-auto-end")

    # Combine
    final_lines = cleaned_lines
    if new_entries:
        # Ensure blank line before our block
        if final_lines and final_lines[-1].strip():
            final_lines.append("")
        final_lines.extend(new_entries)

    # Remove trailing blank lines
    while final_lines and not final_lines[-1].strip():
        final_lines.pop()
    final_lines.append("")  # crontab needs trailing newline

    # Write crontab
    try:
        proc = subprocess.run(
            ["crontab", "-"],
            input="\n".join(final_lines),
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return f"crontab install failed: {proc.stderr.strip()}"
    except Exception as e:
        return f"crontab install failed: {e}"

    return f"{enabled_count} job(s) installed in crontab"


def uninstall_crontab(root: Path) -> str:
    """Remove all crontab entries for this s4 installation."""
    marker = _crontab_marker(root)

    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return "No crontab found"
        existing = result.stdout
    except Exception:
        return "No crontab found"

    cleaned_lines = []
    skip = False
    removed = 0
    for line in existing.splitlines():
        if line.strip().startswith(f"# {marker}"):
            skip = True
            removed += 1
            continue
        if skip and line.strip() == "# s4-auto-end":
            skip = False
            continue
        if not skip:
            cleaned_lines.append(line)

    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()
    cleaned_lines.append("")

    try:
        subprocess.run(
            ["crontab", "-"],
            input="\n".join(cleaned_lines),
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return f"crontab removal failed: {e}"

    return f"{removed} job(s) removed from crontab"


def is_crontab_installed(root: Path) -> bool:
    """Check if this s4 installation has entries in crontab."""
    marker = _crontab_marker(root)
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        return marker in (result.stdout or "")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Job execution (called from CLI: ./s4 run-job <name>)
# ---------------------------------------------------------------------------

def run_job(job_name: str, paths, settings, log: Optional[Callable[[str], None]] = None) -> bool:
    """Execute a single job by name. Called from CLI.

    Args:
        job_name: Name of the job in automation.yaml
        paths: core.paths.Paths instance
        settings: core.settings.Settings instance
        log: Optional log callback (prints to stdout if None)

    Returns:
        True if job succeeded, False otherwise.
    """
    jobs = load_jobs(paths.automation_yaml)
    sched = None
    for s in jobs:
        if s.name == job_name:
            sched = s
            break

    # Always open a per-run logfile; tee output to optional callback
    from core.log_cleanup import resolve_log_dir
    log_dir = resolve_log_dir(paths.logs_dir, "automation", settings.organize_logs)
    timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
    safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', job_name) if job_name else "unknown"
    log_path = log_dir / f"{timestamp}.{safe_name}.automation.log"
    log_file = open(log_path, "w", encoding="utf-8")

    user_log = log

    def log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        plain = re.sub(r'\[/?[a-z_ #0-9]+\]', '', msg)
        log_file.write(f"[{ts}] {plain}\n")
        log_file.flush()
        # Also prefix the UI message so the RichLog shows timestamps
        stamped = f"[dim]\\[{ts}][/dim] {msg}"
        if user_log is not None:
            user_log(stamped)
        else:
            print(f"[{ts}] {plain}")

    if not sched:
        log(f"Job not found: {job_name}")
        available = [s.name for s in jobs if s.name]
        if available:
            log(f"Available jobs: {', '.join(available)}")
        log_file.close()
        return False

    # Try to atomically acquire the running slot — prevents concurrent runs
    now_iso = datetime.now().isoformat(timespec="seconds")
    if not try_acquire_run(paths.automation_state_yaml, sched.name, os.getpid(), now_iso):
        # Another instance is already running — find out who
        other_states = load_state(paths.automation_state_yaml)
        other = other_states.get(sched.name)
        other_pid = other.pid if other else 0
        log(f"Job '{sched.name}' is already running (pid {other_pid}) — aborting")
        log_file.close()
        try:
            log_path.unlink()  # remove empty log file
        except Exception:
            pass
        return False

    actions_str = ", ".join(sched.actions) if sched.actions else "none"
    log(f"Starting job: {sched.name} (actions: {actions_str})")

    # Per-job flags
    repos_updated = False
    repos_checked = False

    # Action implementations
    build_lock = threading.Lock()

    def _action_build(s: JobConfig) -> bool:
        if not s.targets:
            log("No targets specified")
            return False
        from core.builder import BuildEngine
        from core.target import Target
        all_ok = True
        available = dict(Target.list_targets(paths.targets_dir))
        target_list = list(available.keys()) if "all" in s.targets else s.targets
        for target_name in target_list:
            if target_name not in available:
                log(f"Target not found: {target_name}")
                all_ok = False
                continue
            target = Target.load(available[target_name])
            log(f"Building target: {target_name}")
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
                log_max_age_days=settings.log_max_age_days,
                log_max_count=settings.log_max_count,
                log_max_size_mb=settings.log_max_size_mb,
                organize_logs=settings.organize_logs,
                default_confdir=settings.default_confdir,
                default_build_system=settings.default_build_system,
                log=log,
            )
            if not engine.build():
                all_ok = False
        return all_ok

    def _action_update_repos(s: JobConfig) -> bool:
        nonlocal repos_updated, repos_checked
        from core.repo import RepoManager, RepoState
        from core.repos_config import load_repos
        from core.target import Target

        all_repos_config = load_repos(paths.repos_config)
        repo_names: set[str] = set()
        if s.repos:
            if "all" in s.repos:
                repo_names = {r.name for r in all_repos_config}
            else:
                repo_names = set(s.repos)
        elif s.targets:
            available = dict(Target.list_targets(paths.targets_dir))
            targets_to_check = s.targets
            if "all" in targets_to_check:
                targets_to_check = list(available.keys())
            for tn in targets_to_check:
                if tn in available:
                    t = Target.load(available[tn])
                    for src in t.sources:
                        repo_names.add(src.name)
        if not repo_names:
            log("No matching repositories")
            repos_checked = True
            repos_updated = False
            return False

        any_changes = False

        for repo_name in sorted(repo_names):
            log(f"Checking repo '{repo_name}'...")
            rc = next((r for r in all_repos_config if r.name == repo_name), None)
            if not rc:
                log(f"Repo config not found: {repo_name}")
                continue
            repo_dir = paths.repos_dir / f"oscam-{repo_name}"
            if not repo_dir.is_dir():
                repo_dir = paths.repos_dir / repo_name
            if not repo_dir.is_dir():
                log(f"Repo directory not found: {repo_name}")
                continue

            old_state = RepoState.detect(repo_dir)
            old_commit = old_state.commit or old_state.revision or ""

            if old_state.vcs == "git":
                changed = _git_has_new_commits(repo_dir, rc.branch)
            elif old_state.vcs == "svn":
                changed = _svn_has_new_commits(repo_dir)
            else:
                changed = True

            if not changed:
                log(f"No new commits for '{repo_name}'")
                continue

            mgr = RepoManager(rc, repo_dir, paths.backup_dir)
            mgr.update(log=log)
            new_state = RepoState.detect(repo_dir)
            new_commit = new_state.commit or new_state.revision or ""
            log(f"Updated '{repo_name}': {old_commit} → {new_commit}")
            any_changes = True

        repos_checked = True
        repos_updated = any_changes
        if not any_changes:
            log("No changes found in any repo")
        return True

    def _git_has_new_commits(repo_dir: Path, branch: str) -> bool:
        try:
            old_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True, timeout=10,
            ).strip()
            subprocess.run(
                ["git", "fetch", "origin"], cwd=repo_dir,
                capture_output=True, text=True, timeout=120,
            )
            remote_ref = f"origin/{branch}" if branch else "origin/HEAD"
            try:
                remote_head = subprocess.check_output(
                    ["git", "rev-parse", remote_ref], cwd=repo_dir, text=True, timeout=10,
                ).strip()
            except subprocess.CalledProcessError:
                remote_head = subprocess.check_output(
                    ["git", "rev-parse", "FETCH_HEAD"], cwd=repo_dir, text=True, timeout=10,
                ).strip()
            return old_head != remote_head
        except Exception:
            return False

    def _svn_has_new_commits(repo_dir: Path) -> bool:
        try:
            local_rev = subprocess.check_output(
                ["svn", "info", "--show-item", "revision"], cwd=repo_dir, text=True, timeout=10,
            ).strip()
            url = subprocess.check_output(
                ["svn", "info", "--show-item", "url"], cwd=repo_dir, text=True, timeout=10,
            ).strip()
            remote_rev = subprocess.check_output(
                ["svn", "info", "--show-item", "revision", url], text=True, timeout=30,
            ).strip()
            return local_rev != remote_rev
        except Exception:
            return False

    def _action_build_conditional(s: JobConfig) -> bool:
        if not repos_checked:
            log("Skipping build — needs update-repositories action before this action")
            return False
        if not repos_updated:
            log("Skipping build — no repo changes detected")
            return True
        return _action_build(s)

    def _refresh_libraries_yaml() -> bool:
        """Pull only defaults/libraries.yaml from origin — no full s4
        self-update. Keeps recipe/version info current without risking
        a program update during an unattended automation run. The
        working tree becomes dirty for this one file, but the normal
        s4 self-update flow (`git checkout . && git reset --hard`)
        cleans that up on the next manual update.
        """
        root = paths.root
        target = root / "defaults" / "libraries.yaml"
        if not (root / ".git").is_dir():
            log("s4 is not a git repo — skipping libraries.yaml refresh")
            return True
        try:
            subprocess.run(
                ["git", "fetch", "origin", "--quiet"], cwd=root,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
            head = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            )
            branch = (head.stdout or "").strip() or "main"
            result = subprocess.run(
                ["git", "show", f"origin/{branch}:defaults/libraries.yaml"], cwd=root,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if result.returncode != 0 or not result.stdout:
                log("  libraries.yaml not available on remote — keeping local copy")
                return True
            remote_content = result.stdout
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current == remote_content:
                log("  libraries.yaml is up to date")
                return True
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(remote_content, encoding="utf-8")
            log("  libraries.yaml refreshed from origin")
            return True
        except Exception as e:
            log(f"libraries.yaml refresh failed: {e}")
            return True

    def _action_update_libs(s: JobConfig) -> bool:
        from core.toolchain import ToolchainRegistry, scan_libraries, check_library_updates
        from core.toolchain import load_library_recipes
        from core.lib_builder import build_library
        from core.target import Target

        log("Refreshing libraries.yaml from origin...")
        _refresh_libraries_yaml()

        registry = ToolchainRegistry(
            defaults_path=paths.toolchains_defaults,
            local_path=paths.toolchains_local,
            install_dir=paths.toolchains_dir,
        )
        registry.load()
        recipes = load_library_recipes(
            paths.defaults_dir / "libraries.yaml",
            paths.libraries_local,
        )

        tc_names: set[str] = set()
        if s.toolchains:
            if "all" in s.toolchains:
                tc_names = {t.name for t in registry.installed_toolchains()}
            else:
                tc_names = set(s.toolchains)
        elif s.targets:
            available = dict(Target.list_targets(paths.targets_dir))
            targets_to_check = list(available.keys()) if "all" in s.targets else s.targets
            for tn in targets_to_check:
                if tn in available:
                    t = Target.load(available[tn])
                    for tcc in t.toolchains:
                        tc_names.add(tcc.name)
        if not tc_names:
            log("No toolchains or targets specified")
            return False

        all_ok = True
        updated_count = 0

        for tc_name in sorted(tc_names):
            tc = next((t for t in registry.installed_toolchains() if t.name == tc_name), None)
            if not tc:
                log(f"Toolchain not installed: {tc_name}")
                continue
            tc_dir = paths.toolchains_dir / tc.name
            log(f"Scanning libraries for toolchain '{tc.name}'...")

            libs = scan_libraries(tc_dir, tc.sysroot, native=tc.native)
            check_library_updates(libs, recipes, has_c11=True)

            tc_updated = False
            for lib in libs:
                if not lib.found or not lib.update_version or lib.up_to_date:
                    continue
                log(f"  Updating {lib.name}: {lib.version} → {lib.update_version}")
                ok = build_library(
                    lib_name=lib.name,
                    version=lib.update_version,
                    toolchain_dir=tc_dir,
                    toolchain_prefix=tc.prefix,
                    toolchain_sysroot=tc.sysroot,
                    toolchain_cflags=tc.cflags,
                    toolchain_ldflags=tc.ldflags,
                    libraries_yaml=paths.defaults_dir / "libraries.yaml",
                    libraries_local=paths.libraries_local,
                    downloads_dir=paths.downloads_dir,
                    logs_dir=paths.logs_dir,
                    max_cpus=settings.max_cpus,
                    native=tc.native,
                    log=log,
                    organize_logs=settings.organize_logs,
                )
                if ok:
                    updated_count += 1
                    tc_updated = True
                else:
                    all_ok = False

            if tc_updated:
                from core.toolchain_backup import backup_toolchain
                log(f"Backing up '{tc.name}' after library updates...")
                if not backup_toolchain(tc, paths, registry, settings, log=log):
                    all_ok = False

        log(f"Library updates complete: {updated_count} updated")
        return all_ok

    # Action dispatch
    _action_map = {
        "update-repositories": _action_update_repos,
        "update-libraries": _action_update_libs,
        "build-targets-conditional": _action_build_conditional,
        "build-targets-force": _action_build,
    }

    # Expand 'all' → every known action in their defined order. The
    # MultiSelect widget returns ["all"] when every item is selected
    # (that's how the UI collapses the full set into one token), so the
    # job dispatcher has to accept it too. Order matters: libs before
    # repos before conditional before force, to match the pipeline.
    _ACTION_ORDER = (
        "update-libraries",
        "update-repositories",
        "build-targets-conditional",
        "build-targets-force",
    )
    actions_to_run = (
        list(_ACTION_ORDER) if sched.actions == ["all"] else list(sched.actions)
    )

    # Install a SIGTERM handler so an F9 Cancel (killpg with SIGTERM)
    # turns into a regular exception instead of Python's default abrupt
    # exit — we need the `finally` below to run so the state file gets
    # the proper 'canceled' result and running flag flipped off.
    import signal as _signal

    class _JobCanceled(BaseException):
        pass

    def _on_term(signum, frame):
        raise _JobCanceled()

    try:
        _signal.signal(_signal.SIGTERM, _on_term)
    except Exception:
        pass

    success = True
    canceled = False
    try:
        for action in actions_to_run:
            handler = _action_map.get(action)
            if handler:
                log(f"── Action: {action} ──")
                if not handler(sched):
                    success = False
                    break
            else:
                log(f"Unknown action: {action}")
                success = False
                break
    except _JobCanceled:
        log("Job canceled by user (SIGTERM)")
        canceled = True
        success = False
    except KeyboardInterrupt:
        log("Job canceled by user (Ctrl-C)")
        canceled = True
        success = False
    except Exception as e:
        log(f"Job crashed: {e}")
        success = False
    finally:
        # Always clear running state, even on crash/cancel. Compute
        # duration from started_at so the UI can show "duration …"
        # next to last run.
        finish = datetime.now()
        duration_sec = 0
        try:
            start = datetime.fromisoformat(now_iso)
            duration_sec = max(0, int((finish - start).total_seconds()))
        except Exception:
            pass
        if canceled:
            result = "canceled"
        elif success:
            result = "ok"
        else:
            result = "fail"
        update_state(
            paths.automation_state_yaml, sched.name,
            last_run=finish.isoformat(timespec="seconds"),
            last_result=result,
            running=False, pid=0, started_at="",
            duration_sec=duration_sec,
        )

    result_str = "CANCELED" if canceled else ("OK" if success else "FAIL")
    log(f"Job {sched.name} finished: {result_str}")

    log_file.close()
    # Apply retention rules to automation logs
    try:
        from core.log_cleanup import cleanup_logs
        cleanup_logs(
            paths.logs_dir,
            max_age_days=settings.log_max_age_days,
            max_count=settings.log_max_count,
            max_size_mb=settings.log_max_size_mb,
        )
    except Exception:
        pass

    return success
