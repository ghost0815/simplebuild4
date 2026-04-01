"""Path management for s4 directories."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Paths:
    """Central registry of all s4 directory paths."""

    root: Path          # project root (where s4.py lives)
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
        """Create all data directories if they don't exist."""
        for f in (
            self.targets_dir, self.toolchains_dir, self.toolchains_cfg,
            self.logs_dir, self.patches_dir, self.binaries_dir, self.downloads_dir,
        ):
            f.mkdir(parents=True, exist_ok=True)
