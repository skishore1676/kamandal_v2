"""Read-only inventory for the protected unified-engine cutover."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.lifecycle import adopt_legacy_position
from kamandal_v2.strategy_lanes.models import LaneId


@dataclass(frozen=True, slots=True)
class CutoverDecision:
    subject: str
    decision: str
    reason: str
    lifecycle_id: str = ""


@dataclass(frozen=True, slots=True)
class CutoverManifest:
    decisions: tuple[CutoverDecision, ...]

    @property
    def ready(self) -> bool:
        return not any(item.decision == "block" for item in self.decisions)

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "decisions": [asdict(item) for item in self.decisions]}


def build_cutover_manifest(store: LocalStore) -> CutoverManifest:
    """Inventory baseline groups without mutating the database or scheduler."""
    decisions: list[CutoverDecision] = []
    for group in sorted(store.open_live_position_groups(), key=lambda item: str(item.get("group_id") or "")):
        group_id = str(group.get("group_id") or "")
        try:
            lifecycle = adopt_legacy_position(_adoption_payload(group), lifecycle_id=f"adopt:{group_id}", adopted_at=str(group.get("opened_at") or ""))
        except (ValueError, KeyError, TypeError) as exc:
            decisions.append(CutoverDecision(group_id, "block", str(exc)))
        else:
            decisions.append(CutoverDecision(group_id, "create", "legacy position maps exactly", lifecycle.lifecycle_id))
    return CutoverManifest(tuple(decisions))


def _adoption_payload(group: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(group.get("candidate") or {})
    structure = str(candidate.get("structure") or group.get("structure") or "").lower()
    lanes = {
        "short_strangle": LaneId.SHORT_STRANGLE,
        "strangle": LaneId.SHORT_STRANGLE,
        "call_spread": LaneId.CALL_VERTICAL,
        "call_diagonal": LaneId.DIRECTIONAL_DIAGONAL,
        "put_diagonal": LaneId.DIRECTIONAL_DIAGONAL,
        "call_calendar": LaneId.EARNINGS_CALENDAR,
        "put_calendar": LaneId.EARNINGS_CALENDAR,
    }
    if structure not in lanes:
        raise ValueError(f"legacy adoption blocked: unsupported structure {structure or '<missing>'}")
    return {
        "group_id": group.get("group_id"),
        "opportunity_id": str(candidate.get("idea_id") or group.get("group_id") or ""),
        "lane": lanes[structure].value,
        "active_legs": candidate.get("legs") or (),
        "cashflow_ledger": group.get("cashflow_ledger") or (),
        "policy_hash": str(group.get("policy_hash") or "policy-at-adoption"),
    }
