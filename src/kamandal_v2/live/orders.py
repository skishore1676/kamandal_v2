"""Deterministic live order tickets."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from kamandal_v2.domain.models import Candidate, OptionLeg, Plan
from kamandal_v2.market.public import occ_symbol


APPROVE_LIVE = "APPROVE_LIVE"
APPROVE_LIVE_CLOSE = "APPROVE_LIVE_CLOSE"
LIVE_SUBMIT_CONFIRM = "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS"


def build_open_ticket(plan: Plan, candidate: Candidate) -> dict[str, Any]:
    return _build_ticket(
        plan=plan,
        candidate=candidate,
        intent_type="open",
        open_close_indicator="OPEN",
        legs=candidate.legs,
        limit_price=_limit_price(candidate.net_credit),
        preflight=candidate.preflight.to_dict() if candidate.preflight else None,
    )


def build_close_ticket(group: dict[str, Any], *, limit_price: float | None = None) -> dict[str, Any]:
    candidate_payload = group.get("candidate") or {}
    legs = [
        _closing_leg(leg)
        for leg in candidate_payload.get("legs", [])
    ]
    if not legs:
        raise ValueError(f"live position group {group.get('group_id')} has no legs to close")
    net_credit = float(candidate_payload.get("net_credit") or 0.0)
    close_limit = _limit_price(-(limit_price if limit_price is not None else net_credit))
    fake_candidate = _TicketCandidate(
        candidate_id=str(group.get("candidate_id") or candidate_payload.get("candidate_id") or group.get("group_id")),
        idea_id=str(group.get("idea_id") or candidate_payload.get("idea_id") or ""),
        underlying=str(group.get("underlying") or candidate_payload.get("underlying") or ""),
        playbook_id=str(group.get("playbook_id") or candidate_payload.get("playbook_id") or ""),
        structure=str(group.get("structure") or candidate_payload.get("structure") or ""),
        legs=legs,
        net_credit=-(limit_price if limit_price is not None else net_credit),
        preflight=None,
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
            "structure",
            "submit_payload",
            "legs",
            "preflight",
        )
    }
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
) -> dict[str, Any]:
    seed = json.dumps(
        {
            "intent_type": intent_type,
            "plan_id": plan.plan_id,
            "candidate_id": candidate.candidate_id,
            "legs": [_leg_ticket_payload(candidate.underlying, leg, open_close_indicator) for leg in legs],
            "limit_price": limit_price,
        },
        sort_keys=True,
    )
    order_id = str(uuid5(NAMESPACE_URL, "kamandal-live-order:" + seed))
    submit_payload = _submit_payload(order_id, candidate.underlying, legs, limit_price, open_close_indicator)
    ticket = {
        "order_id": order_id,
        "plan_id": plan.plan_id,
        "plan_rank": getattr(plan, "plan_rank", None),
        "candidate_id": candidate.candidate_id,
        "idea_id": candidate.idea_id,
        "intent_type": intent_type,
        "underlying": candidate.underlying,
        "playbook_id": candidate.playbook_id,
        "structure": candidate.structure,
        "quantity": 1,
        "limit_price": limit_price,
        "time_in_force": "DAY",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "preflight": preflight,
        "legs": [leg.to_dict() for leg in legs],
        "submit_payload": submit_payload,
    }
    ticket["ticket_hash"] = ticket_hash(ticket)
    return ticket


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


def _limit_price(net_credit: float) -> str:
    if net_credit > 0:
        return f"-{abs(net_credit):.2f}"
    return f"{abs(net_credit):.2f}"


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
    ) -> None:
        self.candidate_id = candidate_id
        self.idea_id = idea_id
        self.underlying = underlying
        self.playbook_id = playbook_id
        self.structure = structure
        self.legs = legs
        self.net_credit = net_credit
        self.preflight = preflight
