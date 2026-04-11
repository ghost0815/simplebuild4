"""Shared deployment logic for PHASE 5 (builder) and F9 Deployment Check.

Provides a single ``Deployer`` class that handles both remote (SSH/SCP)
and local (file copy) deployments with pre/post install commands. Used by
``core.builder.BuildEngine._phase5_deploy`` and
``ui.screens.deployment_check.DeploymentCheckModal``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from core.target import DeploymentConfig

SSH_OPTS = ["-o", "StrictHostKeyChecking=no",
            "-o", "HostKeyAlgorithms=+ssh-rsa",
            "-o", "ConnectTimeout=10"]


class Deployer:
    """Execute deployment steps for a single DeploymentConfig destination."""

    def __init__(self, config: DeploymentConfig,
                 log: Callable[[str], None],
                 binary_path: Optional[Path] = None,
                 dry_run: bool = False):
        self.config = config
        self._log = log
        self.binary_path = binary_path
        self.dry_run = dry_run
        self.is_local = config.host in ("localhost", "127.0.0.1", "::1")
        self.name = config.name or config.host

        # Build SSH/SCP base commands for remote
        if not self.is_local:
            self._ssh_base = ["ssh", "-p", str(config.port)] + SSH_OPTS
            self._scp_base = ["scp", "-P", str(config.port)] + SSH_OPTS
            if config.key:
                self._ssh_base += ["-i", config.key]
                self._scp_base += ["-i", config.key]
            self._ssh_target = f"{config.user}@{config.host}"

            self._sshpass = []
            if config.key and config.password:
                # SSH key with passphrase: use sshpass -P "Enter passphrase" to feed it
                if shutil.which("sshpass"):
                    self._sshpass = ["sshpass", "-P", "Enter passphrase", "-p", config.password]
                else:
                    self._log("  [yellow]sshpass not installed — key passphrase must be entered manually or use ssh-agent[/yellow]")
            elif not config.key and config.password:
                # No key, password auth: use sshpass for login password
                if shutil.which("sshpass"):
                    self._sshpass = ["sshpass", "-p", config.password]
                else:
                    self._log("  [red]sshpass not installed — cannot use password auth[/red]")

    # -- SSH helpers ----------------------------------------------------------

    def _ssh(self, remote_cmd: str, timeout: int = 10) -> subprocess.CompletedProcess:
        """Run a command on the remote host via SSH."""
        return subprocess.run(
            self._sshpass + self._ssh_base + [self._ssh_target, remote_cmd],
            capture_output=True, text=True, timeout=timeout,
        )

    def _scp(self, local: str, remote: str) -> subprocess.CompletedProcess:
        """SCP deploy with 3-tier fallback (default → SFTP → SCP protocol)."""
        cmd = self._sshpass + self._scp_base + [local, f"{self._ssh_target}:{remote}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            self._log("  [yellow]SCP failed, trying SFTP mode (-s)...[/yellow]")
            cmd_s = self._sshpass + self._scp_base + ["-s", local, f"{self._ssh_target}:{remote}"]
            result = subprocess.run(cmd_s, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            self._log("  [yellow]SFTP failed, trying SCP protocol (-O)...[/yellow]")
            cmd_o = self._sshpass + self._scp_base + ["-O", local, f"{self._ssh_target}:{remote}"]
            result = subprocess.run(cmd_o, capture_output=True, text=True, timeout=60)
        return result

    def _run_local(self, cmd: str, timeout: int = 10) -> subprocess.CompletedProcess:
        """Run a command locally."""
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)

    # -- Steps ----------------------------------------------------------------

    def step_connect(self) -> bool:
        """Step 1: Test SSH connection (remote) or confirm local mode.
        Returns False if connection failed."""
        if self.is_local:
            self._log("[bold]1. Local mode (no SSH)[/bold]")
            self._log("  [green]Host is localhost — using file copy[/green]")
            return True

        self._log("[bold]1. SSH connection... [dim](timeout: 10s)[/dim][/bold]")
        if self._sshpass and not shutil.which("sshpass"):
            self._log("  [red]sshpass not installed[/red]")
            return False
        try:
            result = subprocess.run(
                self._sshpass + self._ssh_base + [self._ssh_target, "echo connected"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                self._log("  [green]Connected successfully[/green]")
                return True
            else:
                err = result.stderr.strip().split("\n")[-1] if result.stderr else "unknown error"
                self._log(f"  [red]Connection failed: {err}[/red]")
                return False
        except subprocess.TimeoutExpired:
            self._log("  [red]Connection timeout[/red]")
            return False
        except FileNotFoundError:
            self._log("  [red]ssh not found[/red]")
            return False
        except Exception as e:
            self._log(f"  [red]Error: {e}[/red]")
            return False

    def step_system_info(self) -> None:
        """Step 2: Show remote/local system information."""
        if self.is_local:
            import platform
            self._log("[bold]2. Local system info...[/bold]")
            self._log(f"  [dim]{platform.node()} {platform.system()} {platform.release()} {platform.machine()}[/dim]")
            try:
                total, used, free = shutil.disk_usage(
                    Path(self.config.target_path).parent
                    if Path(self.config.target_path).parent.exists() else "/"
                )
                self._log(f"  [dim]Disk: {total // (1024**3)}G total, {used // (1024**3)}G used, {free // (1024**3)}G free[/dim]")
            except Exception:
                pass
            return

        self._log("[bold]2. Remote system info... [dim](timeout: 5s)[/dim][/bold]")
        try:
            info_cmd = (
                "uname -snrm 2>/dev/null; "
                "cat /etc/os-release 2>/dev/null | head -2; "
                f"df -h $(dirname '{self.config.target_path}') 2>/dev/null | tail -1"
            )
            result = self._ssh(info_cmd, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    self._log(f"  [dim]{line}[/dim]")
            else:
                self._log("  [dim](could not retrieve)[/dim]")
        except Exception:
            self._log("  [dim](could not retrieve)[/dim]")

    def step_check_binary(self) -> None:
        """Step 3: Check if target binary exists and is running."""
        path = self.config.target_path
        self._log(f"[bold]3. Target binary: {path} [dim](timeout: 5s)[/dim][/bold]")

        if self.is_local:
            target = Path(path)
            if target.exists():
                self._log(f"  [green]File exists ({target.stat().st_size} bytes)[/green]")
                try:
                    result = subprocess.run(
                        ["pidof", target.name], capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        self._log(f"  [green]RUNNING pid={result.stdout.strip()}[/green]")
                    else:
                        self._log("  [yellow]NOT RUNNING[/yellow]")
                except Exception:
                    self._log("  [dim](could not check running state)[/dim]")
            else:
                self._log("  [yellow]File not found (first deploy)[/yellow]")
            return

        try:
            check_cmd = (
                f"if [ -f '{path}' ]; then "
                f"  echo 'EXISTS'; ls -la '{path}'; "
                f"  pid=$(pidof $(basename '{path}') 2>/dev/null); "
                f"  if [ -n \"$pid\" ]; then echo \"RUNNING pid=$pid\"; else echo 'NOT RUNNING'; fi; "
                f"else echo 'NOT FOUND'; fi"
            )
            result = self._ssh(check_cmd, timeout=5)
            output = result.stdout.strip()
            if "EXISTS" in output:
                self._log("  [green]File exists[/green]")
                for line in output.splitlines():
                    if line.startswith("-") or line.startswith("l"):
                        self._log(f"  [dim]{line}[/dim]")
                    elif "RUNNING" in line:
                        self._log(f"  [green]{line}[/green]")
                    elif "NOT RUNNING" in line:
                        self._log(f"  [yellow]{line}[/yellow]")
            else:
                self._log("  [yellow]File not found (first deploy)[/yellow]")
        except Exception as e:
            self._log(f"  [red]Error: {e}[/red]")

    def step_pre_install(self) -> None:
        """Step 4: Execute pre deploy commands."""
        cmd = self.config.pre_cmd
        if not cmd:
            self._log("[bold]4. Pre deploy commands: [dim](none)[/dim][/bold]")
            return

        self._log("[bold]4. Pre deploy commands... [dim](timeout: 10s)[/dim][/bold]")

        # Warn about background commands in pre-deploy — they may race with the deploy
        stripped = cmd.strip()
        if stripped.endswith("&") or stripped.startswith("nohup "):
            self._log("  [yellow]Warning: background command in pre-deploy may race with deploy[/yellow]")
        self._log(f"  [dim]$ {cmd}[/dim]")
        try:
            if self.is_local:
                result = self._run_local(cmd, timeout=10)
            else:
                result = self._ssh(cmd, timeout=10)
            if result.returncode == 0:
                if result.stdout.strip():
                    for line in result.stdout.strip().splitlines():
                        self._log(f"  {line}")
                self._log("  [green]OK (exit 0)[/green]")
            else:
                err = result.stderr.strip() or result.stdout.strip()
                self._log(f"  [red]Failed (exit {result.returncode}): {err}[/red]")
        except Exception as e:
            self._log(f"  [red]Error: {e}[/red]")

    def step_deploy(self) -> bool:
        """Step 5: Deploy/copy the binary. Returns True on success."""
        path = self.config.target_path

        if self.dry_run or self.binary_path is None:
            self._log("[bold]5. Deploy (dry-run)... [dim](timeout: 60s)[/dim][/bold]")
            if self.is_local:
                self._log(f"  [dim]$ cp <binary> {path}[/dim]")
            else:
                scp_str = " ".join(self._sshpass + self._scp_base + ["<binary>", f"{self._ssh_target}:{path}"])
                self._log(f"  [dim]$ {scp_str}[/dim]")
            self._log("  [yellow]Skipped (dry-run)[/yellow]")
            return True

        binary_name = self.binary_path.name
        self._log(f"[bold]5. Deploy: {binary_name}... [dim](timeout: 60s)[/dim][/bold]")

        if self.is_local:
            try:
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(self.binary_path), str(target))
                target.chmod(target.stat().st_mode | 0o111)
                self._log("  [green]Copy OK[/green]")
                return True
            except Exception as e:
                self._log(f"  [red]Copy failed: {e}[/red]")
                return False

        # Remote SCP
        try:
            result = self._scp(str(self.binary_path), path)
            if result.returncode == 0:
                self._log("  [green]Deploy OK[/green]")
                # chmod +x
                try:
                    self._ssh(f"chmod +x '{path}'", timeout=10)
                except Exception:
                    pass
                return True
            else:
                err = result.stderr.strip() or "unknown error"
                self._log(f"  [red]Deploy failed: {err}[/red]")
                return False
        except subprocess.TimeoutExpired:
            self._log("  [red]Deploy timeout (120s)[/red]")
            return False
        except Exception as e:
            self._log(f"  [red]Deploy error: {e}[/red]")
            return False

    def step_post_install(self) -> None:
        """Step 6: Execute post deploy commands."""
        cmd = self.config.post_cmd
        if not cmd:
            self._log("[bold]6. Post deploy commands: [dim](none)[/dim][/bold]")
            return

        self._log("[bold]6. Post deploy commands... [dim](timeout: 10s)[/dim][/bold]")
        self._log(f"  [dim]$ {cmd}[/dim]")
        try:
            if self.is_local:
                result = self._run_local(cmd, timeout=10)
            else:
                result = self._ssh(cmd, timeout=10)
            if result.returncode == 0:
                if result.stdout.strip():
                    for line in result.stdout.strip().splitlines():
                        self._log(f"  {line}")
                self._log("  [green]OK (exit 0)[/green]")
            else:
                err = result.stderr.strip() or result.stdout.strip()
                self._log(f"  [red]Failed (exit {result.returncode}): {err}[/red]")
        except Exception as e:
            self._log(f"  [red]Error: {e}[/red]")

    # -- Full run -------------------------------------------------------------

    def run(self, verbose: bool = True) -> bool:
        """Execute deployment steps. Returns True if deployment succeeded.

        When *verbose* is True (default, used by F9 Deployment modal),
        system info and binary check steps are included. When False
        (used by Phase 5 during build), they are skipped for speed.
        """
        if not self.step_connect():
            return False
        if verbose:
            self.step_system_info()
            self.step_check_binary()
        self.step_pre_install()
        ok = self.step_deploy()
        self.step_post_install()
        self._log(f"  [green]Done: {self.name}[/green]")
        return ok


# Backward compat alias
Uploader = Deployer
