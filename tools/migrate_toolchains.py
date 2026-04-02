#!/usr/bin/env python3
"""Migrate s3 toolchain .cfg files to s4 toolchains.yaml format.

Usage:
    python3 tools/migrate_toolchains.py /path/to/s3_releases/support/toolchains.cfg
"""

import re
import sys
from pathlib import Path

import yaml


def parse_s3_cfg(path: Path) -> dict:
    """Parse a bash-style s3 toolchain config file."""
    tc = {}
    text = path.read_text(encoding="utf-8", errors="replace")

    # Simple key="value" parser
    for line in text.splitlines():
        m = re.match(r'^(\w+)="(.*?)";?\s*$', line)
        if m:
            tc[m.group(1)] = m.group(2)

    return tc


def convert_to_s4(name: str, cfg: dict) -> dict:
    """Convert s3 config dict to s4 toolchain dict."""
    # Decode base64 URL if present
    url = ""
    encoded = cfg.get("_toolchainfilename", "")
    if encoded:
        import base64
        try:
            url = base64.b64decode(encoded).decode("utf-8").strip()
        except Exception:
            url = ""

    tc_name = cfg.get("_toolchainname", name)
    # Rename generic bootlin toolchains: aarch64_generic -> aarch64_standard
    if tc_name.endswith("_generic"):
        tc_name = tc_name.replace("_generic", "_standard")

    return {
        "name": tc_name,
        "description": cfg.get("_description", ""),
        "arch": guess_arch(cfg.get("_toolchainname", name), cfg.get("_compiler", "")),
        "machines": cfg.get("_machines", ""),
        "prefix": cfg.get("_compiler", ""),
        "sysroot": cfg.get("_sysroot", ""),
        "confdir": cfg.get("_oscamconfdir_default", "/etc/tuxbox/config"),
        "useflags": cfg.get("default_use", ""),
        "cc_opts": cfg.get("extra_cc", ""),
        "cflags": cfg.get("extra_c", ""),
        "ldflags": cfg.get("extra_ld", ""),
        "make_vars": cfg.get("extra_use", ""),
        "stapi_lib": cfg.get("stapi_lib_custom", "") if cfg.get("stapi_allowed") == "1" else "",
        "url": url,
        "strip_components": int(cfg.get("_extract_strip", "1")),
    }


def make_bootlin_name(arch: str, libc: str) -> str:
    """Generate s4 name for bootlin toolchains like armv7_standard."""
    return f"{arch}_standard"


def guess_arch(name: str, compiler: str) -> str:
    """Guess architecture from toolchain name or compiler prefix."""
    name_lower = name.lower()
    comp_lower = compiler.lower()

    for arch in ["aarch64", "armv7", "armv6", "armv5", "arm", "mips64",
                 "mips32el", "mipsel", "mips", "powerpc", "ppc", "sh4",
                 "x86_64", "x86", "i686", "csky"]:
        if arch in name_lower or arch in comp_lower:
            return arch
    return ""


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-s3-toolchains.cfg-dir> [output.yaml]")
        sys.exit(1)

    cfg_dir = Path(sys.argv[1])
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("toolchains.yaml")

    if not cfg_dir.is_dir():
        print(f"Error: {cfg_dir} is not a directory")
        sys.exit(1)

    toolchains = []
    for cfg_file in sorted(cfg_dir.iterdir()):
        if cfg_file.is_file() and cfg_file.name != "native":
            cfg = parse_s3_cfg(cfg_file)
            if cfg.get("_toolchainname"):
                tc = convert_to_s4(cfg_file.name, cfg)
                toolchains.append(tc)
                print(f"  Converted: {tc['name']}")

    # Remove empty string values from each toolchain
    cleaned = []
    for tc in toolchains:
        cleaned.append({k: v for k, v in tc.items() if v != "" and v != 0})
        # Always keep name
        if "name" not in cleaned[-1]:
            cleaned[-1]["name"] = tc["name"]
        # strip_components=1 is default, only keep if different
        if tc.get("strip_components", 1) == 1 and "strip_components" in cleaned[-1]:
            del cleaned[-1]["strip_components"]

    data = {"toolchains": cleaned}
    raw = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    # Add blank line between toolchain entries
    raw = raw.replace("\n- name:", "\n\n- name:")

    # Prepend template comment
    template = """# Simplebuild4 Toolchain Registry
#
# Template — all available fields with descriptions:
#
# - name: my_toolchain              # Unique identifier (required)
#   description: "My custom TC"     # Human-readable description
#   arch: aarch64                   # Architecture: aarch64, arm, armv7, mips, mipsel, sh4, powerpc, x86, csky
#   machines: "dm920 dm900"         # Space-separated list of target devices
#   prefix: aarch64-linux-gnu-      # Compiler prefix (required), e.g. arm-linux-gnueabihf-
#   sysroot: sysroot                # Sysroot path relative to toolchain directory
#   confdir: /etc/tuxbox/config     # Default oscam config directory on target
#   useflags: "USE_LIBCRYPTO"       # Default USE flags, space-separated
#   cc_opts: "-mbranch-protection=standard"  # Compiler options → passed as CC_OPTS to make
#   cflags: "-mfpu=vfpv3-d16"      # Architecture-specific C flags → EXTRA_CFLAGS
#   ldflags: "-Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1"  # Linker flags → EXTRA_LDFLAGS
#   make_vars: "LIB_RT= LIB_PTHREAD="  # Raw make variable assignments (e.g. for Android)
#   stapi_lib: "libwi.a"           # STAPI library name (empty = STAPI not supported, "default" = liboscam_stapi.a)
#   url: https://example.com/tc.tar.gz  # Download URL
#   strip_components: 1             # tar --strip-components (default: 1, omit if 1)
#
# Notes:
# - Only 'name' and 'prefix' are required, all others are optional
# - Empty fields can be omitted
# - User additions go to data/toolchains.local.yaml (not tracked by git)
# - When assigned to a target, useflags/cc_opts/cflags/ldflags/make_vars/confdir
#   are copied as defaults and can be customized per target

"""

    output_path.write_text(template + raw, encoding="utf-8")
    print(f"\nWrote {len(toolchains)} toolchains to {output_path}")


if __name__ == "__main__":
    main()
