"""systemd system-level service management for the s4 web interface."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

_BLOCKED_PORTS = frozenset({
    3306, 5432, 6379, 27017,      # databases
    8088, 8880, 9090,             # common admin interfaces
    8888,                         # oscam WebIF
})


def generate_self_signed_cert(
    output_dir: Path,
    dns_name: str = "",
) -> tuple[str, str]:
    """Generate a self-signed ECDSA P-256 certificate.

    Returns (cert_path, key_path).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cert_path = output_dir / "s4-web.crt"
    key_path = output_dir / "s4-web.key"

    # Build SAN list
    san_entries = ["DNS:localhost", "IP:127.0.0.1"]
    if dns_name and dns_name not in ("localhost",):
        san_entries.insert(0, f"DNS:{dns_name}")

    cmd = [
        "openssl", "req", "-x509",
        "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-days", "365",
        "-keyout", str(key_path),
        "-out", str(cert_path),
        "-subj", "/CN=simplebuild4",
        "-addext", f"subjectAltName={','.join(san_entries)}",
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    os.chmod(str(key_path), 0o600)
    return str(cert_path), str(key_path)


def validate_port(port: int) -> str | None:
    """Return an error message if the port is blocked, or None if OK."""
    if port < 1024:
        return f"Port {port} is reserved (ports below 1024 require root)"
    if port > 65535:
        return f"Port {port} is out of range (max 65535)"
    if port in _BLOCKED_PORTS:
        return f"Port {port} is blocked (known service conflict)"
    return None


_UNIT_TEMPLATE = """\
[Unit]
Description=SimpleBuild4 Web Service ({root})
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={root}
ExecStart={python} -c "{server_code}"
Restart=on-failure
RestartSec=3
Environment=PATH={path}
StandardOutput=append:{log_path}
StandardError=append:{log_path}

[Install]
WantedBy=multi-user.target
"""

def _build_server_code(
    python: str, app_py: str, host: str, port: int,
    public_host: str, templates: str,
    ssl: bool = False, ssl_cert: str = "", ssl_key: str = "",
    auth_user: str = "", auth_password: str = "",
    theme: str = "textual-dark",
) -> str:
    """Build the inline Python code for launching the web server."""
    protocol = "https" if ssl else "http"
    parts = [
        "import sys; sys.path.insert(0, '.');",
        "from core.web_server import S4Server;",
        f"S4Server(",
        f"  command='{python} {app_py}',",
        f"  host='{host}', port={port},",
        f"  public_url='{protocol}://{public_host}:{port}',",
        f"  templates_path='{templates}',",
        f"  theme='{theme}',",
    ]
    if ssl and ssl_cert and ssl_key:
        parts.append(f"  ssl_certfile='{ssl_cert}',")
        parts.append(f"  ssl_keyfile='{ssl_key}',")
    if auth_user:
        parts.append(f"  auth_user='{auth_user}',")
        parts.append(f"  auth_password='{auth_password}',")
    parts.append(").serve()")
    return " ".join(parts)


_UNIT_DIR = Path("/etc/systemd/system")


class WebServiceManager:
    """Manage a system-level systemd service for textual-serve."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.service = self._resolve_service_name()
        self._unit_path = _UNIT_DIR / f"{self.service}.service"
        self._sudoers_path = Path(f"/etc/sudoers.d/{self.service}")

    def _resolve_service_name(self) -> str:
        """Find or assign a numbered service name for this s4 root.

        Scans existing s4-web*.service unit files. If one already points
        to this root, reuse its name. Otherwise pick the next free number
        (s4-web, s4-web-2, s4-web-3, ...).
        """
        if not _UNIT_DIR.exists():
            return "s4-web"
        for unit in sorted(_UNIT_DIR.glob("s4-web*.service")):
            try:
                content = unit.read_text(encoding="utf-8")
                if f"WorkingDirectory={self.root}" in content:
                    return unit.stem
            except Exception:
                continue
        if not (_UNIT_DIR / "s4-web.service").exists():
            return "s4-web"
        n = 2
        while (_UNIT_DIR / f"s4-web-{n}.service").exists():
            n += 1
        return f"s4-web-{n}"

    # ---------------------------------------------------------- queries

    @staticmethod
    def systemd_available() -> bool:
        """Return True if systemd is running as PID 1."""
        try:
            return Path("/proc/1/comm").read_text().strip() == "systemd"
        except Exception:
            return False

    def is_installed(self) -> bool:
        return self._unit_path.exists()

    def is_running(self) -> bool:
        if not self.is_installed():
            return False
        try:
            r = subprocess.run(
                ["systemctl", "is-active", self.service],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip() == "active"
        except Exception:
            return False

    def status_text(self) -> str:
        """Return a human-readable status key for translation."""
        if not self.systemd_available():
            return "web_svc_not_available"
        if not self.is_installed():
            return "web_svc_not_installed"
        if self.is_running():
            return "web_svc_installed_running"
        return "web_svc_installed_stopped"

    # --------------------------------------------------------- actions

    def install(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        dns_name: str = "",
        ssl: bool = False,
        ssl_cert: str = "",
        ssl_key: str = "",
        auth_user: str = "",
        auth_password: str = "",
        logs_dir: Optional[Path] = None,
        organize_logs: bool = False,
        password: str = "",
        log: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Write unit file + sudoers, reload daemon, enable and start."""
        _log = log or (lambda m: None)
        python = sys.executable
        app_py = str(self.root / "app.py")
        templates = str(self.root / "ui" / "web" / "templates")
        public_host = dns_name or ("localhost" if host == "0.0.0.0" else host)
        user = os.environ.get("USER", os.environ.get("LOGNAME", "root"))
        # Only include Linux paths, filter out Windows/WSL mount paths
        path = ":".join(
            p for p in os.environ.get("PATH", "").split(":")
            if p and not p.startswith("/mnt/")
        ) or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

        # Auto-generate cert if SSL enabled but no cert provided
        if ssl and (not ssl_cert or not ssl_key):
            web_dir = self.root / "data" / "web"
            ssl_cert, ssl_key = generate_self_signed_cert(web_dir, dns_name)
            _log(f"  Generated self-signed certificate: {ssl_cert}")

        # Resolve log path
        if logs_dir:
            from core.log_cleanup import resolve_log_dir
            _log_dir = resolve_log_dir(logs_dir, "webservice", organize_logs)
        else:
            _log_dir = self.root / "data" / "logs"
            _log_dir.mkdir(parents=True, exist_ok=True)
        log_file = _log_dir / f"{self.service}.log"

        protocol = "https" if ssl else "http"
        server_code = _build_server_code(
            python=python, app_py=app_py, host=host, port=port,
            public_host=public_host, templates=templates,
            ssl=ssl, ssl_cert=ssl_cert, ssl_key=ssl_key,
            auth_user=auth_user, auth_password=auth_password,
        )

        unit = _UNIT_TEMPLATE.format(
            root=self.root, python=python,
            server_code=server_code,
            path=path, log_path=log_file, user=user,
        )

        # sudoers entry: allows start/stop/restart/status without password
        systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
        sudoers = (
            f"# SimpleBuild4 web service — auto-generated\n"
            f"{user} ALL=(ALL) NOPASSWD: "
            f"{systemctl} start {self.service}, "
            f"{systemctl} stop {self.service}, "
            f"{systemctl} restart {self.service}, "
            f"{systemctl} status {self.service}\n"
        )

        # Write unit file + sudoers + daemon-reload + enable via root
        import tempfile
        try:
            # Write to temp files first, then move with root privileges
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".service", delete=False,
            ) as tf_unit:
                tf_unit.write(unit)
                tmp_unit = tf_unit.name

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sudoers", delete=False,
            ) as tf_sudo:
                tf_sudo.write(sudoers)
                tmp_sudoers = tf_sudo.name
            # Ensure sudoers file has correct permissions before moving
            os.chmod(tmp_sudoers, 0o440)

            cmds = (
                f"cp {tmp_unit} {self._unit_path} && "
                f"chmod 644 {self._unit_path} && "
                f"cp {tmp_sudoers} {self._sudoers_path} && "
                f"chmod 440 {self._sudoers_path} && "
                f"systemctl daemon-reload && "
                f"systemctl enable {self.service}"
            )
            if not self._run_privileged(cmds, password, _log):
                return False

            # Clean up temp files
            Path(tmp_unit).unlink(missing_ok=True)
            Path(tmp_sudoers).unlink(missing_ok=True)

        except Exception as e:
            _log(f"[red]Failed to prepare service files: {e}[/red]")
            return False

        # On WSL, ensure systemd is enabled in /etc/wsl.conf
        if self._is_wsl():
            self._ensure_wsl_systemd(password, _log)

        _log("[green]Service installed[/green]")
        return True

    def uninstall(
        self,
        password: str = "",
        log: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Stop, disable, remove unit file and sudoers entry."""
        _log = log or (lambda m: None)
        cmds = (
            f"systemctl stop {self.service} ; "
            f"systemctl disable {self.service} ; "
            f"rm -f {self._unit_path} {self._sudoers_path} && "
            f"systemctl daemon-reload"
        )
        if not self._run_privileged(cmds, password, _log):
            return False
        _log("[green]Service uninstalled[/green]")
        return True

    def start(self, log: Optional[Callable[[str], None]] = None) -> bool:
        """Start the service (uses passwordless sudo via sudoers)."""
        _log = log or (lambda m: None)
        if self._sudo_systemctl("start", _log):
            _log("[green]Service started[/green]")
            return True
        return False

    def stop(self, log: Optional[Callable[[str], None]] = None) -> bool:
        _log = log or (lambda m: None)
        if self._sudo_systemctl("stop", _log):
            _log("[green]Service stopped[/green]")
            return True
        return False

    def restart(self, log: Optional[Callable[[str], None]] = None) -> bool:
        _log = log or (lambda m: None)
        if self._sudo_systemctl("restart", _log):
            _log("[green]Service restarted[/green]")
            return True
        return False

    # --------------------------------------------------------- helpers

    def _sudo_systemctl(self, action: str, log: Callable) -> bool:
        """Run sudo systemctl <action> <service> (passwordless via sudoers)."""
        cmd = ["sudo", "systemctl", action, self.service]
        log(f"[dim]$ {' '.join(cmd)}[/dim]")
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
            )
            if r.stdout.strip():
                log(r.stdout.strip())
            if r.stderr.strip():
                log(r.stderr.strip())
            if r.returncode != 0:
                log(f"[red]Command failed (exit {r.returncode})[/red]")
                return False
            return True
        except Exception as e:
            log(f"[red]Error: {e}[/red]")
            return False

    @staticmethod
    def _run_privileged(
        cmd: str,
        password: str,
        log: Callable[[str], None],
    ) -> bool:
        """Run a shell command with root privileges via sudo -S or su with pty."""
        if shutil.which("sudo"):
            return WebServiceManager._run_sudo(cmd, password, log)
        elif password:
            return WebServiceManager._run_su(cmd, password, log)
        else:
            log("[red]No sudo available and no password provided[/red]")
            return False

    @staticmethod
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

    @staticmethod
    def _run_su(cmd: str, password: str, log: Callable) -> bool:
        """Run via su -c using a pseudo-terminal to feed the password."""
        import pty, select
        escaped = cmd.replace("'", "'\\''")
        display = f"su -c '{escaped}'"
        log(f"[dim]$ {display}[/dim]")
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
            import time
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

    @staticmethod
    def _is_wsl() -> bool:
        """Return True if running inside WSL."""
        try:
            return "microsoft" in Path("/proc/version").read_text().lower()
        except Exception:
            return False

    @staticmethod
    def _ensure_wsl_systemd(password: str, log: Callable[[str], None]) -> None:
        """Ensure /etc/wsl.conf has [boot] systemd=true."""
        wsl_conf = Path("/etc/wsl.conf")
        try:
            content = wsl_conf.read_text() if wsl_conf.exists() else ""
        except Exception:
            content = ""

        if "systemd=true" in content:
            return

        log("[yellow]WSL detected — enabling systemd in /etc/wsl.conf[/yellow]")
        cmd = 'echo -e "\\n[boot]\\nsystemd=true\\n" >> /etc/wsl.conf'
        if WebServiceManager._run_privileged(cmd, password, log):
            log("[green]wsl.conf updated — restart WSL with: wsl --shutdown[/green]")
        else:
            log("[yellow]Add manually to /etc/wsl.conf: [boot] systemd=true[/yellow]")
