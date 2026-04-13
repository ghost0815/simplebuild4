"""Privileged command execution — sudo or su with password."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable


def run_privileged(
    cmd: str,
    password: str = "",
    log: Callable[[str], None] = lambda m: None,
) -> bool:
    """Run a shell command with root privileges via sudo -S or su with pty."""
    if shutil.which("sudo"):
        return _run_sudo(cmd, password, log)
    elif password:
        return _run_su(cmd, password, log)
    else:
        log("[red]No sudo available and no password provided[/red]")
        return False


def _run_sudo(cmd: str, password: str, log: Callable) -> bool:
    """Run via sudo -S (password over stdin)."""
    full_cmd = ["sudo", "-S", "-p", "", "sh", "-c", cmd]
    log(f"[dim]$ sudo sh -c '{cmd}'[/dim]")
    try:
        proc = subprocess.Popen(
            full_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd="/tmp",
        )
        if password:
            proc.stdin.write(password + "\n")
            proc.stdin.flush()
        try:
            proc.stdin.close()
        except Exception:
            pass
        for line in proc.stdout:
            stripped = line.rstrip()
            if stripped:
                if password:
                    stripped = stripped.replace(password, "***")
                log(f"  {stripped}")
        proc.wait()
        if proc.returncode != 0:
            log(f"[red]Command failed (exit {proc.returncode})[/red]")
            return False
        return True
    except Exception as e:
        msg = str(e).replace(password, "***") if password else str(e)
        log(f"[red]Error: {msg}[/red]")
        return False


def _run_su(cmd: str, password: str, log: Callable) -> bool:
    """Run via su -c using a pseudo-terminal to feed the password."""
    import pty
    import select
    import time

    escaped = cmd.replace("'", "'\\''")
    log(f"[dim]$ su -c '{escaped}'[/dim]")
    try:
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            ["su", "-c", cmd],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd="/tmp",
        )
        os.close(slave_fd)
        # Wait for password prompt, then send password
        time.sleep(0.3)
        os.write(master_fd, (password + "\n").encode())
        # Read output
        output = []
        while True:
            r, _, _ = select.select([master_fd], [], [], 1.0)
            if r:
                try:
                    data = os.read(master_fd, 4096).decode(errors="replace")
                    if not data:
                        break
                    output.append(data)
                except OSError:
                    break
            elif proc.poll() is not None:
                break
        os.close(master_fd)
        proc.wait()
        # Log output (mask password, skip prompt lines)
        full = "".join(output)
        for line in full.splitlines():
            stripped = line.strip()
            if not stripped or "assword" in stripped:
                continue
            if password:
                stripped = stripped.replace(password, "***")
            log(f"  {stripped}")
        if proc.returncode != 0:
            log(f"[red]Command failed (exit {proc.returncode})[/red]")
            return False
        return True
    except Exception as e:
        msg = str(e).replace(password, "***") if password else str(e)
        log(f"[red]Error: {msg}[/red]")
        return False
