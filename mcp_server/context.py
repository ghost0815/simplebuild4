"""Shared runtime context for all MCP tools.

Built once at server start, then passed to each tool. Keeps the
tools themselves stateless and test-friendly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.paths import Paths
from core.settings import Settings


@dataclass
class McpContext:
    """Runtime context shared between all tools."""
    paths: Paths
    settings: Settings

    # Feature gates — off by default. The CLI flags flip these on.
    allow_actions: bool = False      # build / deploy / run-job
    allow_dangerous: bool = False    # toolchain build, s4 update, settings write

    # Optional bearer token for HTTP transport. None = no auth check.
    http_token: Optional[str] = None

    # TLS on the HTTP transport. Both paths must be set to enable TLS.
    ssl_certfile: Optional[str] = None
    ssl_keyfile: Optional[str] = None

    @classmethod
    def build(
        cls,
        allow_actions: bool = False,
        allow_dangerous: bool = False,
        http_token: Optional[str] = None,
        ssl_certfile: Optional[str] = None,
        ssl_keyfile: Optional[str] = None,
    ) -> "McpContext":
        paths = Paths.detect()
        paths.ensure_dirs()
        settings = Settings.load(paths.root / "data" / "settings.yaml")
        return cls(
            paths=paths,
            settings=settings,
            allow_actions=allow_actions,
            allow_dangerous=allow_dangerous,
            http_token=http_token,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
        )

    # ------------------------------------------------------------------
    # Re-read settings from disk (in case the TUI changed them while we run)
    def refresh_settings(self) -> None:
        self.settings = Settings.load(self.paths.root / "data" / "settings.yaml")
