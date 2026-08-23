"""CSA directional-diagonal lifecycle proposals.

Directional diagonals are paired positions.  They may open, hold, or close as
a complete far-long/near-short package; they never roll, resell, or manage one
leg in isolation.
"""

from __future__ import annotations

from typing import Any, Mapping

from kamandal_v2.strategy_lanes.lane_common import propose_action, sheet_number
from kamandal_v2.strategy_lanes.models import ActionType, CsaAction, LifecycleState
from kamandal_v2.strategy_lanes.policy import CsaPolicy


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
    if bool(context.get("hard_emergency")):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "hard_emergency", arbiter_class="hard_emergency", proposed_at=proposed_at))
    if _number(context, "loss_multiple") >= sheet_number(policy, "max_loss_multiple"):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "diagonal_loss_exit", arbiter_class="adverse_price_loss", proposed_at=proposed_at))
    if bool(context.get("event_exit_due")):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "mandatory_event_exit", arbiter_class="mandatory_event_exit", proposed_at=proposed_at))
    if _number(context, "profit_pct") >= sheet_number(policy, "profit_target_pct"):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "profit_target", arbiter_class="executable_profit", proposed_at=proposed_at))
    time_exit_due = _number(context, "far_dte") <= sheet_number(policy, "exit_dte_min")
    if time_exit_due:
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "long_leg_time_exit", arbiter_class="time_decision", proposed_at=proposed_at))
    elif bool(context.get("half_time_exit_due")):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "half_time_exit", arbiter_class="time_decision", proposed_at=proposed_at))

    if not bool(context.get("paired_position_complete", context.get("short_leg_present", False))):
        actions.append(propose_action(lifecycle, ActionType.BLOCK, "diagonal_pair_reconciliation_required", arbiter_class="routine_management", proposed_at=proposed_at))
    actions.append(propose_action(lifecycle, ActionType.HOLD, "diagonal_hold", arbiter_class="hold", proposed_at=proposed_at))
    return tuple(actions)


def _number(context: Mapping[str, Any], key: str) -> float:
    raw = context.get(key)
    if isinstance(raw, bool) or raw in (None, ""):
        raise ValueError(f"diagonal context missing numeric {key}")
    return float(raw)
