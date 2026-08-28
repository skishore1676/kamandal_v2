"""Broker-inert conservative fill simulation for CSA strategy tickets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from kamandal_v2.strategy_lanes.models import ActionType, LaneId, LegSide, LifecycleState, ShadowFill, StrategyTicket, stable_csa_id
from kamandal_v2.strategy_engine.lifecycle import finalize_strangle_replacement, validate_strangle_replacement


class ShadowExecutionAdapter:
    """A deliberately broker-free adapter; it accepts quotes, never a broker client."""

    def simulate_fill(
        self,
        ticket: StrategyTicket,
        quotes: Mapping[str, Mapping[str, Any]],
        fill_policy: Mapping[str, Any],
        *,
        observed_at: str,
        attempt: int,
    ) -> ShadowFill:
        max_attempts = _nonnegative_int(fill_policy, "max_attempts")
        standing_target = bool(ticket.metadata.get("resting_profit_order"))
        increment = 0.0 if standing_target else _nonnegative_number(fill_policy, "price_increment")
        if attempt < 0:
            raise ValueError("shadow fill attempt cannot be negative")
        missing = sorted(leg.instrument_id for leg in ticket.legs if leg.instrument_id not in quotes)
        stale = sorted(
            leg.instrument_id
            for leg in ticket.legs
            if leg.instrument_id in quotes and not bool(quotes[leg.instrument_id].get("fresh", False))
        )
        quote_evidence = {key: dict(value) for key, value in sorted(quotes.items()) if key in {leg.instrument_id for leg in ticket.legs}}
        if missing or stale or (attempt > max_attempts and not standing_target):
            status = "working" if standing_target else "missed"
            natural_price = 0.0
            working_price = _working_price(ticket, increment, min(attempt, max_attempts))
            filled_price = None
            quote_evidence["blocking"] = {"missing": missing, "stale": stale, "attempt_exhausted": attempt > max_attempts}
        else:
            # Compare at the same precision persisted in the receipt. Binary
            # float residue must not turn an executable 0.48 credit into a
            # false working order against 0.48000000000000004.
            natural_price = round(_natural_price(ticket, quotes), 6)
            working_price = round(_working_price(ticket, increment, attempt), 6)
            fillable = natural_price >= working_price if ticket.order_kind == "credit" else natural_price <= working_price
            status = "filled" if fillable else ("working" if standing_target else ("missed" if attempt >= max_attempts else "working"))
            filled_price = natural_price if fillable else None
        fill_id = stable_csa_id("shadow-fill", [ticket.ticket_id, attempt, observed_at, status, natural_price, working_price])
        return ShadowFill(
            fill_id=fill_id,
            ticket_id=ticket.ticket_id,
            lifecycle_id=ticket.lifecycle_id,
            status=status,
            attempt=attempt,
            natural_price=round(natural_price, 6),
            working_price=round(working_price, 6),
            filled_price=round(filled_price, 6) if filled_price is not None else None,
            filled_at=observed_at,
            quote_evidence=quote_evidence,
        )

    def adopt_fill(self, lifecycle: LifecycleState, ticket: StrategyTicket, fill: ShadowFill) -> LifecycleState:
        if fill.status != "filled" or fill.filled_price is None:
            raise ValueError("only a completed shadow fill can update a lifecycle")
        if lifecycle.lifecycle_id != ticket.lifecycle_id or lifecycle.version != ticket.lifecycle_version:
            raise ValueError("ticket does not target the current lifecycle version")
        adjustment_kind = str(ticket.metadata.get("adjustment_kind") or "")
        is_strangle_replacement = (
            lifecycle.lane is LaneId.SHORT_STRANGLE
            and adjustment_kind == "untested_side_same_expiry_credit_roll"
        )
        if is_strangle_replacement:
            validate_strangle_replacement(
                lifecycle,
                ticket.legs,
                tested_side=str(ticket.metadata.get("tested_side") or ""),
                net_credit=fill.filled_price,
            )
        active = list(lifecycle.active_legs)
        for leg in ticket.legs:
            if leg.effect.value == "close":
                active = [item for item in active if not _same_contract_role(item, leg.to_dict())]
            else:
                active.append(leg.to_dict())
        signed_cashflow = fill.filled_price if ticket.order_kind == "credit" else -fill.filled_price
        cashflows = list(lifecycle.cashflow_ledger)
        if not any(str(item.get("fill_id") or "") == fill.fill_id for item in cashflows):
            cashflows.append({
                "ticket_id": ticket.ticket_id,
                "fill_id": fill.fill_id,
                "amount": signed_cashflow,
                "filled_at": fill.filled_at,
            })
        metadata = dict(lifecycle.metadata)
        cumulative_cashflow = sum(float(item.get("amount") or 0.0) for item in cashflows)
        metadata["cumulative_cashflow"] = round(cumulative_cashflow, 6)
        metadata["active_cost_basis"] = round(max(-cumulative_cashflow, 0.0), 6)
        metadata["contract_multiplier"] = 100
        if "initial_short_contracts" not in metadata:
            metadata["initial_short_contracts"] = sum(
                int(item.get("quantity") or 0)
                for item in active
                if str(item.get("side") or "") == LegSide.SELL.value
            )
        if lifecycle.lane is LaneId.SHORT_STRANGLE and ticket.metadata.get("action_type") == ActionType.OPEN.value:
            metadata.setdefault("opening_credit", round(max(signed_cashflow, 0.0), 6))
        if adjustment_kind and not is_strangle_replacement:
            metadata["adjustment_count"] = int(metadata.get("adjustment_count") or 0) + 1
            metadata["last_adjustment_at"] = fill.filled_at
        if adjustment_kind == "duration_roll":
            metadata["duration_roll_count"] = int(metadata.get("duration_roll_count") or 0) + 1
        if adjustment_kind == "short_leg_roll_or_resale":
            metadata["front_expiry_roll_count"] = int(metadata.get("front_expiry_roll_count") or 0) + 1
        if adjustment_kind == "bounded_inversion":
            metadata["inverted"] = True
        if active:
            for key in (
                "last_marked_at",
                "mark_liquidation_price",
                "mark_pnl_price",
                "mark_profit_pct",
                "mark_source",
            ):
                metadata.pop(key, None)
        else:
            metadata["realized_pnl_price"] = round(cumulative_cashflow, 6)
            metadata["realized_pnl_usd"] = round(cumulative_cashflow * 100.0, 2)
        adopted = replace(
            lifecycle,
            version=lifecycle.version + 1,
            status="open" if active else "closed",
            active_legs=tuple(active),
            cashflow_ledger=tuple(cashflows),
            updated_at=fill.filled_at,
            metadata=metadata,
        )
        return finalize_strangle_replacement(adopted, filled_at=fill.filled_at) if is_strangle_replacement else adopted


def _same_contract_role(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_instrument = str(left.get("instrument_id") or "")
    right_instrument = str(right.get("instrument_id") or "")
    if left_instrument and right_instrument:
        return left_instrument == right_instrument and str(left.get("role") or "") == str(right.get("role") or "")
    return (
        str(left.get("expiration") or ""),
        str(left.get("option_type") or "").lower(),
        round(float(left.get("strike") or 0.0), 8),
        str(left.get("role") or ""),
    ) == (
        str(right.get("expiration") or ""),
        str(right.get("option_type") or "").lower(),
        round(float(right.get("strike") or 0.0), 8),
        str(right.get("role") or ""),
    )


def _natural_price(ticket: StrategyTicket, quotes: Mapping[str, Mapping[str, Any]]) -> float:
    signed = 0.0
    for leg in ticket.legs:
        quote = quotes[leg.instrument_id]
        raw = quote.get("bid") if leg.side is LegSide.SELL else quote.get("ask")
        if isinstance(raw, bool) or raw in (None, ""):
            raise ValueError(f"quote missing executable side for {leg.instrument_id}")
        price = float(raw) * leg.quantity
        signed += price if leg.side is LegSide.SELL else -price
    return signed if ticket.order_kind == "credit" else abs(signed)


def _working_price(ticket: StrategyTicket, increment: float, attempt: int) -> float:
    if ticket.order_kind == "credit":
        return max(ticket.limit_price - increment * attempt, 0.0)
    return ticket.limit_price + increment * attempt


def _nonnegative_number(values: Mapping[str, Any], key: str) -> float:
    raw = values.get(key)
    if isinstance(raw, bool) or raw in (None, ""):
        raise ValueError(f"fill_policy.{key} must be numeric")
    number = float(raw)
    if number < 0:
        raise ValueError(f"fill_policy.{key} must be nonnegative")
    return number


def _nonnegative_int(values: Mapping[str, Any], key: str) -> int:
    number = _nonnegative_number(values, key)
    if not number.is_integer():
        raise ValueError(f"fill_policy.{key} must be an integer")
    return int(number)
