# syntax=docker/dockerfile:1.6
FROM ubuntu:26.04

# --- Environment (stable — cached across builds) ----------------------
ENV DEBIAN_FRONTEND=noninteractive \
    S4_IN_CONTAINER=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PATH="/app:${PATH}"

# --- Dependency spec + extractors -------------------------------------
# Copy JUST the spec and extractors first, so the apt/pip layers only
# invalidate when the spec (or an extractor) changes — NOT when any
# source file elsewhere changes.
COPY defaults/dependencies.yaml /tmp/deps.yaml
COPY docker/deps-to-apt.py docker/deps-to-pip.py /tmp/

# --- System packages --------------------------------------------------
# Bootstrap: base runtime + Python + PyYAML (needed to run the extractors),
# then a second apt-get pulls the dynamic list derived from deps.yaml.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates dumb-init gosu tzdata openssl \
       python3 python3-pip python3-yaml \
    && apt-get install -y --no-install-recommends \
       $(python3 /tmp/deps-to-apt.py /tmp/deps.yaml) \
    && apt-get install -y --no-install-recommends \
       nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# --- Python packages --------------------------------------------------
# Dynamic list from deps.yaml + container-only extras.
# textual-serve / aiohttp / aiohttp-jinja2 are NOT in deps.yaml because
# s4 nachinstalliert sie zur Laufzeit nur bei aktiviertem Web-Feature;
# im Container wollen wir alles vorinstalliert für zero-network first-run.
RUN pip3 install --no-cache-dir --break-system-packages --root-user-action=ignore \
       $(python3 /tmp/deps-to-pip.py /tmp/deps.yaml) \
       textual-serve aiohttp aiohttp-jinja2

# --- Application ------------------------------------------------------
WORKDIR /app
COPY . /app

# Repack the ge-COPYed .git to compact loose objects — keeps the image
# small and makes future git fetches faster. The trailing `|| true`
# lets the build survive if .git is somehow missing (e.g. a downstream
# consumer excludes it via their own .dockerignore).
RUN git -C /app repack -a -d --depth=250 --window=250 2>/dev/null || true

# Git refuses to operate in dirs owned by another UID than the current
# process (docker containers frequently do this). Register /app as safe
# system-wide so `./s4 update` works no matter which user runs it.
RUN git config --system --add safe.directory /app

# --- Build metadata (kept LAST on purpose) ----------------------------
# S4_IMAGE_COMMIT is baked in via `--build-arg` from CI and changes every
# build. Placing it here means the expensive apt/pip layers above stay
# cached; only the tiny ENV layer below invalidates per commit.
# The entrypoint reads it at runtime to detect when the user pulled a
# fresh image (baked commit differs from what's stored in the data
# volume) and drops any stale user-set pin accordingly. Empty when
# building locally without --build-arg (pin-drop logic then just does
# not fire — pin is honored blindly).
ARG IMAGE_COMMIT=""
ENV S4_IMAGE_COMMIT=$IMAGE_COMMIT

# --- OCI image labels -------------------------------------------------
# Overrides the labels inherited from ubuntu:26.04 so the GHCR package
# page shows our metadata instead of Canonical's Ubuntu description.
# Kept AT THE END so tweaks to these strings don't invalidate the
# apt/pip layers above.
LABEL org.opencontainers.image.title="simplebuild4" \
      org.opencontainers.image.description="SimpleBuild4 (s4) — cross-compilation build system for oscam. Ships web-TUI on port 8080 and MCP server on port 8181. Docs: https://git.streamboard.tv/common/simplebuild4/-/wikis/getting-started/docker" \
      org.opencontainers.image.source="https://git.streamboard.tv/common/simplebuild4" \
      org.opencontainers.image.url="https://git.streamboard.tv/common/simplebuild4" \
      org.opencontainers.image.documentation="https://git.streamboard.tv/common/simplebuild4/-/wikis/getting-started/docker" \
      org.opencontainers.image.vendor="WXbet"

# Persistent state: settings.yaml, downloads/, toolchains/, targets/,
# logs/, patches/, binaries/. Mount with `-v ~/s4-data:/app/data`.
# The mount point must exist for entrypoint.py's uid-detect to stat it.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Web (textual-serve) + MCP (Streamable HTTP or SSE)
EXPOSE 8080 8181 6274 6277

# dumb-init is PID 1 → reaps zombies, forwards signals cleanly.
# entrypoint.py either launches web+mcp per settings.yaml or execs a
# user-supplied CMD (e.g. `docker run … s4` for interactive TUI).
ENTRYPOINT ["dumb-init", "--", "python3", "/app/docker/entrypoint.py"]
CMD []
