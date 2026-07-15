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
        ver = entry.get("min_version")
        reqs.append(f"{pkg}>={ver}" if ver else pkg)

    print(" ".join(reqs))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: deps-to-pip.py <dependencies.yaml>\n")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
