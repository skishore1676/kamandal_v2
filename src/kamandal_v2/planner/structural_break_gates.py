"""Annotate Narrative Ignition ideas with Mala structural-break evidence."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import Idea
from kamandal_v2.sources.structural_breaks import (
    DEFAULT_STRUCTURAL_BREAKS_DIR,
    feed_path_for_date,
    load_structural_breaks,
    matching_structural_break,
)


def annotate_structural_breaks(ideas: list[Idea], config: dict[str, Any]) -> list[Idea]:
    cfg = config.get("structural_breaks") or {}
    enabled = bool(cfg.get("enabled", True))
    if not enabled:
        return ideas
    directory = Path(
        os.environ.get("KAMANDAL_STRUCTURAL_BREAKS_DIR")
        or cfg.get("directory")
        or DEFAULT_STRUCTURAL_BREAKS_DIR
    )
    trade_date = str(cfg.get("date") or date.today().isoformat())
    min_score = int(cfg.get("min_confluence_score") or 2)
    feed = load_structural_breaks(feed_path_for_date(directory, trade_date))
    for idea in ideas:
        if not _is_narrative_ignition_idea(idea):
            continue
        row = feed.get(idea.underlying)
        matched = matching_structural_break(row, direction=idea.direction, min_confluence_score=min_score)
        marker = "structural_break:pass" if matched else "structural_break:block"
        detail = f"{marker} date={trade_date} min_score={min_score}"
        if row:
            detail += f" score={row.get('confluence_score')} notes={row.get('notes', '')}"
        else:
            detail += " reason=feed_missing_or_symbol_absent"
        idea.notes = (idea.notes + "\n" + detail).strip()
    return ideas


def _is_narrative_ignition_idea(idea: Idea) -> bool:
    fields = {
        idea.strategy_hint.lower(),
        idea.mentioned_strategy.lower(),
        *(tag.lower() for tag in idea.thesis_tags),
    }
    return bool(fields.intersection({"narrative_ignition", "narrative ignition"}))
