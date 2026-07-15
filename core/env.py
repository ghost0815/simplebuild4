"""Small runtime-environment helpers (container detection, port probing)."""

from __future__ import annotations

import os
import socket


def probe_port(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True if a TCP listener is answering on ``host:port``.

    Wildcards get rewritten to loopback so a service bound to 0.0.0.0
    inside the container is still detectable from a probe on 127.0.0.1.
    """
    if host in ("0.0.0.0", "::", "::/0", ""):
        host = "127.0.0.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False
    finally:
        sock.close()


def is_running_in_container() -> bool:
    """True when the current process runs inside a container.

    Covers Docker, Podman, containerd, and Kubernetes pods. The Docker
    image sets S4_IN_CONTAINER=1 explicitly so detection is deterministic
    even on kernels where /proc/1/cgroup has been sanitised.
    """
    if os.environ.get("S4_IN_CONTAINER") == "1":
        return True
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as f:
            data = f.read()
    except OSError:
        return False
    return any(k in data for k in ("docker", "containerd", "kubepods"))
