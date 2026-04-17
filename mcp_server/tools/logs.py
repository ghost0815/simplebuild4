"""Log-related MCP tools.

  list_recent_builds   — newest logs, optionally filtered by type
  get_log              — read a paginated slice of a log file
"""

from __future__ import annotations

from typing import Any

from mcp.server import Server
from mcp.types import Tool

from core import jobs

from mcp_server.context import McpContext


_VALID_TYPES = {"build", "selfbuild", "lib", "deploy", "backup", "automation", "repo", "s4", "other"}


def _list_recent_builds_impl(
    ctx: McpContext,
    limit: int = 20,
    type_filter: str = "",
) -> list[dict[str, Any]]:
    tf = type_filter.strip() or None
    if tf and tf not in _VALID_TYPES:
        raise ValueError(f"type must be one of {sorted(_VALID_TYPES)}, got {tf!r}")
    return jobs.list_recent_jobs(ctx.paths.logs_dir, limit=limit, type_filter=tf)


def _get_log_impl(
    ctx: McpContext,
    job_id: str,
    offset: int = -500,
    limit: int = 500,
) -> dict[str, Any]:
    result = jobs.read_log(ctx.paths.logs_dir, job_id, offset=offset, limit=limit)
    result["job_id"] = job_id
    return result


def register(server: Server, ctx: McpContext) -> list[Tool]:
    return [
        Tool(
            name="list_recent_builds",
            description=(
                "List recent log files with their metadata and job status. "
                "Useful to discover recent job_ids for get_build_status / get_log."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                    "type": {
                        "type": "string",
                        "description": "Filter by type: build, selfbuild, lib, deploy, backup, automation, s4, other",
                        "default": "",
                    },
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="get_log",
            description=(
                "Read a slice of a log file. ``offset`` < 0 means tail "
                "(last N lines). Default returns last 500 lines."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "offset": {"type": "integer", "default": -500},
                    "limit": {"type": "integer", "default": 500},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        ),
    ]


def dispatch(ctx: McpContext, name: str, args: dict[str, Any]) -> Any:
    if name == "list_recent_builds":
        return _list_recent_builds_impl(
            ctx,
            limit=int(args.get("limit", 20)),
            type_filter=args.get("type", ""),
        )
    if name == "get_log":
        return _get_log_impl(
            ctx,
            args["job_id"],
            offset=int(args.get("offset", -500)),
            limit=int(args.get("limit", 500)),
        )
    raise KeyError(name)


TOOL_NAMES = {"list_recent_builds", "get_log"}
