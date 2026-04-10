"""Upload check modal — test SSH connectivity and dry-run upload."""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, RichLog, Button
from textual.containers import Horizontal

from core.target import UploadConfig
from translation import t


class UploadCheckModal(ModalScreen[None]):
    """Modal that tests SSH connectivity and performs a dry-run upload check."""

    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    UploadCheckModal {
        align: center middle;
    }
    #check-box {
        width: 120;
        height: 30;
        border: solid $accent;
        background: $surface;
        padding: 1;
    }
    #check-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #check-log {
        height: 1fr;
    }
    #check-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    """

    SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "HostKeyAlgorithms=+ssh-rsa"]

    def __init__(self, uploads: list[UploadConfig], target_name: str,
                 logs_dir: Optional[Path] = None) -> None:
        super().__init__()
        self._uploads = [u for u in uploads if u.host]
        self._target_name = target_name
        self._logs_dir = logs_dir

    def compose(self) -> ComposeResult:
        with Vertical(id="check-box"):
            yield Static(f"[bold]Upload Check — {self._target_name}[/bold]", id="check-title")
            yield RichLog(id="check-log", highlight=False, markup=True, wrap=True)
            with Horizontal(id="check-buttons"):
                yield Button(t("cancel"), id="check-close")

    def on_mount(self) -> None:
        if not self._uploads:
            log = self.query_one("#check-log", RichLog)
            log.write("[yellow]No upload destinations configured.[/yellow]")
            return
        threading.Thread(target=self._run_checks, daemon=True).start()

    def _log(self, msg: str) -> None:
        self.app.call_from_thread(self.query_one("#check-log", RichLog).write, msg)

    def _run_checks(self) -> None:
        log_lines: list[str] = []

        def log(msg: str) -> None:
            log_lines.append(msg)
            self._log(msg)

        log(f"[bold]Upload Check started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/bold]")
        log(f"Target: {self._target_name}")
        log(f"Destinations: {len(self._uploads)}")
        log("")

        for idx, u in enumerate(self._uploads):
            name = u.name or f"#{idx + 1}"
            log(f"[bold]━━━ {name}: {u.user}@{u.host}:{u.target_path} ━━━[/bold]")
            log("")

            # Build SSH base command
            ssh_base = ["ssh", "-p", str(u.port)] + self.SSH_OPTS
            if u.key:
                ssh_base += ["-i", u.key]
            ssh_target = f"{u.user}@{u.host}"

            # Use sshpass if password set
            sshpass_prefix = []
            if u.password:
                sshpass_prefix = ["sshpass", "-p", u.password]

            def ssh_cmd(remote_cmd: str) -> list[str]:
                return sshpass_prefix + ssh_base + [ssh_target, remote_cmd]

            # Step 1: SSH connection test
            log("[bold]1. SSH connection...[/bold]")
            try:
                result = subprocess.run(
                    ssh_cmd("echo connected"),
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    log("  [green]Connected successfully[/green]")
                else:
                    err = result.stderr.strip().split("\n")[-1] if result.stderr else "unknown error"
                    log(f"  [red]Connection failed: {err}[/red]")
                    log("")
                    continue
            except subprocess.TimeoutExpired:
                log("  [red]Connection timeout (15s)[/red]")
                log("")
                continue
            except FileNotFoundError:
                if sshpass_prefix:
                    log("  [red]sshpass not installed — required for password auth[/red]")
                else:
                    log("  [red]ssh not found[/red]")
                log("")
                continue
            except Exception as e:
                log(f"  [red]Error: {e}[/red]")
                log("")
                continue

            # Step 2: System information
            log("[bold]2. Remote system info...[/bold]")
            try:
                info_cmd = "uname -snrm 2>/dev/null; cat /etc/os-release 2>/dev/null | head -2; df -h $(dirname '{path}') 2>/dev/null | tail -1".format(path=u.target_path)
                result = subprocess.run(
                    ssh_cmd(info_cmd),
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    for line in result.stdout.strip().splitlines():
                        log(f"  [dim]{line}[/dim]")
                else:
                    log("  [dim](could not retrieve)[/dim]")
            except Exception:
                log("  [dim](could not retrieve)[/dim]")

            # Step 3: Check if target binary exists and is running
            log(f"[bold]3. Target binary: {u.target_path}[/bold]")
            try:
                check_cmd = (
                    f"if [ -f '{u.target_path}' ]; then "
                    f"  echo 'EXISTS'; ls -la '{u.target_path}'; "
                    f"  pid=$(pidof $(basename '{u.target_path}') 2>/dev/null); "
                    f"  if [ -n \"$pid\" ]; then echo \"RUNNING pid=$pid\"; else echo 'NOT RUNNING'; fi; "
                    f"else echo 'NOT FOUND'; fi"
                )
                result = subprocess.run(
                    ssh_cmd(check_cmd),
                    capture_output=True, text=True, timeout=10,
                )
                output = result.stdout.strip()
                if "EXISTS" in output:
                    log(f"  [green]File exists[/green]")
                    for line in output.splitlines():
                        if line.startswith("-") or line.startswith("l"):
                            log(f"  [dim]{line}[/dim]")
                        elif "RUNNING" in line:
                            log(f"  [green]{line}[/green]")
                        elif "NOT RUNNING" in line:
                            log(f"  [yellow]{line}[/yellow]")
                else:
                    log(f"  [yellow]File not found (first deploy)[/yellow]")
            except Exception as e:
                log(f"  [red]Error: {e}[/red]")

            # Step 4: Pre install commands
            if u.pre_cmd:
                log(f"[bold]4. Pre install commands...[/bold]")
                log(f"  [dim]$ {u.pre_cmd}[/dim]")
                try:
                    result = subprocess.run(
                        ssh_cmd(u.pre_cmd),
                        capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode == 0:
                        if result.stdout.strip():
                            for line in result.stdout.strip().splitlines():
                                log(f"  {line}")
                        log(f"  [green]OK (exit 0)[/green]")
                    else:
                        err = result.stderr.strip() or result.stdout.strip()
                        log(f"  [red]Failed (exit {result.returncode}): {err}[/red]")
                except Exception as e:
                    log(f"  [red]Error: {e}[/red]")
            else:
                log(f"[bold]4. Pre install commands: [dim](none)[/dim][/bold]")

            # Step 5: Dry-run copy
            log(f"[bold]5. Upload (dry-run)...[/bold]")
            scp_base = ["scp", "-P", str(u.port)] + self.SSH_OPTS
            if u.key:
                scp_base += ["-i", u.key]
            scp_cmd_str = " ".join(sshpass_prefix + scp_base + ["<binary>", f"{ssh_target}:{u.target_path}"])
            log(f"  [dim]$ {scp_cmd_str}[/dim]")
            log(f"  [yellow]Skipped (dry-run — no binary to upload)[/yellow]")

            # Step 6: Post install commands
            if u.post_cmd:
                log(f"[bold]6. Post install commands...[/bold]")
                log(f"  [dim]$ {u.post_cmd}[/dim]")
                try:
                    result = subprocess.run(
                        ssh_cmd(u.post_cmd),
                        capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode == 0:
                        if result.stdout.strip():
                            for line in result.stdout.strip().splitlines():
                                log(f"  {line}")
                        log(f"  [green]OK (exit 0)[/green]")
                    else:
                        err = result.stderr.strip() or result.stdout.strip()
                        log(f"  [red]Failed (exit {result.returncode}): {err}[/red]")
                except Exception as e:
                    log(f"  [red]Error: {e}[/red]")
            else:
                log(f"[bold]6. Post install commands: [dim](none)[/dim][/bold]")

            log("")

        log(f"[bold]Check complete: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/bold]")

        # Write log file
        if self._logs_dir:
            try:
                self._logs_dir.mkdir(parents=True, exist_ok=True)
                import re
                timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
                upload_names = "-".join(
                    (u.name or u.host).replace(" ", "_").replace("/", "_")
                    for u in self._uploads
                )
                log_path = self._logs_dir / f"{timestamp}.{self._target_name}.{upload_names}.upload-check.log"
                plain_lines = [re.sub(r'\[/?[a-z_ #0-9]+\]', '', line) for line in log_lines]
                log_path.write_text("\n".join(plain_lines) + "\n", encoding="utf-8")
                log(f"Log: {log_path}")
            except Exception:
                pass

        # Apply log retention
        try:
            from core.log_cleanup import run_retention
            run_retention(self._logs_dir, self.app.settings)
        except Exception:
            pass

    def on_button_pressed(self, event) -> None:
        if event.button.id == "check-close":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
