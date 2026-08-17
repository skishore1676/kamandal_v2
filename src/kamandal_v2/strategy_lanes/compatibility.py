"""Structural compatibility checks for compiled CSA policy."""

from __future__ import annotations

from typing import Any

from kamandal_v2.strategy_lanes.models import LaneId
from kamandal_v2.strategy_lanes.policy import CsaPolicy


_LIFECYCLE_KEYS = {
    LaneId.SHORT_STRANGLE: (
        "tested_side_confirmation",
        "roll",
        "adjustment_limit",
        "inversion",
        "cooldown",
        "loss_stages",
    ),
    LaneId.CALL_VERTICAL: ("close_only",),
    LaneId.DIRECTIONAL_DIAGONAL: ("short_leg", "long_only"),
    LaneId.GENERIC_CLOSE_ONLY: ("close_only",),
    LaneId.EARNINGS_CALENDAR: ("close_only",),
}


def policy_compatibility_reasons(policy: CsaPolicy) -> tuple[str, ...]:
    reasons: list[str] = []
    score_weights = policy.management.get("score_weights")
    if not isinstance(score_weights, dict) or not score_weights:
        reasons.append("policy_score_weights_missing")
    else:
        for name, raw_weight in sorted(score_weights.items()):
            if not str(name).strip() or not _nonnegative_number(raw_weight):
                reasons.append(f"policy_score_weight_invalid:{name}")

    lifecycle = policy.management.get("lifecycle")
    if not isinstance(lifecycle, dict):
        reasons.append("policy_lifecycle_missing")
        return tuple(reasons)
    for key in _LIFECYCLE_KEYS[policy.lane]:
        if lifecycle.get(key) in (None, "", {}, []):
            reasons.append(f"policy_lifecycle_missing:{key}")
    fill = lifecycle.get("fill")
    if not isinstance(fill, dict) or any(fill.get(key) in (None, "") for key in ("max_attempts", "price_increment")):
        reasons.append("policy_lifecycle_missing:fill")
    if policy.lane is LaneId.CALL_VERTICAL and policy.source_mode.value == "portfolio_hedge":
        for key in ("portfolio_delta_trigger", "hedge_underlyings"):
            if lifecycle.get(key) in (None, "", {}, []):
                reasons.append(f"policy_lifecycle_missing:{key}")
    return tuple(reasons)


def source_structure_compatible(policy: CsaPolicy, opportunity_evidence: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    reasons = list(policy_compatibility_reasons(policy))
    if not bool(opportunity_evidence.get("source_approved", False)):
        reasons.append("source_not_approved")
    if not bool(opportunity_evidence.get("source_fresh", False)):
        reasons.append("source_stale")
    return not reasons, tuple(reasons)


def _nonnegative_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return float(value) >= 0
    except (TypeError, ValueError):
        return False
