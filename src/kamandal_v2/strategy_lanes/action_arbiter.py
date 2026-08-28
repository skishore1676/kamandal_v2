"""Global deterministic one-action arbiter for CSA lifecycles."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from kamandal_v2.strategy_lanes.models import ActionDisposition, CsaAction


_PRECEDENCE = (
    "working_order_conflict",
    "ownership_ambiguity",
    "hard_emergency",
    "mandatory_event_exit",
    "executable_profit",
    "time_decision",
    "adverse_price_loss",
    "lane_adjustment",
    "resting_profit",
    "routine_management",
    "hold",
)
_PRECEDENCE_RANK = {name: index for index, name in enumerate(_PRECEDENCE)}


@dataclass(frozen=True, slots=True)
class ArbitrationResult:
    selected: CsaAction
    actions: tuple[CsaAction, ...]

    def to_dict(self) -> dict:
        return {"selected": self.selected.to_dict(), "actions": [action.to_dict() for action in self.actions]}


def arbitrate_actions(actions: Iterable[CsaAction]) -> ArbitrationResult:
    proposals = tuple(actions)
    if not proposals:
        raise ValueError("action arbiter requires at least one proposal")
    identities = {(action.lifecycle_id, action.lifecycle_version) for action in proposals}
    if len(identities) != 1:
        raise ValueError("action proposals must target one lifecycle version")
    unknown = sorted(
        {
            str(action.payload.get("arbiter_class") or "")
            for action in proposals
            if str(action.payload.get("arbiter_class") or "") not in _PRECEDENCE_RANK
        }
    )
    if unknown:
        raise ValueError(f"unknown arbiter classes: {', '.join(unknown)}")
    ordered = sorted(
        proposals,
        key=lambda action: (_PRECEDENCE_RANK[str(action.payload["arbiter_class"])], action.action_id),
    )
    selected_id = ordered[0].action_id
    dispositions = tuple(
        replace(
            action,
            disposition=(ActionDisposition.SELECTED if action.action_id == selected_id else ActionDisposition.SUPERSEDED),
            priority=_PRECEDENCE_RANK[str(action.payload["arbiter_class"])],
        )
        for action in ordered
    )
    return ArbitrationResult(selected=dispositions[0], actions=dispositions)
