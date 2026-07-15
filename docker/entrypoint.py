#!/usr/bin/env python3
"""Docker entrypoint for simplebuild4.

Two modes, selected by CMD:

- No CMD (default): launcher mode. Reads ``/app/data/settings.yaml``
  (seeded with defaults on first run) and spawns:
    * web service if ``web_enabled``
    * MCP service if ``mcp_enabled``
  If both are disabled, blocks on ``signal.pause()`` so the user can
  ``docker exec -it <container> s4`` and configure via the TUI.

- Non-empty CMD (e.g. ``docker run image s4``): exec the CMD directly.
  Used for interactive TUI, one-off ``s4 mcp`` / ``s4 build`` runs, or
  a debugging ``bash`` shell.

PID 1 in this container is dumb-init → this script → children. dumb-init
handles zombie reaping; we forward SIGTERM/SIGINT to the children and
propagate the first non-zero exit so ``docker restart`` is enough to
recover from a bad config.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_ROOT = Path("/app")
SETTINGS_PATH = APP_ROOT / "data" / "settings.yaml"

# Two SHA memories in the persistent data volume:
#   - TARGET_SHA_FILE:      what the user pinned via `s4 update` (may be
#                           newer, older, or on a different branch than
#                           what the image ships with)
#   - IMAGE_BASELINE_FILE:  the S4_IMAGE_COMMIT the entrypoint saw on
#                           its last start. Compared with the current
#                           S4_IMAGE_COMMIT (baked in at image build)
#                           to detect a `docker pull`.
TARGET_SHA_FILE = APP_ROOT / "data" / "s4-target-sha"
IMAGE_BASELINE_FILE = APP_ROOT / "data" / "s4-image-baseline"

# Baked into the image at build time via ``--build-arg IMAGE_COMMIT=<sha>``.
# Empty for local builds without the arg — pin-drop logic then does not fire.
IMAGE_COMMIT = os.environ.get("S4_IMAGE_COMMIT", "").strip()


def _log(msg: str) -> None:
    sys.stderr.write(f"[entrypoint] {msg}\n")
    sys.stderr.flush()


def _git(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run git inside /app with a timeout. Never raises — inspect returncode."""
    return subprocess.run(
        ["git", "-C", str(APP_ROOT), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _read_shafile(path: Path) -> str:
    """Return the trimmed content of a SHA file, or empty string on any failure."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_shafile(path: Path, sha: str) -> None:
    """Persist a SHA to the volume; log a warning but never raise on failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sha + "\n", encoding="utf-8")
    except OSError as e:
        _log(f"[warn] cannot write {path.name}: {e}")


def _handle_image_change() -> None:
    """Drop the user's pin when the container image itself was updated.

    Compares the image's baked-in ``S4_IMAGE_COMMIT`` with the baseline
    stored in the data volume from the previous start:

      - Same or unknown       → no-op (either running the same image, or
                                built locally without --build-arg)
      - Differ, no pin        → nothing to drop, just refresh baseline
      - Differ, pin present   → user did ``docker pull`` to a newer image;
                                the old pin is by definition stale — drop
                                it so the new image state wins

    Baseline is always refreshed to the current IMAGE_COMMIT afterwards.
    """
    if not IMAGE_COMMIT:
        return  # Local build without --build-arg, can't detect changes.
    baseline = _read_shafile(IMAGE_BASELINE_FILE)
    if baseline and baseline != IMAGE_COMMIT:
        if TARGET_SHA_FILE.exists():
            _log(
                f"image updated ({baseline[:12]} → {IMAGE_COMMIT[:12]}) — "
                "dropping user pin"
            )
            try:
                TARGET_SHA_FILE.unlink()
            except OSError as e:
                _log(f"[warn] cannot drop stale pin: {e}")
        else:
            _log(f"image updated ({baseline[:12]} → {IMAGE_COMMIT[:12]})")
    _write_shafile(IMAGE_BASELINE_FILE, IMAGE_COMMIT)


def _sync_to_target_sha() -> None:
    """Reset /app to the SHA the user pinned via ``./s4 update``.

    Docker's writable layer resets on ``docker rm && docker run``, so any
    in-container update would be lost. To keep it persistent across
    container recreations, the s4 update path writes the post-update HEAD
    to ``/app/data/s4-target-sha`` (in the volume). On start:

      - No pin file             → nothing to do, run image state
      - Pin == HEAD             → already there, nothing to do
      - Pin resolves to a commit → reset --hard to it (respects the user's
        (locally or via fetch)     explicit choice, including downgrades
                                   and sideways switches)
      - Pin no longer resolves  → likely orphaned by a force-push, drop
        anywhere                   the pin as unrecoverable

    Bewusst KEIN ancestor check — der Pin ist die User-Absicht und
    gewinnt, egal ob das Image weiter oder woanders ist. Image-Updates
    (docker pull) werden vorher in :func:`_handle_image_change` behandelt,
    das den Pin dann als überholt einstuft und löscht.
    """
    if not TARGET_SHA_FILE.exists():
        return
    target = _read_shafile(TARGET_SHA_FILE)
    if not target:
        return
    if not (APP_ROOT / ".git").exists():
        _log("pin present but /app has no .git — skipping sync")
        return

    head = _git("rev-parse", "HEAD", timeout=10)
    if head.returncode != 0:
        _log(f"[warn] cannot read HEAD ({head.stderr.strip()}) — running image state")
        return
    current = head.stdout.strip()
    if current == target:
        return

    # Try to fetch — target may be a commit that landed AFTER the image
    # was built. Fetch failure is not fatal: target might already be in
    # the image's shipped .git.
    fetch = _git("fetch", "--prune", "--tags", timeout=120)
    if fetch.returncode != 0:
        _log(f"[warn] fetch failed ({fetch.stderr.strip()}) — will try local objects only")

    # Does the target commit exist locally (either from image or fetch)?
    exists = _git("cat-file", "-e", f"{target}^{{commit}}", timeout=10)
    if exists.returncode != 0:
        _log(
            f"[warn] pinned commit {target[:12]} not resolvable "
            "(likely orphaned by a force-push) — dropping stale pin"
        )
        try:
            TARGET_SHA_FILE.unlink()
        except OSError:
            pass
        return

    _log(f"pin {target[:12]} != HEAD {current[:12]} — syncing")
    reset = _git("reset", "--hard", target, timeout=30)
    if reset.returncode != 0:
        _log(f"[warn] reset failed ({reset.stderr.strip()}) — running image state")
        return
    _log(f"synced to {target[:12]}")


def _load_settings():
    """Load Settings, seeding defaults with both services enabled on first run."""
    sys.path.insert(0, str(APP_ROOT))
    from core.settings import Settings
    if not SETTINGS_PATH.exists():
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        s = Settings()
        s.web_enabled = True
        s.mcp_enabled = True
        s.save(SETTINGS_PATH)
        _log(f"first run — wrote defaults to {SETTINGS_PATH}")
        return s
    return Settings.load(SETTINGS_PATH)


def _spawn_web(settings) -> subprocess.Popen | None:
    if not settings.web_enabled:
        _log("web: disabled in settings")
        return None
    from core.web_service import _build_server_code
    _wildcards = ("0.0.0.0", "::", "::/0")
    host = settings.web_host or "0.0.0.0"
    port = settings.web_port or 8080
    public_host = settings.web_dns_name or (
        "localhost" if host in _wildcards else host
    )
    server_code = _build_server_code(
        python=sys.executable,
        app_py=str(APP_ROOT / "app.py"),
        host=host, port=port,
        public_host=public_host,
        templates=str(APP_ROOT / "ui" / "web" / "templates"),
        ssl=settings.web_ssl,
        ssl_cert=settings.web_ssl_cert,
        ssl_key=settings.web_ssl_key,
        auth_user=settings.web_auth_user,
        auth_password=settings.web_auth_password,
        theme=settings.theme,
    )
    _log(f"web: starting on {host}:{port}")
    return subprocess.Popen(
        [sys.executable, "-c", server_code],
        cwd=str(APP_ROOT),
    )


def _spawn_mcp(settings) -> subprocess.Popen | None:
    if not settings.mcp_enabled:
        _log("mcp: disabled in settings")
        return None
    host = settings.mcp_host or "0.0.0.0"
    port = settings.mcp_port or 8181
    transport = settings.mcp_transport if settings.mcp_transport_explicit else "http"
    args = [
        sys.executable, "-m", "mcp_server",
        "--transport", transport,
        "--host", host,
        "--port", str(port),
    ]
    if settings.mcp_token:
        args += ["--token", settings.mcp_token]
    if settings.mcp_allow_actions:
        args.append("--allow-actions")
    if settings.mcp_ssl and settings.mcp_ssl_cert and settings.mcp_ssl_key:
        args += [
            "--ssl-cert", settings.mcp_ssl_cert,
            "--ssl-key", settings.mcp_ssl_key,
        ]
    if settings.mcp_root_path:
        args += ["--root-path", settings.mcp_root_path]
    _log(f"mcp: starting on {host}:{port} transport={transport}")
    return subprocess.Popen(args, cwd=str(APP_ROOT))


def _maybe_drop_root() -> None:
    """Drop from root to the volume-owner's uid if a bind mount was used.

    When the user runs the container with ``-v ~/s4-data:/app/data``, the
    mount point inherits the host user's uid/gid. We detect that (owner
    != 0) and:

      1. ``chown -R /app`` to that uid so git operations, s4 update, etc.
         can write.
      2. Re-exec ourselves via ``gosu`` as that uid so every subsequent
         process (web, mcp, TUI, subshells) creates files owned by the
         user on the host — no more "root owns my ~/s4-data" surprise.

    Skipped when:
      - We're already non-root (user passed ``--user`` explicitly)
      - ``/app/data`` is root-owned (Named Volume or no ``-v`` at all —
        keep the legacy root-in-container behaviour)
      - ``S4_KEEP_ROOT=1`` (explicit opt-out)
    """
    if os.geteuid() != 0:
        return
    if os.environ.get("S4_DROP_ROOT_DONE") == "1":
        return  # already exec'd once, avoid loop
    if os.environ.get("S4_KEEP_ROOT") == "1":
        return

    data = APP_ROOT / "data"
    try:
        st = data.stat()
    except FileNotFoundError:
        data.mkdir(parents=True, exist_ok=True)
        st = data.stat()

    uid, gid = st.st_uid, st.st_gid
    if uid == 0 and gid == 0:
        return  # no host user detected on the mount

    _log(f"data volume owned by uid={uid} gid={gid} — dropping root")
    try:
        if APP_ROOT.stat().st_uid != uid:
            subprocess.run(
                ["chown", "-R", f"{uid}:{gid}", str(APP_ROOT)],
                check=True, timeout=120,
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        _log(f"[warn] chown /app failed: {e} — proceeding as root")
        return

    os.environ["S4_DROP_ROOT_DONE"] = "1"
    try:
        os.execvp(
            "gosu",
            ["gosu", f"{uid}:{gid}", sys.executable, str(Path(__file__).resolve())]
            + sys.argv[1:],
        )
    except FileNotFoundError:
        _log("[warn] gosu not installed — proceeding as root")
        os.environ.pop("S4_DROP_ROOT_DONE", None)


def _pass_through() -> int:
    """Exec whatever CMD the user supplied verbatim."""
    args = sys.argv[1:]
    _log(f"exec: {' '.join(args)}")
    try:
        os.execvp(args[0], args)
    except FileNotFoundError:
        _log(f"command not found: {args[0]}")
        return 127
    return 1  # unreachable


def _launcher() -> int:
    _handle_image_change()
    _sync_to_target_sha()
    settings = _load_settings()
    procs: list[subprocess.Popen] = []
    for spawn in (_spawn_web, _spawn_mcp):
        p = spawn(settings)
        if p is not None:
            procs.append(p)

    if not procs:
        _log("both services disabled — sleeping (docker exec -it <c> s4 to configure)")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        signal.pause()
        return 0

    def _terminate(*_):
        for p in procs:
            if p.poll() is None:
                p.terminate()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(p.poll() is not None for p in procs):
                return
            time.sleep(0.1)
        for p in procs:
            if p.poll() is None:
                p.kill()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    # Wait for any child to exit, then tear down the rest and propagate.
    while True:
        for p in procs:
            rc = p.poll()
            if rc is not None:
                _log(f"child pid={p.pid} exited rc={rc} — shutting down siblings")
                _terminate()
                return rc
        time.sleep(1)


def main() -> int:
    _maybe_drop_root()
    if len(sys.argv) > 1:
        return _pass_through()
    return _launcher()


if __name__ == "__main__":
    sys.exit(main())
