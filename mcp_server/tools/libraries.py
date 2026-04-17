"""Library-related MCP tools.

  list_libraries   — recipes from defaults/libraries.yaml + local overlay
  get_library      — one recipe with all variants
  install_library  — build + install into toolchain sysroot (needs --allow-actions)
  remove_library   — uninstall from toolchain sysroot (needs --allow-actions)
"""

from __future__ import annotations

from typing import Any

from mcp.server import Server
from mcp.types import Tool

from core.toolchain import (
    load_library_recipes, remove_library, ToolchainRegistry,
)
from core import jobs as _jobs

from mcp_server.context import McpContext


def _libraries_yaml(ctx: McpContext):
    return ctx.paths.defaults_dir / "libraries.yaml", ctx.paths.libraries_local


def _list_libraries_impl(ctx: McpContext) -> list[dict[str, Any]]:
    default_path, local_path = _libraries_yaml(ctx)
    recipes = load_library_recipes(default_path, local_path)
    return [
        {
            "name": r.get("name", ""),
            "pc_file": r.get("pc_file", ""),
            "version_check": r.get("version_check", ""),
            "variants": [
                (v.get("version") if isinstance(v, dict) else str(v))
                for v in (r.get("variants") or [])
            ],
            "depends": list(r.get("depends", []) or []),
        }
        for r in recipes
    ]


def _get_library_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    default_path, local_path = _libraries_yaml(ctx)
    recipes = load_library_recipes(default_path, local_path)
    for r in recipes:
        if r.get("name") == name:
            return r
    raise ValueError(f"Unknown library: {name}")


def _install_library_impl(
    ctx: McpContext, toolchain: str, library: str, version: str = "auto",
) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "install_library is disabled — start the MCP server with --allow-actions"
        )
    ctx.refresh_settings()
    handle = _jobs.start_install_library_job(
        ctx.paths, ctx.settings, toolchain, library, version or "auto",
    )
    return {
        "job_id": handle.job_id,
        "log_path": str(handle.log_path),
        "status": "running",
        "tip": "Poll with get_build_status(job_id).",
    }


def _remove_library_impl(
    ctx: McpContext, toolchain: str, library: str,
) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "remove_library is disabled — start the MCP server with --allow-actions"
        )
    # Synchronous — remove_library is fast (delete files from sysroot).
    reg = ToolchainRegistry(
        defaults_path=ctx.paths.toolchains_defaults,
        local_path=ctx.paths.toolchains_local,
        install_dir=ctx.paths.toolchains_dir,
    )
    reg.load()
    tc = reg.get(toolchain)
    if not tc:
        raise ValueError(f"Unknown toolchain: {toolchain}")
    if not tc.installed:
        raise ValueError(f"Toolchain not installed: {toolchain}")

    lines: list[str] = []
    def _log(msg: str) -> None:
        lines.append(msg)

    install_dir = ctx.paths.toolchains_dir / tc.name
    libraries_yaml = ctx.paths.defaults_dir / "libraries.yaml"
    ok = remove_library(
        install_dir=install_dir,
        sysroot=tc.sysroot,
        lib_name=library,
        libraries_yaml=libraries_yaml,
        libraries_local=ctx.paths.libraries_local,
        log=_log,
    )
    # Update persisted libraries dict in toolchains.local.yaml
    if ok:
        try:
            local = reg._load_file(reg.local_path)
            for entry in local:
                if entry.name == tc.name:
                    entry.libraries = {
                        k: v for k, v in (entry.libraries or {}).items() if k != library
                    }
                    break
            reg.save_local(local)
        except Exception as exc:
            lines.append(f"[warn] persist: {exc}")
    return {
        "toolchain": toolchain,
        "library": library,
        "removed": bool(ok),
        "log": lines,
    }


def register(server: Server, ctx: McpContext) -> list[Tool]:
    tools: list[Tool] = [
        Tool(
            name="list_libraries",
            description=(
                "List all library build recipes (OpenSSL, libcurl, pcsclite, ...) "
                "shipped with s4 plus local overlays."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="get_library",
            description="Return the full recipe for one library, including all variants.",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
    ]
    if ctx.allow_actions:
        tools.extend([
            Tool(
                name="install_library",
                description=(
                    "Build and install a library (OpenSSL, libcurl, ...) into an "
                    "installed toolchain's sysroot. Returns a job_id — poll with "
                    "get_build_status. `version='auto'` uses the newest variant "
                    "compatible with the toolchain."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "toolchain": {"type": "string"},
                        "library": {"type": "string"},
                        "version": {"type": "string", "default": "auto"},
                    },
                    "required": ["toolchain", "library"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="remove_library",
                description=(
                    "Remove an installed library from a toolchain's sysroot "
                    "(deletes .a/.so + include files). Synchronous."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "toolchain": {"type": "string"},
                        "library": {"type": "string"},
                    },
                    "required": ["toolchain", "library"],
                    "additionalProperties": False,
                },
            ),
        ])
    return tools


def dispatch(ctx: McpContext, name: str, args: dict[str, Any]) -> Any:
    if name == "list_libraries":
        return _list_libraries_impl(ctx)
    if name == "get_library":
        return _get_library_impl(ctx, args["name"])
    if name == "install_library":
        return _install_library_impl(
            ctx, args["toolchain"], args["library"], args.get("version", "auto"),
        )
    if name == "remove_library":
        return _remove_library_impl(ctx, args["toolchain"], args["library"])
    raise KeyError(name)


TOOL_NAMES = {
    "list_libraries", "get_library",
    "install_library", "remove_library",
}
