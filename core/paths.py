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
            translation_dir=root / "translation",
        )

    def ensure_dirs(self) -> None:
        """Create all data directories and seed defaults if needed."""
        for d in (
            self.targets_dir, self.repos_dir, self.backup_dir,
            self.toolchains_dir,
            self.logs_dir, self.patches_dir, self.binaries_dir, self.downloads_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        # seed default targets on first run
        # Only copy files that are actual target configs (contain 'sources:' or 'toolchain:')
        if self.defaults_dir.is_dir():
            for default in self.defaults_dir.glob("*.yaml"):
                if default.name in ("toolchains.yaml", "libraries.yaml"):
                    continue
                dest = self.targets_dir / default.name
                if not dest.exists():
                    shutil.copy2(default, dest)
