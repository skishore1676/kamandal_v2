"""Bounded source-priority policy for otherwise eligible planner candidates."""

from __future__ import annotations

from typing import Any

from kamandal_v2.domain.models import Candidate, Idea


SOURCE_PRIORITY_ABS_MAX = 2.0


def idea_ranking_source(idea: Idea) -> str:
    """Return the stable planner-facing source key for an ordinary Idea."""

    source = str(idea.source or "").strip().lower()
    if source == "market_scan":
        return "market_scan"
    parts = source.split(":", 2)
    if len(parts) >= 2 and parts[0] == "correspondent":
        return parts[1]
    return source or "unattributed"


def candidate_ranking_source(candidate: Candidate) -> str:
    """Read the normalized source key retained on a candidate."""

    metadata = candidate.metadata or {}
    source = str(metadata.get("ranking_source") or metadata.get("source_profile") or "").strip().lower()
    if source:
        return source
    if str(candidate.idea_id).startswith("market_scan:"):
        return "market_scan"
    return "unattributed"


def candidate_source_priority(candidate: Candidate, control: dict[str, Any]) -> float:
    """Return a small, explicit ranking component; safety gates run separately."""

    by_playbook = (((control.get("planner") or {}).get("source_priority") or {}).get(candidate.playbook_id) or {})
    if not isinstance(by_playbook, dict):
        return 0.0
    raw = by_playbook.get(candidate_ranking_source(candidate), 0.0)
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(-SOURCE_PRIORITY_ABS_MAX, min(SOURCE_PRIORITY_ABS_MAX, parsed))


def source_lane_label(candidate: Candidate) -> str:
    source = candidate_ranking_source(candidate)
    return "direct_iv" if source == "market_scan" else source
