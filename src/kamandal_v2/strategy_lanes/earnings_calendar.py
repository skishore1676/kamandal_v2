"""CSA earnings-calendar specialization lifecycle proposals."""

from __future__ import annotations

from typing import Any, Mapping

from kamandal_v2.strategy_lanes.lane_common import propose_action, sheet_number
from kamandal_v2.strategy_lanes.models import ActionType, CsaAction, LifecycleState
from kamandal_v2.strategy_lanes.policy import CsaPolicy


def propose_earnings_calendar_actions(
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
    if str(context.get("event_state") or "") not in {"known", "confirmed"}:
        actions.append(propose_action(lifecycle, ActionType.BLOCK, "event_state_ambiguous", arbiter_class="ownership_ambiguity", proposed_at=proposed_at))
        actions.append(propose_action(lifecycle, ActionType.HOLD, "earnings_calendar_hold", arbiter_class="hold", proposed_at=proposed_at))
        return tuple(actions)
    if bool(context.get("hard_emergency")):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "hard_emergency", arbiter_class="hard_emergency", proposed_at=proposed_at))
    if _number(context, "days_to_event") <= sheet_number(policy, "exit_pre_event_days"):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "earnings_event_exit", arbiter_class="mandatory_event_exit", proposed_at=proposed_at))
    if _number(context, "profit_pct") >= sheet_number(policy, "profit_target_pct"):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "profit_target", arbiter_class="executable_profit", proposed_at=proposed_at))
    if bool(context.get("near_leg_expired")):
        actions.append(propose_action(lifecycle, ActionType.CLOSE, "near_leg_expiry_close", arbiter_class="time_decision", proposed_at=proposed_at))
    actions.append(propose_action(lifecycle, ActionType.HOLD, "earnings_calendar_hold", arbiter_class="hold", proposed_at=proposed_at))
    return tuple(actions)


def _number(context: Mapping[str, Any], key: str) -> float:
    raw = context.get(key)
    if isinstance(raw, bool) or raw in (None, ""):
        raise ValueError(f"earnings calendar context missing numeric {key}")
    return float(raw)
