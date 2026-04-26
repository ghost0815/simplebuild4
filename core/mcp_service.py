"""systemd system-level service management for the s4 MCP server.

Structurally a leaner sibling of ``web_service.py``:

  * No SSL here — MCP always binds to 127.0.0.1 and TLS is handled by
    the reverse proxy (Apache/Caddy). That removes the whole
    cert-generation path.
  * Bearer token instead of Basic-Auth user/password — a single
    ``Authorization: Bearer <token>`` header.
  * A convenience helper ``generate_token()`` to produce a long,
    URL-safe random token.

The service runs ``./s4 mcp --http ...`` under the user account that
also owns the install — no root daemon, no capability elevation.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional


_UNIT_DIR = Path("/etc/systemd/system")

# Ports the MCP server should never bind — same reasoning as web_service.py.
_BLOCKED_PORTS = frozenset({
    3306, 5432, 6379, 27017,      # databases
    8088, 8880, 9090,             # common admin interfaces
    8888,                         # oscam WebIF
    8000, 8080,                   # typical s4-web ports
})


def generate_token(byte_length: int = 32) -> str:
    """Return a fresh URL-safe bearer token.

    32 bytes → ~43 base64 characters. More than enough entropy for a
    bearer secret and still fits on one line in systemd EnvironmentFile.
    """
    return secrets.token_urlsafe(byte_length)


def validate_port(port: int) -> Optional[str]:
    """Return an error message for a bad port, or None if OK."""
    if port < 1024:
        return f"Port {port} is reserved (< 1024 needs root)"
    if port > 65535:
        return f"Port {port} out of range (max 65535)"
    if port in _BLOCKED_PORTS:
        return f"Port {port} conflicts with a known service"
    return None


# The service body. We keep it tiny by delegating to the existing
# ``./s4 mcp --transport <sse|http>`` entry point. ``--token`` and the
# SSL file paths are read from the env variable so they don't show up
# in ``ps`` output.
_UNIT_TEMPLATE = """\
[Unit]
Description=SimpleBuild4 MCP Server ({root})
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={root}
ExecStart={s4} mcp --transport {transport} --host {host} --port {port}{action_flags}{ssl_flags}{root_path_flag}
Environment=PATH={path}
EnvironmentFile={env_file}
Restart=on-failure
RestartSec=3
StandardOutput={output_mode}:{log_path}
StandardError={output_mode}:{log_path}

[Install]
WantedBy=multi-user.target
"""


def _normalize_transport(transport: str) -> str:
    """Accept only 'sse' / 'http'; fall back to 'sse' (backward-compat)."""
    return transport if transport in ("sse", "http") else "sse"


def _systemd_output_mode() -> str:
    """Return 'append' on systemd >= 240, else 'file' for older versions."""
    try:
        out = subprocess.check_output(
            ["systemctl", "--version"], text=True, timeout=5,
        ).splitlines()[0]
        import re
        m = re.search(r"(\d+)", out)
        if m and int(m.group(1)) >= 240:
            return "append"
    except Exception:
        pass
    return "file"


def _ensure_mcp_packages(python: str, log: Callable[[str], None]) -> bool:
    """Ensure mcp + starlette + uvicorn are importable.

    Skipped silently if everything already imports. Otherwise delegates
    to :class:`core.dep_checker.DependencyChecker` so the install runs
    through the same safety net the F1 dependency modal uses: never as
    root, drops to $SUDO_USER when needed, retries with
    --break-system-packages on PEP 668. Note that ``starlette`` and
    ``uvicorn`` are NOT pulled in as transitive deps of ``mcp`` — the
    HTTP transport needs them explicitly.
    """
    needed: list[str] = []
    for mod in ("mcp", "starlette", "uvicorn"):
        try:
            __import__(mod)
        except ImportError:
            needed.append(mod)
    if not needed:
        return True
    log(f"  Installing: {', '.join(needed)}")
    # Late import to avoid pulling yaml at module-load time when this
    # file is imported by very-early bootstrap code.
    from pathlib import Path
    from core.dep_checker import DependencyChecker
    deps_yaml = Path(__file__).resolve().parent.parent / "defaults" / "dependencies.yaml"
    checker = DependencyChecker(deps_yaml, log=log)
    return checker.install_python_specs(needed)


class McpServiceManager:
    """Manage a system-level systemd service for the s4 MCP server.

    Close cousin of :class:`core.web_service.WebServiceManager` but
    smaller — MCP has no SSL / no auth user to juggle.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.service = self._resolve_service_name()
        self._unit_path = _UNIT_DIR / f"{self.service}.service"
        self._sudoers_path = Path(f"/etc/sudoers.d/{self.service}")
        # Env file holds the bearer token so it doesn't appear in `ps`.
        # Permissions are 0600 owned by the service user.
        self._env_file = self.root / "data" / "mcp" / f"{self.service}.env"

    # ------------------------------------------------------------------ naming

    def _resolve_service_name(self) -> str:
        """Find or assign a per-install service name (s4-mcp[-N])."""
        if not _UNIT_DIR.exists():
            return "s4-mcp"
        for unit in sorted(_UNIT_DIR.glob("s4-mcp*.service")):
            try:
                content = unit.read_text(encoding="utf-8")
                if f"WorkingDirectory={self.root}" in content:
                    return unit.stem
            except Exception:
                continue
        if not (_UNIT_DIR / "s4-mcp.service").exists():
            return "s4-mcp"
        n = 2
        while (_UNIT_DIR / f"s4-mcp-{n}.service").exists():
            n += 1
        return f"s4-mcp-{n}"

    # ------------------------------------------------------------------ queries

    @staticmethod
    def systemd_available() -> bool:
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
        if not self.systemd_available():
            return "mcp_svc_not_available"
        if not self.is_installed():
            return "mcp_svc_not_installed"
        if self.is_running():
            return "mcp_svc_installed_running"
        return "mcp_svc_installed_stopped"

    # ------------------------------------------------------------------ privileged

    @staticmethod
    def _run_privileged(
        cmds: str,
        password: str,
        log: Callable[[str], None],
    ) -> bool:
        """Execute a shell command string with root privileges.

        Tries passwordless sudo first, falls back to ``sudo -S`` with
        a user-supplied password. Same behaviour the web-service
        manager uses so the UX is identical.
        """
        sudo = shutil.which("sudo") or "/usr/bin/sudo"
        # Try passwordless first
        try:
            r = subprocess.run(
                [sudo, "-n", "sh", "-c", cmds],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                if r.stderr.strip():
                    log(r.stderr.strip())
                return True
            # fall through to password-based sudo
        except Exception as e:
            log(f"[yellow]sudo -n failed: {e}[/yellow]")
        if not password:
            log("[red]sudo requires a password, none was provided[/red]")
            return False
        try:
            r = subprocess.run(
                [sudo, "-S", "sh", "-c", cmds],
                input=password + "\n",
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                log(f"[red]sudo failed: {r.stderr.strip() or r.stdout.strip()}[/red]")
                return False
            if r.stderr.strip():
                log(r.stderr.strip())
            return True
        except Exception as e:
            log(f"[red]sudo error: {e}[/red]")
            return False

    # ------------------------------------------------------------------ systemctl via sudoers (no password)

    def _sudo_systemctl(self, action: str, log: Callable[[str], None]) -> bool:
        """Run ``sudo systemctl <action> [<service>]`` (passwordless via sudoers).

        Shape mirrors WebServiceManager._sudo_systemctl so Web and MCP
        service actions produce identical log output in the TUI.
        ``daemon-reload`` takes no service argument.
        """
        if action == "daemon-reload":
            cmd = ["sudo", "systemctl", "daemon-reload"]
        else:
            cmd = ["sudo", "systemctl", action, self.service]
        log(f"[dim]$ {' '.join(cmd)}[/dim]")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
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

    # ------------------------------------------------------------------ env file

    def _write_env_file(self, token: str) -> None:
        """Write the MCP env file (0600). Creates the parent dir if needed.

        Only the bearer token lives here so it doesn't show up in
        ``ps`` output. SSL file paths go directly into the unit.
        """
        self._env_file.parent.mkdir(parents=True, exist_ok=True)
        content = f"S4_MCP_TOKEN={token}\n"
        self._env_file.write_text(content, encoding="utf-8")
        try:
            os.chmod(self._env_file, 0o600)
        except Exception:
            pass

    # ------------------------------------------------------------------ actions

    def install(
        self,
        host: str = "0.0.0.0",
        port: int = 8181,
        dns_name: str = "",
        token: str = "",
        allow_actions: bool = False,
        ssl: bool = False,
        ssl_cert: str = "",
        ssl_key: str = "",
        transport: str = "sse",
        root_path: str = "",
        logs_dir: Optional[Path] = None,
        organize_logs: bool = False,
        password: str = "",
        log: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Install the systemd unit + sudoers entry."""
        _log = log or (lambda m: None)
        python = sys.executable
        s4 = str(self.root / "s4")
        user = os.environ.get("USER", os.environ.get("LOGNAME", "root"))
        path = ":".join(
            p for p in os.environ.get("PATH", "").split(":")
            if p and not p.startswith("/mnt/")
        ) or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

        if not token:
            token = generate_token()
            _log(f"  Generated fresh bearer token ({len(token)} chars)")

        # Auto-generate a self-signed cert if SSL is on and no pair is set.
        if ssl and (not ssl_cert or not ssl_key):
            from core.web_service import generate_self_signed_cert
            mcp_dir = self.root / "data" / "mcp"
            ssl_cert, ssl_key = generate_self_signed_cert(
                mcp_dir, dns_name, host,
            )
            _log(f"  Generated self-signed certificate: {ssl_cert}")

        self._write_env_file(token)

        if logs_dir:
            from core.log_cleanup import resolve_log_dir
            _log_dir = resolve_log_dir(logs_dir, "mcpservice", organize_logs)
        else:
            _log_dir = self.root / "data" / "logs"
            _log_dir.mkdir(parents=True, exist_ok=True)
        log_file = _log_dir / f"{self.service}.log"

        action_flags = " --allow-actions" if allow_actions else ""
        # SSL file paths go directly into the unit — not secrets, and
        # baking them in avoids systemd env-var expansion quirks.
        ssl_flags = f" --ssl-cert {ssl_cert} --ssl-key {ssl_key}" if ssl else ""
        root_path_flag = f" --root-path {root_path}" if root_path else ""

        unit = _UNIT_TEMPLATE.format(
            root=self.root, s4=s4,
            host=host, port=port,
            transport=_normalize_transport(transport),
            action_flags=action_flags,
            ssl_flags=ssl_flags,
            root_path_flag=root_path_flag,
            path=path, log_path=log_file, user=user,
            env_file=self._env_file,
            output_mode=_systemd_output_mode(),
        )

        systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
        tee = shutil.which("tee") or "/usr/bin/tee"
        sudoers = (
            f"# SimpleBuild4 MCP service \u2014 auto-generated\n"
            f"{user} ALL=(ALL) NOPASSWD: "
            f"{systemctl} start {self.service}, "
            f"{systemctl} stop {self.service}, "
            f"{systemctl} restart {self.service}, "
            f"{systemctl} status {self.service}, "
            f"{systemctl} daemon-reload, "
            f"{tee} {self._unit_path}\n"
        )

        import tempfile
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".service", delete=False) as tf_unit:
                tf_unit.write(unit)
                tmp_unit = tf_unit.name
            with tempfile.NamedTemporaryFile(mode="w", suffix=".sudoers", delete=False) as tf_sudo:
                tf_sudo.write(sudoers)
                tmp_sudoers = tf_sudo.name
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
            Path(tmp_unit).unlink(missing_ok=True)
            Path(tmp_sudoers).unlink(missing_ok=True)
        except Exception as e:
            _log(f"[red]Failed to prepare service files: {e}[/red]")
            return False

        _log("Checking Python dependencies …")
        _ensure_mcp_packages(sys.executable, _log)

        _log("[green]MCP service installed[/green]")
        return True

    def update_unit(
        self,
        host: str = "0.0.0.0",
        port: int = 8181,
        dns_name: str = "",
        token: str = "",
        allow_actions: bool = False,
        ssl: bool = False,
        ssl_cert: str = "",
        ssl_key: str = "",
        transport: str = "sse",
        root_path: str = "",
        logs_dir: Optional[Path] = None,
        organize_logs: bool = False,
        password: str = "",
        log: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Rewrite the unit + env-file with current settings (preserves token)."""
        if not self.is_installed():
            return False
        _log = log or (lambda m: None)
        python = sys.executable
        s4 = str(self.root / "s4")
        user = os.environ.get("USER", os.environ.get("LOGNAME", "root"))
        path = ":".join(
            p for p in os.environ.get("PATH", "").split(":")
            if p and not p.startswith("/mnt/")
        ) or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

        if token:
            self._write_env_file(token)

        # Auto-generate cert if SSL is on and paths missing
        if ssl and (not ssl_cert or not ssl_key):
            from core.web_service import generate_self_signed_cert
            mcp_dir = self.root / "data" / "mcp"
            ssl_cert, ssl_key = generate_self_signed_cert(
                mcp_dir, dns_name, host,
            )
            _log(f"  Generated self-signed certificate: {ssl_cert}")

        if logs_dir:
            from core.log_cleanup import resolve_log_dir
            _log_dir = resolve_log_dir(logs_dir, "mcpservice", organize_logs)
        else:
            _log_dir = self.root / "data" / "logs"
            _log_dir.mkdir(parents=True, exist_ok=True)
        log_file = _log_dir / f"{self.service}.log"
        action_flags = " --allow-actions" if allow_actions else ""
        ssl_flags = f" --ssl-cert {ssl_cert} --ssl-key {ssl_key}" if ssl else ""
        root_path_flag = f" --root-path {root_path}" if root_path else ""

        unit = _UNIT_TEMPLATE.format(
            root=self.root, s4=s4,
            host=host, port=port,
            transport=_normalize_transport(transport),
            action_flags=action_flags,
            ssl_flags=ssl_flags,
            root_path_flag=root_path_flag,
            path=path, log_path=log_file, user=user,
            env_file=self._env_file,
            output_mode=_systemd_output_mode(),
        )

        # Write via ``sudo tee`` — that command is whitelisted in our
        # sudoers entry and needs no password. ``cp`` / ``chmod`` /
        # ``sh -c`` are NOT in the sudoers allow-list so ``_run_privileged``
        # would prompt for a password here and fail with -n.
        tee = shutil.which("tee") or "/usr/bin/tee"
        try:
            r = subprocess.run(
                ["sudo", "-n", tee, str(self._unit_path)],
                input=unit, capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                _log(f"[red]Failed to write unit: {r.stderr.strip()}[/red]")
                return False
            # daemon-reload picks up the new content (also whitelisted)
            self._sudo_systemctl("daemon-reload", _log)
            return True
        except Exception as e:
            _log(f"[red]update_unit failed: {e}[/red]")
            return False

    def uninstall(
        self,
        password: str = "",
        log: Optional[Callable[[str], None]] = None,
    ) -> bool:
        _log = log or (lambda m: None)
        cmds = (
            f"systemctl stop {self.service} ; "
            f"systemctl disable {self.service} ; "
            f"rm -f {self._unit_path} {self._sudoers_path} && "
            f"systemctl daemon-reload"
        )
        if not self._run_privileged(cmds, password, _log):
            return False
        # Remove env file (non-privileged — owned by our user)
        try:
            self._env_file.unlink(missing_ok=True)
        except Exception:
            pass
        _log("[green]MCP service uninstalled[/green]")
        return True

    def start(self, log: Optional[Callable[[str], None]] = None) -> bool:
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
