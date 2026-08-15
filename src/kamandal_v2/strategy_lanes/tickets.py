"""Deterministic open, close, and mixed CSA strategy tickets."""

from __future__ import annotations

from collections.abc import Iterable

from kamandal_v2.domain.models import Candidate, OptionLeg
from kamandal_v2.market.public import occ_symbol
from kamandal_v2.strategy_lanes.models import (
    CsaAction,
    LaneId,
    LegEffect,
    LegSide,
    LifecycleState,
    StrategyTicket,
    TicketLeg,
    stable_csa_id,
)
from kamandal_v2.strategy_lanes.policy import CsaPolicy


def open_ticket_from_candidate(
    candidate: Candidate,
    action: CsaAction,
    policy: CsaPolicy,
    *,
    created_at: str,
    limit_price: float,
) -> StrategyTicket:
    legs = tuple(_ticket_leg(candidate.underlying, leg, LegEffect.OPEN) for leg in candidate.legs)
    return _ticket(
        action,
        policy,
        underlying=candidate.underlying,
        legs=legs,
        created_at=created_at,
        limit_price=limit_price,
    )


def mixed_ticket(
    action: CsaAction,
    policy: CsaPolicy,
    *,
    underlying: str,
    close_legs: Iterable[OptionLeg],
    open_legs: Iterable[OptionLeg],
    created_at: str,
    limit_price: float,
    lifecycle: LifecycleState | None = None,
) -> StrategyTicket:
    legs = tuple(
        [*(_ticket_leg(underlying, leg, LegEffect.CLOSE) for leg in close_legs), *(_ticket_leg(underlying, leg, LegEffect.OPEN) for leg in open_legs)]
    )
    if action.action_type.value in {"adjust", "duration_roll"}:
        if lifecycle is None:
            raise ValueError("adjustment tickets require current lifecycle state")
        _enforce_short_contract_limit(lifecycle, legs)
    return _ticket(action, policy, underlying=underlying, legs=legs, created_at=created_at, limit_price=limit_price)


def _ticket(
    action: CsaAction,
    policy: CsaPolicy,
    *,
    underlying: str,
    legs: tuple[TicketLeg, ...],
    created_at: str,
    limit_price: float,
) -> StrategyTicket:
    ticket_id = stable_csa_id(
        "ticket",
        [action.action_id, action.lifecycle_version, [leg.to_dict() for leg in legs], limit_price],
    )
    return StrategyTicket(
        ticket_id=ticket_id,
        action_id=action.action_id,
        lifecycle_id=action.lifecycle_id,
        lifecycle_version=action.lifecycle_version,
        lane=policy.lane,
        underlying=underlying,
        order_kind="credit" if limit_price >= 0 else "debit",
        limit_price=abs(float(limit_price)),
        legs=legs,
        policy_hash=policy.policy_hash,
        created_at=created_at,
        metadata={
            "execution_boundary": policy.stage.value,
            "playbook_id": policy.playbook_id,
            "deployment_stage": policy.stage.value,
            "fill_policy": dict((policy.management.get("lifecycle") or {}).get("fill") or {}),
            "action_type": action.action_type.value,
            **({"adjustment_kind": action.payload["adjustment_kind"]} if action.payload.get("adjustment_kind") else {}),
            **(
                {
                    "tested_side": str(action.payload.get("tested_side") or ""),
                    "breached_strike": action.payload.get("breached_strike"),
                    "episode_id": str(action.payload.get("episode_id") or ""),
                }
                if action.payload.get("tested_side")
                else {}
            ),
        },
    )


def _ticket_leg(underlying: str, leg: OptionLeg, effect: LegEffect) -> TicketLeg:
    side = LegSide.SELL if leg.side == "sell" else LegSide.BUY
    if effect is LegEffect.CLOSE:
        side = LegSide.BUY if side is LegSide.SELL else LegSide.SELL
    return TicketLeg(
        instrument_id=occ_symbol(underlying, leg),
        side=side,
        effect=effect,
        quantity=int(leg.quantity),
        option_type=leg.option_type,
        expiration=leg.expiration,
        strike=float(leg.strike),
        role=leg.role,
    )


def _enforce_short_contract_limit(lifecycle: LifecycleState, ticket_legs: tuple[TicketLeg, ...]) -> None:
    active_short = sum(
        int(item.get("quantity") or 0)
        for item in lifecycle.active_legs
        if str(item.get("side") or "") == LegSide.SELL.value
    )
    initial_limit = int(lifecycle.metadata.get("initial_short_contracts") or active_short)
    closed_short = sum(
        leg.quantity
        for leg in ticket_legs
        if leg.effect is LegEffect.CLOSE and leg.side is LegSide.BUY
    )
    opened_short = sum(
        leg.quantity
        for leg in ticket_legs
        if leg.effect is LegEffect.OPEN and leg.side is LegSide.SELL
    )
    after = active_short - closed_short + opened_short
    if after > initial_limit:
        raise ValueError(f"adjustment would increase short contracts above lifecycle limit: {after}>{initial_limit}")
