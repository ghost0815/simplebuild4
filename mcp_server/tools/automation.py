"""Automation-related MCP tools.

  list_automation_jobs   — from data/automation.yaml
  run_automation_job     — start a job in the background (needs --allow-actions)
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from mcp.server import Server
from mcp.types import Tool

import re
from core.automation import JobConfig, load_jobs, run_job, save_jobs

from mcp_server.context import McpContext
from core import jobs as _jobs


_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_job_name(name: str) -> None:
    if not name:
        raise ValueError("job name is required")
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid job name '{name}' — only letters, digits, '_', '-', '.' allowed"
        )


def _apply_job_patch(job: JobConfig, patch: dict[str, any]) -> None:
    """Apply a patch dict onto a JobConfig in place."""
    if "enabled" in patch:
        job.enabled = bool(patch["enabled"])
    if "cron" in patch:
        job.cron = str(patch["cron"] or "")
    for list_key in ("actions", "targets", "repos", "toolchains", "templates"):
        if list_key in patch:
            val = patch[list_key]
            setattr(job, list_key, list(val or []))


def _list_automation_jobs_impl(ctx: McpContext) -> list[dict[str, Any]]:
    job_list = load_jobs(ctx.paths.automation_yaml)
    return [
        {
            "name": j.name,
            "enabled": bool(j.enabled),
            "cron": j.cron or "",
            "actions": list(j.actions or []),
            "targets": list(j.targets or []),
            "repos": list(j.repos or []),
            "toolchains": list(j.toolchains or []),
            "templates": list(j.templates or []),
        }
        for j in job_list
    ]


def _run_automation_job_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "run_automation_job is disabled — start the MCP server with --allow-actions"
        )
    # Verify job exists
    job_list = load_jobs(ctx.paths.automation_yaml)
    if not any(j.name == name for j in job_list):
        raise ValueError(f"Unknown automation job: {name}")

    ctx.refresh_settings()

    # Reserve a log file + status JSON analogous to build jobs
    log_path, job_id = _jobs._allocate_log_path(
        ctx.paths.logs_dir, name, "automation", "", ctx.settings.organize_logs,
    )
    status_path = log_path.with_suffix(log_path.suffix + ".status.json")
    import os as _os
    _jobs._write_status(status_path, {
        "job_id": job_id,
        "type": "automation",
        "target": name,
        "started": datetime.now().isoformat(timespec="seconds"),
        "finished": None,
        "success": None,
        "pid": _os.getpid(),
        "params": {},
    })

    def _worker() -> None:
        log_file = None
        success = False
        error = None
        try:
            log_file = log_path.open("w", encoding="utf-8")

            def _log(msg: str) -> None:
                try:
                    log_file.write(_jobs._strip_markup(msg) + "\n")
                    log_file.flush()
                except Exception:
                    pass

            success = bool(run_job(name, ctx.paths, ctx.settings, log=_log))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if log_file:
                try:
                    log_file.close()
                except Exception:
                    pass
            data = _jobs._read_status(status_path) or {}
            data.update({
                "finished": datetime.now().isoformat(timespec="seconds"),
                "success": success,
            })
            if error:
                data["error"] = error
            try:
                _jobs._write_status(status_path, data)
            except Exception:
                pass
            _jobs._unregister(job_id)

    thread = threading.Thread(target=_worker, name=f"s4-automation-{name}", daemon=True)
    handle = _jobs.JobHandle(
        job_id=job_id, log_path=log_path, status_path=status_path, thread=thread,
    )
    _jobs._register(handle)
    thread.start()

    return {
        "job_id": job_id,
        "log_path": str(log_path),
        "status": "running",
        "tip": "Poll with get_build_status(job_id).",
    }


# ------------------------------------------------------------------ write impls


def _create_automation_job_impl(
    ctx: McpContext, name: str, spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "create_automation_job is disabled — start the MCP server with --allow-actions"
        )
    _validate_job_name(name)
    spec = spec or {}
    jobs_list = load_jobs(ctx.paths.automation_yaml)
    if any(j.name == name for j in jobs_list):
        raise ValueError(f"Automation job '{name}' already exists")
    new = JobConfig(name=name, enabled=bool(spec.get("enabled", True)), cron=str(spec.get("cron", "0 3 * * *")))
    _apply_job_patch(new, spec)
    jobs_list.append(new)
    save_jobs(jobs_list, ctx.paths.automation_yaml)
    return {"name": name, "created": True}


def _update_automation_job_impl(
    ctx: McpContext, name: str, patch: dict[str, Any],
) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "update_automation_job is disabled — start the MCP server with --allow-actions"
        )
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    jobs_list = load_jobs(ctx.paths.automation_yaml)
    for j in jobs_list:
        if j.name == name:
            _apply_job_patch(j, patch)
            save_jobs(jobs_list, ctx.paths.automation_yaml)
            return {"name": name, "saved": True, "applied_keys": sorted(patch.keys())}
    raise ValueError(f"Unknown automation job: {name}")


def _delete_automation_job_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "delete_automation_job is disabled — start the MCP server with --allow-actions"
        )
    jobs_list = load_jobs(ctx.paths.automation_yaml)
    new = [j for j in jobs_list if j.name != name]
    if len(new) == len(jobs_list):
        raise ValueError(f"Unknown automation job: {name}")
    save_jobs(new, ctx.paths.automation_yaml)
    return {"name": name, "deleted": True}


def register(server: Server, ctx: McpContext) -> list[Tool]:
    tools = [
        Tool(
            name="list_automation_jobs",
            description=(
                "List configured automation jobs from data/automation.yaml "
                "(cron schedule, actions, targets, ...)."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
    ]
    if ctx.allow_actions:
        tools.extend([
            Tool(
                name="run_automation_job",
                description=(
                    "Run an automation job manually in the background. "
                    "Returns a job_id — poll with get_build_status."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="create_automation_job",
                description=(
                    "Create a new automation job. 'spec' fields (all "
                    "optional): enabled, cron (5-field cron expression), "
                    "actions[], targets[], repos[], toolchains[], "
                    "templates[]. Known actions: update-repositories, "
                    "update-libraries, build-targets-conditional, "
                    "build-targets-force, build-toolchains."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "spec": {"type": "object"},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="update_automation_job",
                description=(
                    "Patch an existing automation job. 'patch' accepts "
                    "enabled, cron, actions, targets, repos, toolchains, "
                    "templates. List fields are replaced as a whole."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "patch": {"type": "object"},
                    },
                    "required": ["name", "patch"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="delete_automation_job",
                description="Remove an automation job from data/automation.yaml.",
                inputSchema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
        ])
    return tools


def dispatch(ctx: McpContext, name: str, args: dict[str, Any]) -> Any:
    if name == "list_automation_jobs":
        return _list_automation_jobs_impl(ctx)
    if name == "run_automation_job":
        return _run_automation_job_impl(ctx, args["name"])
    if name == "create_automation_job":
        return _create_automation_job_impl(ctx, args["name"], args.get("spec") or {})
    if name == "update_automation_job":
        return _update_automation_job_impl(ctx, args["name"], args["patch"])
    if name == "delete_automation_job":
        return _delete_automation_job_impl(ctx, args["name"])
    raise KeyError(name)


TOOL_NAMES = {
    "list_automation_jobs", "run_automation_job",
    "create_automation_job", "update_automation_job", "delete_automation_job",
}
