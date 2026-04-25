"""Project paths."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
OLD_KAMANDAL_ROOT = PROJECT_ROOT.parent / "kamandal"


def resolve_path(raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()

