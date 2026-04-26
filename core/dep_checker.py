"""System dependency checker and installer for s4.

Detects the Linux distribution, checks for required build tools and
libraries, and installs missing packages via the system package manager.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import yaml


def _parse_version(s: str) -> tuple[int, ...]:
    """Lenient ``X.Y[.Z][.…]`` parser. Non-numeric tails are dropped, so
    ``8.2.3.dev0`` is read as ``(8, 2, 3)``. Empty / unparseable yields
    ``(0,)`` — comparing against any real version then sorts as oldest.
    """
    out: list[int] = []
    for part in (s or "").split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        out.append(int(digits))
    return tuple(out) if out else (0,)


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

        # Check Python modules
        py_result = self.check_python()

        return {
            "distro_id": self._distro_id,
            "distro_name": self._distro_name,
            "pkg_mgr": self._pkg_mgr,
            "missing_bins": missing_bins,
            "missing_headers": missing_headers,
            "installed_bins": installed_bins,
            "installed_headers": installed_headers,
            "meta_needed": meta_needed,
            "missing_python": py_result["missing"],
            "outdated_python": py_result["outdated"],
            "installed_python": py_result["installed"],
        }

    def check_python(self) -> dict:
        """Check Python modules listed under ``python:`` in dependencies.yaml.

        Returns ``{"installed": [...], "missing": [...], "outdated": [...]}``.

        For each entry, ``import <module>`` is attempted and the resolved
        ``__version__`` (or distribution metadata) is checked against the
        optional ``min_version`` / ``max_version`` constraints.
        ``optional: true`` entries are tracked but never installed unless
        the user opts in — they show up in ``missing`` for visibility.
        """
        installed: list[dict] = []
        missing: list[dict] = []
        outdated: list[dict] = []
        for dep in self._deps.get("python", []) or []:
            mod_name = dep.get("name", "")
            if not mod_name:
                continue
            pip_name = dep.get("pip_name") or mod_name
            min_v = dep.get("min_version") or ""
            max_v = dep.get("max_version") or ""
            optional = bool(dep.get("optional", False))
            entry = {
                "name": mod_name,
                "pip_name": pip_name,
                "min_version": min_v,
                "max_version": max_v,
                "optional": optional,
                "installed_version": "",
            }
            # Prefer dist-info metadata over the cached module's
            # ``__version__`` attribute. Once a module is imported into
            # the running process, ``mod.__version__`` is frozen at the
            # value Python saw at first import — pip upgrades on disk
            # don't refresh it. importlib.metadata.version() reads the
            # ``.dist-info/METADATA`` file off disk every call, so it
            # reflects the current pip state. Falls back to the module
            # attribute only when no dist-info is available (rare:
            # vendored / locally-checked-out packages).
            version = ""
            try:
                version = importlib.metadata.version(pip_name)
            except Exception:
                version = ""
            try:
                mod = importlib.import_module(mod_name)
            except Exception:
                if not version:
                    missing.append(entry)
                    continue
                # Importable through metadata but failed to import — odd,
                # treat as installed with the metadata version.
                mod = None
            if not version and mod is not None:
                version = getattr(mod, "__version__", "") or ""
            entry["installed_version"] = version
            v = _parse_version(version) if version else (0,)
            if min_v and v < _parse_version(min_v):
                entry["reason"] = f"too old (have {version}, need >= {min_v})"
                outdated.append(entry)
                continue
            if max_v and v >= _parse_version(max_v):
                entry["reason"] = f"too new (have {version}, supported < {max_v})"
                outdated.append(entry)
                continue
            installed.append(entry)
        return {"installed": installed, "missing": missing, "outdated": outdated}

    def check_python_updates(self) -> dict[str, str]:
        """Query pip for newer versions of any installed Python module.

        Returns ``{pip_name: latest_version}`` for every package that
        has a newer release on PyPI. Calls ``pip list --outdated`` once
        (single subprocess) so the network round-trips are batched.

        Empty dict on any failure — this is best-effort UI sugar, not
        critical path. Times out after 20 s to keep the modal snappy.
        """
        try:
            res = subprocess.run(
                [sys.executable, "-m", "pip", "list", "--outdated",
                 "--format=json", "--disable-pip-version-check"],
                capture_output=True, text=True, timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return {}
        if res.returncode != 0:
            return {}
        try:
            import json as _json
            data = _json.loads(res.stdout or "[]")
        except Exception:
            return {}
        out: dict[str, str] = {}
        for entry in data:
            name = (entry.get("name") or "").lower()
            latest = entry.get("latest_version") or ""
            if name and latest:
                out[name] = latest
        return out

    def install_python(self, include_updates: bool = True) -> bool:
        """Install missing / upgrade outdated Python modules via pip.

        Always runs ``pip install --user --upgrade`` — never with sudo,
        never inside a privileged context. If s4 was launched as root
        (``sudo s4``), drops privileges to ``$SUDO_USER`` so packages
        end up in the originating user's site-packages, not /root.

        Optional entries are skipped (user-only-on-demand).

        ``include_updates`` (default True): also upgrade modules that
        satisfy min/max but have a newer version on PyPI. Set False to
        only fix missing / out-of-bounds entries.
        """
        py = self.check_python()
        targets: list[dict] = []
        for e in py["missing"] + py["outdated"]:
            if e.get("optional"):
                continue
            targets.append(e)

        # Include "installed-but-newer-on-PyPI" modules so a user-driven
        # Install press also picks up routine version bumps.
        # Note: ``optional`` means "don't auto-install if MISSING" — but
        # once present, we keep it current so optional modules don't
        # silently rot at their first-install version.
        if include_updates:
            updates = self.check_python_updates()
            already = {e["pip_name"] or e["name"] for e in targets}
            for e in py["installed"]:
                pip_name = e["pip_name"] or e["name"]
                if pip_name in already:
                    continue
                latest = updates.get(pip_name.lower(), "")
                if latest and latest != e.get("installed_version"):
                    targets.append(e)

        if not targets:
            self._log("[green]All Python modules are up to date[/green]")
            return True

        # Build pip specs honouring min/max constraints
        specs: list[str] = []
        for e in targets:
            spec = e["pip_name"]
            if e.get("min_version"):
                spec += f">={e['min_version']}"
            if e.get("max_version"):
                spec += f",<{e['max_version']}"
            specs.append(spec)

        # Decide who runs pip — never root, even if s4 was started with sudo
        sudo_user = os.environ.get("SUDO_USER", "")
        as_user = ""
        try:
            is_root = os.geteuid() == 0  # type: ignore[attr-defined]
        except AttributeError:
            is_root = False
        if is_root and sudo_user and sudo_user != "root":
            as_user = sudo_user
            self._log(
                f"[yellow]Detected sudo context — dropping to user "
                f"'{as_user}' for pip install[/yellow]"
            )

        # Be loud about which Python is actually used — install ends up
        # in ``~/.local/lib/pythonX.Y/site-packages`` for THIS interpreter,
        # so if the user normally launches s4 with a different python
        # (pyenv, conda, system 3.10 vs sudo's 3.12) the install lands
        # in a parallel directory and the next ``import`` will still
        # fail. The path + version line lets them spot the mismatch.
        py_ver = ".".join(str(x) for x in sys.version_info[:3])
        self._log(
            f"[dim]Using Python {py_ver} at {sys.executable}[/dim]"
        )
        if is_root and not as_user:
            self._log(
                "[yellow]WARNING: running as root with no SUDO_USER — "
                "modules will install to /root/.local. If you normally "
                "launch s4 as a regular user, exit and re-run without "
                "sudo so the modules land in your home directory.[/yellow]"
            )

        env = dict(os.environ)
        env.pop("PIP_REQUIRE_VIRTUALENV", None)  # never relevant for --user

        def _run(extra_flags: list[str]) -> subprocess.CompletedProcess[str]:
            base = [sys.executable, "-m", "pip", "install",
                    "--user", "--upgrade", *extra_flags]
            if as_user:
                joined = " ".join(f"'{s}'" for s in base + specs)
                cmd = ["su", "-", as_user, "-c", joined]
            else:
                cmd = base + specs
            return subprocess.run(
                cmd, env=env,
                capture_output=True, text=True, timeout=600,
            )

        self._log(f"[bold]Installing Python modules:[/bold] {', '.join(specs)}")
        try:
            res = _run([])
            # PEP 668: Debian / Ubuntu 24.04+ ship Python with the
            # externally-managed marker, which blocks plain ``pip --user``.
            # Auto-retry with --break-system-packages — that's the
            # documented escape hatch for user-local installs that
            # don't touch the system site-packages.
            if (
                res.returncode != 0
                and "externally-managed-environment" in (res.stderr or "")
            ):
                self._log(
                    "[dim]Distro marks Python as externally-managed; "
                    "retrying with --break-system-packages (only affects "
                    "your --user dir, not the system).[/dim]"
                )
                res = _run(["--break-system-packages"])
        except FileNotFoundError as e:
            self._log(f"[red]pip invocation failed: {e}[/red]")
            return False
        except subprocess.TimeoutExpired:
            self._log("[red]pip install timed out (10 min)[/red]")
            return False

        # Stream output (pip is chatty — only show non-empty trailing
        # progress lines, not the megabytes of wheel-cache noise)
        for line in (res.stdout or "").splitlines()[-30:]:
            self._log(f"  {line}")
        for line in (res.stderr or "").splitlines()[-10:]:
            if line.strip():
                self._log(f"  [yellow]{line}[/yellow]")

        if res.returncode == 0:
            self._log("[green]Python modules installed[/green]")
            return True
        self._log(f"[red]pip exited with code {res.returncode}[/red]")
        return False

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

        self._log(f"[bold]Installing packages...[/bold]")

        from core.privilege import run_privileged
        ok = run_privileged(full_cmd, password, self._log)
        if ok:
            self._log("[green]Installation complete[/green]")
        else:
            self._log("[red]Installation failed[/red]")
        return ok

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
