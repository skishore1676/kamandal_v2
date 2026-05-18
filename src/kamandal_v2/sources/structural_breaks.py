"""Read Mala-owned structural-break feed files."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any


DEFAULT_STRUCTURAL_BREAKS_DIR = Path("/Users/sunny/Documents/mala_v2/data/results/structural_breaks")


def load_structural_breaks(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load a structural-break feed and return rows keyed by symbol."""

    feed_path = Path(path)
    if not feed_path.exists():
        return {}
    payload = json.loads(feed_path.read_text(encoding="utf-8"))
    rows = payload.get("symbols") or []
    return {
        str(row.get("symbol") or "").upper(): dict(row)
        for row in rows
        if isinstance(row, dict) and row.get("symbol")
    }


def feed_path_for_date(directory: str | Path, trade_date: date | str) -> Path:
    day = trade_date if isinstance(trade_date, str) else trade_date.isoformat()
    return Path(directory) / f"{day}.json"


def matching_structural_break(row: dict[str, Any] | None, *, direction: str, min_confluence_score: int = 2) -> bool:
    if not row:
        return False
    direction = direction.lower()
    if direction == "bullish":
        reclaim = bool(row.get("weekly_reclaim_long"))
        score = int(row.get("confluence_score_long") or row.get("confluence_score") or 0)
    elif direction == "bearish":
        reclaim = bool(row.get("weekly_reclaim_short"))
        score = int(row.get("confluence_score_short") or row.get("confluence_score") or 0)
    else:
        return False
    return reclaim and score >= min_confluence_score
