"""SimpleBuild4 Model Context Protocol server.

Exposes s4 operations (list targets, build, deploy, inspect logs, ...)
as MCP tools so LLM clients (Claude Desktop, Claude Code, custom apps)
can drive the build system.

Entry point: ``python -m mcp_server`` or ``./s4 mcp``.

Transports:
  * stdio — default, for local LLM clients configured with the
    server command.
  * http  — HTTP+SSE, for remote access behind a reverse proxy.

The server does NOT replace the TUI: both share the same ``data/``
directory, so builds started via MCP show up in the TUI's log viewer
and vice versa.
"""

from __future__ import annotations

__version__ = "0.1.0"
