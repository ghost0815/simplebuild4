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
    """CLI build: ./s4 build <target> [--make|--cmake] [USE_FLAGS...]"""
    if not args:
        print("Usage: ./s4 build <target> [--make|--cmake] [USE_SSL USE_PCSC_off ...]")
        return 1

    from pathlib import Path
    from core.paths import Paths
    from core.settings import Settings
    from core.target import Target

    paths = Paths.detect()
    paths.ensure_dirs()
    settings = Settings.load(paths.root / "data" / "settings.yaml")

    # Find target
    target_name = args[0]
    target_path = None
    for name, path in Target.list_targets(paths.targets_dir):
        if name == target_name:
            target_path = path
            break

    if not target_path:
        # Try direct path
        p = Path(target_name)
        if p.exists():
            target_path = p
        else:
            p = paths.targets_dir / f"{target_name}.yaml"
            if p.exists():
                target_path = p

    if not target_path:
        print(f"Target '{target_name}' not found")
        print(f"Available: {', '.join(n for n, _ in Target.list_targets(paths.targets_dir))}")
        return 1

    target = Target.load(target_path)

    # Parse remaining args
    build_system = None
    overrides = []
    for arg in args[1:]:
        if arg == "--make":
            build_system = "make"
        elif arg == "--cmake":
            build_system = "cmake"
        else:
            overrides.append(arg)

    # Override build system if specified
    if build_system:
        target.build.build_system = build_system

    build_overrides = " ".join(overrides)

    # Log to stdout
    def log(msg: str):
        import re
        plain = re.sub(r'\[/?[a-z_ #0-9]+\]', '', msg)
        print(plain)

    # Build
    from core.builder import BuildEngine
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
        log=log,
    )

    success = engine.build()
    return 0 if success else 1


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
