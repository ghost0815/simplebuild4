"""Sysroot introspection helpers shared by builder + toolchain UI."""

from __future__ import annotations

from pathlib import Path


def read_linux_version(sysroot: Path) -> str | None:
    """Return the kernel version as ``"X.Y.Z"`` from a toolchain
    sysroot's ``linux/version.h``, or ``None`` if the header is
    missing or its ``LINUX_VERSION_CODE`` cannot be parsed.

    Used in two places:

    * QEMU smoke / unit tests — qemu-user reports a kernel version
      baked into the QEMU binary via ``uname()``, which can be older
      than the value the sysroot's glibc was built against
      (``--enable-kernel=``). Setting ``QEMU_UNAME`` to this value
      avoids the spurious ``FATAL: kernel too old`` abort.
    * Toolchain details panel — displays the sysroot's kernel header
      version.
    """
    for candidate in [
        sysroot / "usr" / "include" / "linux" / "version.h",
        sysroot / "include" / "linux" / "version.h",
    ]:
        if not candidate.exists():
            continue
        try:
            content = candidate.read_text(errors="replace")
        except Exception:
            continue
        for line in content.splitlines():
            if "LINUX_VERSION_CODE" in line and "#define" in line:
                try:
                    code = int(line.split()[-1])
                    return f"{code >> 16}.{(code >> 8) & 0xFF}.{code & 0xFF}"
                except (ValueError, IndexError):
                    return None
    return None
