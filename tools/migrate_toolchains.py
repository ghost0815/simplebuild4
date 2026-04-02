#!/usr/bin/env python3
"""Migrate s3 toolchain .cfg files to s4 toolchains.yaml format.

Usage:
    python3 tools/migrate_toolchains.py /path/to/s3/support/toolchains.cfg
"""

import base64
import re
import sys
from pathlib import Path

import yaml

# Toolchains to skip (not migrated to s4)
DELETE_LIST = {
    "aarch64--glibc--bleeding-edge-2024.02-1",
    "aarch64--glibc--stable-2024.02-1",
    "aarch64--musl--stable-2020.02-2",
    "armv7-eabihf--glibc--stable-2018.11-1_generic",
    "gbquad4kpro_oealliance_8.0",
    "h8_openpli_10.0",
    "sf8008_oealliance_7.6",
    "sf8008_oealliance_8.0",
    "sf8008_openatv_8.0",
    "sf8008_openpli_10.0",
    "vusolo4k_openbh_7.6",
    "vuultimo4k_openbh_7.6",
    "dreamone_oealliance_8.0",
    "fos_u69x_mips",
}

# Category mapping: toolchain name -> category
CATEGORY_MAP = {
    # standard (Bootlin generic)
    "aarch64_standard": "standard",
    "armv5_standard": "standard",
    "armv6_standard": "standard",
    "armv7_standard": "standard",
    "mips32el_standard": "standard",
    "powerpc_standard": "standard",
    "sh4_standard": "standard",
    # stb (OE-Alliance, OpenPLi, OpenDreambox, etc.)
    "aarch64_oealliance_5.6": "stb", "aarch64_oealliance_6.0": "stb",
    "aarch64_opendreambox_pyro": "stb",
    "armv7ahf_openpli_scarthgap": "stb", "armv7vehf_openpli_scarthgap": "stb",
    "cortexa15hf_oealliance_5.6": "stb", "cortexa15hf_oealliance_6.0": "stb",
    "cortexa15hf_opendreambox_krogoth": "stb", "cortexa15hf_opendreambox_pyro": "stb",
    "cortexa15hf_openpli_scarthgap": "stb",
    "cortexa7hf_oealliance_5.6": "stb", "cortexa7hf_oealliance_6.0": "stb",
    "cortexa7hf_openpli_scarthgap": "stb", "cortexa7thf_openpli_scarthgap": "stb",
    "cortexa7vfp_arm": "stb",
    "cortexa9hf_oealliance_5.6": "stb", "cortexa9hf_oealliance_6.0": "stb",
    "dreamone_oealliance_8.0": "stb",
    "mips32el-nf_openpli_scarthgap": "stb",
    "mips32el_oealliance_5.6": "stb", "mips32el_oealliance_6.0": "stb",
    "mips32el_opendreambox_krogoth": "stb", "mips32el_opendreambox_pyro": "stb",
    "mips32el_openpli_scarthgap": "stb",
    # android
    "android_10_arm": "android", "android_11_arm": "android",
    "android_12_arm": "android", "android_13_arm": "android",
    "android_14_arm": "android", "android_15_arm": "android",
    "android_nougat_arm": "android", "android_oreo_arm": "android",
    "android_pie_arm": "android",
    # openwrt
    "aarch64--musl--stable-2020.02-2_generic": "openwrt",
    "aarch64--musl--stable-2020.02-2_standard": "openwrt",
    "armv5-eabi--musl--stable-2020.02-2_generic": "openwrt",
    "armv5-eabi--musl--stable-2020.02-2_standard": "openwrt",
    "armv6-eabihf--musl--stable-2020.02-2_generic": "openwrt",
    "armv6-eabihf--musl--stable-2020.02-2_standard": "openwrt",
    "openwrt1505cc": "openwrt",
    "owrt_ar71xx_mips": "openwrt", "owrt_ath79_mips": "openwrt",
    "owrt_kirkwood_arm": "openwrt", "owrt_mediatek_armv8": "openwrt",
    "owrt_mpc85xx_ppc": "openwrt", "owrt_ramips_mips": "openwrt",
    "owrt_rpi_armv6": "openwrt", "owrt_rpi_armv7": "openwrt",
    # router
    "fos_u69x_mips": "router", "fritz73xxOS65": "router", "fritz74xxOS65": "router",
    "ubnt_aarch64": "router", "ubnt_mips64": "router",
    # nas
    "qnap_armv5": "nas", "qnap_armv7": "nas", "qnap_x64": "nas",
    "syn_atom": "nas",
    "synology_aarch64": "nas", "synology_armv5": "nas",
    "synology_armv7": "nas", "synology_armv7a": "nas", "synology_ppc": "nas",
    # minipc
    "igel_x86": "minipc", "pogoplug_arm": "minipc",
    "rpi_aarch64": "minipc", "rpi_armv6": "minipc",
    "rpi_armv7": "minipc", "rpi_armv8": "minipc",
    # tv
    "samsungtv": "tv",
}
# Fritz long names
for k in list(CATEGORY_MAP.keys()):
    pass
# Anything starting with fritz that's not already mapped
FRITZ_PREFIX = "fritz"

# Rename map: old name -> new name
RENAME_MAP = {
    "aarch64--musl--stable-2020.02-2_generic": "aarch64--musl--stable-2020.02-2_standard",
    "armv5-eabi--musl--stable-2020.02-2_generic": "armv5-eabi--musl--stable-2020.02-2_standard",
    "armv6-eabihf--musl--stable-2020.02-2_generic": "armv6-eabihf--musl--stable-2020.02-2_standard",
}


def parse_s3_cfg(path: Path) -> dict:
    """Parse a bash-style s3 toolchain config file."""
    tc = {}
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        m = re.match(r'^(\w+)="(.*?)";?\s*$', line)
        if m:
            tc[m.group(1)] = m.group(2)
    return tc


def convert_to_s4(name: str, cfg: dict) -> dict:
    """Convert s3 config dict to s4 toolchain dict."""
    url = ""
    encoded = cfg.get("_toolchainfilename", "")
    if encoded:
        try:
            url = base64.b64decode(encoded).decode("utf-8").strip()
        except Exception:
            url = ""

    tc_name = cfg.get("_toolchainname", name)
    if tc_name.endswith("_generic"):
        tc_name = tc_name.replace("_generic", "_standard")

    # Apply rename
    tc_name = RENAME_MAP.get(tc_name, tc_name)

    # Determine category
    category = CATEGORY_MAP.get(tc_name, "")
    if not category and tc_name.startswith(FRITZ_PREFIX):
        category = "router"
    if not category:
        category = "legacy"

    return {
        "name": tc_name,
        "description": cfg.get("_description", ""),
        "category": category,
        "arch": guess_arch(cfg.get("_toolchainname", name), cfg.get("_compiler", "")),
        "machines": cfg.get("_machines", ""),
        "prefix": cfg.get("_compiler", ""),
        "sysroot": cfg.get("_sysroot", ""),
        "useflags": cfg.get("default_use", ""),
        "cc_opts": cfg.get("extra_cc", ""),
        "cflags": cfg.get("extra_c", ""),
        "ldflags": cfg.get("extra_ld", ""),
        "make_vars": cfg.get("extra_use", ""),
        "stapi_lib": cfg.get("stapi_lib_custom", "") if cfg.get("stapi_allowed") == "1" else "",
        "url": url,
        "checksum": _parse_checksum(cfg),
        "strip_components": int(cfg.get("_extract_strip", "1")),
    }


def _parse_checksum(cfg: dict) -> str:
    """Extract checksum from s3 config. Returns 'md5:hash' or empty."""
    md5 = cfg.get("_md5sum", "")
    if md5:
        # Format in s3: "hash  filename" — extract just the hash
        parts = md5.strip().split()
        if parts:
            return f"md5:{parts[0]}"
    return ""


def guess_arch(name: str, compiler: str) -> str:
    name_lower = name.lower()
    comp_lower = compiler.lower()
    for arch in ["aarch64", "armv7", "armv6", "armv5", "arm", "mips64",
                 "mips32el", "mipsel", "mips", "powerpc", "ppc", "sh4",
                 "x86_64", "x86", "i686", "csky"]:
        if arch in name_lower or arch in comp_lower:
            return arch
    return ""


# Manual additions not in s3 configs
MANUAL_ADDITIONS = [
    {
        "name": "armv5_standard",
        "description": "armv5 devices (generic)",
        "category": "standard",
        "arch": "armv5",
        "prefix": "armv5-eabi-buildroot-linux-musleabi-",
        "sysroot": "armv5-eabi-buildroot-linux-musleabi/sysroot",
        "useflags": "USE_LIBCRYPTO",
        "url": "https://toolchains.bootlin.com/downloads/releases/toolchains/armv5-eabi/tarballs/armv5-eabi--glibc--stable-2018.11-1.tar.bz2",
    },
]


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-s3-toolchains.cfg-dir> [output.yaml]")
        sys.exit(1)

    cfg_dir = Path(sys.argv[1])
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("toolchains.yaml")

    if not cfg_dir.is_dir():
        print(f"Error: {cfg_dir} is not a directory")
        sys.exit(1)

    seen_names = set()
    toolchains = []

    for cfg_file in sorted(cfg_dir.iterdir()):
        if cfg_file.is_file() and cfg_file.name != "native":
            cfg = parse_s3_cfg(cfg_file)
            tc_name = cfg.get("_toolchainname", cfg_file.name)

            # Check delete list (original name before rename)
            if tc_name in DELETE_LIST or cfg_file.name in DELETE_LIST:
                print(f"  Skipped (deleted): {tc_name}")
                continue

            if cfg.get("_toolchainname"):
                tc = convert_to_s4(cfg_file.name, cfg)

                # Skip duplicates
                if tc["name"] in seen_names:
                    print(f"  Skipped (duplicate): {tc['name']}")
                    continue
                seen_names.add(tc["name"])

                toolchains.append(tc)
                print(f"  Converted: {tc['name']} [{tc['category']}]")

    # Add manual additions
    for tc in MANUAL_ADDITIONS:
        if tc["name"] not in seen_names:
            toolchains.append(tc)
            seen_names.add(tc["name"])
            print(f"  Added manually: {tc['name']} [{tc.get('category', '')}]")

    # Sort by category then name
    category_order = ["standard", "stb", "android", "openwrt", "router", "nas", "minipc", "tv", "legacy"]
    def sort_key(tc):
        cat = tc.get("category", "legacy")
        idx = category_order.index(cat) if cat in category_order else 99
        return (idx, tc["name"])
    toolchains.sort(key=sort_key)

    # Remove empty string values
    cleaned = []
    for tc in toolchains:
        c = {k: v for k, v in tc.items() if v != "" and v != 0}
        if "name" not in c:
            c["name"] = tc["name"]
        if tc.get("strip_components", 1) == 1 and "strip_components" in c:
            del c["strip_components"]
        cleaned.append(c)

    data = {"toolchains": cleaned}
    raw = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    raw = raw.replace("\n- name:", "\n\n- name:")

    template = """# SimpleBuild4 (s4) Toolchain Registry
#
# Template — all available fields with descriptions:
#
# - name: my_toolchain              # Unique identifier (required)
#   description: "My custom TC"     # Human-readable description
#   category: standard              # Category: standard, stb, android, openwrt, router, nas, minipc, tv, legacy, custom
#   arch: aarch64                   # Architecture: aarch64, arm, armv7, mips, mipsel, sh4, powerpc, x86, csky
#   machines: "dm920 dm900"         # Space-separated list of target devices
#   prefix: aarch64-linux-gnu-      # Compiler prefix (required), e.g. arm-linux-gnueabihf-
#   sysroot: sysroot                # Sysroot path relative to toolchain directory (required)
#   useflags: "USE_LIBCRYPTO"       # Default USE flags, space-separated
#   cc_opts: "-mbranch-protection=standard"  # Compiler options → passed as CC_OPTS to make
#   cflags: "-mfpu=vfpv3-d16"      # Architecture-specific C flags → EXTRA_CFLAGS
#   ldflags: "-Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1"  # Linker flags → EXTRA_LDFLAGS
#   make_vars: "LIB_RT= LIB_PTHREAD="  # Raw make variable assignments (e.g. for Android)
#   stapi_lib: "libwi.a"           # STAPI library name (empty = STAPI not supported, "default" = liboscam_stapi.a)
#   url: https://example.com/tc.tar.gz  # Download URL
#   checksum: "sha256:abc123..."    # Download verification: "md5:hash" or "sha256:hash"
#   strip_components: 1             # tar --strip-components (default: 1, omit if 1)
#
# Categories: standard (Bootlin generic), stb (OE-Alliance/OpenPLi/OpenDreambox),
#   android, openwrt, router (Fritz/Ubiquiti), nas (Synology/QNAP),
#   minipc (RPi/Pogoplug), tv, legacy, custom (user-defined)
#
# Notes:
# - 'name', 'prefix' and 'sysroot' are required, all others are optional
# - Empty fields can be omitted
# - User additions go to data/toolchains.local.yaml (not tracked by git)
# - When assigned to a target, useflags/cc_opts/cflags/ldflags/make_vars
#   are copied as defaults and can be customized per target

"""

    output_path.write_text(template + raw, encoding="utf-8")
    print(f"\nWrote {len(cleaned)} toolchains to {output_path}")

    # Print category summary
    from collections import Counter
    cats = Counter(tc.get("category", "legacy") for tc in cleaned)
    for cat in category_order:
        if cat in cats:
            print(f"  {cat}: {cats[cat]}")


if __name__ == "__main__":
    main()
