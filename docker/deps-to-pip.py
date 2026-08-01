#!/usr/bin/env python3
"""Extract pip requirements from defaults/dependencies.yaml.

Prints a space-separated list suitable for:

    pip3 install --no-cache-dir --break-system-packages $(deps-to-pip.py <file>)

Rules:
  - ``pip`` itself is skipped (bootstrap circularity).
  - ``optional: true`` entries ARE included — the container wants everything
    pre-installed so first-run has no network dependency.
  - ``pip_name`` overrides ``name`` for install (name is the import name).
  - ``min_version`` becomes ``>=`` in the requirement.
  - ``max_version`` becomes ``<`` in the requirement — use it as an upper
    bound to guard against breaking major bumps (e.g. mcp 2.0 broke the
    ``@server.list_tools()`` API).
"""

from __future__ import annotations

import sys
import yaml


def main(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    reqs: list[str] = []
    for entry in data.get("python") or []:
        name = entry.get("name")
        if not name or name == "pip":
            continue
        pkg = entry.get("pip_name", name)
        lo = entry.get("min_version")
        hi = entry.get("max_version")
        parts: list[str] = []
        if lo:
            parts.append(f">={lo}")
        if hi:
            parts.append(f"<{hi}")
        reqs.append(f"{pkg}{','.join(parts)}" if parts else pkg)

    print(" ".join(reqs))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: deps-to-pip.py <dependencies.yaml>\n")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
