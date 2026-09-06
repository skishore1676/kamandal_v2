"""Deterministic live order tickets."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from kamandal_v2.domain.models import Candidate, OptionLeg, Plan
from kamandal_v2.liquidity import candidate_liquidity_metrics
from kamandal_v2.market.public import occ_symbol
from kamandal_v2.strategy_lanes.models import LegEffect, StrategyTicket


APPROVE_LIVE = "APPROVE_LIVE"
APPROVE_LIVE_CLOSE = "APPROVE_LIVE_CLOSE"
REJECT_CLOSE = "REJECT_CLOSE"
LIVE_SUBMIT_CONFIRM = "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS"


def build_open_ticket(plan: Plan, candidate: Candidate) -> dict[str, Any]:
    return _build_ticket(
        plan=plan,
        candidate=candidate,
        intent_type="open",
        open_close_indicator="OPEN",
        legs=candidate.legs,
        limit_price=_accepted_preflight_limit_price(candidate) or _limit_price(candidate.net_credit),
        preflight=candidate.preflight.to_dict() if candidate.preflight else None,
    )


def build_csa_live_ticket(ticket: StrategyTicket) -> dict[str, Any]:
    """Translate one app-owned CSA strategy ticket into a broker-ready live ticket."""

    effects = {leg.effect for leg in ticket.legs}
    if effects == {LegEffect.OPEN}:
        intent_type = "open"
    elif effects == {LegEffect.CLOSE}:
        intent_type = "close"
    else:
        intent_type = "adjust"
    limit_price = _nickel_price(
        abs(float(ticket.limit_price)),
        rounding=ROUND_CEILING if ticket.order_kind == "credit" else ROUND_FLOOR,
    )
    if ticket.order_kind == "credit":
        limit_price = f"-{limit_price}"
    seed = json.dumps(
        {
            "strategy_ticket_id": ticket.ticket_id,
            "execution_venue": str(ticket.metadata.get("execution_venue") or "public_primary"),
            "intent_type": intent_type,
            "legs": [leg.to_dict() for leg in ticket.legs],
            "limit_price": limit_price,
        },
        sort_keys=True,
    )
    order_id = str(uuid5(NAMESPACE_URL, "kamandal-csa-live-order:" + seed))
    submit_payload = _csa_submit_payload(order_id, ticket, limit_price)
    live_ticket = {
        "order_id": order_id,
        "client_order_id": order_id,
        "plan_id": ticket.lifecycle_id,
        "plan_rank": 1,
        "candidate_id": ticket.ticket_id,
        "idea_id": "",
        "intent_type": intent_type,
        "underlying": ticket.underlying,
        "execution_venue": str(ticket.metadata.get("execution_venue") or "public_primary"),
        "playbook_id": str(ticket.metadata.get("playbook_id") or ""),
        "structure": ticket.lane.value,
        "quantity": 1,
        "limit_price": limit_price,
        "time_in_force": "DAY",
        "created_at": ticket.created_at,
        "preflight": None,
        "execution_quality": {},
        "legs": [leg.to_dict() for leg in ticket.legs],
        "submit_payload": submit_payload,
        "csa_strategy_ticket": ticket.to_dict(),
        "csa_policy_hash": ticket.policy_hash,
        "csa_playbook_id": str(ticket.metadata.get("playbook_id") or ""),
        "csa_stage": str(ticket.metadata.get("deployment_stage") or ""),
        "csa_lifecycle_id": ticket.lifecycle_id,
        "csa_action_type": str(ticket.metadata.get("action_type") or ""),
        "csa_action_reason": str(ticket.metadata.get("action_reason") or ticket.metadata.get("exit_reason") or ""),
        "csa_action_reason_class": str(ticket.metadata.get("action_reason_class") or ticket.metadata.get("exit_reason_class") or ""),
        "stage_authorized": True,
    }
    for key in (
        "decision_observation_id",
        "exit_reason",
        "exit_reason_class",
        "exit_midpoint_net",
        "exit_natural_net",
        "exit_profit_floor_net",
        "exit_target_net",
        "target_profit_dollars",
        "resting_profit_order",
        "resting_profit_arm_progress_pct",
        "resting_order_day",
        "execution_envelope",
    ):
        if key in ticket.metadata:
            live_ticket[key] = ticket.metadata[key]
    projection_id = str(ticket.metadata.get("position_projection_id") or "")
    if projection_id:
        live_ticket["position_projection_id"] = projection_id
        live_ticket["group_id"] = projection_id
    live_ticket["ticket_hash"] = ticket_hash(live_ticket)
    return live_ticket


def build_close_ticket(
    group: dict[str, Any],
    *,
    limit_price: float | None = None,
    close_net_credit: float | None = None,
    seed_salt: str = "",
) -> dict[str, Any]:
    candidate_payload = group.get("candidate") or {}
    legs = [
        _closing_leg(leg)
        for leg in candidate_payload.get("legs", [])
    ]
    if not legs:
        raise ValueError(f"live position group {group.get('group_id')} has no legs to close")
    if close_net_credit is None:
        close_net_credit = _close_net_credit_from_legs(legs) if limit_price is None else -(limit_price)
    close_limit = _close_limit_price(legs, close_net_credit)
    fake_candidate = _TicketCandidate(
        candidate_id=str(group.get("candidate_id") or candidate_payload.get("candidate_id") or group.get("group_id")),
        idea_id=str(group.get("idea_id") or candidate_payload.get("idea_id") or ""),
        underlying=str(group.get("underlying") or candidate_payload.get("underlying") or ""),
        playbook_id=str(group.get("playbook_id") or candidate_payload.get("playbook_id") or ""),
        structure=str(group.get("structure") or candidate_payload.get("structure") or ""),
        legs=legs,
        net_credit=close_net_credit,
        preflight=None,
        execution_venue=str(
            group.get("execution_venue")
            or candidate_payload.get("execution_venue")
            or "public_primary"
        ),
    )
    fake_plan = _TicketPlan(str(group.get("plan_id") or ""), str(group.get("group_id") or ""))
    ticket = _build_ticket(
        plan=fake_plan,
        candidate=fake_candidate,
        intent_type="close",
        open_close_indicator="CLOSE",
        legs=legs,
        limit_price=close_limit,
        preflight=None,
        seed_salt=seed_salt,
    )
    ticket["group_id"] = str(group.get("group_id") or "")
    return ticket


def ticket_hash(ticket: dict[str, Any]) -> str:
    stable = {
        key: ticket.get(key)
        for key in (
            "order_id",
            "plan_id",
            "candidate_id",
            "idea_id",
            "intent_type",
            "underlying",
            "execution_venue",
            "structure",
            "submit_payload",
            "legs",
            "preflight",
        )
    }
    for key in ("entry_risk_budget", "source_valid_until"):
        if key in ticket:
            stable[key] = ticket[key]
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def _build_ticket(
    *,
    plan: Any,
    candidate: Any,
    intent_type: str,
    open_close_indicator: str,
    legs: list[OptionLeg],
    limit_price: str,
    preflight: dict[str, Any] | None,
    seed_salt: str = "",
) -> dict[str, Any]:
    seed_payload = {
        "intent_type": intent_type,
        "plan_id": plan.plan_id,
        "candidate_id": candidate.candidate_id,
        "execution_venue": str(getattr(candidate, "execution_venue", None) or "public_primary"),
        "legs": [_leg_ticket_payload(candidate.underlying, leg, open_close_indicator) for leg in legs],
        "limit_price": limit_price,
    }
    if seed_salt:
        # Distinguish retries: without a salt, a same-priced close rebuilt on a
        # later day reuses the dead order's broker identity and never fills.
        seed_payload["seed_salt"] = seed_salt
    seed = json.dumps(seed_payload, sort_keys=True)
    order_id = str(uuid5(NAMESPACE_URL, "kamandal-live-order:" + seed))
    submit_payload = _submit_payload(order_id, candidate.underlying, legs, limit_price, open_close_indicator)
    ticket = {
        "order_id": order_id,
        "client_order_id": order_id,
        "plan_id": plan.plan_id,
        "plan_rank": getattr(plan, "plan_rank", None),
        "candidate_id": candidate.candidate_id,
        "idea_id": candidate.idea_id,
        "intent_type": intent_type,
        "underlying": candidate.underlying,
        "execution_venue": str(getattr(candidate, "execution_venue", None) or "public_primary"),
        "playbook_id": candidate.playbook_id,
        "structure": candidate.structure,
        "quantity": 1,
        "limit_price": limit_price,
        "time_in_force": "DAY",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "preflight": preflight,
        "execution_quality": _execution_quality(candidate),
        **({
            "entry_risk_budget": float(candidate.estimated_bpr),
            "source_valid_until": str((getattr(candidate, "metadata", {}) or {}).get("source_valid_until") or ""),
        } if intent_type == "open" else {}),
        "legs": [leg.to_dict() for leg in legs],
        "submit_payload": submit_payload,
    }
    ticket["ticket_hash"] = ticket_hash(ticket)
    return ticket


def _execution_quality(candidate: Any) -> dict[str, Any]:
    try:
        metrics = candidate_liquidity_metrics(candidate)
    except Exception:  # noqa: BLE001
        metrics = {}
    reasons = list(getattr(candidate, "reasons", []) or [])
    return {
        **metrics,
        "wide_bid_ask_price_through": "wide_bid_ask_price_through=true" in reasons,
        "low_oi_price_through": "low_oi_price_through=true" in reasons,
        "filter_warnings": [reason[len("filter_warning="):] for reason in reasons if str(reason).startswith("filter_warning=")],
    }


def _submit_payload(order_id: str, underlying: str, legs: list[OptionLeg], limit_price: str, open_close_indicator: str) -> dict[str, Any]:
    if len(legs) == 1:
        leg = legs[0]
        return {
            "orderId": order_id,
            "instrument": {"symbol": occ_symbol(underlying, leg), "type": "OPTION"},
            "orderSide": leg.side.upper(),
            "orderType": "LIMIT",
            "expiration": {"timeInForce": "DAY"},
            "quantity": "1",
            "limitPrice": limit_price,
            "openCloseIndicator": open_close_indicator,
        }
    return {
        "orderId": order_id,
        "quantity": "1",
        "type": "LIMIT",
        "limitPrice": limit_price,
        "expiration": {"timeInForce": "DAY"},
        "legs": [_leg_ticket_payload(underlying, leg, open_close_indicator) for leg in legs],
    }


def _leg_ticket_payload(underlying: str, leg: OptionLeg, open_close_indicator: str) -> dict[str, Any]:
    return {
        "instrument": {"symbol": occ_symbol(underlying, leg), "type": "OPTION"},
        "side": leg.side.upper(),
        "openCloseIndicator": open_close_indicator,
        "ratioQuantity": int(leg.quantity or 1),
    }


def _csa_submit_payload(order_id: str, ticket: StrategyTicket, limit_price: str) -> dict[str, Any]:
    legs = [
        {
            "instrument": {"symbol": leg.instrument_id, "type": "OPTION"},
            "side": leg.side.value.upper(),
            "openCloseIndicator": leg.effect.value.upper(),
            "ratioQuantity": int(leg.quantity),
        }
        for leg in ticket.legs
    ]
    if len(legs) == 1:
        leg = legs[0]
        return {
            "orderId": order_id,
            "instrument": leg["instrument"],
            "orderSide": leg["side"],
            "orderType": "LIMIT",
            "expiration": {"timeInForce": "DAY"},
            "quantity": str(leg["ratioQuantity"]),
            "limitPrice": limit_price,
            "openCloseIndicator": leg["openCloseIndicator"],
        }
    return {
        "orderId": order_id,
        "quantity": "1",
        "type": "LIMIT",
        "limitPrice": limit_price,
        "expiration": {"timeInForce": "DAY"},
        "legs": legs,
    }


def _limit_price(net_credit: float) -> str:
    if net_credit > 0:
        return f"-{_nickel_price(abs(net_credit), rounding=ROUND_FLOOR)}"
    return _nickel_price(abs(net_credit), rounding=ROUND_CEILING)


def _close_limit_price(legs: list[OptionLeg], net_credit: float) -> str:
    if len(legs) == 1:
        rounding = ROUND_FLOOR if net_credit > 0 else ROUND_CEILING
        return _nickel_price(abs(net_credit), rounding=rounding)
    return _limit_price(net_credit)


def _close_net_credit_from_legs(legs: list[OptionLeg]) -> float:
    total = 0.0
    for leg in legs:
        qty = int(leg.quantity or 1)
        total += (float(leg.mid) if leg.side == "sell" else -float(leg.mid)) * qty
    return total


def _accepted_preflight_limit_price(candidate: Candidate) -> str:
    if candidate.preflight is None or not candidate.preflight.ok:
        return ""
    request = (candidate.preflight.raw or {}).get("request") or {}
    value = request.get("limitPrice")
    return str(value) if value not in (None, "") else ""


def _nickel_price(value: float, *, rounding: str) -> str:
    price = Decimal(str(value))
    nickel = Decimal("0.05")
    rounded = (price / nickel).to_integral_value(rounding=rounding) * nickel
    return f"{rounded.quantize(Decimal('0.01'))}"


def _closing_leg(leg_payload: dict[str, Any]) -> OptionLeg:
    side = "buy" if str(leg_payload.get("side") or "").lower() == "sell" else "sell"
    return OptionLeg(
        role=str(leg_payload.get("role") or ""),
        side=side,
        option_type=str(leg_payload.get("option_type") or ""),
        strike=float(leg_payload.get("strike") or 0.0),
        expiration=str(leg_payload.get("expiration") or ""),
        quantity=int(leg_payload.get("quantity") or 1),
        mid=float(leg_payload.get("mid") or 0.0),
        bid=float(leg_payload.get("bid") or 0.0),
        ask=float(leg_payload.get("ask") or 0.0),
        delta=float(leg_payload.get("delta") or 0.0),
        gamma=float(leg_payload.get("gamma") or 0.0),
        theta=float(leg_payload.get("theta") or 0.0),
        vega=float(leg_payload.get("vega") or 0.0),
        open_interest=int(leg_payload.get("open_interest") or 0),
    )


class _TicketPlan:
    def __init__(self, plan_id: str, group_id: str) -> None:
        self.plan_id = plan_id or group_id
        self.plan_rank = None


class _TicketCandidate:
    def __init__(
        self,
        *,
        candidate_id: str,
        idea_id: str,
        underlying: str,
        playbook_id: str,
        structure: str,
        legs: list[OptionLeg],
        net_credit: float,
        preflight: Any,
        execution_venue: str = "public_primary",
    ) -> None:
        self.candidate_id = candidate_id
        self.idea_id = idea_id
        self.underlying = underlying
        self.playbook_id = playbook_id
        self.structure = structure
        self.legs = legs
        self.net_credit = net_credit
        self.preflight = preflight
        self.execution_venue = execution_venue
