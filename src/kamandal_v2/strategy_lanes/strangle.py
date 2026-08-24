"""CSA short-strangle lifecycle proposals."""

from __future__ import annotations

from typing import Any, Mapping

from kamandal_v2.strategy_lanes.lane_common import lifecycle_number, lifecycle_value, nested_number, propose_action, sheet_number
from kamandal_v2.strategy_lanes.models import ActionType, CsaAction, LifecycleState
from kamandal_v2.strategy_lanes.policy import CsaPolicy
from kamandal_v2.strategy_lanes.tickets import mixed_ticket
from kamandal_v2.domain.models import OptionLeg
from kamandal_v2.strategy_lanes.models import StrategyTicket
from kamandal_v2.strategy_engine.lifecycle import validate_strangle_replacement


def propose_strangle_actions(
    lifecycle: LifecycleState,
    policy: CsaPolicy,
    context: Mapping[str, Any],
    *,
    proposed_at: str,
) -> tuple[CsaAction, ...]:
    actions: list[CsaAction] = []
    if bool(context.get("working_order_conflict")):
        actions.append(propose_action(lifecycle, ActionType.BLOCK, "working_order_conflict", arbiter_class="working_order_conflict", proposed_at=proposed_at))
    if not bool(context.get("ownership_clear", False)):
        actions.append(propose_action(lifecycle, ActionType.BLOCK, "ownership_ambiguous", arbiter_class="ownership_ambiguity", proposed_at=proposed_at))
    if bool(context.get("hard_emergency")):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "hard_emergency", arbiter_class="hard_emergency", proposed_at=proposed_at))
    if bool(context.get("event_exit_due")):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "mandatory_event_exit", arbiter_class="mandatory_event_exit", proposed_at=proposed_at))

    profit_pct = _context_number(context, "profit_pct")
    if profit_pct >= sheet_number(policy, "profit_target_pct"):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "profit_target", arbiter_class="executable_profit", proposed_at=proposed_at))
    dte = _context_number(context, "dte")
    time_exit_due = dte <= sheet_number(policy, "exit_dte_min")
    if time_exit_due:
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "time_exit", arbiter_class="time_decision", proposed_at=proposed_at))
    elif bool(context.get("half_time_exit_due")):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "half_time_exit", arbiter_class="time_decision", proposed_at=proposed_at))

    if policy.resolved_fields.get("loss_close_multiple") not in (None, ""):
        close_multiple = sheet_number(policy, "loss_close_multiple")
    else:
        loss_stages = lifecycle_value(policy, "loss_stages")
        close_multiple = nested_number(loss_stages, "close_multiple", prefix="lifecycle.loss_stages")
    if _context_number(context, "loss_multiple") >= close_multiple:
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "loss_stage_close", arbiter_class="adverse_price_loss", proposed_at=proposed_at))

    if _tested_side_confirmed(policy, context) and _adjustment_available(policy, context):
        roll = lifecycle_value(policy, "roll")
        roll_credit = _context_number(context, "same_expiry_roll_credit")
        if isinstance(roll, dict) and roll.get("min_credit") not in (None, "") and roll_credit >= nested_number(roll, "min_credit", prefix="lifecycle.roll"):
            actions.append(
                propose_action(
                    lifecycle,
                    ActionType.ADJUST,
                    "tested_side_confirmed",
                    arbiter_class="lane_adjustment",
                    proposed_at=proposed_at,
                    payload={
                        "adjustment_kind": "untested_side_same_expiry_credit_roll",
                        "tested_side": str(context.get("tested_side") or ""),
                        "breached_strike": context.get("breached_strike"),
                        "episode_id": str(context.get("strangle_episode_id") or ""),
                    },
                )
            )
    actions.append(propose_action(lifecycle, ActionType.HOLD, "no_higher_precedence_action", arbiter_class="hold", proposed_at=proposed_at))
    return tuple(actions)


def build_strangle_adjustment_ticket(
    lifecycle: LifecycleState,
    action: CsaAction,
    policy: CsaPolicy,
    *,
    underlying: str,
    close_legs: tuple[OptionLeg, ...],
    open_legs: tuple[OptionLeg, ...],
    created_at: str,
    limit_price: float,
) -> StrategyTicket:
    if action.action_type is not ActionType.ADJUST:
        raise ValueError("strangle adjustment ticket requires an adjustment action")
    ticket = mixed_ticket(
        action,
        policy,
        underlying=underlying,
        close_legs=close_legs,
        open_legs=open_legs,
        created_at=created_at,
        limit_price=limit_price,
        lifecycle=lifecycle,
    )
    validate_strangle_replacement(
        lifecycle,
        ticket.legs,
        tested_side=str(action.payload.get("tested_side") or ""),
        minimum_credit=float(((policy.management.get("lifecycle") or {}).get("roll") or {}).get("min_credit") or 0.10),
        net_credit=limit_price,
    )
    return ticket


def _tested_side_confirmed(policy: CsaPolicy, context: Mapping[str, Any]) -> bool:
    if "strangle_episode_eligible" in context and not bool(context.get("strangle_episode_eligible")):
        return False
    return (
        bool(context.get("tested_side"))
        and bool(context.get("cooldown_elapsed"))
        and _context_number(context, "tested_side_confirmations") >= _policy_number(
            policy,
            "tested_side_confirmations",
            lifecycle_fallback="tested_side_confirmation",
        )
    )


def _adjustment_available(policy: CsaPolicy, context: Mapping[str, Any]) -> bool:
    return _context_number(context, "adjustment_count") < _policy_number(
        policy,
        "filled_side_adjustment_limit",
        lifecycle_fallback="adjustment_limit",
    )


def _policy_number(policy: CsaPolicy, field: str, *, lifecycle_fallback: str) -> float:
    if policy.resolved_fields.get(field) not in (None, ""):
        return sheet_number(policy, field)
    return lifecycle_number(policy, lifecycle_fallback)


def _context_number(context: Mapping[str, Any], key: str) -> float:
    raw = context.get(key)
    if isinstance(raw, bool) or raw in (None, ""):
        raise ValueError(f"strangle context missing numeric {key}")
    return float(raw)
