"""crosstool-ng builder integration.

Workflow (matches s3 behaviour):

1. setup() — clone crosstool-ng, bootstrap, configure, make (if ct-ng
   is not present)
2. build(template) — copy template config, patch CT_PREFIX_DIR/CT_LOCAL_TARBALLS_DIR/
   CT_PARALLEL_JOBS, run `./ct-ng build`, leave the result in toolchains_dir/<name>/

The crosstool-ng install lives at <ctng_dir> (default: data/builders/crosstool-ng).
All builder configuration is hardcoded — no external YAML needed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from rich.markup import escape as rich_escape

from core.template import Template


def _expand_tokens(cmd: list, tokens: dict) -> list:
    """Replace @TOKEN@ placeholders in each argument of a command list."""
    result = []
    for arg in cmd:
        s = str(arg)
        for key, val in tokens.items():
            s = s.replace(f"@{key}@", str(val))
        result.append(s)
    return result


def _dir_size(path: Path) -> int:
    """Cheap recursive size of a directory in bytes. Returns 0 if missing."""
    if not path.is_dir():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"


class CrosstoolNG:
    """crosstool-ng builder runner."""

    # All builder configuration — no external YAML needed.
    REPO = "https://github.com/crosstool-ng/crosstool-ng"
    BINARY = "ct-ng"
    SETUP_STEPS = [
        ["git", "clone", "--progress", "@REPO@", "@INSTALL_DIR@"],
        ["./bootstrap"],
        ["./configure", "--enable-local"],
        ["make", "-j@CPUS@"],
    ]
    CUSTOMIZE_CMD = ["./ct-ng", "menuconfig"]

    def __init__(
        self,
        ctng_dir: Path,
        toolchains_dir: Path,
        downloads_dir: Path,
        max_cpus: int = 4,
        log: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ):
        self.ctng_dir = ctng_dir
        self.toolchains_dir = toolchains_dir
        self.downloads_dir = downloads_dir
        self.max_cpus = max_cpus
        self.log = log or (lambda m: None)
        self.cancel_check = cancel_check or (lambda: False)

    @classmethod
    def resolve_binary(cls, ctng_dir: Path) -> Optional[Path]:
        """Return the existing ct-ng binary inside ``ctng_dir``, or None.

        Two supported layouts:
          * `<dir>/ct-ng`       — `./configure --enable-local && make`
                                   (produced by s4's own bootstrap)
          * `<dir>/bin/ct-ng`   — `./configure --prefix=<dir> && make install`
                                   (produced by a system-style install)
        """
        for c in (ctng_dir / cls.BINARY, ctng_dir / "bin" / cls.BINARY):
            if c.is_file():
                return c
        return None

    @property
    def ct_ng(self) -> Path:
        """Path to the ct-ng executable inside the install dir."""
        found = self.resolve_binary(self.ctng_dir)
        # Fall back to the local-build path when nothing is there yet so
        # callers that log/format the path still get a sensible value.
        return found if found is not None else (self.ctng_dir / self.BINARY)

    def is_installed(self) -> bool:
        """True if the builder binary is built and ready."""
        return self.ct_ng.is_file() and os.access(self.ct_ng, os.X_OK)

    # ------------------------------------------------------------------ setup

    def setup(self) -> bool:
        """Execute setup steps. Idempotent.

        Honours an existing user-supplied install at ctng_dir:
          * If ct-ng (or bin/ct-ng) is already present and executable
            → nothing to do.
          * If the directory exists and is non-empty (a partial source
            checkout, or a system install missing only the build) →
            skip the clone step and run the remaining steps in place.
          * Empty / missing directory → full setup.
        """
        if self.is_installed():
            self.log(f"  [green]{self.ct_ng.name} already installed at {self.ctng_dir}[/green]")
            return True

        # Detect a non-empty pre-existing dir we should not overwrite
        try:
            preexisting = self.ctng_dir.is_dir() and any(self.ctng_dir.iterdir())
        except Exception:
            preexisting = False

        if preexisting:
            self.log(
                f"  [yellow]{self.ctng_dir} already exists — reusing the existing tree, "
                f"skipping clone[/yellow]"
            )

        self.ctng_dir.parent.mkdir(parents=True, exist_ok=True)

        tokens = {
            "REPO": self.REPO,
            "INSTALL_DIR": str(self.ctng_dir),
            "CPUS": str(self.max_cpus),
        }

        self.log(f"[bold]Setting up crosstool-ng → {self.ctng_dir}[/bold]")
        for raw_cmd in self.SETUP_STEPS:
            cmd = _expand_tokens(list(raw_cmd), tokens)
            is_clone = (len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "clone")
            if is_clone and preexisting:
                self.log(f"  [dim]skip clone (existing tree at {self.ctng_dir})[/dim]")
                continue
            cwd = self.ctng_dir.parent if is_clone else self.ctng_dir
            if not is_clone and not self.ctng_dir.exists():
                self.log(f"[red]Install dir missing: {self.ctng_dir}[/red]")
                return False
            if not self._run(cmd, cwd=cwd):
                return False

        if not self.is_installed():
            self.log(f"[red]{self.BINARY} binary not found after setup[/red]")
            return False

        self.log(f"[green]crosstool-ng setup complete[/green]")
        return True

    # ------------------------------------------------------------------ build

    def build(self, template: Template) -> bool:
        """Build a toolchain from a template. Result lives in toolchains_dir/<name>/."""
        if not self.is_installed():
            self.log("[red]ct-ng not installed — run setup first[/red]")
            return False

        cfg_src = template.config_path
        if not cfg_src or not cfg_src.exists():
            self.log(f"[red]Template config not found: {template.config}[/red]")
            return False

        self.log(f"[bold]Building toolchain: {template.name}[/bold]")
        self.log(f"  Builder:    crosstool-ng")
        self.log(f"  Source cfg: {cfg_src}")

        # Clean any previous .config*
        for stale in self.ctng_dir.glob(".config*"):
            try:
                stale.unlink()
            except Exception:
                pass

        # Copy template → .config
        target_cfg = self.ctng_dir / ".config"
        shutil.copy2(cfg_src, target_cfg)
        self.log(f"  Copied .config")

        # Determine final install dir
        install_dir = self.toolchains_dir / template.name
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        # If a previous failed build left a partial dir, remove it
        if install_dir.exists():
            self.log(f"  Removing existing {install_dir}")
            shutil.rmtree(install_dir, ignore_errors=True)

        # Decide where the tarballs land.
        # Template `download_location` (relative paths resolve against the
        # project root, ~ expanded) wins. If empty, leave whatever the .config
        # already says — the user manages it via menuconfig.
        tarballs_dir: Path | None = None
        dl = (getattr(template, "download_location", "") or "").strip()
        if dl:
            dl = os.path.expanduser(dl)
            p = Path(dl)
            if not p.is_absolute():
                # binaries_dir is data/binaries → root is its grandparent
                root = self.toolchains_dir.parent.parent
                p = root / p
            tarballs_dir = p
            tarballs_dir.mkdir(parents=True, exist_ok=True)

        # Patch .config:
        #   CT_LOCAL_TARBALLS_DIR  → tarballs_dir (only if template asks)
        #   CT_PREFIX_DIR          → final toolchain install dir
        #   CT_TOOLCHAIN_PKGVERSION→ "simplebuild4"
        #   CT_PARALLEL_JOBS       → max_cpus
        try:
            content = target_cfg.read_text(encoding="utf-8", errors="replace")
            if tarballs_dir is not None:
                content = re.sub(
                    r'^\s*(?:#\s*)?CT_LOCAL_TARBALLS_DIR.*$',
                    f'CT_LOCAL_TARBALLS_DIR="{tarballs_dir}"',
                    content, flags=re.MULTILINE,
                )
            content = re.sub(
                r'^\s*(?:#\s*)?CT_PREFIX_DIR.*$',
                f'CT_PREFIX_DIR="{install_dir}"',
                content, flags=re.MULTILINE,
            )
            content = re.sub(
                r'^\s*(?:#\s*)?CT_TOOLCHAIN_PKGVERSION.*$',
                'CT_TOOLCHAIN_PKGVERSION="simplebuild4"',
                content, flags=re.MULTILINE,
            )
            # Append parallel jobs (don't replace — may not exist)
            if "CT_PARALLEL_JOBS=" not in content:
                content += f"\nCT_PARALLEL_JOBS={self.max_cpus}\n"
            else:
                content = re.sub(
                    r'^\s*(?:#\s*)?CT_PARALLEL_JOBS.*$',
                    f"CT_PARALLEL_JOBS={self.max_cpus}",
                    content, flags=re.MULTILINE,
                )
            target_cfg.write_text(content, encoding="utf-8")
            patched = ["PREFIX", "JOBS"]
            if tarballs_dir is not None:
                patched.insert(1, "DOWNLOADS")
            self.log(f"  Patched .config ({', '.join(patched)})")
        except Exception as e:
            self.log(f"[red]Failed to patch .config: {e}[/red]")
            return False

        # Run ./ct-ng show-config (informational)
        self._run(["./ct-ng", "show-config"], cwd=self.ctng_dir, ignore_failure=True)

        # Run ./ct-ng build (raise file descriptor limit — crosstool-ng
        # needs more than the default 1024 for large toolchains)
        self.log("[bold]Running ct-ng build...[/bold]")
        ok = self._run(["bash", "-c", "ulimit -n 4096; ./ct-ng build"], cwd=self.ctng_dir)

        if not ok:
            self.log("[red]ct-ng build failed[/red]")
            # Remove partial install dir on failure. Leave .build/ and
            # build.log in place so the user can inspect what went wrong —
            # they can run ``./ct-ng clean`` manually (or re-run the
            # build, which overwrites) to free the disk.
            if install_dir.exists():
                shutil.rmtree(install_dir, ignore_errors=True)
            return False

        if not install_dir.exists():
            self.log(f"[red]Build reported success but {install_dir} missing[/red]")
            return False

        # --- Cleanup: run ct-ng's own ``clean`` target. -----------------
        # It wipes the whole ``.build/`` scratch tree (sources, object
        # files, tools, previous-target leftovers) plus ``*.log`` /
        # ``build.log.*``. On a multi-template checkout this can recover
        # tens of GB that our previous "delete CT_BUILD_TOP_DIR"
        # heuristic missed (.build/src/, .build/tools/, old targets).
        # The .config is also dropped — fine because every build() call
        # copies a fresh one from the template before configuring.
        try:
            size_before = _dir_size(self.ctng_dir / ".build")
        except Exception:
            size_before = 0
        self._run(["./ct-ng", "clean"], cwd=self.ctng_dir, ignore_failure=True)
        if size_before:
            self.log(f"  Cleaned ct-ng scratch tree ({_format_bytes(size_before)} freed)")

        self.log(f"[green]Toolchain {template.name} installed → {install_dir}[/green]")
        return True

    # ------------------------------------------------------------------ helper

    def _run(self, cmd: list[str], cwd: Path | None = None, ignore_failure: bool = False) -> bool:
        """Run a command, stream output to log. Returns success bool."""
        if self.cancel_check():
            self.log("[yellow]Cancelled[/yellow]")
            return False
        try:
            self.log(f"[dim]$ {' '.join(cmd)}[/dim]")
            proc = subprocess.Popen(
                cmd, cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            assert proc.stdout is not None
            # Spinner suppression: crosstool-ng prints animated spinner lines
            # like "[00:13] /", "[00:13] -", "[00:13] \", "[00:13] |" using \r.
            # Read by '\n' separators, then drop any line that ends with a
            # bare spinner char (after the timestamp).
            spinner_re = re.compile(r'^\[[0-9:]+\]\s+[/\\|\-]\s*$')
            for raw in proc.stdout:
                if self.cancel_check():
                    proc.terminate()
                    self.log("[yellow]Cancelled[/yellow]")
                    return False
                # Strip ANSI escapes and CR-overdrawn spinner frames
                line = raw.rstrip("\r\n")
                # Many of crosstool-ng's spinner lines come together via \r;
                # the last segment after the final \r is what would be visible.
                if "\r" in line:
                    line = line.split("\r")[-1]
                if not line.strip():
                    continue
                if spinner_re.match(line):
                    continue
                self.log(rich_escape(line))
            proc.wait()
            if proc.returncode != 0 and not ignore_failure:
                return False
            return True
        except FileNotFoundError as e:
            self.log(f"[red]Command not found: {e}[/red]")
            return False
        except Exception as e:
            self.log(f"[red]Command failed: {e}[/red]")
            return False
