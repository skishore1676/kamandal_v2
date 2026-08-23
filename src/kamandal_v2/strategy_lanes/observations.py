"""Validated package economics for the canonical lifecycle manager."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from kamandal_v2.domain.models import OptionLeg
from kamandal_v2.strategy_lanes.models import LifecycleState, stable_csa_id
from kamandal_v2.strategy_lanes.policy import CsaPolicy


@dataclass(frozen=True, slots=True)
class PackageObservation:
    observation_id: str
    lifecycle_id: str
    lifecycle_version: int
    mode: str
    underlying: str
    observed_at: str
    snapshot_id: str
    snapshot_captured_at: str
    quote_source: str
    midpoint_liquidation: float
    natural_liquidation: float
    midpoint_pnl: float
    natural_pnl: float
    profit_pct: float
    loss_multiple: float
    max_leg_bid_ask_pct: float
    package_bid_ask_pct: float
    max_bid_ask_pct: float
    quote_fresh: bool
    pricing_complete: bool
    quote_actionable: bool
    quote_blockers: tuple[str, ...]
    adverse_loss_watch: bool = False
    loss_window_allowed: bool = False
    loss_confirmation_count: int = 0
    selected_action_type: str = ""
    selected_reason: str = ""
    selected_reason_class: str = ""
    execution_status: str = "observed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def observe_package(
    lifecycle: LifecycleState,
    policy: CsaPolicy,
    legs: tuple[OptionLeg, ...],
    snapshot: Any,
    *,
    observed_at: str,
    quote_max_age_minutes: int = 10,
) -> PackageObservation:
    """Build one decision mark without confusing valuation with execution."""

    blockers: list[str] = []
    midpoint_liquidation = 0.0
    natural_liquidation = 0.0
    gross_mid = 0.0
    gross_spread = 0.0
    max_leg_spread = 0.0
    for leg in legs:
        bid = float(leg.bid)
        ask = float(leg.ask)
        mid = (bid + ask) / 2.0
        label = f"{leg.role}:{leg.expiration}:{leg.strike:g}"
        if not all(math.isfinite(value) for value in (bid, ask, mid)):
            blockers.append(f"non_finite_quote:{label}")
            continue
        if bid <= 0 or ask <= 0 or ask < bid or mid <= 0:
            blockers.append(f"invalid_two_sided_quote:{label}")
        if mid <= 0:
            continue
        spread_pct = max(ask - bid, 0.0) / mid
        max_leg_spread = max(max_leg_spread, spread_pct)
        gross_mid += mid * leg.quantity
        gross_spread += (ask - bid) * leg.quantity
        if leg.side == "buy":
            midpoint_liquidation += mid * leg.quantity
            natural_liquidation += bid * leg.quantity
        else:
            midpoint_liquidation -= mid * leg.quantity
            natural_liquidation -= ask * leg.quantity

    captured_at = str(getattr(snapshot, "captured_at", "") or "")
    quote_fresh = _fresh(captured_at, observed_at, max_age_minutes=quote_max_age_minutes)
    if not quote_fresh:
        blockers.append("stale_snapshot")
    pricing_complete = len(legs) == len(lifecycle.active_legs) and not any(
        item.startswith(("non_finite_quote:", "invalid_two_sided_quote:")) for item in blockers
    )
    if not pricing_complete:
        blockers.append("incomplete_package")
    max_allowed = float(policy.resolved_fields["max_bid_ask_pct"])
    package_spread = (gross_spread / gross_mid) if gross_mid > 0 else math.inf
    if max_leg_spread > max_allowed:
        blockers.append("spread_exceeds_frozen_policy")
    if package_spread > max_allowed:
        blockers.append("package_spread_exceeds_frozen_policy")

    cumulative = float(lifecycle.metadata.get("cumulative_cashflow") or 0.0)
    entry = float(lifecycle.cashflow_ledger[0]["amount"]) if lifecycle.cashflow_ledger else cumulative
    midpoint_pnl = cumulative + midpoint_liquidation
    natural_pnl = cumulative + natural_liquidation
    profit_pct = (midpoint_pnl / abs(entry) * 100.0) if entry else 0.0
    loss_multiple = (
        abs(midpoint_liquidation) / entry
        if entry > 0
        else max(-midpoint_pnl / max(abs(entry), 0.01), 0.0)
    )
    identity = stable_csa_id(
        "package-observation",
        [
            lifecycle.lifecycle_id,
            lifecycle.version,
            observed_at,
            getattr(snapshot, "chain_snapshot_id", ""),
            round(midpoint_liquidation, 6),
            round(natural_liquidation, 6),
        ],
    )
    return PackageObservation(
        observation_id=identity,
        lifecycle_id=lifecycle.lifecycle_id,
        lifecycle_version=lifecycle.version,
        mode=str(lifecycle.metadata.get("execution_mode") or "shadow"),
        underlying=str(getattr(snapshot, "underlying", "") or lifecycle.metadata.get("underlying") or ""),
        observed_at=observed_at,
        snapshot_id=str(getattr(snapshot, "chain_snapshot_id", "") or ""),
        snapshot_captured_at=captured_at,
        quote_source=str(getattr(snapshot, "source", "") or "unknown"),
        midpoint_liquidation=round(midpoint_liquidation, 6),
        natural_liquidation=round(natural_liquidation, 6),
        midpoint_pnl=round(midpoint_pnl, 6),
        natural_pnl=round(natural_pnl, 6),
        profit_pct=round(profit_pct, 6),
        loss_multiple=round(loss_multiple, 6),
        max_leg_bid_ask_pct=round(max_leg_spread, 6),
        package_bid_ask_pct=round(package_spread, 6) if math.isfinite(package_spread) else 999999.0,
        max_bid_ask_pct=max_allowed,
        quote_fresh=quote_fresh,
        pricing_complete=pricing_complete,
        quote_actionable=not blockers,
        quote_blockers=tuple(dict.fromkeys(blockers)),
    )


def _fresh(captured_at: str, observed_at: str, *, max_age_minutes: int) -> bool:
    if not captured_at:
        return False
    try:
        captured = _parse(captured_at)
        observed = _parse(observed_at)
    except ValueError:
        return False
    age_seconds = max((observed - captured).total_seconds(), 0.0)
    return age_seconds <= max(max_age_minutes, 1) * 60


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
