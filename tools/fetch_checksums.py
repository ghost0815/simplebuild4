#!/usr/bin/env python3
"""Fetch SHA256 checksums for all toolchain download URLs.

Reads defaults/toolchains.yaml, fetches sha256 for each URL, updates the file.

Bootlin URLs: replace .tar.bz2 with .sha256
simplebuild.dedyn.io URLs: append .sha256 to URL
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

PVERSION = "0.27.0"


def get_sha256_url(url: str) -> str:
    """Derive the SHA256 URL from a download URL."""
    # Resolve ${pversion}
    url_resolved = url.replace("${pversion}", PVERSION)

    if "toolchains.bootlin.com" in url_resolved:
        # Replace .tar.bz2 or .tar.xz with .sha256
        sha_url = re.sub(r'\.tar\.(bz2|xz|gz)$', '.sha256', url_resolved)
        return sha_url
    elif "simplebuild.dedyn.io" in url_resolved:
        return url_resolved + ".sha256"
    return ""


def fetch_sha256(sha_url: str) -> str:
    """Fetch SHA256 hash from URL. Returns hash or empty string."""
    try:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", "10", sha_url],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Format: "hash  filename" or just "hash"
            line = result.stdout.strip().splitlines()[0]
            parts = line.split()
            if parts and len(parts[0]) == 64:  # SHA256 is 64 hex chars
                return parts[0]
    except Exception:
        pass
    return ""


def main():
    yaml_path = Path("defaults/toolchains.yaml")
    if not yaml_path.exists():
        print(f"Error: {yaml_path} not found")
        sys.exit(1)

    # Read raw text to preserve comments and formatting
    raw = yaml_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)

    updated = 0
    failed = 0
    skipped = 0

    for tc in data["toolchains"]:
        url = tc.get("url", "")
        if not url:
            continue

        name = tc["name"]
        existing = tc.get("checksum", "")

        # Skip if already has sha256
        if existing.startswith("sha256:"):
            skipped += 1
            continue

        sha_url = get_sha256_url(url)
        if not sha_url:
            print(f"  {name}: no sha256 URL pattern known")
            failed += 1
            continue

        print(f"  {name}: fetching...", end=" ", flush=True)
        sha = fetch_sha256(sha_url)

        if sha:
            tc["checksum"] = f"sha256:{sha}"
            print(f"sha256:{sha[:16]}...")
            updated += 1
        else:
            print("FAILED")
            failed += 1

    # Write back
    # Remove empty values and write clean YAML
    cleaned = []
    for tc in data["toolchains"]:
        c = {k: v for k, v in tc.items() if v != "" and v != 0}
        if "name" not in c:
            c["name"] = tc["name"]
        if tc.get("strip_components", 1) == 1 and "strip_components" in c:
            del c["strip_components"]
        cleaned.append(c)

    data["toolchains"] = cleaned
    new_raw = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    new_raw = new_raw.replace("\n- name:", "\n\n- name:")

    # Preserve the template header from original file
    header_end = raw.find("\ntoolchains:")
    if header_end > 0:
        header = raw[:header_end + 1]
    else:
        header = ""

    yaml_path.write_text(header + new_raw, encoding="utf-8")

    print(f"\nDone: {updated} updated, {skipped} already had sha256, {failed} failed")


if __name__ == "__main__":
    main()
