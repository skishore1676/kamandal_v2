"""Live entry limit-price policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
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


@dataclass(frozen=True)
class EntryCampaignPolicy:
    """Opt-in midpoint-centred entry campaign controls.

    The legacy entry-pricing policy still computes the full market-sensitive
    improvement.  This policy only controls how that improvement is staged and
    how much terminal concession is permitted.
    """

    enabled: bool = False
    initial_improvement_multiplier: float = 0.50
    allowance_pct_of_midpoint: float = 5.0
    allowance_max_fraction_of_improvement: float = 0.50
    absolute_allowance_cap: float | None = None
    valid_tick: float = 0.01
    absurd_bid_ask_pct: float = 3.0


@dataclass(frozen=True)
class EntryCampaign:
    enabled: bool
    side: str
    midpoint: float
    improvement: float
    initial_improvement_multiplier: float
    allowance: float
    prices: tuple[str, ...]
    metadata: dict[str, Any]


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


def entry_campaign_policy(config: dict[str, Any] | None) -> EntryCampaignPolicy:
    live_cfg = ((config or {}).get("live") or {})
    raw_pricing = live_cfg.get("entry_pricing") or {}
    if not isinstance(raw_pricing, dict):
        raw_pricing = {}
    raw = raw_pricing.get("campaign") or {}
    if not isinstance(raw, dict):
        raw = {}
    mode = str(raw_pricing.get("mode") or "").strip().lower()
    enabled = _as_bool(raw.get("enabled"), mode in {"midpoint_campaign", "campaign"})
    absolute = _optional_positive_float(raw.get("absolute_allowance_cap"))
    return EntryCampaignPolicy(
        enabled=enabled,
        initial_improvement_multiplier=_clamp(_as_float(raw.get("initial_improvement_multiplier"), 0.50), 0.0, 1.0),
        allowance_pct_of_midpoint=max(_as_float(raw.get("allowance_pct_of_midpoint"), 5.0), 0.0),
        allowance_max_fraction_of_improvement=max(_as_float(raw.get("allowance_max_fraction_of_improvement"), 0.50), 0.0),
        absolute_allowance_cap=absolute,
        valid_tick=max(_as_float(raw.get("valid_tick"), 0.01), 0.0001),
        absurd_bid_ask_pct=max(_as_float(raw.get("absurd_bid_ask_pct"), _as_float((live_cfg.get("liquidity_policy") or {}).get("absurd_bid_ask_pct"), 3.0)), 0.0),
    )


def entry_campaign(candidate: Candidate, config: dict[str, Any] | None) -> EntryCampaign:
    """Build a frozen, bounded three-price campaign without broker effects."""

    campaign_policy = entry_campaign_policy(config)
    side = "credit" if candidate.net_credit > 0 else "debit"
    disabled = EntryCampaign(False, side, 0.0, 0.0, campaign_policy.initial_improvement_multiplier, 0.0, (), {
        "enabled": False,
        "side": side,
        "skip_reason": "campaign_disabled",
    })
    if not campaign_policy.enabled:
        return disabled

    pricing_policy = entry_pricing_policy(config)
    if pricing_policy.mode in {"midpoint_campaign", "campaign"}:
        pricing_policy = replace(pricing_policy, mode="liquidity_adjusted_mid")
    base_mid = abs(float(candidate.net_credit or 0.0))
    metrics, quote_reason = _campaign_quote_metrics(candidate, campaign_policy)
    if quote_reason:
        return _campaign_terminal(side, quote_reason, campaign_policy, base_mid)
    if base_mid <= 0 or not math.isfinite(base_mid):
        return _campaign_terminal(side, "invalid_midpoint", campaign_policy, base_mid)

    # A broker-valid midpoint must stay on the operator's side of the true
    # midpoint.  A credit rounded down (or a debit rounded up) has already
    # conceded through M before the midpoint attempt even begins.
    midpoint = (
        _tick_ceil(base_mid, campaign_policy.valid_tick)
        if side == "credit"
        else _tick_floor(base_mid, campaign_policy.valid_tick)
    )
    improvement = _improvement(candidate, pricing_policy)
    if improvement <= 0 or not math.isfinite(improvement):
        return _campaign_terminal(side, "invalid_improvement", campaign_policy, midpoint)

    economic_headroom = _campaign_economic_headroom(candidate, midpoint)
    if economic_headroom is None:
        return _campaign_terminal(side, "economic_bound_missing", campaign_policy, midpoint, improvement)
    if economic_headroom <= 0 or not math.isfinite(economic_headroom):
        return _campaign_terminal(
            side,
            "economic_headroom_exhausted",
            campaign_policy,
            midpoint,
            improvement,
            {"economic_headroom": economic_headroom},
        )
    bounds = {
        "midpoint_pct": midpoint * campaign_policy.allowance_pct_of_midpoint / 100.0,
        "improvement_fraction": improvement * campaign_policy.allowance_max_fraction_of_improvement,
        "absolute_cap": campaign_policy.absolute_allowance_cap or 0.0,
        "economic_headroom": economic_headroom,
    }
    if bounds["absolute_cap"] <= 0:
        return _campaign_terminal(side, "absolute_allowance_cap_not_configured", campaign_policy, midpoint, improvement, bounds)
    positive_bounds = {key: value for key, value in bounds.items() if value > 0 and math.isfinite(value)}
    if len(positive_bounds) != len(bounds):
        return _campaign_terminal(side, "allowance_bound_invalid", campaign_policy, midpoint, improvement, bounds)
    allowance = min(positive_bounds.values())
    allowance = _tick_floor(allowance, campaign_policy.valid_tick)
    if allowance < campaign_policy.valid_tick:
        return _campaign_terminal(side, "allowance_below_valid_tick", campaign_policy, midpoint, improvement, bounds)

    if side == "credit":
        raw_prices = (
            midpoint + (campaign_policy.initial_improvement_multiplier * improvement),
            midpoint,
            midpoint - allowance,
        )
        prices = (
            _campaign_price(_tick_ceil(raw_prices[0], campaign_policy.valid_tick), candidate),
            _campaign_price(midpoint, candidate),
            _campaign_price(_tick_ceil(raw_prices[2], campaign_policy.valid_tick), candidate),
        )
        p3_magnitude = abs(float(prices[2]))
        if p3_magnitude >= abs(float(prices[1])):
            return _campaign_terminal(side, "allowance_does_not_move_from_midpoint", campaign_policy, midpoint, improvement, bounds, allowance)
    else:
        raw_prices = (
            midpoint - (campaign_policy.initial_improvement_multiplier * improvement),
            midpoint,
            midpoint + allowance,
        )
        prices = (
            _campaign_price(_tick_floor(raw_prices[0], campaign_policy.valid_tick), candidate),
            _campaign_price(midpoint, candidate),
            _campaign_price(_tick_floor(raw_prices[2], campaign_policy.valid_tick), candidate),
        )
        p3_magnitude = abs(float(prices[2]))
        if p3_magnitude <= abs(float(prices[1])):
            return _campaign_terminal(side, "allowance_does_not_move_from_midpoint", campaign_policy, midpoint, improvement, bounds, allowance)

    binding_cap = min(positive_bounds, key=positive_bounds.get)
    metadata = {
        "enabled": True,
        "side": side,
        "midpoint": midpoint,
        "improvement": round(improvement, 6),
        "initial_improvement_multiplier": campaign_policy.initial_improvement_multiplier,
        "allowance": round(allowance, 6),
        "allowance_bounds": {key: round(value, 6) for key, value in bounds.items()},
        "allowance_binding_cap": binding_cap,
        "valid_tick": campaign_policy.valid_tick,
        "economic_bound_source": candidate.entry_economic_bound_source,
        "economic_bound": round(
            float(candidate.entry_credit_floor if side == "credit" else candidate.entry_debit_ceiling),
            6,
        ),
        "liquidity_metrics": metrics,
        "prices": list(prices),
        "skip_reason": "",
    }
    return EntryCampaign(True, side, midpoint, improvement, campaign_policy.initial_improvement_multiplier, allowance, prices, metadata)


def entry_campaign_metadata(candidate: Candidate, config: dict[str, Any] | None) -> dict[str, Any]:
    return dict(entry_campaign(candidate, config).metadata)


def candidate_entry_limit_price(candidate: Candidate, config: dict[str, Any] | None, *, nickel: bool = False) -> str:
    """Return the configured Public limit price for an opening candidate.

    Kamandal represents net credit as positive and net debit as negative. Public
    expects multileg credit orders as negative limit prices; existing single-leg
    order semantics stay positive.
    """

    campaign = entry_campaign(candidate, config)
    if campaign.enabled:
        if not campaign.prices:
            raise ValueError(f"entry campaign suppressed: {campaign.metadata.get('skip_reason') or 'invalid_campaign'}")
        price = abs(float(campaign.prices[0]))
        if nickel:
            price = _nickel_entry_price(price, candidate.net_credit)
        if len(candidate.legs) > 1 and candidate.net_credit > 0:
            return f"-{price:.2f}"
        return f"{price:.2f}"

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
    metadata = {
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
    campaign = entry_campaign(candidate, config)
    if campaign.enabled or campaign.metadata.get("skip_reason") != "campaign_disabled":
        metadata["campaign"] = campaign.metadata
    return metadata


def shadow_entry_limit_price(
    candidate: Candidate,
    config: dict[str, Any] | None,
    *,
    max_concession: float | None = None,
) -> float:
    """Freeze the low-liquidity price-through decision into a shadow ticket.

    Existing shadow fills intentionally start at the candidate midpoint.  Only
    a candidate explicitly admitted under the low-OI warning policy should ask
    for additional credit, matching the live pricing calculation without
    turning unrelated shadow evidence into a pricing-policy migration.
    """

    reasons = {str(reason) for reason in (candidate.reasons or [])}
    if candidate.structure not in {"short_strangle", "strangle"} or "low_oi_price_through=true" not in reasons:
        return float(candidate.net_credit)
    midpoint = abs(float(candidate.net_credit))
    improved = abs(float(entry_price_metadata(candidate, config)["improved_limit"]))
    if max_concession is not None:
        improved = min(improved, midpoint + max(float(max_concession), 0.0))
    return improved


def normalize_campaign_entry_metadata(
    candidate: Candidate,
    metadata: dict[str, Any],
    *,
    valid_tick: float,
) -> dict[str, Any]:
    """Freeze every campaign price to a broker-accepted tick after retry."""

    result = dict(metadata)
    campaign = dict(result.get("campaign") or {})
    raw_prices = [str(value) for value in campaign.get("prices") or []]
    if not campaign.get("enabled") or not raw_prices:
        return result
    tick = max(float(valid_tick), 0.0001)
    side = str(campaign.get("side") or ("credit" if candidate.net_credit > 0 else "debit"))
    normalized: list[str] = []
    for raw in raw_prices:
        magnitude = abs(float(raw))
        # Every normalized price must be at least as favorable as the frozen
        # cent-price campaign.  This keeps P2 on the operator's side of M and
        # prevents P3 tick rounding from widening any allowance/economic cap.
        rounding = ROUND_CEILING if side == "credit" else ROUND_FLOOR
        snapped = float((Decimal(str(magnitude)) / Decimal(str(tick))).to_integral_value(rounding=rounding) * Decimal(str(tick)))
        normalized.append(_campaign_price(snapped, candidate))
    # A tick can collapse the terminal concession into the midpoint.  Preserve
    # only distinct, monotone prices; never invent a wider economic envelope.
    compact: list[str] = []
    for price in normalized:
        if not compact or price != compact[-1]:
            compact.append(price)
    # Favorable rounding preserves the original monotone ladder by
    # construction.  Keep each distinct step: when P2 and P3 collapse to one
    # broker tick, that shared price is still the valid midpoint attempt.
    campaign["raw_prices"] = raw_prices
    campaign["prices"] = compact
    campaign["valid_tick"] = tick
    campaign["accepted_tick"] = tick
    campaign["tick_normalized"] = True
    campaign["tick_normalization_reason"] = "broker_rejected_cent_increment"
    result["campaign"] = campaign
    result["accepted_limit_price"] = compact[0] if compact else ""
    return result


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


def _campaign_quote_metrics(candidate: Candidate, policy: EntryCampaignPolicy) -> tuple[dict[str, Any], str]:
    try:
        metrics = candidate_liquidity_metrics(candidate)
    except (TypeError, ValueError, ZeroDivisionError):
        return {}, "invalid_quote_metrics"
    for reason in getattr(candidate, "reasons", []) or []:
        normalized = str(reason).strip().lower()
        if any(marker in normalized for marker in ("quote_stale", "stale_quote", "quote_unstable", "unstable_quote")):
            return metrics, "quote_stale_or_unstable"
    for leg in candidate.legs:
        values = (leg.bid, leg.ask, leg.mid)
        if any(not math.isfinite(float(value)) for value in values):
            return metrics, "invalid_quote"
        if float(leg.bid) < 0 or float(leg.ask) <= 0 or float(leg.ask) < float(leg.bid):
            return metrics, "invalid_quote"
    if float(metrics.get("max_bid_ask_pct") or 0.0) > policy.absurd_bid_ask_pct:
        return metrics, "absurdly_wide_quote"
    return metrics, ""


def _campaign_economic_headroom(candidate: Candidate, midpoint: float) -> float | None:
    """Return the bound produced at candidate construction, never a free-form reason."""

    if candidate.net_credit > 0:
        floor = candidate.entry_credit_floor
        if floor is None or not math.isfinite(float(floor)):
            return None
        return float(midpoint) - max(float(floor), 0.0)
    ceiling = candidate.entry_debit_ceiling
    if ceiling is None or not math.isfinite(float(ceiling)):
        return None
    return max(float(ceiling), 0.0) - float(midpoint)


def _campaign_terminal(
    side: str,
    reason: str,
    policy: EntryCampaignPolicy,
    midpoint: float,
    improvement: float = 0.0,
    bounds: dict[str, float] | None = None,
    allowance: float = 0.0,
) -> EntryCampaign:
    metadata = {
        "enabled": True,
        "side": side,
        "midpoint": round(midpoint, 6),
        "improvement": round(improvement, 6),
        "initial_improvement_multiplier": policy.initial_improvement_multiplier,
        "allowance": round(allowance, 6),
        "allowance_bounds": {key: round(value, 6) for key, value in (bounds or {}).items()},
        "allowance_binding_cap": "",
        "valid_tick": policy.valid_tick,
        "prices": [],
        "skip_reason": reason,
    }
    return EntryCampaign(True, side, midpoint, improvement, policy.initial_improvement_multiplier, allowance, (), metadata)


def _campaign_price(magnitude: float, candidate: Candidate) -> str:
    value = max(float(magnitude), 0.0)
    if len(candidate.legs) > 1 and candidate.net_credit > 0:
        return f"-{value:.2f}"
    return f"{value:.2f}"


def _tick_floor(value: float, tick: float) -> float:
    return float((Decimal(str(round(value, 10))) / Decimal(str(tick))).to_integral_value(rounding=ROUND_FLOOR) * Decimal(str(tick)))


def _tick_ceil(value: float, tick: float) -> float:
    return float((Decimal(str(round(value, 10))) / Decimal(str(tick))).to_integral_value(rounding=ROUND_CEILING) * Decimal(str(tick)))


def _tick_nearest(value: float, tick: float) -> float:
    return float((Decimal(str(value)) / Decimal(str(tick))).to_integral_value() * Decimal(str(tick)))


def _optional_positive_float(value: Any) -> float | None:
    parsed = _as_float(value, 0.0)
    return parsed if parsed > 0 else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))
