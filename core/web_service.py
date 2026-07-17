"""systemd system-level service management for the s4 web interface."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from core.env import is_running_in_container, probe_port

def _ensure_web_packages(python: str, log: Callable[[str], None]) -> bool:
    """Install textual-serve if not already present.

    Returns True on success (or already installed), False on failure.
    """
    pkgs = []
    for mod, pkg in [("textual_serve", "textual-serve")]:
        try:
            __import__(mod)
        except ImportError:
            pkgs.append(pkg)
    if not pkgs:
        return True
    log(f"  Installing: {', '.join(pkgs)} …")
    pip = [python, "-m", "pip", "install", "--user"] + pkgs
    try:
        subprocess.check_call(pip, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        try:
            subprocess.check_call(
                pip[:5] + ["--break-system-packages"] + pkgs,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            return True
        except Exception as e:
            log(f"[red]  Failed to install {pkgs}: {e}[/red]")
            return False


_BLOCKED_PORTS = frozenset({
    3306, 5432, 6379, 27017,      # databases
    8088, 8880, 9090,             # common admin interfaces
    8888,                         # oscam WebIF
})


def generate_self_signed_cert(
    output_dir: Path,
    dns_name: str = "",
    bind_host: str = "",
) -> tuple[str, str]:
    """Generate a self-signed certificate.

    Uses ECDSA P-256 on modern OpenSSL (>= 1.1.1), RSA 2048 on older.
    Uses a config file for SAN instead of -addext (not available on OpenSSL < 1.1.1).
    Includes bind address and DNS name in SAN for browser compatibility.

    Returns (cert_path, key_path).
    """
    import tempfile

    output_dir.mkdir(parents=True, exist_ok=True)
    cert_path = output_dir / "s4-web.crt"
    key_path = output_dir / "s4-web.key"

    # Build SAN list — dual-stack (IPv4 + IPv6 loopback always)
    san_entries = ["DNS:localhost", "IP:127.0.0.1", "IP:::1"]
    _wildcards = {"0.0.0.0", "::", "::/0", "localhost", "127.0.0.1", "::1"}
    if bind_host and bind_host not in _wildcards:
        # Strip brackets from IPv6 like [::1]
        clean_host = bind_host.strip("[]")
        san_entries.append(f"IP:{clean_host}")
    if dns_name and dns_name not in ("localhost",):
        san_entries.insert(0, f"DNS:{dns_name}")

    # Detect OpenSSL version for feature support
    try:
        ver_out = subprocess.check_output(
            ["openssl", "version"], text=True, timeout=5,
        ).strip()
        # e.g. "OpenSSL 1.1.1f  31 Mar 2020" or "OpenSSL 3.0.2 ..."
        import re
        ver_match = re.search(r'(\d+)\.(\d+)\.(\d+)', ver_out)
        if ver_match:
            major, minor, patch = int(ver_match.group(1)), int(ver_match.group(2)), int(ver_match.group(3))
            modern_ssl = (major, minor, patch) >= (1, 1, 1)
        else:
            modern_ssl = False
    except Exception:
        modern_ssl = False

    # Write OpenSSL config with SAN (works on all versions)
    san_config = (
        "[req]\n"
        "distinguished_name = req_dn\n"
        "x509_extensions = v3_ext\n"
        "prompt = no\n"
        "\n"
        "[req_dn]\n"
        "CN = simplebuild4\n"
        "\n"
        "[v3_ext]\n"
        f"subjectAltName = {','.join(san_entries)}\n"
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".cnf", delete=False) as cnf:
        cnf.write(san_config)
        cnf_path = cnf.name

    try:
        if modern_ssl:
            # ECDSA P-256
            cmd = [
                "openssl", "req", "-x509",
                "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                "-nodes", "-days", "365",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-config", cnf_path,
            ]
        else:
            # RSA 2048 fallback for old OpenSSL
            cmd = [
                "openssl", "req", "-x509",
                "-newkey", "rsa:2048",
                "-nodes", "-days", "365",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-config", cnf_path,
            ]
        subprocess.run(cmd, check=True, capture_output=True)
    finally:
        try:
            os.unlink(cnf_path)
        except OSError:
            pass

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
StandardOutput={output_mode}:{log_path}
StandardError={output_mode}:{log_path}

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
        "import os, sys; sys.path.insert(0, '.');",
        "os.environ['S4_WEB_MODE'] = '1';",
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


def _format_host_for_url(host: str) -> str:
    """Wrap IPv6 addresses in brackets for URL use."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _systemd_output_mode() -> str:
    """Return 'append' if systemd >= 240, else 'file' for older versions."""
    try:
        out = subprocess.check_output(
            ["systemctl", "--version"], text=True, timeout=5,
        ).splitlines()[0]  # e.g. "systemd 245 (245.4-4ubuntu3.22)"
        import re
        m = re.search(r'(\d+)', out)
        if m and int(m.group(1)) >= 240:
            return "append"
    except Exception:
        pass
    return "file"


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

    @staticmethod
    def container_mode() -> bool:
        """True when running inside a container — no systemd management."""
        return is_running_in_container()

    def _load_settings(self):
        from core.settings import Settings
        return Settings.load(self.root / "data" / "settings.yaml")

    def _save_settings(self, settings) -> None:
        settings.save(self.root / "data" / "settings.yaml")

    def is_installed(self) -> bool:
        if self.container_mode():
            try:
                return bool(self._load_settings().web_enabled)
            except Exception:
                return False
        return self._unit_path.exists()

    def is_running(self) -> bool:
        if self.container_mode():
            try:
                s = self._load_settings()
            except Exception:
                return False
            return probe_port(s.web_host or "0.0.0.0", s.web_port or 8080)
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
        if self.container_mode():
            return (
                "web_svc_container_running"
                if self.is_running()
                else "web_svc_container_stopped"
            )
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

        if self.container_mode():
            try:
                s = self._load_settings()
                s.web_enabled = True
                s.web_host = host
                s.web_port = port
                s.web_dns_name = dns_name
                s.web_ssl = ssl
                s.web_ssl_cert = ssl_cert
                s.web_ssl_key = ssl_key
                s.web_auth_user = auth_user
                s.web_auth_password = auth_password
                self._save_settings(s)
            except Exception as e:
                _log(f"[red]Failed to persist settings: {e}[/red]")
                return False
            _log("[green]Settings saved. Restart the container to apply.[/green]")
            return True

        python = sys.executable
        app_py = str(self.root / "app.py")
        templates = str(self.root / "ui" / "web" / "templates")
        _wildcards = ("0.0.0.0", "::", "::/0")
        public_host = dns_name or ("localhost" if host in _wildcards else host)
        public_host = _format_host_for_url(public_host)
        user = os.environ.get("USER", os.environ.get("LOGNAME", "root"))
        # Only include Linux paths, filter out Windows/WSL mount paths
        path = ":".join(
            p for p in os.environ.get("PATH", "").split(":")
            if p and not p.startswith("/mnt/")
        ) or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

        # Auto-generate cert if SSL enabled but no cert provided
        if ssl and (not ssl_cert or not ssl_key):
            web_dir = self.root / "data" / "web"
            ssl_cert, ssl_key = generate_self_signed_cert(web_dir, dns_name, host)
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
            output_mode=_systemd_output_mode(),
        )

        # sudoers entry: allows start/stop/restart/status without password
        systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
        tee = shutil.which("tee") or "/usr/bin/tee"
        sudoers = (
            f"# SimpleBuild4 web service — auto-generated\n"
            f"{user} ALL=(ALL) NOPASSWD: "
            f"{systemctl} start {self.service}, "
            f"{systemctl} stop {self.service}, "
            f"{systemctl} restart {self.service}, "
            f"{systemctl} status {self.service}, "
            f"{systemctl} daemon-reload, "
            f"{tee} {self._unit_path}\n"
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

        # Ensure required Python packages are installed before the service starts
        _log("Checking Python dependencies …")
        _ensure_web_packages(sys.executable, _log)

        _log("[green]Service installed[/green]")
        return True

    def install_wsl_systemd_only(
        self,
        password: str = "",
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        """WSL pre-step: write /etc/wsl.conf and instruct the user to restart.

        Called when WSL is detected but systemd is not yet running as PID 1.
        The full install() must be run again after the WSL restart.
        """
        _log = log or (lambda m: None)
        self._ensure_wsl_systemd(password, _log)
        _log("")
        _log("[yellow]WSL restart required to activate systemd.[/yellow]")
        _log("[yellow]From Windows CMD or PowerShell, run:[/yellow]")
        _log("[bold]  wsl.exe --shutdown[/bold]")
        _log("[yellow]Then reopen s4 and install the service again.[/yellow]")

    def uninstall(
        self,
        password: str = "",
        log: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Stop, disable, remove unit file and sudoers entry."""
        _log = log or (lambda m: None)

        if self.container_mode():
            try:
                s = self._load_settings()
                s.web_enabled = False
                self._save_settings(s)
            except Exception as e:
                _log(f"[red]Failed to persist settings: {e}[/red]")
                return False
            _log("[green]Disabled. Restart the container to apply.[/green]")
            return True

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

    def update_unit(
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
        theme: str = "textual-dark",
        log: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Update the systemd unit file with current settings (no password needed).

        Uses the existing sudoers entry to overwrite the unit file.
        """
        _log = log or (lambda m: None)

        if self.container_mode():
            # Settings are persisted by the caller; nothing to update on disk.
            return True

        if not self.is_installed():
            return False

        python = sys.executable
        app_py = str(self.root / "app.py")
        templates = str(self.root / "ui" / "web" / "templates")
        _wildcards = ("0.0.0.0", "::", "::/0")
        public_host = dns_name or ("localhost" if host in _wildcards else host)
        public_host = _format_host_for_url(public_host)
        user = os.environ.get("USER", os.environ.get("LOGNAME", "root"))
        path = ":".join(
            p for p in os.environ.get("PATH", "").split(":")
            if p and not p.startswith("/mnt/")
        ) or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

        if ssl and (not ssl_cert or not ssl_key):
            web_dir = self.root / "data" / "web"
            ssl_cert, ssl_key = generate_self_signed_cert(web_dir, dns_name, host)

        if logs_dir:
            from core.log_cleanup import resolve_log_dir
            _log_dir = resolve_log_dir(logs_dir, "webservice", organize_logs)
        else:
            _log_dir = self.root / "data" / "logs"
            _log_dir.mkdir(parents=True, exist_ok=True)
        log_file = _log_dir / f"{self.service}.log"

        server_code = _build_server_code(
            python=python, app_py=app_py, host=host, port=port,
            public_host=public_host, templates=templates,
            ssl=ssl, ssl_cert=ssl_cert, ssl_key=ssl_key,
            auth_user=auth_user, auth_password=auth_password,
            theme=theme,
        )

        unit = _UNIT_TEMPLATE.format(
            root=self.root, python=python,
            server_code=server_code,
            path=path, log_path=log_file, user=user,
            output_mode=_systemd_output_mode(),
        )

        # Write unit via sudo tee (allowed by sudoers)
        tee = shutil.which("tee") or "/usr/bin/tee"
        try:
            r = subprocess.run(
                ["sudo", tee, str(self._unit_path)],
                input=unit, capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                _log(f"[red]Failed to write unit: {r.stderr.strip()}[/red]")
                return False
            # daemon-reload to pick up changes
            self._sudo_systemctl("daemon-reload", _log)
        except Exception as e:
            _log(f"[red]Failed to update unit: {e}[/red]")
            return False
        return True

    def start(self, log: Optional[Callable[[str], None]] = None) -> bool:
        """Start the service (uses passwordless sudo via sudoers)."""
        _log = log or (lambda m: None)
        if self.container_mode():
            return self._container_toggle(True, _log)
        if self._sudo_systemctl("start", _log):
            _log("[green]Service started[/green]")
            return True
        return False

    def stop(self, log: Optional[Callable[[str], None]] = None) -> bool:
        _log = log or (lambda m: None)
        if self.container_mode():
            return self._container_toggle(False, _log)
        if self._sudo_systemctl("stop", _log):
            _log("[green]Service stopped[/green]")
            return True
        return False

    def restart(self, log: Optional[Callable[[str], None]] = None) -> bool:
        _log = log or (lambda m: None)
        if self.container_mode():
            _log("[green]Restart the container to apply settings.[/green]")
            return True
        if self._sudo_systemctl("restart", _log):
            _log("[green]Service restarted[/green]")
            return True
        return False

    def _container_toggle(self, enabled: bool, log: Callable[[str], None]) -> bool:
        try:
            s = self._load_settings()
            s.web_enabled = enabled
            self._save_settings(s)
        except Exception as e:
            log(f"[red]Failed to persist settings: {e}[/red]")
            return False
        state = "enabled" if enabled else "disabled"
        log(f"[green]{state.capitalize()}. Restart the container to apply.[/green]")
        return True

    # --------------------------------------------------------- helpers

    def _sudo_systemctl(self, action: str, log: Callable) -> bool:
        """Run sudo systemctl <action> [<service>] (passwordless via sudoers)."""
        if action == "daemon-reload":
            cmd = ["sudo", "systemctl", "daemon-reload"]
        else:
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
        from core.privilege import run_privileged
        return run_privileged(cmd, password, log)

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
