"""Path management for s4 directories."""

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Paths:
    """Central registry of all s4 directory paths."""

    root: Path          # project root (where s4.py lives)
    defaults_dir: Path  # shipped default configs
    targets_dir: Path   # saved target configs (yaml)
    repos_dir: Path     # checked out source repos
    repos_config: Path  # repos.yaml config file
    backup_dir: Path    # repo backups
    toolchains_dir: Path  # installed toolchains
    toolchains_defaults: Path  # shipped toolchain registry (defaults/toolchains.yaml)
    toolchains_local: Path     # user toolchain additions (data/toolchains.local.yaml)
    libraries_local: Path      # user library overrides (data/libraries.local.yaml)
    logs_dir: Path
    patches_dir: Path
    binaries_dir: Path
    downloads_dir: Path
    bin_dir: Path              # data/bin/ for custom tool symlinks (upx etc.)
    builders_dir: Path         # data/builders/ for toolchain builders (crosstool-ng etc.)
    builders_recipes: Path     # defaults/builders.yaml recipes for setting them up
    templates_defaults: Path   # defaults/templates/ shipped toolchain templates
    templates_local: Path      # data/templates/ user-added templates
    translation_dir: Path

    @classmethod
    def detect(cls) -> "Paths":
        root = Path(__file__).resolve().parent.parent
        data = root / "data"
        return cls(
            root=root,
            defaults_dir=root / "defaults",
            targets_dir=data / "targets",
            repos_dir=data / "repos",
            repos_config=data / "repos.yaml",
            backup_dir=data / "backups",
            toolchains_dir=data / "toolchains",
            toolchains_defaults=root / "defaults" / "toolchains.yaml",
            toolchains_local=data / "toolchains.local.yaml",
            libraries_local=data / "libraries.local.yaml",
            logs_dir=data / "logs",
            patches_dir=data / "patches",
            binaries_dir=data / "binaries",
            downloads_dir=data / "downloads",
            bin_dir=data / "bin",
            builders_dir=data / "builders",
            builders_recipes=root / "defaults" / "builders.yaml",
            templates_defaults=root / "defaults" / "templates",
            templates_local=data / "templates",
            translation_dir=root / "translation",
        )

    def ensure_dirs(self) -> None:
        """Create all data directories and seed defaults if needed."""
        for d in (
            self.targets_dir, self.repos_dir, self.backup_dir,
            self.toolchains_dir,
            self.logs_dir, self.patches_dir, self.binaries_dir, self.downloads_dir,
            self.bin_dir, self.builders_dir, self.templates_local,
        ):
            d.mkdir(parents=True, exist_ok=True)

        # seed default targets on first run — the shipped defaults/
        # directory holds target samples alongside registry files
        # (toolchains.yaml, libraries.yaml, builders.yaml, patches.yaml).
        # Only the target samples should end up in data/targets/.
        _not_targets = {
            "toolchains.yaml",
            "libraries.yaml",
            "builders.yaml",
            "patches.yaml",
        }
        if self.defaults_dir.is_dir():
            for default in self.defaults_dir.glob("*.yaml"):
                if default.name in _not_targets:
                    continue
                dest = self.targets_dir / default.name
                if not dest.exists():
                    shutil.copy2(default, dest)

        # seed data/patches.yaml from defaults/patches.yaml on first
        # run so the Repository screen's Download button has a real
        # (commented-out) config to start from.
        patches_default = self.defaults_dir / "patches.yaml"
        patches_dest = self.repos_config.parent / "patches.yaml"
        if patches_default.exists() and not patches_dest.exists():
            shutil.copy2(patches_default, patches_dest)
