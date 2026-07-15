#!/usr/bin/env python3
"""Extract apt package names from defaults/dependencies.yaml.

Prints a space-separated list suitable for:

    apt-get install -y --no-install-recommends $(deps-to-apt.py <file>)

Sections covered:
  - ``meta.apt`` (build-essential and friends)
  - ``binaries[]`` (``package.apt`` if set, else ``name`` = the binary itself
    is the Debian package name — true for cmake, meson, git, wget, curl, upx,
    flex, bison, zip, unzip, automake, ...)
  - ``headers[]`` (``package.apt`` only — ``name`` is a header file path)
"""

from __future__ import annotations

import sys
import yaml


def main(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    pkgs: set[str] = set()

    meta_apt = (data.get("meta") or {}).get("apt")
    if meta_apt:
        for p in meta_apt.split():
            pkgs.add(p)

    for entry in data.get("binaries") or []:
        apt_pkg = ((entry.get("package") or {}).get("apt"))
        if apt_pkg:
            pkgs.add(apt_pkg)
        elif "name" in entry:
            pkgs.add(entry["name"])

    for entry in data.get("headers") or []:
        apt_pkg = ((entry.get("package") or {}).get("apt"))
        if apt_pkg:
            pkgs.add(apt_pkg)

    print(" ".join(sorted(pkgs)))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: deps-to-apt.py <dependencies.yaml>\n")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
