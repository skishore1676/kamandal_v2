"""Load local idea files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from kamandal_v2.domain.models import Idea
from kamandal_v2.paths import resolve_path


def load_ideas(paths: list[str | Path]) -> list[Idea]:
    ideas: list[Idea] = []
    for raw_path in paths:
        path = resolve_path(raw_path)
        if path.is_dir():
            for child in sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml")) + sorted(path.glob("*.json")):
                ideas.extend(_load_file(child))
        else:
            ideas.extend(_load_file(path))
    return [idea for idea in ideas if idea.operator_status in {"approved", "pending"}]


def _load_file(path: Path) -> list[Idea]:
    if not path.exists():
        raise FileNotFoundError(f"idea file not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(payload, list):
        raw_ideas = payload
    elif isinstance(payload, dict) and isinstance(payload.get("ideas"), list):
        raw_ideas = payload["ideas"]
    elif isinstance(payload, dict):
        raw_ideas = [payload]
    else:
        raw_ideas = []
    return [Idea.from_dict(item) for item in raw_ideas if isinstance(item, dict)]

