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
    toolchains_dir: Path  # installed toolchains
    toolchains_cfg: Path  # toolchain config definitions
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
            toolchains_dir=data / "toolchains",
            toolchains_cfg=data / "toolchains.cfg",
            logs_dir=data / "logs",
            patches_dir=data / "patches",
            binaries_dir=data / "binaries",
            downloads_dir=data / "downloads",
            translation_dir=root / "translation",
        )

    def ensure_dirs(self) -> None:
        """Create all data directories and seed defaults if needed."""
        for d in (
            self.targets_dir, self.toolchains_dir, self.toolchains_cfg,
            self.logs_dir, self.patches_dir, self.binaries_dir, self.downloads_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        # seed default targets on first run
        if self.defaults_dir.is_dir():
            for default in self.defaults_dir.glob("*.yaml"):
                dest = self.targets_dir / default.name
                if not dest.exists():
                    shutil.copy2(default, dest)
