"""Toolchain-related MCP tools.

  list_toolchains              — all known toolchains
  get_toolchain                — details of one toolchain
  scan_toolchain_libraries     — live pkg-config scan
  install_toolchain            — PreBuild download + extract (needs --allow-actions)
  build_toolchain_from_template — SelfBuild via ct-ng/OE-SDK/Android-NDK (needs --allow-actions)
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mcp.server import Server
from mcp.types import Tool

from core.toolchain import ToolchainRegistry, scan_libraries
from core import jobs as _jobs

from mcp_server.context import McpContext


def _registry(ctx: McpContext) -> ToolchainRegistry:
    reg = ToolchainRegistry(
        defaults_path=ctx.paths.toolchains_defaults,
        local_path=ctx.paths.toolchains_local,
        install_dir=ctx.paths.toolchains_dir,
    )
    reg.load()
    return reg


def _toolchain_summary(tc, local_names: set[str]) -> dict[str, Any]:
    return {
        "name": tc.name,
        "arch": tc.arch,
        "description": tc.description,
        "category": tc.category,
        "installed": bool(tc.installed),
        "source": "local" if tc.name in local_names else "default",
        "selfbuild": bool(getattr(tc, "selfbuild", False)),
        "native": bool(getattr(tc, "native", False)),
        "libraries": dict(getattr(tc, "libraries", {}) or {}),
    }


def _list_toolchains_impl(ctx: McpContext) -> list[dict[str, Any]]:
    reg = _registry(ctx)
    local_names = {tc.name for tc in reg.local_toolchains()}
    return [_toolchain_summary(tc, local_names) for tc in reg.toolchains]


def _get_toolchain_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    reg = _registry(ctx)
    tc = reg.get(name)
    if not tc:
        raise ValueError(f"Unknown toolchain: {name}")
    local_names = {t.name for t in reg.local_toolchains()}
    base = _toolchain_summary(tc, local_names)
    base.update({
        "prefix": getattr(tc, "prefix", ""),
        "sysroot": getattr(tc, "sysroot", ""),
        "cflags": getattr(tc, "cflags", ""),
        "ldflags": getattr(tc, "ldflags", ""),
        "cc_opts": getattr(tc, "cc_opts", ""),
        "make_vars": getattr(tc, "make_vars", ""),
        "stapi_lib": getattr(tc, "stapi_lib", ""),
        "machines": getattr(tc, "machines", ""),
        "url": getattr(tc, "url", ""),
    })
    return base


def _scan_toolchain_libraries_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    reg = _registry(ctx)
    tc = reg.get(name)
    if not tc:
        raise ValueError(f"Unknown toolchain: {name}")
    if not tc.installed:
        raise ValueError(f"Toolchain not installed: {name}")
    install_dir = ctx.paths.toolchains_dir / tc.name
    libs = scan_libraries(install_dir, tc.sysroot, native=bool(getattr(tc, "native", False)))
    return {
        "toolchain": name,
        "installed": True,
        "sysroot": tc.sysroot,
        "libraries": [asdict(lib) for lib in libs],
    }


def _install_toolchain_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "install_toolchain is disabled — start the MCP server with --allow-actions"
        )
    ctx.refresh_settings()
    handle = _jobs.start_install_toolchain_job(ctx.paths, ctx.settings, name)
    return {
        "job_id": handle.job_id,
        "log_path": str(handle.log_path),
        "status": "running",
        "tip": "Poll with get_build_status(job_id).",
    }


def _build_toolchain_from_template_impl(ctx: McpContext, template: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "build_toolchain_from_template is disabled — start the MCP server with --allow-actions"
        )
    ctx.refresh_settings()
    handle = _jobs.start_build_toolchain_job(ctx.paths, ctx.settings, template)
    return {
        "job_id": handle.job_id,
        "log_path": str(handle.log_path),
        "status": "running",
        "tip": "Poll with get_build_status(job_id). Toolchain builds can take minutes to hours.",
    }


def register(server: Server, ctx: McpContext) -> list[Tool]:
    tools: list[Tool] = [
        Tool(
            name="list_toolchains",
            description=(
                "List all toolchains known to s4 (PreBuild catalog + SelfBuild / "
                "local entries). Each entry has an `installed` flag."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="get_toolchain",
            description="Return full configuration of a single toolchain by name.",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="scan_toolchain_libraries",
            description=(
                "Scan an installed toolchain's sysroot for integrated libraries "
                "via pkg-config (.pc files). Returns the current set with "
                "detected versions — use this to verify what actually got built "
                "vs what's persisted in toolchains.local.yaml."
            ),
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
                name="install_toolchain",
                description=(
                    "Install a PreBuild toolchain from its download URL "
                    "(download + extract + relocate + register). Returns a "
                    "job_id — poll with get_build_status."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="build_toolchain_from_template",
                description=(
                    "Build a toolchain from a SelfBuild template "
                    "(crosstool-ng / OE-SDK / Android-NDK). Returns a job_id — "
                    "poll with get_build_status. Can take minutes to hours."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"template": {"type": "string"}},
                    "required": ["template"],
                    "additionalProperties": False,
                },
            ),
        ])
    return tools


def dispatch(ctx: McpContext, name: str, args: dict[str, Any]) -> Any:
    if name == "list_toolchains":
        return _list_toolchains_impl(ctx)
    if name == "get_toolchain":
        return _get_toolchain_impl(ctx, args["name"])
    if name == "scan_toolchain_libraries":
        return _scan_toolchain_libraries_impl(ctx, args["name"])
    if name == "install_toolchain":
        return _install_toolchain_impl(ctx, args["name"])
    if name == "build_toolchain_from_template":
        return _build_toolchain_from_template_impl(ctx, args["template"])
    raise KeyError(name)


TOOL_NAMES = {
    "list_toolchains", "get_toolchain", "scan_toolchain_libraries",
    "install_toolchain", "build_toolchain_from_template",
}
