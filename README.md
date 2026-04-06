# SimpleBuild4 (s4): cross-compilation build system for oscam

[![GitLab Last Commit](https://img.shields.io/gitlab/last-commit/19?gitlab_url=https%3A%2F%2Fgit.streamboard.tv&style=for-the-badge)](https://git.streamboard.tv/common/simplebuild4/-/commits/master)
[![GitLab Tag](https://img.shields.io/gitlab/v/tag/19?gitlab_url=https%3A%2F%2Fgit.streamboard.tv&style=for-the-badge)](https://git.streamboard.tv/common/simplebuild4/-/tags)
[![GitLab License](https://img.shields.io/gitlab/license/19?gitlab_url=https%3A%2F%2Fgit.streamboard.tv&style=for-the-badge)](https://git.streamboard.tv/common/simplebuild4/-/blob/master/COPYING)


## Overview

s4 builds oscam for arbitrary platforms. The build process is controlled by four pillars:

```
Settings ─── global settings (compiler flags, signing, QEMU, theme)
    │
Repositories ─── oscam source code (git/svn repos with backup/restore)
    │
Toolchains ─── cross-compilers + libraries (registry with catalog + local extensions)
    │
Targets ─── build configurations (combines repos + toolchains + options)
```

## Settings (F1)

Global application settings in `data/settings.yaml`. Apply to all builds:

- **Compiler**: base flags (`-O2 -ggdb ...`), max CPUs
- **Signing**: X.509 certificate + private key for binary signing
- **QEMU**: smoke test after cross-build (expected output, timeout)
- **Update**: s4 URL, update check at startup
- **Theme**: color scheme (persistent)
- **Language**: UI language (prepared)

Settings are **not stored per target** — they apply globally.

## Repositories (F3)

oscam source code management in `data/repos.yaml`:

- Each repo has: **Name**, **URL** (git/svn), **Branch**, **Clone Depth**
- Default: `https://git.streamboard.tv/common/oscam.git`
- Repos are cloned to `data/repos/oscam-<name>/`
- **Backup/Restore**: after each clone/update, a tar.gz backup is created (`data/backups/`). Before the build, the backup is extracted → guarantees a clean state.
- Multiple repos possible (e.g. different oscam forks)
- A target references one or more repos as "Sources"

## Toolchains (F4)

Cross-compilers and libraries. Two independent YAML sources:

### Registry

```
defaults/toolchains.yaml    ← shipped catalog (80+ toolchains), read-only
data/toolchains.local.yaml  ← local entries + installed toolchains
```

- **Catalog** (defaults): prebuilt toolchains by categories (Common, STB, Android, OpenWrt, Router, NAS, Mini PC, TV, Legacy)
- **Local** (local): user-defined toolchains with custom categories
- **Override**: local entry with same name replaces catalog entry
- **Install**: catalog toolchain is downloaded → entry copied to local yaml → directory in `data/toolchains/<name>/`
- **Remove**: only delete directory, YAML entry remains
- **Native**: system compiler is automatically detected and registered as symlinks in `data/toolchains/native/`

### Toolchain Properties

Each toolchain defines:
- Compiler: prefix, sysroot, arch
- Build defaults: cc_opts, cflags, ldflags, make_vars
- Download: url, checksum, strip_components
- Category: for UI grouping

### Libraries

Libraries (OpenSSL, libusb, pcsclite, ccid, libdvbcsa, zlib, libcurl) are built per toolchain in its sysroot:

```
defaults/libraries.yaml      ← shipped recipes, read-only
data/libraries.local.yaml    ← user overrides (same name replaces)
```

- Each library has variants (versions) with configure/make/install commands
- C11 check: variants requiring C11 are filtered out for older compilers
- Dependency sorting: libraries are built in the correct order (e.g. zlib → OpenSSL → libcurl)
- Download cache: `data/downloads/` — downloaded once, reused
- Toolchain check (Check Button): shows compiler info, sysroot, headers, libraries with versions

## Targets (F2 Main Screen)

A target is a **complete build configuration** in `data/targets/<name>.yaml`:

### Structure

```yaml
name: pulse4kmini
description: Pulse 4K Mini Build
sources:                          # ← which repos
  - name: default
    update_before_build: true
    patch_before_build: true
    patches: [oscam-emu.patch]
toolchains:                       # ← which toolchains
  - name: cortexa15hf_oealliance_6.0
    cc_opts: ""                   # override per toolchain
    cflags: ""
    ldflags: ""
build:                            # ← oscam options
  modules: [WEBIF, WITH_SSL, ...]
  protocols: [MODULE_CCCAM, ...]
  readers: [READER_NAGRA, ...]
  card_readers: [CARDREADER_PHOENIX, ...]
  use_libcrypto: true
  use_ssl: true
  use_compress: true
  compress_level: --lzma
  upx_binary: ""
  build_system: make
  confdir: /etc/tuxbox/config
upload: []                        # ← SSH upload (planned)
```

### Interaction

A target combines:
1. **Sources** (1..n repos) — which oscam source code, with which patches
2. **Toolchains** (1..n) — for which platforms, with optional flag overrides
3. **Build options** — oscam modules/protocols/readers/card readers + USE_FLAGS
4. **Build system** — make or cmake

The build produces: `Sources × Toolchains` binaries.

### Build Overrides

In addition to the stored target options, overrides can be provided at runtime:
- **GUI**: textbox next to the target dropdown
- **CLI**: `./s4 build <target> USE_SSL USE_PCSC_off`

Format: `OPTION` enables, `OPTION_off` disables. Affects PHASE 3 (config.sh) and PHASE 4 (USE_FLAGS).

## Build Pipeline

```
PHASE 1: Prepare Repository
  └─ restore from backup → git pull → new backup

PHASE 2: Apply Patches
  └─ git apply → WebIF patch info

PHASE 3: Configure oscam
  └─ config.sh: restore → disable all → enable selected → overrides

PHASE 4: Build (per Source × Toolchain)
  └─ distclean → make/cmake → post-build (delete .debug, save list_smargo)
  └─ QEMU smoke test (optional)

(PHASE 5: Upload — planned)
```

### Build Output

- Binaries: `data/binaries/oscam-<vcs><rev>@<commit>-<repo>-<toolchain>-<features>-<linking>`
- Logs: `data/logs/<timestamp>.<target>.build-<make|cmake>.log`
- Colored output in GUI (s3-style: BUILD/LINK/STRIP/SIGN/UPX labels)
- Logfile contains everything unfiltered

## Data Flow

```
                    ┌─────────────┐
                    │  Settings   │ global flags, signing, QEMU
                    └──────┬──────┘
                           │
┌──────────────┐    ┌──────┴──────┐    ┌──────────────┐
│ Repositories │───→│   Target    │←───│  Toolchains  │
│  (Sources)   │    │ (Build-Cfg) │    │ (Compiler+   │
│              │    │             │    │  Libraries)  │
└──────────────┘    └──────┬──────┘    └──────────────┘
                           │
                    ┌──────┴──────┐
                    │ BuildEngine │ PHASE 1-4
                    └──────┬──────┘
                           │
                    ┌──────┴──────┐
                    │  Binaries   │ + Logs
                    └─────────────┘
```

## Filesystem

```
simplebuild4/
├── s4                        Shell launcher (Python check, self-update)
├── s4.py                     entry point (GUI + CLI)
├── app.py                    main() → S4App
├── defaults/                 shipped configs (read-only, versioned)
│   ├── toolchains.yaml       toolchain catalog
│   ├── libraries.yaml        library build recipes
│   └── native.yaml           default target
├── support/
│   └── stapi/                STAPI libraries (proprietary)
├── core/                     business logic
│   ├── builder.py            build engine (PHASE 1-5, make+cmake)
│   ├── toolchain.py          toolchain registry + library check
│   ├── lib_builder.py        library build engine
│   ├── target.py             target dataclass
│   ├── repo.py               repository management + backup/restore
│   ├── oscam_config.py       config.sh interface
│   ├── settings.py           app settings
│   └── paths.py              path registry
├── ui/                       textual TUI
│   ├── app.py                S4App + keyboard help
│   ├── header.py             header (version)
│   ├── footer.py             footer (F-keys)
│   ├── commands.py           command palette entries
│   ├── theme.tcss            CSS styling
│   └── screens/              screen modules
│       ├── main.py           F2 targets + build
│       ├── settings.py       F1 settings
│       ├── repository.py     F3 repositories
│       ├── target_edit.py    target editor (4 tabs)
│       └── toolchains.py     F4 toolchains + libraries
└── data/                     user data (not version controlled)
    ├── settings.yaml         s4 settings configuration
    ├── repos.yaml            s4 repositories  configuration
    ├── toolchains.local.yaml s4 local toolchains configuration
    ├── libraries.local.yaml  s4 local libraries build recipes
    ├── targets/              target YAML files
    ├── repos/                cloned repositories
    ├── backups/              repo tar.gz backups
    ├── toolchains/           installed toolchains
    ├── binaries/             built oscam binaries
    ├── downloads/            cached downloads
    ├── logs/                 build + update logs
    ├── patches/              patch files
    └── bin/                  custom tool symlinks (upx)
```
