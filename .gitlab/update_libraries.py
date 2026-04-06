#!/usr/bin/env python3
"""Auto-update library versions in defaults/libraries.yaml.

Checks each library's version_check source for newer versions and updates
the YAML file. Designed to run in GitLab CI or locally.

Usage:
    python3 .gitlab/update_libraries.py [--dry-run]
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

import yaml

LIBRARIES_YAML = Path(__file__).resolve().parent.parent / "defaults" / "libraries.yaml"
PRERELEASE_TAGS = ("alpha", "beta", "preview", "rc", "dev", "pre")
# OpenSSL is the only library where we add alpha/beta versions
PRERELEASE_ALLOWED = {"OpenSSL"}


def fetch_json(url: str) -> list | dict:
    """Fetch JSON from URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "s4-updater/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_html(url: str) -> str:
    """Fetch HTML from URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "s4-updater/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def is_prerelease(version: str) -> bool:
    """Check if version string contains pre-release indicators."""
    v_lower = version.lower()
    return any(tag in v_lower for tag in PRERELEASE_TAGS)


def parse_version(version: str) -> tuple:
    """Parse version string into comparable tuple."""
    # Strip leading 'v' or 'openssl-' etc
    v = re.sub(r'^[a-zA-Z_-]+', '', version)
    parts = []
    for p in re.split(r'[.\-]', v):
        try:
            parts.append((0, int(p)))
        except ValueError:
            parts.append((1, p))  # strings sort after numbers
    return tuple(parts)


def get_branch(version: str) -> str:
    """Extract branch from version by removing the last numeric/alpha segment.

    Examples:
        '3.6.1'  → '3.6'    (major.minor branch)
        '1.1.1w' → '1.1.1'  (OpenSSL 1.1.1 branch, 'w' is the patch)
        '1.1.0l' → '1.1.0'  (OpenSSL 1.1.0 branch, 'l' is the patch)
        '1.0.29' → '1.0'    (libusb branch)
        '8.19.0' → '8'      (libcurl major branch)
        '2.4.1'  → '2.4'    (pcsclite branch)
    """
    # Split into parts
    parts = version.split(".")
    if len(parts) <= 1:
        return version

    # Check if last part is purely numeric or has trailing alpha (like '1w', '19')
    last = parts[-1]

    # If last part has letters mixed with numbers (like "1w"), the branch includes
    # everything except the trailing letters: "1.1.1w" → branch "1.1.1"
    alpha_match = re.match(r'^(\d+)([a-zA-Z]+)$', last)
    if alpha_match:
        return ".".join(parts[:-1] + [alpha_match.group(1)])

    # Otherwise branch = everything except last part: "3.6.1" → "3.6"
    return ".".join(parts[:-1])


# ---------------------------------------------------------------------------
# Version fetchers
# ---------------------------------------------------------------------------

def fetch_github_versions(repo: str) -> list[dict]:
    """Fetch versions from GitHub releases API.

    Returns list of {version, prerelease, tag} dicts.
    """
    url = f"https://api.github.com/repos/{repo}/releases?per_page=100"
    try:
        releases = fetch_json(url)
    except Exception as e:
        print(f"  WARNING: GitHub API failed for {repo}: {e}")
        return []

    versions = []
    for rel in releases:
        tag = rel.get("tag_name", "")
        # Normalize tag to version
        version = tag
        # Strip common prefixes
        for prefix in ["openssl-", "OpenSSL_", "curl-", "v"]:
            if version.startswith(prefix):
                version = version[len(prefix):]
        # curl uses underscores: curl-8_19_0 → 8.19.0
        if repo == "curl/curl":
            version = version.replace("_", ".")
        # OpenSSL old style: 1_1_1w → 1.1.1w
        version = re.sub(r'^(\d+)_(\d+)_(\d+)', r'\1.\2.\3', version)

        if not version or not version[0].isdigit():
            continue

        versions.append({
            "version": version,
            "prerelease": rel.get("prerelease", False) or is_prerelease(version),
            "tag": tag,
        })

    return versions


def fetch_url_versions(url: str, lib_name: str) -> list[dict]:
    """Fetch versions by scraping a directory listing page.

    Used for pcsclite (pcsclite.apdu.fr) and ccid (ccid.apdu.fr).
    """
    try:
        html = fetch_html(url)
    except Exception as e:
        print(f"  WARNING: URL fetch failed for {url}: {e}")
        return []

    versions = []
    # Find links to tar files
    pattern = re.compile(r'href="[^"]*?(\d+\.\d+\.\d+)[^"]*?\.tar\.')
    seen = set()
    for match in pattern.finditer(html):
        version = match.group(1)
        if version not in seen:
            seen.add(version)
            versions.append({
                "version": version,
                "prerelease": is_prerelease(version),
                "tag": "",
            })

    return versions


def fetch_versions(version_check: str, lib_name: str) -> list[dict]:
    """Dispatch to appropriate version fetcher."""
    if version_check.startswith("github:"):
        repo = version_check[7:]
        return fetch_github_versions(repo)
    elif version_check.startswith("url:"):
        url = version_check[4:]
        return fetch_url_versions(url, lib_name)
    return []


# ---------------------------------------------------------------------------
# Update logic
# ---------------------------------------------------------------------------

def find_updates(lib: dict, all_versions: list[dict]) -> list[tuple[str, str]]:
    """Find version updates for a library's variants.

    Returns list of (old_version, new_version) tuples.
    Does NOT modify the lib dict — changes are applied via string replacement.
    """
    lib_name = lib["name"]
    variants = lib.get("variants", [])
    if not variants:
        return []

    allow_prerelease = lib_name in PRERELEASE_ALLOWED
    updates = []
    all_variant_versions = {v.get("version", "") for v in variants}

    for variant in variants:
        v = variant.get("version", "")
        if v in ("auto", "master", "custom"):
            continue
        # Skip fixed bitslice variants like 1.1.0-64, 1.1.0-32
        if re.match(r'^\d+\.\d+\.\d+-\d+$', v):
            continue

        # Determine branch from label or version
        label = variant.get("label", "")
        label_branch = re.match(r'^([\d.]+)\.x', label)
        branch = label_branch.group(1) if label_branch else get_branch(v)
        is_pre = is_prerelease(v)

        # Find best version in same branch
        candidates = []
        for av in all_versions:
            av_branch = get_branch(av["version"])
            if not (av_branch == branch or av_branch.startswith(branch + ".")):
                continue
            if av["prerelease"] and not allow_prerelease:
                continue
            if av["prerelease"] and not is_pre and not allow_prerelease:
                continue
            candidates.append(av)

        if not candidates:
            continue

        candidates.sort(key=lambda x: parse_version(x["version"]), reverse=True)
        newest = candidates[0]["version"]

        if newest != v and parse_version(newest) > parse_version(v) and newest not in all_variant_versions:
            updates.append((v, newest))

    return updates


def find_new_versions(lib: dict, all_versions: list[dict]) -> list[str]:
    """Find new pre-release versions to add (OpenSSL only).

    Returns list of new version strings.
    """
    lib_name = lib["name"]
    if lib_name not in PRERELEASE_ALLOWED:
        return []

    variants = lib.get("variants", [])
    existing_versions = {v.get("version", "") for v in variants}
    existing_branches = {get_branch(v.get("version", "")) for v in variants
                        if v.get("version") not in ("auto", "master", "custom")}
    new_versions = []

    for av in all_versions:
        if av["prerelease"]:
            branch = get_branch(av["version"])
            if branch not in existing_branches and av["version"] not in existing_versions:
                new_versions.append(av["version"])
                existing_branches.add(branch)

    return new_versions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def apply_updates(content: str, updates: list[tuple[str, str]], lib_name: str) -> tuple[str, list[str]]:
    """Apply version updates via string replacement in YAML content.

    Preserves comments and formatting.
    Returns (new_content, list_of_change_descriptions).
    """
    changes = []
    for old_ver, new_ver in updates:
        # Replace version: "old" → version: "new" (with quotes)
        old_pattern = f'version: "{old_ver}"'
        new_pattern = f'version: "{new_ver}"'
        if old_pattern in content:
            content = content.replace(old_pattern, new_pattern, 1)
        else:
            # Try without quotes
            old_pattern = f"version: {old_ver}"
            new_pattern = f"version: {new_ver}"
            if old_pattern in content:
                content = content.replace(old_pattern, new_pattern, 1)

        # Also update label if it contains the old version
        old_label = f"{old_ver}"
        new_label = f"{new_ver}"
        # Only replace in labels, not in URLs etc — look for label: "...old_ver..."
        content = re.sub(
            rf'(label:\s*["\']?)([^"\'\n]*){re.escape(old_ver)}([^"\'\n]*["\']?)',
            rf'\g<1>\g<2>{new_ver}\g<3>',
            content, count=1,
        )
        changes.append(f"{lib_name}: {old_ver} → {new_ver}")

    return content, changes


def apply_new_versions(content: str, new_versions: list[str], lib_name: str) -> tuple[str, list[str]]:
    """Insert new pre-release variants into YAML content.

    Inserts before the first existing variant of the library.
    """
    changes = []
    for ver in new_versions:
        # Find insertion point: first "    variants:" under the library, then first "      - version:"
        # We insert the new variant as the first entry after "variants:"
        lib_pattern = f"name: {lib_name}"
        lib_pos = content.find(lib_pattern)
        if lib_pos == -1:
            continue
        variants_pos = content.find("variants:", lib_pos)
        if variants_pos == -1:
            continue
        # Find the first "- version:" after variants:
        first_variant_pos = content.find("- version:", variants_pos)
        if first_variant_pos == -1:
            continue
        # Extract the existing line to copy its exact indentation
        line_start = content.rfind("\n", 0, first_variant_pos) + 1
        existing_line = content[line_start:content.find("\n", first_variant_pos)]
        # Get whitespace before "- version:"
        indent = existing_line[:len(existing_line) - len(existing_line.lstrip())]

        new_entry = (
            f'{indent}- version: "{ver}"\n'
            f'{indent}  label: "{ver} (experimental)"\n'
            f'{indent}  requires: "c11"\n'
        )
        first_variant_pos = line_start  # insert before the whole line
        content = content[:first_variant_pos] + new_entry + content[first_variant_pos:]
        changes.append(f"{lib_name}: NEW {ver} (prerelease)")

    return content, changes


def main():
    dry_run = "--dry-run" in sys.argv

    print(f"Loading {LIBRARIES_YAML}")
    content = LIBRARIES_YAML.read_text(encoding="utf-8")
    data = yaml.safe_load(content)
    if not data or "libraries" not in data:
        print("ERROR: Invalid libraries.yaml")
        sys.exit(1)

    all_changes = []

    for lib in data["libraries"]:
        name = lib["name"]
        version_check = lib.get("version_check", "")
        if not version_check:
            print(f"  {name}: no version_check, skipping")
            continue

        print(f"  {name}: checking {version_check}...")
        versions = fetch_versions(version_check, name)
        if not versions:
            print(f"  {name}: no versions found")
            continue

        print(f"  {name}: found {len(versions)} releases")

        # Find updates
        updates = find_updates(lib, versions)
        new_vers = find_new_versions(lib, versions)

        if updates or new_vers:
            if not dry_run:
                content, changes = apply_updates(content, updates, name)
                content, new_changes = apply_new_versions(content, new_vers, name)
                all_changes.extend(changes + new_changes)
            else:
                for old_v, new_v in updates:
                    print(f"    {name}: {old_v} → {new_v}")
                    all_changes.append(f"{name}: {old_v} → {new_v}")
                for nv in new_vers:
                    print(f"    {name}: NEW {nv} (prerelease)")
                    all_changes.append(f"{name}: NEW {nv} (prerelease)")
        else:
            print(f"    up to date")

    if all_changes:
        if dry_run:
            print(f"\nDRY RUN — {len(all_changes)} changes would be made:")
            for c in all_changes:
                print(f"  {c}")
        else:
            LIBRARIES_YAML.write_text(content, encoding="utf-8")
            print(f"\nUpdated {LIBRARIES_YAML} with {len(all_changes)} changes")
            for c in all_changes:
                print(f"  {c}")
    else:
        print("\nAll libraries up to date")

    return 0


if __name__ == "__main__":
    sys.exit(main())
