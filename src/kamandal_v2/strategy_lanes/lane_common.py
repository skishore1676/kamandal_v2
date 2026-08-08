"""Shared pure helpers for CSA lane lifecycle decisions."""

from __future__ import annotations

from typing import Any

from kamandal_v2.strategy_lanes.models import ActionDisposition, ActionType, CsaAction, LifecycleState, stable_csa_id
from kamandal_v2.strategy_lanes.policy import CsaPolicy, PolicyError


def propose_action(
    lifecycle: LifecycleState,
    action_type: ActionType,
    reason: str,
    *,
    arbiter_class: str,
    proposed_at: str,
    payload: dict[str, Any] | None = None,
) -> CsaAction:
    action_payload = {"arbiter_class": arbiter_class, **(payload or {})}
    action_id = stable_csa_id(
        "action-proposal",
        [lifecycle.lifecycle_id, lifecycle.version, action_type.value, reason, action_payload],
    )
    return CsaAction(
        action_id=action_id,
        lifecycle_id=lifecycle.lifecycle_id,
        lifecycle_version=lifecycle.version,
        action_type=action_type,
        disposition=ActionDisposition.PROPOSED,
        reason_codes=(reason,),
        proposed_at=proposed_at,
        priority=0,
        payload=action_payload,
    )


def sheet_number(policy: CsaPolicy, field_name: str) -> float:
    return _number(policy.resolved_fields, field_name, prefix="Sheet")


def lifecycle_value(policy: CsaPolicy, field_name: str) -> Any:
    lifecycle = policy.management.get("lifecycle")
    if not isinstance(lifecycle, dict) or lifecycle.get(field_name) in (None, ""):
        raise PolicyError(f"{policy.playbook_id}: missing lifecycle policy {field_name}")
    return lifecycle[field_name]


def lifecycle_number(policy: CsaPolicy, field_name: str) -> float:
    lifecycle = policy.management.get("lifecycle")
    if not isinstance(lifecycle, dict):
        raise PolicyError(f"{policy.playbook_id}: missing lifecycle policy")
    return _number(lifecycle, field_name, prefix="lifecycle")


def nested_number(values: Any, field_name: str, *, prefix: str) -> float:
    if not isinstance(values, dict):
        raise PolicyError(f"{prefix} must be an object")
    return _number(values, field_name, prefix=prefix)


def policy_bool(value: Any, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise PolicyError(f"{label} must be boolean")


def _number(values: dict[str, Any], field_name: str, *, prefix: str) -> float:
    raw = values.get(field_name)
    if isinstance(raw, bool) or raw in (None, ""):
        raise PolicyError(f"{prefix} field {field_name} must be numeric")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{prefix} field {field_name} must be numeric") from exc
