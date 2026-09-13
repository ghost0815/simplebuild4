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
import shlex
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


def _start_sshd() -> None:
    """Start sshd once at container boot.

    The image ships openssh-server and a ``builder:builder`` login. The
    entrypoint is responsible for creating host keys on first start and
    launching sshd. sshd self-daemonizes (double-fork) so we don't hold
    on to a Popen — kernel reparents it to PID 1 (dumb-init), which
    reaps it on container shutdown.

    Silently skipped when sshd or ssh-keygen is missing (older image, a
    stripped-down variant, or a stdio-only build) — sshd is nice-to-have
    for `ssh builder@host -p 2222`, never load-bearing.
    """
    sshd = "/usr/sbin/sshd"
    keygen = "/usr/bin/ssh-keygen"
    if not (os.path.isfile(sshd) and os.access(sshd, os.X_OK)):
        return
    try:
        if not any(os.path.exists(f"/etc/ssh/ssh_host_{a}_key") for a in ("rsa", "ed25519")):
            subprocess.run(["sudo", "-n", keygen, "-A"], check=False,
                           capture_output=True, text=True, timeout=30)
        subprocess.run(["sudo", "-n", "mkdir", "-p", "/run/sshd"], check=False,
                       capture_output=True, text=True, timeout=10)
        r = subprocess.run(["sudo", "-n", sshd], check=False,
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            _log(f"[warn] sshd start failed: {r.stderr.strip() or r.stdout.strip()}")
        else:
            _log("sshd: started (login: builder / builder)")
    except FileNotFoundError:
        _log("[warn] sudo not available — sshd not started")


def _ensure_data_writable() -> None:
    """Heal ``/app/data`` ownership if we can't write to it.

    Docker copies the image-side ownership into a named volume only on
    the first mount, when the volume is still empty. Volumes populated
    by an older, root-based s4 image keep their ``root:root`` files
    forever — the switch to a non-root ``builder`` USER then sees them
    as unwritable and everything (settings.yaml, pin/baseline, download
    cache) fails with EACCES. Fresh volumes and bind mounts that are
    already uid-1000 owned skip this branch.

    Uses passwordless sudo (installed and wired in the Dockerfile). If
    sudo is missing (very old image), we log a warning and let the
    original permission errors surface — better than pretending it's
    fine.
    """
    data = APP_ROOT / "data"
    try:
        data.mkdir(exist_ok=True)
    except OSError:
        pass  # may exist but be unreadable; the chown below fixes both
    if os.access(data, os.W_OK):
        return
    uid, gid = os.getuid(), os.getgid()
    _log(f"{data} not writable — sudo chown {uid}:{gid} to heal stale ownership")
    try:
        r = subprocess.run(
            ["sudo", "-n", "chown", "-R", f"{uid}:{gid}", str(data)],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        _log("[warn] sudo not available — cannot heal /app/data ownership")
        return
    if r.returncode != 0:
        _log(f"[warn] chown failed ({r.stderr.strip()}) — writes will fail")


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


def _spawn_ttyd(settings) -> subprocess.Popen | None:
    """Spawn ttyd (WebSocket→PTY bridge) on localhost only.

    ttyd hosts a single fixed command — ``ct-ng menuconfig`` in the
    crosstool-NG builder dir. Each browser connect gets its own PTY and
    ncurses runs live; disconnect kills the subshell (and menuconfig).

    Binds ``lo`` — the port is not published; only s4's aiohttp WS-proxy
    (same container, running as builder) can reach it. All auth is done
    at the s4 layer via signed HMAC tokens; ttyd itself is unauthenticated.

    Only spawned when web is enabled and the ttyd binary is present.
    Silently skipped otherwise — the WS-proxy in web_server.py refuses
    connects with a clear error, so a missing ttyd surfaces as a UI
    notification, not a crash.
    """
    if not settings.web_enabled:
        return None
    ttyd = "/usr/local/bin/ttyd"
    if not os.access(ttyd, os.X_OK):
        _log("ttyd: /usr/local/bin/ttyd not found — menuconfig-in-browser disabled")
        return None
    # Binary and work dir (.config) follow the ctng_dir setting, mirroring
    # CrosstoolNG.resolve_binary() / resolve_work_dir(): a writable
    # --enable-local tree (<dir>/ct-ng) is its own work dir; a --prefix
    # install (<dir>/bin/ct-ng) or read-only tree uses
    # data/builders/crosstool-ng. Decided in the shell at connect time,
    # since setup may only build the binary after ttyd has started; a
    # changed ctng_dir setting needs a container restart.
    fallback = APP_ROOT / "data" / "builders" / "crosstool-ng"
    raw = (getattr(settings, "ctng_dir", "") or "").strip()
    ctng = Path(os.path.expanduser(raw)) if raw else fallback
    if not ctng.is_absolute():
        ctng = APP_ROOT / ctng
    q_fb = shlex.quote(str(fallback))
    q_ctng = shlex.quote(str(ctng))
    # Client disconnect → ttyd sends SIGHUP to its direct child (the outer
    # bash). Without the trap that only kills bash; make/mconf get orphaned
    # under dumb-init and keep running forever. The trap tears down the
    # whole process group so tab-close actually terminates menuconfig.
    inner = (
        f'trap "kill -TERM 0 2>/dev/null" EXIT HUP TERM INT; '
        f'if [ -f {q_ctng}/ct-ng ] && [ -w {q_ctng} ]; '
        f'then W={q_ctng}; CTNG={q_ctng}/ct-ng; '
        f'else W={q_fb}; CTNG={q_ctng}/bin/ct-ng; '
        f'[ -f {q_ctng}/ct-ng ] && CTNG={q_ctng}/ct-ng; fi; '
        f'mkdir -p "$W" && cd "$W" || exit 1; '
        f'"$CTNG" menuconfig'
    )
    cmd = [
        ttyd,
        "-i", "lo",           # bind loopback only — internal proxy target
        "-p", "7681",
        "-W",                 # writable client input
        "-t", "titleFixed=ct-ng menuconfig",
        "-t", "disableLeaveAlert=true",
        "bash", "-c", inner,
    ]
    _log("ttyd: starting on 127.0.0.1:7681 (loopback only)")
    return subprocess.Popen(cmd, cwd=str(APP_ROOT))


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
        language=settings.language,
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
    settings = _load_settings()
    # Critical services: their exit means the container is done.
    procs: list[subprocess.Popen] = []
    for spawn in (_spawn_web, _spawn_mcp):
        p = spawn(settings)
        if p is not None:
            procs.append(p)
    # Nice-to-haves: shut down with the container but their exit alone
    # doesn't tear the container down. ttyd (menuconfig-in-browser) is
    # here — if it crashes, s4 web/mcp keep running; menuconfig-in-web
    # just breaks until next docker restart.
    opt_procs: list[subprocess.Popen] = []
    for spawn in (_spawn_ttyd,):
        p = spawn(settings)
        if p is not None:
            opt_procs.append(p)

    if not procs:
        _log("both services disabled — sleeping (docker exec -it <c> s4 to configure)")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        signal.pause()
        return 0

    def _terminate(*_):
        all_procs = procs + opt_procs
        for p in all_procs:
            if p.poll() is None:
                p.terminate()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(p.poll() is not None for p in all_procs):
                return
            time.sleep(0.1)
        for p in all_procs:
            if p.poll() is None:
                p.kill()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    # Wait for a *critical* child to exit, then tear down the rest and
    # propagate. Optional procs (ttyd) are monitored only for logging.
    while True:
        for p in procs:
            rc = p.poll()
            if rc is not None:
                _log(f"child pid={p.pid} exited rc={rc} — shutting down siblings")
                _terminate()
                return rc
        for p in list(opt_procs):
            rc = p.poll()
            if rc is not None:
                _log(f"[warn] optional child pid={p.pid} exited rc={rc} — continuing")
                opt_procs.remove(p)
        time.sleep(1)


def main() -> int:
    _ensure_data_writable()
    _handle_image_change()
    _sync_to_target_sha()
    _start_sshd()
    if len(sys.argv) > 1:
        return _pass_through()
    return _launcher()


if __name__ == "__main__":
    sys.exit(main())
