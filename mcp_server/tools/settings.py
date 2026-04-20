"""Settings-related MCP tools.

  get_settings    — dump all non-secret settings
  set_setting     — change a safe, non-secret setting (needs --allow-actions)
"""

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any

from mcp.server import Server
from mcp.types import Tool

from mcp_server.context import McpContext


# Fields that must never leave the server via MCP.
_SECRET_FIELDS = {
    "web_auth_password",
    "sign_key",
    "mcp_token",
}

# Safe fields that can be set via MCP. Anything that affects network
# binding, systemd services, credentials, or privileged paths is OUT.
# The TUI remains the only place to flip those.
_WRITABLE_FIELDS = {
    # General UX
    "language",
    "theme",
    "ascii_mode",
    "confirm_dangerous",
    "warn_unsaved_on_exit",
    "update_check",

    # Build defaults
    "compiler_opt",
    "max_cpus",
    "patch_webif",
    "delete_debug",
    "save_smargo",
    "export_libs_zip",
    "organize_logs",
    "default_compress_level",
    "default_confdir",
    "default_build_system",
    "default_deploy_user",
    "default_deploy_port",
    "default_deploy_path",

    # QEMU
    "qemu_test",
    "qemu_timeout",
    "qemu_expect",

    # Log retention
    "log_max_age_days",
    "log_max_count",
    "log_max_size_mb",

    # MCP service knobs (host, port, token, allow_actions, root_path,
    # web_* counterparts) are intentionally NOT writable via MCP — an LLM
    # must not be able to self-escalate its own permissions or rebind the
    # service. All MCP/Web policy lives in the TUI only.
}


def _get_settings_impl(ctx: McpContext) -> dict[str, Any]:
    ctx.refresh_settings()
    data = asdict(ctx.settings)
    for key in _SECRET_FIELDS:
        if key in data and data[key]:
            data[key] = "<redacted>"
    # Annotate which keys can be changed via set_setting
    data["_writable"] = sorted(_WRITABLE_FIELDS)
    data["_secret"] = sorted(_SECRET_FIELDS)
    return data


def _set_setting_impl(ctx: McpContext, key: str, value: Any) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "set_setting is disabled \u2014 start the MCP server with --allow-actions"
        )
    if key not in _WRITABLE_FIELDS:
        raise ValueError(
            f"Setting '{key}' is not writable via MCP. "
            f"Allowed: {sorted(_WRITABLE_FIELDS)}"
        )
    ctx.refresh_settings()
    # Type-coerce to the dataclass field type so YAML stays clean
    type_map = {f.name: f.type for f in fields(ctx.settings)}
    target_type = type_map.get(key, str)
    # dataclass field types come through as strings on Python 3.10+ when
    # `from __future__ import annotations` is active — resolve the basic ones.
    type_name = target_type if isinstance(target_type, str) else target_type.__name__
    try:
        if type_name == "bool":
            if isinstance(value, str):
                coerced = value.strip().lower() in {"1", "true", "yes", "on", "y", "t"}
            else:
                coerced = bool(value)
        elif type_name == "int":
            coerced = int(value)
        elif type_name == "str":
            coerced = str(value)
        else:
            coerced = value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Cannot coerce {value!r} to {type_name}: {exc}")

    previous = getattr(ctx.settings, key)
    setattr(ctx.settings, key, coerced)
    ctx.settings.save(ctx.paths.root / "data" / "settings.yaml")

    return {
        "key": key,
        "old_value": previous,
        "new_value": coerced,
        "saved": True,
    }


def _get_s4_commits_impl(
    ctx: McpContext, limit: int = 20, path: str | None = None,
) -> dict[str, Any]:
    """Return recent commits from the s4 (simplebuild4) repo itself.

    Useful when explaining behavior: the assistant can correlate a
    reported problem with the last few code changes that shipped.
    """
    # Lazy import avoids pulling subprocess machinery on every tool list.
    from mcp_server.tools.repositories import _git_log
    root = ctx.paths.root
    if not (root / ".git").exists():
        raise ValueError(
            f"s4 root at {root} is not a git checkout — no commit history available"
        )
    commits = _git_log(root, limit, path)
    return {
        "root": str(root),
        "path_filter": path,
        "count": len(commits),
        "commits": commits,
    }


def register(server: Server, ctx: McpContext) -> list[Tool]:
    tools: list[Tool] = [
        Tool(
            name="get_settings",
            description=(
                "Return the full s4 application settings (credentials "
                "redacted). The result also lists `_writable` (keys that "
                "can be changed via set_setting) and `_secret` (redacted "
                "keys)."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="get_s4_commits",
            description=(
                "Return the recent git commit log of the s4 (simplebuild4) "
                "installation itself. Useful for correlating a reported "
                "behavior with recent s4 code changes when explaining what "
                "the tool is doing. Requires the s4 root to be a git "
                "checkout."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer", "minimum": 1, "maximum": 200,
                        "default": 20,
                        "description": "Max commits to return (default 20, cap 200).",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional path filter relative to the s4 root "
                            "(e.g. 'core/builder.py' or 'mcp_server/')."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        ),
    ]
    if ctx.allow_actions:
        tools.append(Tool(
            name="set_setting",
            description=(
                "Change one non-critical setting and persist it to "
                "data/settings.yaml. Only an allowlist of UX/build/retention "
                "fields can be changed this way \u2014 network binding, "
                "credentials, service ports, and SSL paths stay TUI-only. "
                "Returns old and new value."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Setting name (see get_settings._writable)",
                    },
                    "value": {
                        "description": "New value. Strings, bools and ints are coerced.",
                    },
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        ))
    return tools


def dispatch(ctx: McpContext, name: str, args: dict[str, Any]) -> Any:
    if name == "get_settings":
        return _get_settings_impl(ctx)
    if name == "get_s4_commits":
        return _get_s4_commits_impl(
            ctx, limit=args.get("limit", 20), path=args.get("path"),
        )
    if name == "set_setting":
        return _set_setting_impl(ctx, args["key"], args.get("value"))
    raise KeyError(name)


TOOL_NAMES = {"get_settings", "get_s4_commits", "set_setting"}
