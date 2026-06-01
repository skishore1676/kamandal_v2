"""Live entry limit-price policy."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from kamandal_v2.domain.models import Candidate
from kamandal_v2.liquidity import candidate_liquidity_metrics, nonlinear_width_improvement_pct

DEFAULT_MAX_IMPROVEMENT_BY_TIER = {
    "tight": 0.05,
    "normal": 0.10,
    "wide": 0.15,
    "very_wide": 0.25,
    "extreme": 0.35,
}


@dataclass(frozen=True)
class EntryPricingPolicy:
    mode: str = "improved_mid"
    improvement_pct_of_spread: float = 10.0
    low_oi_improvement_pct_of_spread: float = 20.0
    very_low_oi_improvement_pct_of_spread: float = 35.0
    good_oi_threshold: int = 500
    low_oi_threshold: int = 100
    min_improvement: float = 0.01
    max_improvement: float = 0.10
    max_improvement_by_liquidity_tier: dict[str, float] | None = None
    max_improvement_pct_of_premium: float = 40.0
    normal_bid_ask_pct: float = 0.30
    width_improvement_max_pct_of_spread: float = 45.0
    width_improvement_curve: float = 0.85
    apply_to_credit: bool = True
    apply_to_debit: bool = True


def entry_pricing_policy(config: dict[str, Any] | None) -> EntryPricingPolicy:
    live_cfg = ((config or {}).get("live") or {})
    raw = live_cfg.get("entry_pricing") or {}
    if not isinstance(raw, dict):
        raw = {}
    tier_caps = _tier_caps(raw.get("max_improvement_by_liquidity_tier"), fallback=_as_float(raw.get("max_improvement"), 0.10))
    return EntryPricingPolicy(
        mode=str(raw.get("mode") or "improved_mid").strip().lower(),
        improvement_pct_of_spread=_as_float(raw.get("improvement_pct_of_spread"), 10.0),
        low_oi_improvement_pct_of_spread=_as_float(raw.get("low_oi_improvement_pct_of_spread"), 20.0),
        very_low_oi_improvement_pct_of_spread=_as_float(raw.get("very_low_oi_improvement_pct_of_spread"), 35.0),
        good_oi_threshold=int(_as_float(raw.get("good_oi_threshold"), 500.0)),
        low_oi_threshold=int(_as_float(raw.get("low_oi_threshold"), 100.0)),
        min_improvement=_as_float(raw.get("min_improvement"), 0.01),
        max_improvement=_as_float(raw.get("max_improvement"), 0.10),
        max_improvement_by_liquidity_tier=tier_caps,
        max_improvement_pct_of_premium=_as_float(raw.get("max_improvement_pct_of_premium"), 40.0),
        normal_bid_ask_pct=_as_float(raw.get("normal_bid_ask_pct"), 0.30),
        width_improvement_max_pct_of_spread=_as_float(raw.get("width_improvement_max_pct_of_spread"), 45.0),
        width_improvement_curve=_as_float(raw.get("width_improvement_curve"), 0.85),
        apply_to_credit=_as_bool(raw.get("apply_to_credit"), True),
        apply_to_debit=_as_bool(raw.get("apply_to_debit"), True),
    )


def candidate_entry_limit_price(candidate: Candidate, config: dict[str, Any] | None, *, nickel: bool = False) -> str:
    """Return the configured Public limit price for an opening candidate.

    Kamandal represents net credit as positive and net debit as negative. Public
    expects multileg credit orders as negative limit prices; existing single-leg
    order semantics stay positive.
    """

    policy = entry_pricing_policy(config)
    base_price = abs(float(candidate.net_credit))
    price = _improved_price(candidate, base_price, policy)
    if nickel:
        price = _nickel_entry_price(price, candidate.net_credit)
    return _signed_public_price(candidate, price)


def entry_price_metadata(candidate: Candidate, config: dict[str, Any] | None) -> dict[str, Any]:
    policy = entry_pricing_policy(config)
    base_price = abs(float(candidate.net_credit))
    improved_price = _improved_price(candidate, base_price, policy)
    metrics = candidate_liquidity_metrics(candidate)
    cap = _max_improvement_cap(candidate, policy, metrics)
    return {
        "mode": policy.mode,
        "base_mid_limit": round(base_price, 2),
        "improved_limit": round(improved_price, 2),
        "aggregate_bid_ask_spread": round(_aggregate_spread(candidate), 4),
        "max_bid_ask_pct": metrics["max_bid_ask_pct"],
        "aggregate_spread_to_mid_pct": metrics["aggregate_spread_to_mid_pct"],
        "execution_liquidity_tier": metrics["execution_liquidity_tier"],
        "improvement": round(abs(improved_price - base_price), 4),
        "improvement_pct_of_spread": _selected_improvement_pct(candidate, policy, metrics),
        "raw_improvement_pct_of_spread": _raw_improvement_pct(candidate, policy, metrics),
        "max_improvement_cap": round(cap["effective_cap"], 4),
        "tier_max_improvement": round(cap["tier_cap"], 4),
        "premium_max_improvement": round(cap["premium_cap"], 4),
        "max_improvement_pct_of_premium": policy.max_improvement_pct_of_premium,
        "min_open_interest": _min_open_interest(candidate),
        "side": "credit" if candidate.net_credit > 0 else "debit",
    }


def _improved_price(candidate: Candidate, base_price: float, policy: EntryPricingPolicy) -> float:
    if policy.mode in {"", "mid", "none", "disabled"}:
        return _nearest_cent(base_price)
    if policy.mode not in {"improved_mid", "liquidity_adjusted_mid"}:
        return _nearest_cent(base_price)
    if candidate.net_credit > 0 and not policy.apply_to_credit:
        return _nearest_cent(base_price)
    if candidate.net_credit < 0 and not policy.apply_to_debit:
        return _nearest_cent(base_price)

    improvement = _improvement(candidate, policy)
    if candidate.net_credit > 0:
        return _favorable_cent(base_price + improvement, net_credit=candidate.net_credit)
    return _favorable_cent(max(0.01, base_price - improvement), net_credit=candidate.net_credit)


def _improvement(candidate: Candidate, policy: EntryPricingPolicy) -> float:
    metrics = candidate_liquidity_metrics(candidate)
    spread = _aggregate_spread(candidate)
    improvement = spread * max(_selected_improvement_pct(candidate, policy, metrics), 0.0) / 100.0
    improvement = max(improvement, max(policy.min_improvement, 0.0))
    cap = _max_improvement_cap(candidate, policy, metrics)["effective_cap"]
    if cap > 0:
        improvement = min(improvement, cap)
    return improvement


def _selected_improvement_pct(candidate: Candidate, policy: EntryPricingPolicy, metrics: dict[str, Any] | None = None) -> float:
    pct = _raw_improvement_pct(candidate, policy, metrics)
    cap = _max_improvement_cap(candidate, policy, metrics)["effective_cap"]
    if cap > 0:
        spread = _aggregate_spread(candidate)
        if spread > 0:
            pct = min(pct, (cap / spread) * 100.0)
    return round(pct, 4)


def _raw_improvement_pct(candidate: Candidate, policy: EntryPricingPolicy, metrics: dict[str, Any] | None = None) -> float:
    if policy.mode != "liquidity_adjusted_mid":
        return policy.improvement_pct_of_spread
    metrics = metrics or candidate_liquidity_metrics(candidate)
    min_oi = _min_open_interest(candidate)
    pct = policy.improvement_pct_of_spread
    if min_oi < policy.low_oi_threshold:
        pct = max(pct, policy.very_low_oi_improvement_pct_of_spread)
    elif min_oi < policy.good_oi_threshold:
        pct = max(pct, policy.low_oi_improvement_pct_of_spread)
    pct = max(
        pct,
        nonlinear_width_improvement_pct(
            max_bid_ask_pct=max(float(metrics["max_bid_ask_pct"]), float(metrics["aggregate_spread_to_mid_pct"])),
            base_pct=policy.improvement_pct_of_spread,
            normal_bid_ask_pct=policy.normal_bid_ask_pct,
            max_width_pct=policy.width_improvement_max_pct_of_spread,
            curve=policy.width_improvement_curve,
        ),
    )
    return round(pct, 4)


def _max_improvement_cap(candidate: Candidate, policy: EntryPricingPolicy, metrics: dict[str, Any] | None = None) -> dict[str, float]:
    metrics = metrics or candidate_liquidity_metrics(candidate)
    tier = str(metrics.get("execution_liquidity_tier") or "")
    tier_caps = policy.max_improvement_by_liquidity_tier or {}
    tier_cap = float(tier_caps.get(tier, policy.max_improvement) or 0.0)
    if tier_cap <= 0 < policy.max_improvement:
        tier_cap = policy.max_improvement
    premium = abs(float(candidate.net_credit or 0.0))
    premium_pct = max(float(policy.max_improvement_pct_of_premium or 0.0), 0.0)
    premium_cap = premium * premium_pct / 100.0 if premium > 0 and premium_pct > 0 else tier_cap
    caps = [cap for cap in (tier_cap, premium_cap) if cap > 0]
    effective_cap = min(caps) if caps else 0.0
    return {"tier_cap": tier_cap, "premium_cap": premium_cap, "effective_cap": effective_cap}


def _min_open_interest(candidate: Candidate) -> int:
    values = [int(leg.open_interest or 0) for leg in candidate.legs]
    return min(values) if values else 0


def _aggregate_spread(candidate: Candidate) -> float:
    return sum(max(float(leg.ask) - float(leg.bid), 0.0) * int(leg.quantity or 1) for leg in candidate.legs)


def _signed_public_price(candidate: Candidate, price: float) -> str:
    if len(candidate.legs) > 1 and candidate.net_credit > 0:
        return f"-{price:.2f}"
    return f"{price:.2f}"


def _nickel_entry_price(price: float, net_credit: float) -> float:
    rounding = ROUND_CEILING if net_credit > 0 else ROUND_FLOOR
    return float(_nickel_price(price, rounding=rounding))


def _nickel_price(value: float, *, rounding: str) -> Decimal:
    price = Decimal(str(value))
    nickel = Decimal("0.05")
    rounded = (price / nickel).to_integral_value(rounding=rounding) * nickel
    return rounded.quantize(Decimal("0.01"))


def _nearest_cent(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _favorable_cent(value: float, *, net_credit: float) -> float:
    rounding = ROUND_CEILING if net_credit > 0 else ROUND_FLOOR
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=rounding))


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tier_caps(raw: Any, *, fallback: float) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {tier: fallback for tier in DEFAULT_MAX_IMPROVEMENT_BY_TIER}
    caps = dict(DEFAULT_MAX_IMPROVEMENT_BY_TIER)
    for tier in DEFAULT_MAX_IMPROVEMENT_BY_TIER:
        if tier in raw:
            caps[tier] = _as_float(raw.get(tier), caps[tier])
    return caps


def _as_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
