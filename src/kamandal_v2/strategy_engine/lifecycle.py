"""Canonical, restart-safe lifecycle rules shared by strategy capabilities.

This module deliberately has no market, broker, Sheet, or database dependency.
It validates the state transition which an adapter may persist only after a
complete package fill.  The existing CSA store serializes ``LifecycleState``,
so these helpers make the target rules available without an unsafe runtime
migration.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from kamandal_v2.strategy_lanes.models import LaneId, LegEffect, LegSide, LifecycleState, TicketLeg


def freeze_lifecycle_policy(
    lifecycle: LifecycleState,
    *,
    compiled_policy: dict[str, Any],
    policy_at_adoption: bool = False,
) -> LifecycleState:
    """Attach the complete policy snapshot exactly once.

    An open trade continues under the policy that selected it.  Later Sheet
    changes therefore cannot silently rewrite management of an active trade.
    """
    metadata = dict(lifecycle.metadata)
    existing = metadata.get("compiled_management_policy")
    if existing is not None and existing != compiled_policy:
        raise ValueError("open lifecycle policy is immutable")
    metadata["compiled_management_policy"] = dict(compiled_policy)
    metadata["policy_at_adoption"] = bool(policy_at_adoption)
    return replace(lifecycle, metadata=metadata)


def observe_strangle_episode(
    lifecycle: LifecycleState,
    *,
    tested_side: str,
    breached_strike: float | None,
    required_confirmations: int,
    rearm_inside_confirmations: int = 2,
) -> LifecycleState:
    """Persist the same-side test episode before action arbitration.

    A repeated breach of one exact strike increments confirmation.  Inside
    observations reset the run and two consecutive inside observations re-arm
    a consumed episode.  A side or breached-strike change starts a distinct
    episode, which is independently eligible after confirmation.
    """
    if lifecycle.lane is not LaneId.SHORT_STRANGLE:
        return lifecycle
    if required_confirmations < 1 or rearm_inside_confirmations < 1:
        raise ValueError("strangle episode confirmations must be positive")
    side = str(tested_side or "").strip().lower()
    if side not in {"", "put", "call"}:
        raise ValueError("tested_side must be put, call, or empty")
    metadata = dict(lifecycle.metadata)
    episode = dict(metadata.get("strangle_test_episode") or {})
    if not side:
        inside = int(episode.get("inside_observations") or 0) + 1
        episode.update({"inside_observations": inside, "confirmations": 0})
        if inside >= rearm_inside_confirmations:
            episode["consumed"] = False
        metadata["strangle_test_episode"] = episode
        return replace(lifecycle, metadata=metadata)

    strike = float(breached_strike) if breached_strike is not None else None
    same = episode.get("tested_side") == side and episode.get("breached_strike") == strike
    if same:
        episode["confirmations"] = int(episode.get("confirmations") or 0) + 1
    else:
        episode = {
            "episode_id": _episode_id(side, strike, lifecycle.version),
            "tested_side": side,
            "breached_strike": strike,
            "confirmations": 1,
            "inside_observations": 0,
            "consumed": False,
        }
    episode["required_confirmations"] = required_confirmations
    metadata["strangle_test_episode"] = episode
    return replace(lifecycle, metadata=metadata)


def strangle_adjustment_eligible(lifecycle: LifecycleState) -> bool:
    episode = dict(lifecycle.metadata.get("strangle_test_episode") or {})
    return bool(episode.get("tested_side")) and not bool(episode.get("consumed")) and int(episode.get("confirmations") or 0) >= int(episode.get("required_confirmations") or 0)


def validate_strangle_replacement(
    lifecycle: LifecycleState,
    legs: Iterable[TicketLeg],
    *,
    tested_side: str,
    minimum_credit: float = 0.10,
    net_credit: float | None = None,
) -> None:
    """Require an atomic untested-short replacement and nothing else."""
    if lifecycle.lane is not LaneId.SHORT_STRANGLE:
        raise ValueError("strangle replacement targets a non-strangle lifecycle")
    side = str(tested_side or "").strip().lower()
    if side not in {"put", "call"}:
        raise ValueError("strangle replacement requires tested_side")
    if int(lifecycle.metadata.get("adjustment_count") or 0) >= 2:
        raise ValueError("strangle replacement limit reached")
    episode = dict(lifecycle.metadata.get("strangle_test_episode") or {})
    if episode and (episode.get("tested_side") != side or not strangle_adjustment_eligible(lifecycle)):
        raise ValueError("strangle replacement is not eligible for the persisted tested-side episode")
    if net_credit is not None and float(net_credit) < float(minimum_credit):
        raise ValueError("strangle replacement credit is below minimum")
    active = _active_shorts(lifecycle)
    if len(active) != 2 or {str(item.get("role")) for item in active} != {"short_put", "short_call"}:
        raise ValueError("strangle lifecycle must contain exactly short_put and short_call before replacement")
    old_role = "short_call" if side == "put" else "short_put"
    tested_role = "short_put" if side == "put" else "short_call"
    old = next(item for item in active if item["role"] == old_role)
    tested = next(item for item in active if item["role"] == tested_role)
    ticket_legs = tuple(legs)
    closes = [leg for leg in ticket_legs if leg.effect is LegEffect.CLOSE]
    opens = [leg for leg in ticket_legs if leg.effect is LegEffect.OPEN]
    if len(closes) != 1 or len(opens) != 1:
        raise ValueError("strangle replacement must contain exactly one close and one open leg")
    close, opening = closes[0], opens[0]
    if close.role != old_role or opening.role != old_role:
        raise ValueError("strangle replacement must target only the current untested short")
    if close.side is not LegSide.BUY or opening.side is not LegSide.SELL:
        raise ValueError("strangle replacement requires BUY/CLOSE then SELL/OPEN")
    for key, ticket_value, active_value in (
        ("option_type", close.option_type, old["option_type"]),
        ("expiration", close.expiration, old["expiration"]),
        ("quantity", close.quantity, old["quantity"]),
        ("strike", close.strike, old["strike"]),
    ):
        if str(ticket_value) != str(active_value):
            raise ValueError(f"strangle replacement close does not match active {key}")
    for key, ticket_value, active_value in (
        ("option_type", opening.option_type, old["option_type"]),
        ("expiration", opening.expiration, old["expiration"]),
        ("quantity", opening.quantity, old["quantity"]),
    ):
        if str(ticket_value) != str(active_value):
            raise ValueError(f"strangle replacement open does not preserve {key}")
    if side == "put":
        inward = float(tested["strike"]) < opening.strike < float(old["strike"])
    else:
        inward = float(old["strike"]) < opening.strike < float(tested["strike"])
    if not inward:
        raise ValueError("strangle replacement strike must be strictly inward and non-crossing")


def finalize_strangle_replacement(lifecycle: LifecycleState, *, filled_at: str) -> LifecycleState:
    """Record economics only after a complete validated replacement package fills."""
    metadata = dict(lifecycle.metadata)
    episode = dict(metadata.get("strangle_test_episode") or {})
    if not episode.get("tested_side"):
        raise ValueError("filled strangle replacement has no persisted tested-side episode")
    episode["consumed"] = True
    episode["filled_at"] = filled_at
    metadata["strangle_test_episode"] = episode
    metadata["adjustment_count"] = int(metadata.get("adjustment_count") or 0) + 1
    metadata["last_adjustment_at"] = filled_at
    cashflow = sum(float(item.get("amount") or 0.0) for item in lifecycle.cashflow_ledger)
    active = _active_shorts(lifecycle)
    put = next((item for item in active if item["role"] == "short_put"), None)
    call = next((item for item in active if item["role"] == "short_call"), None)
    if put is None or call is None:
        raise ValueError("filled strangle replacement did not retain two active shorts")
    opening_credit = float(metadata.get("opening_credit") or 0.0)
    metadata["cumulative_credit"] = round(cashflow, 6)
    metadata["put_breakeven"] = round(float(put["strike"]) - cashflow, 6)
    metadata["call_breakeven"] = round(float(call["strike"]) + cashflow, 6)
    metadata["profit_target_dollars"] = round(opening_credit * 0.40, 6)
    return replace(lifecycle, metadata=metadata)


def adopt_legacy_position(payload: dict[str, Any], *, lifecycle_id: str, adopted_at: str) -> LifecycleState:
    """Map a legacy position fixture or fail before mutation with a blocker."""
    lane = LaneId(str(payload.get("lane") or ""))
    supplied_metadata = payload.get("metadata")
    metadata = dict(supplied_metadata) if isinstance(supplied_metadata, dict) else {}
    underlying = str(metadata.get("underlying") or payload.get("underlying") or "").upper()
    legs = tuple(_canonical_adopted_leg(underlying, item) for item in (payload.get("active_legs") or ()))
    if lane is LaneId.SHORT_STRANGLE:
        shorts = [item for item in legs if str(item.get("side")) == "sell"]
        roles = {str(item.get("role")) for item in shorts}
        if len(shorts) != 2 or roles != {"short_put", "short_call"}:
            raise ValueError("legacy adoption blocked: short strangle requires exactly one short_put and one short_call")
    if lane is LaneId.DIRECTIONAL_DIAGONAL:
        roles = {str(item.get("role")) for item in legs}
        if roles != {"long_far", "short_near"}:
            raise ValueError("legacy adoption blocked: directional diagonal requires a complete paired position")
    if lane is LaneId.GENERIC_CLOSE_ONLY and not legs:
        raise ValueError("legacy adoption blocked: generic close-only position requires active legs")
    policy_hash = str(payload.get("policy_hash") or "policy-at-adoption")
    compiled_policy = payload.get("compiled_management_policy")
    if not isinstance(compiled_policy, dict):
        raise ValueError("legacy adoption blocked: complete policy_at_adoption snapshot is required")
    projection_id = str(payload.get("group_id") or "")
    metadata.update(
        {
            "policy_at_adoption": True,
            "legacy_source_id": projection_id,
            "position_projection_id": projection_id,
            "compiled_management_policy": dict(compiled_policy),
        }
    )
    return LifecycleState(
        lifecycle_id=lifecycle_id,
        opportunity_id=str(payload.get("opportunity_id") or f"legacy:{lifecycle_id}"),
        lane=lane,
        version=1,
        status="open",
        active_legs=legs,
        cashflow_ledger=tuple(dict(item) for item in (payload.get("cashflow_ledger") or ())),
        opened_at=adopted_at,
        updated_at=adopted_at,
        policy_hash=policy_hash,
        metadata=metadata,
    )


def _canonical_adopted_leg(underlying: str, raw: Any) -> dict[str, Any]:
    leg = dict(raw)
    if leg.get("instrument_id"):
        return leg
    expiration = str(leg.get("expiration") or "").replace("-", "")
    option_type = str(leg.get("option_type") or "").lower()
    if not underlying or len(expiration) != 8 or option_type not in {"call", "put"}:
        raise ValueError("legacy adoption blocked: option leg lacks canonical contract identity")
    flag = "C" if option_type == "call" else "P"
    strike = int(round(float(leg.get("strike")) * 1000))
    leg["instrument_id"] = f"{underlying}{expiration[2:]}{flag}{strike:08d}"
    return leg


def _active_shorts(lifecycle: LifecycleState) -> list[dict[str, Any]]:
    return [dict(item) for item in lifecycle.active_legs if str(item.get("side")) == LegSide.SELL.value]


def _episode_id(side: str, strike: float | None, version: int) -> str:
    strike_part = "" if strike is None else f"{strike:.8f}"
    return f"{side}:{strike_part}:{version}"
