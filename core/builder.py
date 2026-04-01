"""Build engine — compiles oscam using a target configuration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

from core.target import Target
from core.toolchain import ToolchainInfo


@dataclass
class BuildResult:
    """Result of a build operation."""

    success: bool
    binary_path: Optional[Path] = None
    log_path: Optional[Path] = None
    duration_seconds: float = 0.0


async def run_build(
    target: Target,
    toolchain: ToolchainInfo,
    repo_path: Path,
    output_dir: Path,
    log_callback: Optional[Callable[[str], None]] = None,
) -> BuildResult:
    """Execute the build process, streaming output via log_callback.

    This is the core build function that will:
    1. Reset oscam config
    2. Enable/disable modules per target settings
    3. Invoke make with cross-compiler settings
    4. Collect the built binary

    Args:
        target: Target configuration with build settings.
        toolchain: Resolved toolchain info.
        repo_path: Path to oscam source checkout.
        output_dir: Where to place the built binary.
        log_callback: Called with each line of build output.

    Returns:
        BuildResult with success status and paths.
    """
    # TODO: implement build pipeline
    raise NotImplementedError("Build engine not yet implemented")
