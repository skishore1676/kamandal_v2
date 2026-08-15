"""Compile one normalized policy for every enabled playbook row.

This module is source-only during Phase 2.  It deliberately does not read or
write Sheets, databases, brokers, or launchd; callers supply already-read rows.
The only legacy routing knowledge lives in :func:`_mode_from_row`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from copy import deepcopy
from typing import Any, Iterable

from kamandal_v2.strategy_engine.registry import Capability, CapabilityRegistry, capability_registry


class PolicyError(ValueError):
    """A playbook cannot be made safe for the unified engine."""


class ExecutionMode(StrEnum):
    SHADOW = "shadow"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class StrangleManagementPolicy:
    entry_delta_range: tuple[float, float]
    target_delta: float
    max_delta: float
    tested_side_confirmations: int
    rearm_inside_confirmations: int
    minimum_credit: float
    cooldown_minutes: int
    filled_side_adjustment_limit: int
    dte_action: str
    dte_threshold: int
    duration_roll_limit: int
    inversion_enabled: bool
    inversion_max_width: float | None


@dataclass(frozen=True, slots=True)
class PlaybookPolicy:
    playbook_id: str
    capability: Capability
    structure: str
    source_mode: str
    mode: ExecutionMode
    fields: dict[str, Any]
    management: dict[str, Any]
    compatibility: dict[str, Any]
    policy_hash: str
    strangle_management: StrangleManagementPolicy | None = None


@dataclass(frozen=True, slots=True)
class PolicyCompilation:
    policies: tuple[PlaybookPolicy, ...]
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def compile_playbook_policies(
    rows: Iterable[dict[str, Any]],
    *,
    registry: CapabilityRegistry | None = None,
) -> PolicyCompilation:
    policies: list[PlaybookPolicy] = []
    errors: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not _as_bool(row.get("enabled")):
            continue
        try:
            policy = compile_playbook_policy(row, registry=registry)
        except PolicyError as exc:
            errors.append(str(exc))
            continue
        if policy.playbook_id in seen:
            errors.append(f"{policy.playbook_id}: duplicate enabled playbook_id")
            continue
        seen.add(policy.playbook_id)
        policies.append(policy)
    return PolicyCompilation(tuple(policies), tuple(errors))


def compile_playbook_policy(
    row: dict[str, Any],
    *,
    registry: CapabilityRegistry | None = None,
) -> PlaybookPolicy:
    playbook_id = _required_text(row, "playbook_id")
    if not _as_bool(row.get("enabled")):
        raise PolicyError(f"{playbook_id}: enabled=TRUE is required")
    _validate_activation(row, playbook_id)
    capability_key = _required_text(row, "strategy_family")
    structure = _required_text(row, "structure").lower()
    active_registry = registry or capability_registry()
    try:
        capability = active_registry.resolve(capability_key)
    except LookupError as exc:
        raise PolicyError(f"{playbook_id}: {exc}") from exc
    if structure not in capability.allowed_structures:
        allowed = ", ".join(sorted(capability.allowed_structures))
        raise PolicyError(f"{playbook_id}: structure={structure!r} is incompatible with {capability.key}; expected {allowed}")

    mode, compatibility = _mode_from_row(row, playbook_id)
    source_mode = str(row.get("source_mode") or "idea").strip().lower() or "idea"
    if source_mode not in {"idea", "market_scan", "portfolio_hedge"}:
        raise PolicyError(f"{playbook_id}: invalid source_mode={source_mode!r}")
    fields = {str(key): value for key, value in sorted(row.items()) if value not in (None, "")}
    management = _management(row, playbook_id)
    management = _normalize_legacy_management(capability, management, compatibility)
    _reject_live_approval_branch(mode, management, playbook_id)
    _validate_directional_diagonal(capability, management, playbook_id)
    _validate_earnings_calendar(capability, row, source_mode, playbook_id)
    strangle_management = _compile_strangle_management(row, management, compatibility, capability, playbook_id)
    canonical = {
        "playbook_id": playbook_id,
        "capability": capability.key,
        "structure": structure,
        "source_mode": source_mode,
        "mode": mode.value,
        "fields": fields,
        "management": management,
        "compatibility": compatibility,
        "strangle_management": _as_jsonable(strangle_management),
    }
    policy_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    return PlaybookPolicy(
        playbook_id=playbook_id,
        capability=capability,
        structure=structure,
        source_mode=source_mode,
        mode=mode,
        fields=fields,
        management=management,
        compatibility=compatibility,
        policy_hash=policy_hash,
        strangle_management=strangle_management,
    )


def _mode_from_row(row: dict[str, Any], playbook_id: str) -> tuple[ExecutionMode, dict[str, Any]]:
    explicit = str(row.get("mode") or "").strip().lower()
    legacy_stage = str(row.get("csa_stage") or "baseline").strip().lower() or "baseline"
    if explicit:
        try:
            return ExecutionMode(explicit), {"legacy_stage": legacy_stage, "mode_source": "explicit"}
        except ValueError as exc:
            raise PolicyError(f"{playbook_id}: invalid mode={explicit!r}; expected shadow or live") from exc
    if legacy_stage == "shadow":
        return ExecutionMode.SHADOW, {"legacy_stage": legacy_stage, "mode_source": "legacy_stage"}
    if legacy_stage in {"", "baseline", "pilot_live", "live"}:
        return ExecutionMode.LIVE, {"legacy_stage": legacy_stage or "baseline", "mode_source": "legacy_stage"}
    raise PolicyError(f"{playbook_id}: invalid legacy csa_stage={legacy_stage!r}")


def _compile_strangle_management(
    row: dict[str, Any],
    management: dict[str, Any],
    compatibility: dict[str, Any],
    capability: Capability,
    playbook_id: str,
) -> StrangleManagementPolicy | None:
    if capability.key != "short_strangle":
        return None
    lifecycle = management.get("lifecycle") or {}
    if not isinstance(lifecycle, dict):
        raise PolicyError(f"{playbook_id}: lifecycle must be an object")
    entry_min = _number(row, "short_delta_min", playbook_id)
    entry_max = _number(row, "short_delta_max", playbook_id)
    if entry_min > entry_max:
        raise PolicyError(f"{playbook_id}: short entry delta range is inverted")
    legacy_defaults = _as_bool(row.get("legacy_management_defaults"), default=True)
    target = _optional_number(row.get("management_delta_target"))
    maximum = _optional_number(row.get("management_delta_max"))
    if target is None and legacy_defaults:
        target = 0.30
        compatibility["legacy_management_delta_defaults"] = True
    if maximum is None and legacy_defaults:
        maximum = 0.40
        compatibility["legacy_management_delta_defaults"] = True
    if target is None:
        raise PolicyError(f"{playbook_id}: management_delta_target is required and may not fall back to entry delta")
    if maximum is None:
        raise PolicyError(f"{playbook_id}: management_delta_max is required and may not fall back to entry delta")
    if not 0 < target <= maximum <= 1:
        raise PolicyError(f"{playbook_id}: invalid management delta range")
    roll = lifecycle.get("roll") or {}
    cooldown = lifecycle.get("cooldown") or {}
    inversion = lifecycle.get("inversion") or {}
    dte_action = str(row.get("dte_action") or "close").strip().lower() or "close"
    if dte_action not in {"close", "duration_roll"}:
        raise PolicyError(f"{playbook_id}: dte_action must be close or duration_roll")
    duration_roll_limit = _integer(row.get("duration_roll_limit"), default=0, field="duration_roll_limit", playbook_id=playbook_id)
    if dte_action == "close" and duration_roll_limit != 0:
        raise PolicyError(f"{playbook_id}: dte_action=close requires duration_roll_limit=0")
    if dte_action == "duration_roll" and duration_roll_limit < 1:
        raise PolicyError(f"{playbook_id}: dte_action=duration_roll requires a positive duration_roll_limit")
    inversion_enabled = _as_bool(row.get("inversion_enabled"), default=False)
    if _as_bool(inversion.get("allowed")) and not inversion_enabled:
        compatibility["legacy_inversion_ignored"] = True
    if inversion_enabled:
        for field in ("inversion_max_width", "inversion_min_credit", "inversion_remaining_profit", "inversion_adjusted_profit_target"):
            if _optional_number(row.get(field)) is None:
                raise PolicyError(f"{playbook_id}: {field} is required when inversion is enabled")
    return StrangleManagementPolicy(
        entry_delta_range=(entry_min, entry_max),
        target_delta=target,
        max_delta=maximum,
        tested_side_confirmations=_integer(row.get("tested_side_confirmations", lifecycle.get("tested_side_confirmation")), default=2, field="tested_side_confirmations", playbook_id=playbook_id),
        rearm_inside_confirmations=_integer(row.get("rearm_inside_confirmations"), default=2, field="rearm_inside_confirmations", playbook_id=playbook_id),
        minimum_credit=_number(roll, "min_credit", playbook_id, default=0.10),
        cooldown_minutes=_integer(cooldown.get("minutes"), default=30, field="cooldown.minutes", playbook_id=playbook_id),
        filled_side_adjustment_limit=_integer(row.get("filled_side_adjustment_limit", lifecycle.get("adjustment_limit")), default=2, field="filled_side_adjustment_limit", playbook_id=playbook_id),
        dte_action=dte_action,
        dte_threshold=_integer(row.get("dte_action_threshold", row.get("exit_dte_min", roll.get("duration_trigger_dte"))), default=21, field="dte_action_threshold", playbook_id=playbook_id),
        duration_roll_limit=duration_roll_limit,
        inversion_enabled=inversion_enabled,
        inversion_max_width=_optional_number(row.get("inversion_max_width")) if inversion_enabled else None,
    )


def _normalize_legacy_management(
    capability: Capability,
    management: dict[str, Any],
    compatibility: dict[str, Any],
) -> dict[str, Any]:
    """Translate only the two known baseline diagonal scaffolding branches.

    The deployed Sheet still has a short-leg roll and approval-gated long-only
    branch on baseline diagonals.  Those fields are historical CSA scaffolding,
    not valid target behavior.  The adapter is intentionally conditional on
    legacy-stage mode so a newly authored unified policy with either branch
    fails closed instead of being silently repaired.
    """
    if capability.key not in {"call_diagonal", "put_diagonal"}:
        return management
    if compatibility.get("mode_source") != "legacy_stage":
        return management
    lifecycle = management.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return management
    ignored = [name for name in ("long_only", "short_leg") if name in lifecycle]
    if not ignored:
        return management
    normalized = deepcopy(management)
    normalized_lifecycle = dict(normalized.get("lifecycle") or {})
    for name in ignored:
        normalized_lifecycle.pop(name, None)
    normalized["lifecycle"] = normalized_lifecycle
    compatibility["legacy_diagonal_management_ignored"] = ignored
    compatibility["directional_diagonal_management"] = "paired_hold_or_full_close"
    return normalized


def _validate_activation(row: dict[str, Any], playbook_id: str) -> None:
    tier = str(row.get("tier") or "").strip().lower()
    if tier in {"proposed", "held", "rejected"}:
        raise PolicyError(f"{playbook_id}: proposed/held/rejected universe policy is not tradable")


def _validate_directional_diagonal(capability: Capability, management: dict[str, Any], playbook_id: str) -> None:
    if capability.key not in {"call_diagonal", "put_diagonal", "narrative_ignition"}:
        return
    lifecycle = management.get("lifecycle") or {}
    serialized = json.dumps(lifecycle, sort_keys=True).lower()
    if "short_leg" in serialized and "roll" in serialized:
        raise PolicyError(f"{playbook_id}: directional diagonal short-leg roll is not permitted")
    if "long_only" in serialized or "resale" in serialized:
        raise PolicyError(f"{playbook_id}: directional diagonal long-only/resale management is not permitted")


def _validate_earnings_calendar(capability: Capability, row: dict[str, Any], source_mode: str, playbook_id: str) -> None:
    if capability.key != "earnings_calendar":
        return
    if source_mode != "idea":
        raise PolicyError(f"{playbook_id}: earnings calendar requires idea source_mode")
    if not str(row.get("applicable_direction") or "").strip():
        raise PolicyError(f"{playbook_id}: earnings calendar requires direction selection")
    if _integer(row.get("long_dte_min"), default=0, field="long_dte_min", playbook_id=playbook_id) < 45 or _integer(row.get("long_dte_max"), default=0, field="long_dte_max", playbook_id=playbook_id) > 60:
        raise PolicyError(f"{playbook_id}: earnings calendar far leg must be 45-60 DTE")
    if _integer(row.get("dte_min"), default=0, field="dte_min", playbook_id=playbook_id) < 5 or _integer(row.get("dte_max"), default=0, field="dte_max", playbook_id=playbook_id) > 7:
        raise PolicyError(f"{playbook_id}: earnings calendar near leg must be 5-7 DTE")
    timing = str(row.get("event_timing") or "").strip().lower()
    after_event = _as_bool(row.get("near_expiry_after_event")) or _integer(
        row.get("event_near_expiry_after_days"), default=0, field="event_near_expiry_after_days", playbook_id=playbook_id
    ) > 0
    if timing not in {"confirmed", "confirmed_bmo_or_amc_final_pre_event_session"} or not after_event:
        raise PolicyError(f"{playbook_id}: earnings calendar requires confirmed event timing and post-event near expiry")


def _reject_live_approval_branch(mode: ExecutionMode, management: dict[str, Any], playbook_id: str) -> None:
    if mode is not ExecutionMode.LIVE:
        return
    serialized = json.dumps(management, sort_keys=True).lower()
    if "requires_approval" in serialized or "approval_required" in serialized:
        raise PolicyError(f"{playbook_id}: live policy contains a reachable operator approval branch")


def _management(row: dict[str, Any], playbook_id: str) -> dict[str, Any]:
    raw = row.get("management_policy_json")
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        parsed = raw
    else:
        try:
            parsed = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PolicyError(f"{playbook_id}: management_policy_json is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise PolicyError(f"{playbook_id}: management_policy_json must be an object")
    return parsed


def _required_text(row: dict[str, Any], field: str) -> str:
    value = str(row.get(field) or "").strip()
    if not value:
        raise PolicyError(f"<unnamed-playbook>: {field} is required")
    return value


def _number(source: dict[str, Any], field: str, playbook_id: str, *, default: float | None = None) -> float:
    value = _optional_number(source.get(field))
    if value is None:
        if default is not None:
            return default
        raise PolicyError(f"{playbook_id}: {field} is required")
    return value


def _optional_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"invalid numeric policy value: {value!r}") from exc


def _integer(value: Any, *, default: int, field: str, playbook_id: str) -> int:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{playbook_id}: {field} must be an integer") from exc
    if not number.is_integer() or number < 0:
        raise PolicyError(f"{playbook_id}: {field} must be a non-negative integer")
    return int(number)


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _as_jsonable(value: Any) -> Any:
    if value is None:
        return None
    return {
        "entry_delta_range": value.entry_delta_range,
        "target_delta": value.target_delta,
        "max_delta": value.max_delta,
        "tested_side_confirmations": value.tested_side_confirmations,
        "rearm_inside_confirmations": value.rearm_inside_confirmations,
        "minimum_credit": value.minimum_credit,
        "cooldown_minutes": value.cooldown_minutes,
        "filled_side_adjustment_limit": value.filled_side_adjustment_limit,
        "dte_action": value.dte_action,
        "dte_threshold": value.dte_threshold,
        "duration_roll_limit": value.duration_roll_limit,
        "inversion_enabled": value.inversion_enabled,
        "inversion_max_width": value.inversion_max_width,
    }
