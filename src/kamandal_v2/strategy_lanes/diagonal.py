"""CSA directional-diagonal lifecycle and short-leg proposals."""

from __future__ import annotations

from typing import Any, Mapping

from kamandal_v2.strategy_lanes.lane_common import lifecycle_value, policy_bool, propose_action, sheet_number
from kamandal_v2.strategy_lanes.models import ActionType, CsaAction, LifecycleState
from kamandal_v2.strategy_lanes.policy import CsaPolicy
from kamandal_v2.domain.models import OptionLeg
from kamandal_v2.strategy_lanes.models import StrategyTicket
from kamandal_v2.strategy_lanes.tickets import mixed_ticket


def propose_diagonal_actions(
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
    if bool(context.get("hard_emergency")) or _number(context, "loss_multiple") >= sheet_number(policy, "max_loss_multiple"):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "diagonal_loss_exit", arbiter_class="hard_emergency", proposed_at=proposed_at))
    if bool(context.get("event_exit_due")):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "mandatory_event_exit", arbiter_class="mandatory_event_exit", proposed_at=proposed_at))
    if _number(context, "profit_pct") >= sheet_number(policy, "profit_target_pct"):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "profit_target", arbiter_class="executable_profit", proposed_at=proposed_at))
    if _number(context, "far_dte") <= sheet_number(policy, "exit_dte_min"):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "long_leg_time_exit", arbiter_class="time_decision", proposed_at=proposed_at))

    if not bool(context.get("short_leg_present")):
        long_only = lifecycle_value(policy, "long_only")
        requires_approval = isinstance(long_only, dict) and policy_bool(long_only.get("requires_approval"), label="lifecycle.long_only.requires_approval")
        if requires_approval and not bool(context.get("long_only_approved")):
            actions.append(propose_action(lifecycle, ActionType.BLOCK, "long_only_requires_approval", arbiter_class="routine_management", proposed_at=proposed_at))
    elif bool(context.get("short_leg_roll_due")):
        short_leg = lifecycle_value(policy, "short_leg")
        can_roll = isinstance(short_leg, dict) and policy_bool(short_leg.get("roll"), label="lifecycle.short_leg.roll")
        if can_roll:
            actions.append(
                propose_action(
                    lifecycle,
                    ActionType.ADJUST,
                    "short_leg_roll_due",
                    arbiter_class="lane_adjustment",
                    proposed_at=proposed_at,
                    payload={
                        "adjustment_kind": "short_leg_roll_or_resale",
                        "active_cost_basis": _number(context, "active_cost_basis"),
                    },
                )
            )
    actions.append(propose_action(lifecycle, ActionType.HOLD, "diagonal_hold", arbiter_class="hold", proposed_at=proposed_at))
    return tuple(actions)


def build_diagonal_short_leg_ticket(
    lifecycle: LifecycleState,
    action: CsaAction,
    policy: CsaPolicy,
    *,
    underlying: str,
    current_short: OptionLeg | None,
    replacement_short: OptionLeg,
    created_at: str,
    limit_price: float,
) -> StrategyTicket:
    if action.action_type is not ActionType.ADJUST:
        raise ValueError("diagonal short-leg ticket requires an adjustment action")
    return mixed_ticket(
        action,
        policy,
        underlying=underlying,
        close_legs=(current_short,) if current_short else (),
        open_legs=(replacement_short,),
        created_at=created_at,
        limit_price=limit_price,
        lifecycle=lifecycle,
    )


def _number(context: Mapping[str, Any], key: str) -> float:
    raw = context.get(key)
    if isinstance(raw, bool) or raw in (None, ""):
        raise ValueError(f"diagonal context missing numeric {key}")
    return float(raw)
