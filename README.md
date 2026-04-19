# SimpleBuild4 (s4): cross-compilation build system for oscam

[![GitLab Last Commit](https://img.shields.io/gitlab/last-commit/19?gitlab_url=https%3A%2F%2Fgit.streamboard.tv&style=for-the-badge)](https://git.streamboard.tv/common/simplebuild4/-/commits/master)
[![GitLab Tag](https://img.shields.io/gitlab/v/tag/19?gitlab_url=https%3A%2F%2Fgit.streamboard.tv&style=for-the-badge)](https://git.streamboard.tv/common/simplebuild4/-/tags)
[![GitLab License](https://img.shields.io/gitlab/license/19?gitlab_url=https%3A%2F%2Fgit.streamboard.tv&style=for-the-badge)](https://git.streamboard.tv/common/simplebuild4/-/blob/master/COPYING)
[![Wiki](https://img.shields.io/badge/wiki-docs-informational?style=for-the-badge)](https://git.streamboard.tv/common/simplebuild4/-/wikis/home)


## Overview

s4 builds oscam for arbitrary platforms. The build process is controlled
by four **core pillars** plus optional extensions that automate and
expose builds to the outside world:

```
Core:

  Settings ─── global settings (compiler flags, signing, QEMU,
      │       service config, theme)
  Repositories ─── oscam source code (git/svn repos with backup/restore)
      │
  Toolchains ─── cross-compilers + libraries
      │        (catalog + self-build + customize)
  Targets ─── build configurations + deployment
             (repos + toolchains + options + SSH upload)

Extensions (optional):

  Automation (F7) ──── cron-style / triggered target builds
  Web Service   ────── read-only status page + log tail over HTTP(S)
  MCP Server    ────── tool surface for LLM clients
                       (Claude Desktop / Claude.ai / custom agents)
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

*→ [Wiki: Settings](https://git.streamboard.tv/common/simplebuild4/-/wikis/core/settings)*

## Repositories (F3)

oscam source code management in `data/repos.yaml`:

- Each repo has: **Name**, **URL** (git/svn), **Branch**, **Clone Depth**
- Default: `https://git.streamboard.tv/common/oscam.git`
- Repos are cloned to `data/repos/oscam-<name>/`
- **Backup/Restore**: after each clone/update, a tar.gz backup is created (`data/backups/`). Before the build, the backup is extracted → guarantees a clean state.
- Multiple repos possible (e.g. different oscam forks)
- A target references one or more repos as "Sources"

*→ [Wiki: Repositories](https://git.streamboard.tv/common/simplebuild4/-/wikis/core/repositories)*

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

*→ [Wiki: Toolchains](https://git.streamboard.tv/common/simplebuild4/-/wikis/core/toolchains)*

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

*→ [Wiki: Toolchain Libraries](https://git.streamboard.tv/common/simplebuild4/-/wikis/core/toolchains-libraries)*

### Self-Build & Customize

Some catalog entries are **templates** for three builder backends instead
of direct tarball downloads:

- **crosstool-NG** — full cross-compiler build from upstream sources
- **OE-SDK** — Yocto/OpenEmbedded SDK extraction + wrapping
- **Android NDK** — NDK-based toolchain for Android targets

**F5 Build** on a selected template starts the build and produces a
fresh toolchain in `data/toolchains/<name>/`. If an install already
exists under that name, F5 requires a double-press to confirm overwrite.

**F6 Customize** opens the builder's interactive configurator:

- crosstool-NG / freetz-ng → `menuconfig` (TUI suspends, configurator
  runs in the full terminal, changes persist back into the template)
- OE-SDK → form-based modal for BSP / machine tweaks
- Android NDK → not customizable (NDK archive is self-contained)

*→ [Wiki: Self-Build](https://git.streamboard.tv/common/simplebuild4/-/wikis/core/toolchains-self-build) · [Customize](https://git.streamboard.tv/common/simplebuild4/-/wikis/core/toolchains-customize)*

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
deployment:                       # ← SSH upload after build
  - name: office
    enabled: true
    source: default
    toolchain: cortexa15hf_oealliance_6.0
    host: 192.168.1.100
    user: root
    port: 22
    key: ""                       # path to SSH private key (optional)
    target_path: /usr/bin/oscam
    pre_cmd: "systemctl stop oscam"
    post_cmd: "systemctl start oscam"
```

### Interaction

A target combines:
1. **Sources** (1..n repos) — which oscam source code, with which patches
2. **Toolchains** (1..n) — for which platforms, with optional flag overrides
3. **Build options** — oscam modules/protocols/readers/card readers + USE_FLAGS
4. **Build system** — make or cmake

The build produces: `Sources × Toolchains` binaries.

*→ [Wiki: Targets](https://git.streamboard.tv/common/simplebuild4/-/wikis/core/targets) · [Build Options](https://git.streamboard.tv/common/simplebuild4/-/wikis/core/targets-build-options)*

### Build Overrides

In addition to the stored target options, overrides can be provided at runtime:
- **GUI**: textbox next to the target dropdown
- **CLI**: `./s4 build <target> USE_SSL USE_PCSC_off`

Format: `OPTION` enables, `OPTION_off` disables. Affects PHASE 3 (config.sh) and PHASE 4 (USE_FLAGS).

*→ [Wiki: Build Overrides](https://git.streamboard.tv/common/simplebuild4/-/wikis/core/targets-overrides)*

### Deployment

After a successful build, each enabled deployment entry copies the binary
to the target host via SSH (PHASE 5):

1. Match the entry's `source` + `toolchain` against the build matrix
2. Run `pre_cmd` on the remote (e.g. stop the running oscam)
3. `scp` the binary to `target_path`
4. Run `post_cmd` on the remote (e.g. restart the service)

Authentication: an SSH `key` path takes priority; falls back to password
auth via `sshpass` when `password` (encrypted in the YAML) is set.
A separate `su_password` can be configured for deployments that need
`su -c` wrapping for commands normally requiring root.

*→ [Wiki: Deployment](https://git.streamboard.tv/common/simplebuild4/-/wikis/core/targets-deployment) · [Workflow: Configure Deployment](https://git.streamboard.tv/common/simplebuild4/-/wikis/workflows/configure-deployment)*

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

PHASE 5: Deploy (per enabled deployment entry)
  └─ match source × toolchain → pre_cmd → scp → post_cmd
```

### Build Output

- Binaries: `data/binaries/oscam-<vcs><rev>@<commit>-<repo>-<toolchain>-<features>-<linking>`
- Logs: `data/logs/<timestamp>.<target>.build-<make|cmake>.log`
- Colored output in GUI (s3-style: BUILD/LINK/STRIP/SIGN/UPX labels)
- Logfile contains everything unfiltered

*→ [Wiki: First oscam Build](https://git.streamboard.tv/common/simplebuild4/-/wikis/workflows/first-oscam-build)*

## Services

Optional long-running helpers that s4 can install as systemd units
alongside the TUI. All three are configured in F1 Settings; install /
start / stop runs via a setup modal that shells out through `sudo -n`
with a minimal sudoers template (whitelisted `systemctl` and `tee`
invocations only — no free-form shell).

### Web Service

Serves a read-only status page plus live log tail over HTTP / HTTPS.
Useful when s4 runs on a headless build host and you want a live view
from elsewhere on the LAN.

- Config: `web_host`, `web_port`, `web_dns_name`, `web_ssl`,
  `web_ssl_cert`, `web_ssl_key`, optional basic-auth via
  `web_auth_user` + `web_auth_password` (encrypted at rest)
- Systemd unit: `s4-web.service`, installed from F1 → *Setup*
- Live public URL is shown in the collapsible section title for quick
  copy-paste

*→ [Wiki: Web Service](https://git.streamboard.tv/common/simplebuild4/-/wikis/extensions/web-service)*

### MCP Server

Model Context Protocol server that exposes s4's state and controls to
LLM clients (Claude Desktop, Claude.ai connectors, custom agents).

CLI:

```bash
./s4 mcp                       # stdio transport — spawn from an MCP client
./s4 mcp --http                # HTTP / SSE server
    [--host 0.0.0.0]
    [--port 8181]
    [--token SECRET]           # Bearer auth (empty = anonymous, localhost-only)
    [--allow-actions]          # enable writable tools (build / install / …)
    [--root-path /s4-mcp]      # reverse-proxy URL prefix
    [--ssl-cert … --ssl-key …]
```

Env var `S4_MCP_ROOT_PATH` overrides `--root-path`. Behind an Apache /
nginx reverse proxy, setting `X-Forwarded-Prefix` in the proxy config is
enough — s4 picks the prefix up automatically via the ASGI
`root_path`, no extra CLI flag needed.

**Security policy** (`mcp_server/tools/`):

- Read-only tools always available — repository/target/toolchain/log inspection; wiki documentation browsing (`list_wiki_pages`, `get_wiki_page`)
- Write tools require `--allow-actions` / `mcp_allow_actions: true`
- Credential fields (repo `auth_pass`, deployment `password` /
  `su_password`, bearer tokens, signing key) are **TUI-only** — cannot
  be set or read through MCP
- Service knobs (`mcp_*`, `web_*`) are also TUI-only — prevents MCP
  self-escalation
- Deployment secrets survive `update_target` calls (matched by `name`
  with a fallback to `host:user`)

**F1 Test button** spawns `@modelcontextprotocol/inspector` against the
running local service for interactive tool exploration (Web UI or CLI
`tools/list`). If Node.js is missing, the modal offers a user-local
install into `$XDG_DATA_HOME/simplebuild4/node/` — no sudo, ~22 MB
download, fully reversible with `rm -rf`.

*→ [Wiki: MCP Server](https://git.streamboard.tv/common/simplebuild4/-/wikis/extensions/mcp-server) · [Clients](https://git.streamboard.tv/common/simplebuild4/-/wikis/extensions/mcp-clients) · [Security](https://git.streamboard.tv/common/simplebuild4/-/wikis/extensions/mcp-security) · [Reverse Proxy](https://git.streamboard.tv/common/simplebuild4/-/wikis/extensions/mcp-reverse-proxy) · [Inspector](https://git.streamboard.tv/common/simplebuild4/-/wikis/extensions/mcp-inspector)*

### Automation (F7)

Scheduled / triggered build jobs defined in `data/automation.yaml`. Each
job:

- Selects a target, optional runtime overrides
- Runs on a cron-style schedule or on demand via *Run*
- Persists state (last run, status, pid) in
  `data/automation.state.yaml`
- Writes per-job logs under `data/logs/automation/`

Useful for nightly full-matrix rebuilds, triggered deployments, or
keeping a CI-style workflow close to the build bench.

*→ [Wiki: Automation](https://git.streamboard.tv/common/simplebuild4/-/wikis/extensions/automation) · [Workflow: Schedule Nightly Builds](https://git.streamboard.tv/common/simplebuild4/-/wikis/workflows/schedule-nightly-builds)*

## Data Flow

```
                    ┌─────────────┐
                    │  Settings   │ global flags, signing, QEMU,
                    └──────┬──────┘ web / mcp service config
                           │
┌──────────────┐    ┌──────┴──────┐    ┌──────────────┐
│ Repositories │───→│   Target    │←───│  Toolchains  │
│  (Sources)   │    │ (Build-Cfg) │    │ (Compiler +  │
│              │    │             │    │  Libraries)  │
└──────────────┘    └──────┬──────┘    └──────────────┘
                           │
                    ┌──────┴──────┐    ┌──────────────┐
                    │ BuildEngine │◀───│  Automation  │ F7 scheduled
                    │ PHASE 1-5   │    │   (cron)     │    / triggered
                    └──────┬──────┘    └──────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
       ┌───────────┐ ┌──────────┐ ┌────────────┐
       │ Binaries  │ │   Logs   │ │ Deployment │ PHASE 5
       │           │ │          │ │  (SSH scp) │
       └───────────┘ └─────┬────┘ └────────────┘
                           │
                     ┌─────┴──────┐
                     │  Services  │ Web / MCP expose logs + state
                     │ (optional) │ to browsers / LLM clients
                     └────────────┘
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
│       ├── main.py                  F2 targets + build
│       ├── settings.py              F1 settings
│       ├── repository.py            F3 repositories
│       ├── target_edit.py           target editor (4 tabs)
│       ├── toolchains.py            F4 toolchains + libraries
│       ├── automation.py            F7 automation jobs
│       ├── web_service_modal.py     Web Service setup
│       ├── mcp_service_modal.py     MCP Server setup
│       └── mcp_inspector_modal.py   MCP Inspector launcher
├── mcp_server/               MCP server (SSE + stdio transports)
│   ├── server.py             entry point (./s4 mcp …)
│   ├── http_transport.py     SSE + ForwardedPrefixMiddleware
│   └── tools/                tool dispatcher (repos, targets, builds,
│                             toolchains, settings, automation, …)
└── data/                     user data (not version controlled)
    ├── settings.yaml            s4 settings configuration
    ├── repos.yaml               s4 repositories configuration
    ├── toolchains.local.yaml    s4 local toolchains configuration
    ├── libraries.local.yaml     s4 local libraries build recipes
    ├── automation.yaml          F7 automation job definitions
    ├── automation.state.yaml    F7 per-job runtime state
    ├── targets/                 target YAML files
    ├── repos/                   cloned repositories
    ├── backups/                 repo tar.gz backups
    ├── toolchains/              installed toolchains
    ├── binaries/                built oscam binaries
    ├── downloads/               cached downloads
    ├── logs/                    build + update logs
    │   └── automation/          F7 per-job logs
    ├── mcp/                     s4-mcp.env (service token, host/port)
    ├── web/                     s4-web.env (service config)
    ├── patches/                 patch files
    └── bin/                     custom tool symlinks (upx)
```
