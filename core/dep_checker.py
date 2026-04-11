"""System dependency checker and installer for s4.

Detects the Linux distribution, checks for required build tools and
libraries, and installs missing packages via the system package manager.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import yaml


class DependencyChecker:
    """Check and install system dependencies from dependencies.yaml."""

    # Map distro ID (from /etc/os-release) to package manager key
    _DISTRO_MAP = {
        "debian": "apt", "ubuntu": "apt", "raspbian": "apt",
        "linuxmint": "apt", "pop": "apt", "elementary": "apt",
        "fedora": "dnf", "rhel": "dnf", "centos": "dnf",
        "rocky": "dnf", "alma": "dnf", "oracle": "dnf",
        "arch": "pacman", "manjaro": "pacman", "endeavouros": "pacman",
        "opensuse-leap": "zypper", "opensuse-tumbleweed": "zypper", "sles": "zypper",
    }

    # Package manager install commands (non-interactive)
    _INSTALL_CMD = {
        "apt": "apt-get -y install",
        "dnf": "dnf -y install",
        "yum": "yum -y install",
        "pacman": "pacman -S --noconfirm",
        "zypper": "zypper --non-interactive install",
    }

    # Package manager update commands
    _UPDATE_CMD = {
        "apt": "apt-get -y update",
        "dnf": "dnf -y check-update || true",
        "yum": "yum -y check-update || true",
        "pacman": "pacman -Sy",
        "zypper": "zypper --non-interactive refresh",
    }

    def __init__(self, deps_yaml: Path, log: Optional[Callable[[str], None]] = None):
        self._log = log or (lambda msg: None)
        self._deps = {}
        self._pkg_mgr = ""
        self._distro_id = ""
        self._distro_name = ""
        try:
            self._deps = yaml.safe_load(deps_yaml.read_text(encoding="utf-8")) or {}
        except Exception as e:
            self._log(f"[red]Failed to load {deps_yaml}: {e}[/red]")

    def detect_distro(self) -> str:
        """Detect Linux distribution and return package manager key."""
        try:
            release = Path("/etc/os-release")
            if release.exists():
                for line in release.read_text().splitlines():
                    if line.startswith("ID="):
                        self._distro_id = line.split("=", 1)[1].strip('"').lower()
                    elif line.startswith("PRETTY_NAME="):
                        self._distro_name = line.split("=", 1)[1].strip('"')
        except Exception:
            pass

        # Try direct mapping
        self._pkg_mgr = self._DISTRO_MAP.get(self._distro_id, "")

        # Fallback: detect by which package manager is installed
        if not self._pkg_mgr:
            for mgr in ("apt-get", "dnf", "yum", "pacman", "zypper"):
                if shutil.which(mgr):
                    self._pkg_mgr = mgr.replace("apt-get", "apt").replace("yum", "dnf")
                    break

        return self._pkg_mgr

    def check_all(self) -> dict:
        """Check all dependencies. Returns dict with results."""
        if not self._pkg_mgr:
            self.detect_distro()

        missing_bins = []
        missing_headers = []
        installed_bins = []
        installed_headers = []

        # Check binaries
        for dep in self._deps.get("binaries", []):
            name = dep.get("name", "")
            if not name:
                continue
            check = dep.get("check", "")
            if check.startswith("file "):
                found = Path(check[5:].strip()).exists()
            else:
                found = shutil.which(name) is not None
            if found:
                installed_bins.append(name)
            else:
                pkg = self._resolve_package(dep)
                missing_bins.append({"name": name, "package": pkg})

        # Check headers
        for dep in self._deps.get("headers", []):
            name = dep.get("name", "")
            if not name:
                continue
            found = self._find_header(name)
            if found:
                installed_headers.append(name)
            else:
                pkg = self._resolve_package(dep)
                missing_headers.append({"name": name, "package": pkg})

        # Check meta package need
        meta_needed = len(missing_bins) > 0 or not shutil.which("gcc")

        return {
            "distro_id": self._distro_id,
            "distro_name": self._distro_name,
            "pkg_mgr": self._pkg_mgr,
            "missing_bins": missing_bins,
            "missing_headers": missing_headers,
            "installed_bins": installed_bins,
            "installed_headers": installed_headers,
            "meta_needed": meta_needed,
        }

    def install(self, password: str = "") -> bool:
        """Install all missing packages. Returns True on success.

        If password is provided, uses su -c. Otherwise tries sudo.
        """
        if not self._pkg_mgr:
            self.detect_distro()

        result = self.check_all()
        missing = result["missing_bins"] + result["missing_headers"]

        if not missing and not result["meta_needed"]:
            self._log("[green]All dependencies are installed[/green]")
            return True

        install_cmd_base = self._INSTALL_CMD.get(self._pkg_mgr, "")
        update_cmd = self._UPDATE_CMD.get(self._pkg_mgr, "")
        if not install_cmd_base:
            self._log(f"[red]Unknown package manager: {self._pkg_mgr}[/red]")
            return False

        # Collect unique package names
        packages = set()
        meta = self._deps.get("meta", {}).get(self._pkg_mgr, "")

        if result["meta_needed"] and meta:
            # Meta packages may have special syntax (e.g. @group, -t pattern)
            if meta.startswith("@") or meta.startswith("-t "):
                # Group install needs separate command
                pass
            else:
                packages.add(meta)

        for dep in missing:
            pkg = dep.get("package", "")
            if pkg:
                packages.add(pkg)

        if not packages and not meta:
            self._log("[green]Nothing to install[/green]")
            return True

        # Build the command sequence
        cmds = []
        if update_cmd:
            cmds.append(update_cmd)
        # Meta package with special syntax (group install)
        if result["meta_needed"] and meta and (meta.startswith("@") or meta.startswith("-t ")):
            meta_install = install_cmd_base.replace("install", "group install") if meta.startswith("@") else install_cmd_base
            cmds.append(f"{meta_install} {meta}")
            packages.discard(meta)
        if packages:
            cmds.append(f"{install_cmd_base} {' '.join(sorted(packages))}")

        full_cmd = " && ".join(cmds)

        # Determine privilege escalation
        if password:
            escaped = full_cmd.replace("'", "'\\''")
            actual_cmd = f"echo '{password}' | su -c '{escaped}'"
            display_cmd = f"echo '***' | su -c '{escaped}'"
        elif shutil.which("sudo"):
            actual_cmd = f"sudo sh -c '{full_cmd}'"
            display_cmd = actual_cmd
        else:
            self._log("[red]No sudo available and no password provided[/red]")
            return False

        self._log(f"[bold]Installing packages...[/bold]")
        self._log(f"[dim]$ {display_cmd}[/dim]")

        try:
            proc = subprocess.Popen(
                actual_cmd, shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in proc.stdout:
                stripped = line.rstrip()
                if stripped:
                    # Mask password in output
                    if password:
                        stripped = stripped.replace(password, "***")
                    self._log(f"  {stripped}")
            proc.wait()
            if proc.returncode == 0:
                self._log("[green]Installation complete[/green]")
                return True
            else:
                self._log(f"[red]Installation failed (exit {proc.returncode})[/red]")
                return False
        except Exception as e:
            msg = str(e)
            if password:
                msg = msg.replace(password, "***")
            self._log(f"[red]Error: {msg}[/red]")
            return False

    def _resolve_package(self, dep: dict) -> str:
        """Resolve the package name for the current distro."""
        pkg_map = dep.get("package", {})
        if isinstance(pkg_map, dict):
            return pkg_map.get(self._pkg_mgr, dep.get("name", ""))
        return dep.get("name", "")

    def _find_header(self, name: str) -> bool:
        """Check if a header file exists in standard include paths."""
        for base in ("/usr/include", "/usr/local/include"):
            if (Path(base) / name).exists():
                return True
        return False
