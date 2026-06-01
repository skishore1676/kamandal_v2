"""Execution-liquidity metrics shared by planning and live pricing."""

from __future__ import annotations

import math
from typing import Any


def leg_spread_pct(leg: Any) -> float:
    bid = _get_float(leg, "bid")
    ask = _get_float(leg, "ask")
    mid = _get_float(leg, "mid")
    if mid <= 0:
        mid = (bid + ask) / 2.0
    if mid <= 0:
        return math.inf
    return max(ask - bid, 0.0) / mid


def candidate_liquidity_metrics(candidate: Any) -> dict[str, Any]:
    if isinstance(candidate, dict):
        legs = list(candidate.get("legs") or [])
        raw_net_credit = candidate.get("net_credit", 0.0)
    else:
        legs = list(getattr(candidate, "legs", []) or [])
        raw_net_credit = getattr(candidate, "net_credit", 0.0)
    spreads = [leg_spread_pct(leg) for leg in legs]
    aggregate_spread = sum(max(_get_float(leg, "ask") - _get_float(leg, "bid"), 0.0) * int(_get_float(leg, "quantity", 1.0) or 1.0) for leg in legs)
    reference_mid = abs(float(raw_net_credit or 0.0))
    if reference_mid <= 0:
        reference_mid = sum(max(_get_float(leg, "mid"), 0.0) * int(_get_float(leg, "quantity", 1.0) or 1.0) for leg in legs)
    max_spread_pct = max(spreads, default=0.0)
    avg_spread_pct = sum(spreads) / max(len(spreads), 1)
    aggregate_spread_pct = aggregate_spread / max(reference_mid, 0.01)
    tier = liquidity_tier(max_spread_pct=max_spread_pct, aggregate_spread_pct=aggregate_spread_pct)
    return {
        "avg_bid_ask_pct": round(avg_spread_pct, 4),
        "max_bid_ask_pct": round(max_spread_pct, 4),
        "aggregate_bid_ask_spread": round(aggregate_spread, 4),
        "aggregate_spread_to_mid_pct": round(aggregate_spread_pct, 4),
        "min_open_interest": min((int(_get_float(leg, "open_interest", 0.0) or 0.0) for leg in legs), default=0),
        "execution_liquidity_tier": tier,
    }


def liquidity_tier(*, max_spread_pct: float, aggregate_spread_pct: float) -> str:
    pressure = max(max_spread_pct, aggregate_spread_pct)
    if pressure <= 0.15:
        return "tight"
    if pressure <= 0.30:
        return "normal"
    if pressure <= 0.75:
        return "wide"
    if pressure <= 1.50:
        return "very_wide"
    return "extreme"


def nonlinear_width_improvement_pct(
    *,
    max_bid_ask_pct: float,
    base_pct: float,
    normal_bid_ask_pct: float,
    max_width_pct: float,
    curve: float,
) -> float:
    """Convex improvement bump for wide markets.

    The output is still a percent of aggregate bid/ask spread. Once width is
    beyond the normal threshold, the improvement rises quickly and then
    asymptotically approaches the configured cap.
    """

    base_pct = max(float(base_pct), 0.0)
    normal = max(float(normal_bid_ask_pct), 0.01)
    cap = max(float(max_width_pct), base_pct)
    pressure = max(float(max_bid_ask_pct) / normal - 1.0, 0.0)
    if pressure <= 0:
        return base_pct
    bump = (cap - base_pct) * (1.0 - math.exp(-max(float(curve), 0.01) * pressure))
    return round(base_pct + bump, 4)


def bad_quote_reason(leg: Any, *, absurd_bid_ask_pct: float = 3.0) -> str:
    bid = _get_float(leg, "bid")
    ask = _get_float(leg, "ask")
    mid = _get_float(leg, "mid")
    if bid < 0 or ask <= 0:
        return "bad_quote_missing_bid_ask"
    if ask < bid:
        return "bad_quote_crossed_market"
    if mid <= 0:
        return "bad_quote_non_positive_mid"
    spread_pct = leg_spread_pct(leg)
    if spread_pct > absurd_bid_ask_pct:
        return f"bad_quote_absurd_bid_ask_pct:{spread_pct:.4f}>{absurd_bid_ask_pct}"
    return ""


def _get_float(obj: Any, key: str, default: float = 0.0) -> float:
    if isinstance(obj, dict):
        raw = obj.get(key, default)
    else:
        raw = getattr(obj, key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default
