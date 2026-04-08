#!/usr/bin/env python3
"""SimpleBuild4 (s4) — Cross-compilation build system for oscam."""

import sys
import os

MIN_PYTHON = (3, 9)
if sys.version_info < MIN_PYTHON:
    sys.exit(f"s4 requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+, found {sys.version}")

# ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def cli_build(args: list[str]) -> int:
    """CLI build: ./s4 build <target> [<target>...] [--make|--cmake] [USE_SSL USE_PCSC_off ...]

    Multiple targets are built sequentially in the order they were
    given on the command line. --make / --cmake (if supplied) override
    the build system for ALL targets. Anything else not matching a
    known target name is forwarded as a build override.
    """
    if not args:
        print("Usage: ./s4 build <target> [<target>...] [--make|--cmake] [USE_SSL USE_PCSC_off ...]")
        return 1

    from pathlib import Path
    from core.paths import Paths
    from core.settings import Settings
    from core.target import Target

    paths = Paths.detect()
    paths.ensure_dirs()
    settings = Settings.load(paths.root / "data" / "settings.yaml")

    available = dict(Target.list_targets(paths.targets_dir))

    target_paths: list[Path] = []
    overrides: list[str] = []
    build_system = None
    unknown: list[str] = []

    for arg in args:
        if arg == "--make":
            build_system = "make"
            continue
        if arg == "--cmake":
            build_system = "cmake"
            continue
        # Try as target name from registered list
        if arg in available:
            target_paths.append(available[arg])
            continue
        # Try as direct path
        p = Path(arg)
        if p.is_file() and p.suffix in (".yaml", ".yml"):
            target_paths.append(p)
            continue
        # Try as relative to targets_dir
        p2 = paths.targets_dir / f"{arg}.yaml"
        if p2.exists():
            target_paths.append(p2)
            continue
        # Looks like a build override (e.g. USE_SSL, MODULE_GBOX_off)
        if arg.upper() == arg or arg.endswith("_off"):
            overrides.append(arg)
            continue
        unknown.append(arg)

    if unknown:
        print(f"Unknown argument(s): {', '.join(unknown)}")
        print(f"Available targets: {', '.join(sorted(available))}")
        return 1
    if not target_paths:
        print("No target specified")
        print(f"Available: {', '.join(sorted(available))}")
        return 1

    build_overrides = " ".join(overrides)

    def log(msg: str):
        import re
        plain = re.sub(r'\[/?[a-z_ #0-9]+\]', '', msg)
        print(plain)

    from core.builder import BuildEngine
    from datetime import datetime

    total = len(target_paths)
    results: list[tuple[str, bool, float]] = []
    overall_start = datetime.now()

    for idx, tpath in enumerate(target_paths, start=1):
        target = Target.load(tpath)
        if build_system:
            target.build.build_system = build_system

        print()
        print(f"══ {idx}/{total}: {target.name} ══")
        print()
        started = datetime.now()
        engine = BuildEngine(
            target=target,
            repos_dir=paths.repos_dir,
            backup_dir=paths.backup_dir,
            repos_config=paths.repos_config,
            patches_dir=paths.patches_dir,
            logs_dir=paths.logs_dir,
            binaries_dir=paths.binaries_dir,
            bin_dir=paths.bin_dir,
            toolchains_dir=paths.toolchains_dir,
            toolchains_local=paths.toolchains_local,
            toolchains_defaults=paths.toolchains_defaults,
            max_cpus=settings.max_cpus,
            compiler_opt=settings.compiler_opt,
            sign_cert=settings.sign_cert,
            sign_key=settings.sign_key,
            delete_debug=settings.delete_debug,
            save_smargo=settings.save_smargo,
            patch_webif=settings.patch_webif,
            qemu_test=settings.qemu_test,
            qemu_timeout=settings.qemu_timeout,
            qemu_expect=settings.qemu_expect,
            build_overrides=build_overrides,
            log_max_age_days=settings.log_max_age_days,
            log_max_count=settings.log_max_count,
            log_max_size_mb=settings.log_max_size_mb,
            log=log,
        )
        success = engine.build()
        elapsed = (datetime.now() - started).total_seconds()
        results.append((target.name, success, elapsed))

    print()
    print("══ Batch summary ══")
    print()
    ok = sum(1 for _, s, _ in results if s)
    fail = sum(1 for _, s, _ in results if not s)
    for name, success, elapsed in results:
        marker = "OK" if success else "FAIL"
        print(f"  [{marker}] {name}  ({elapsed:.0f}s)")
    overall = (datetime.now() - overall_start).total_seconds()
    print()
    print(f"  {ok} ok, {fail} failed — total {overall:.0f}s")
    return 0 if fail == 0 else 1


def cli_version() -> int:
    """Show version."""
    from ui.header import _get_version
    print(f"s4 {_get_version()}")
    return 0


def cli_targets() -> int:
    """List available targets."""
    from core.paths import Paths
    from core.target import Target
    paths = Paths.detect()
    targets = Target.list_targets(paths.targets_dir)
    if not targets:
        print("No targets found")
        return 0
    for name, path in targets:
        t = Target.load(path)
        tc_str = ", ".join(tc.name for tc in t.toolchains) or "-"
        print(f"  {name:<20s} {tc_str:<30s} {t.description}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "build":
            sys.exit(cli_build(sys.argv[2:]))
        elif cmd == "version":
            sys.exit(cli_version())
        elif cmd == "targets":
            sys.exit(cli_targets())
        elif cmd == "update":
            # Handled by shell wrapper
            print("Use ./s4 update (handled by shell wrapper)")
            sys.exit(0)
        else:
            # Unknown command — launch GUI
            from app import main
            main()
    else:
        # No args — launch GUI
        from app import main
        main()
