"""Strict Google-Sheet policy compilation for CSA lanes."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

from kamandal_v2.strategy_lanes.models import CsaStage, LaneId, SourceMode


class PolicyError(ValueError):
    """Raised when an enabled CSA row cannot be compiled safely."""


@dataclass(frozen=True, slots=True)
class CsaPolicy:
    playbook_id: str
    lane: LaneId
    stage: CsaStage
    source_mode: SourceMode
    management: dict[str, Any]
    resolved_fields: dict[str, Any]
    policy_hash: str
    source: str
    read_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "lane": self.lane.value,
            "stage": self.stage.value,
            "source_mode": self.source_mode.value,
            "management": self.management,
            "resolved_fields": self.resolved_fields,
            "policy_hash": self.policy_hash,
            "source": self.source,
            "read_at": self.read_at,
        }


@dataclass(frozen=True, slots=True)
class PolicyCompilation:
    policies: tuple[CsaPolicy, ...]
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


_LANE_SOURCE_MODES = {
    LaneId.SHORT_STRANGLE: {SourceMode.MARKET_SCAN, SourceMode.OBSERVED_PACKAGE},
    LaneId.CALL_VERTICAL: {SourceMode.IDEA, SourceMode.PORTFOLIO_HEDGE, SourceMode.OBSERVED_PACKAGE},
    LaneId.DIRECTIONAL_DIAGONAL: {SourceMode.IDEA, SourceMode.OBSERVED_PACKAGE},
    LaneId.GENERIC_CLOSE_ONLY: {SourceMode.IDEA, SourceMode.MARKET_SCAN, SourceMode.PORTFOLIO_HEDGE, SourceMode.OBSERVED_PACKAGE},
    LaneId.EARNINGS_CALENDAR: {SourceMode.IDEA, SourceMode.OBSERVED_PACKAGE},
}

_COMMON_REQUIRED_FIELDS = (
    "playbook_id",
    "enabled",
    "strategy_family",
    "structure",
    "csa_stage",
    "source_mode",
    "management_policy_json",
    "sizing_method",
    "sizing_value",
    "max_contracts",
    "score_weight_credit",
    "score_weight_pop",
    "score_weight_liquidity",
    "score_weight_spread",
    "max_bid_ask_pct",
    "min_option_oi",
)

_LANE_REQUIRED_FIELDS = {
    LaneId.SHORT_STRANGLE: (
        "dte_min",
        "dte_max",
        "short_delta_min",
        "short_delta_max",
        "iv_rank_min",
        "iv_rank_max",
        "profit_target_pct",
        "exit_dte_min",
        "live_max_bpr_per_order",
    ),
    LaneId.CALL_VERTICAL: (
        "dte_min",
        "dte_max",
        "short_delta_min",
        "short_delta_max",
        "profit_target_pct",
        "max_loss_multiple",
        "exit_dte_min",
        "spread_width",
        "live_max_bpr_per_order",
    ),
    LaneId.DIRECTIONAL_DIAGONAL: (
        "dte_min",
        "dte_max",
        "long_dte_min",
        "long_dte_max",
        "short_delta_min",
        "short_delta_max",
        "long_delta_min",
        "long_delta_max",
        "max_debit_to_width_ratio",
        "profit_target_pct",
        "max_loss_multiple",
        "exit_dte_min",
        "live_max_bpr_per_order",
    ),
    LaneId.GENERIC_CLOSE_ONLY: (
        "dte_min",
        "dte_max",
        "profit_target_pct",
        "max_loss_multiple",
        "exit_dte_min",
        "live_max_bpr_per_order",
    ),
    LaneId.EARNINGS_CALENDAR: (
        "dte_min",
        "dte_max",
        "long_dte_min",
        "long_dte_max",
        "long_delta_min",
        "long_delta_max",
        "profit_target_pct",
        "live_max_bpr_per_order",
    ),
}


def compile_csa_policy(
    row: dict[str, Any],
    *,
    source: str,
    read_at: str,
    source_fresh: bool = True,
) -> CsaPolicy | None:
    raw_stage = str(row.get("csa_stage") or "").strip().lower()
    if not raw_stage or raw_stage == CsaStage.BASELINE.value:
        return None
    if source != "google_sheet":
        raise PolicyError(f"{_row_name(row)}: active CSA policy must come from google_sheet, got {source!r}")
    if not source_fresh:
        raise PolicyError(f"{_row_name(row)}: Google Sheet policy evidence is stale")
    try:
        stage = CsaStage(raw_stage)
    except ValueError as exc:
        raise PolicyError(f"{_row_name(row)}: invalid csa_stage={raw_stage!r}") from exc

    structure = str(row.get("structure") or "").strip().lower()
    lane = _lane_from_row(row, structure)
    if not _as_bool(row.get("enabled")):
        raise PolicyError(f"{_row_name(row)}: CSA stage requires enabled=TRUE")

    raw_mode = str(row.get("source_mode") or "").strip().lower()
    try:
        source_mode = SourceMode(raw_mode)
    except ValueError as exc:
        raise PolicyError(f"{_row_name(row)}: invalid source_mode={raw_mode!r}") from exc
    if source_mode not in _LANE_SOURCE_MODES[lane]:
        allowed = ", ".join(sorted(item.value for item in _LANE_SOURCE_MODES[lane]))
        raise PolicyError(f"{_row_name(row)}: source_mode={source_mode.value!r} is incompatible; expected {allowed}")
    if source_mode is SourceMode.OBSERVED_PACKAGE:
        if stage is not CsaStage.SHADOW:
            raise PolicyError(f"{_row_name(row)}: observed_package source mode is shadow-only")
        if not _text_list(row.get("source_profiles")):
            raise PolicyError(f"{_row_name(row)}: observed_package source mode requires source_profiles")

    missing = [
        field_name
        for field_name in (*_COMMON_REQUIRED_FIELDS, *_LANE_REQUIRED_FIELDS[lane])
        if row.get(field_name) in (None, "")
    ]
    if lane is LaneId.SHORT_STRANGLE and _as_bool(row.get("universe_expansion_enabled")):
        missing.extend(
            field_name
            for field_name in ("underlying_price_min", "underlying_price_max")
            if row.get(field_name) in (None, "")
        )
    if missing:
        raise PolicyError(f"{_row_name(row)}: missing required Sheet fields: {', '.join(sorted(set(missing)))}")

    management = _management_object(row.get("management_policy_json"), row_name=_row_name(row))
    if "score_weights" in management:
        raise PolicyError(f"{_row_name(row)}: score_weights must use the existing Sheet columns, not management_policy_json")
    lifecycle = management.get("lifecycle")
    if not isinstance(lifecycle, dict) or not lifecycle:
        raise PolicyError(f"{_row_name(row)}: management_policy_json.lifecycle must be a non-empty object")
    management = {
        **management,
        "score_weights": {
            "credit": row["score_weight_credit"],
            "pop": row["score_weight_pop"],
            "liquidity": row["score_weight_liquidity"],
            "spread": row["score_weight_spread"],
        },
    }
    _validate_lifecycle_shape(management, lane=lane, source_mode=source_mode, row_name=_row_name(row))
    _validate_numeric_policy(row, lane=lane, management=management, row_name=_row_name(row))

    resolved = {
        str(name): value
        for name, value in sorted(row.items(), key=lambda item: str(item[0]))
        if value not in (None, "") and str(name) != "management_policy_json"
    }
    canonical = {
        "lane": lane.value,
        "stage": stage.value,
        "source_mode": source_mode.value,
        "management": management,
        "resolved_fields": resolved,
    }
    policy_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return CsaPolicy(
        playbook_id=str(row["playbook_id"]).strip(),
        lane=lane,
        stage=stage,
        source_mode=source_mode,
        management=management,
        resolved_fields=resolved,
        policy_hash=policy_hash,
        source=source,
        read_at=read_at,
    )


def _lane_from_row(row: dict[str, Any], structure: str) -> LaneId:
    """Map capability first; structure only constrains its order shape.

    An ordinary calendar is not an earnings calendar merely because both use
    calendar legs.  Only the explicit earnings capability receives the
    event-relative lifecycle; every other supported family uses the one
    generic close-only lifecycle until it earns a specialised owner.
    """
    family = str(row.get("strategy_family") or "").strip().lower()
    if family == "earnings_calendar":
        if structure not in {"call_calendar", "put_calendar"}:
            raise PolicyError(f"{_row_name(row)}: earnings_calendar requires a calendar structure")
        return LaneId.EARNINGS_CALENDAR
    if family == "short_strangle" or structure in {"short_strangle", "strangle"}:
        return LaneId.SHORT_STRANGLE
    if family in {"call_vertical", "call_spread"} or structure in {"call_spread", "call_vertical"}:
        return LaneId.CALL_VERTICAL
    if family in {"directional_diagonal", "narrative_ignition", "call_diagonal", "put_diagonal"} or structure in {"call_diagonal", "put_diagonal"}:
        return LaneId.DIRECTIONAL_DIAGONAL
    if structure in {
        "short_put", "long_call", "long_put", "put_spread", "iron_condor", "jade_lizard", "call_calendar", "put_calendar",
    }:
        return LaneId.GENERIC_CLOSE_ONLY
    raise PolicyError(f"{_row_name(row)}: unsupported CSA structure={structure!r}")


def compile_csa_policies(
    rows: Iterable[dict[str, Any]],
    *,
    source: str,
    read_at: str,
    source_fresh: bool = True,
) -> PolicyCompilation:
    policies: list[CsaPolicy] = []
    errors: list[str] = []
    seen: set[str] = set()
    for row in rows:
        try:
            policy = compile_csa_policy(row, source=source, read_at=read_at, source_fresh=source_fresh)
        except PolicyError as exc:
            errors.append(str(exc))
            continue
        if policy is None:
            continue
        if policy.playbook_id in seen:
            errors.append(f"{policy.playbook_id}: duplicate CSA playbook_id")
            continue
        seen.add(policy.playbook_id)
        policies.append(policy)
    return PolicyCompilation(tuple(policies), tuple(errors))


def _management_object(value: Any, *, row_name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        parsed = value
    else:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PolicyError(f"{row_name}: management_policy_json is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise PolicyError(f"{row_name}: management_policy_json must be a JSON object")
    return parsed


def _row_name(row: dict[str, Any]) -> str:
    return str(row.get("playbook_id") or "<unnamed-playbook>")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _text_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _validate_lifecycle_shape(management: dict[str, Any], *, lane: LaneId, source_mode: SourceMode, row_name: str) -> None:
    paths: list[tuple[str, ...]] = [("lifecycle", "fill", "max_attempts"), ("lifecycle", "fill", "price_increment")]
    if lane is LaneId.SHORT_STRANGLE:
        paths.extend(
            [
                ("lifecycle", "tested_side_confirmation"),
                ("lifecycle", "roll", "min_credit"),
                ("lifecycle", "roll", "duration_trigger_dte"),
                ("lifecycle", "adjustment_limit"),
                ("lifecycle", "inversion", "allowed"),
                ("lifecycle", "inversion", "max_width"),
                ("lifecycle", "cooldown", "minutes"),
                ("lifecycle", "loss_stages", "watch_multiple"),
                ("lifecycle", "loss_stages", "close_multiple"),
            ]
        )
    elif lane is LaneId.CALL_VERTICAL:
        paths.append(("lifecycle", "close_only"))
        if source_mode is SourceMode.PORTFOLIO_HEDGE:
            paths.extend([("lifecycle", "portfolio_delta_trigger"), ("lifecycle", "hedge_underlyings")])
    elif lane is LaneId.DIRECTIONAL_DIAGONAL:
        paths.extend(
            [
                ("lifecycle", "short_leg", "roll"),
                ("lifecycle", "short_leg", "roll_dte"),
                ("lifecycle", "long_only", "requires_approval"),
            ]
        )
    elif lane is LaneId.GENERIC_CLOSE_ONLY:
        paths.append(("lifecycle", "close_only"))
        if source_mode is SourceMode.PORTFOLIO_HEDGE:
            paths.extend([("lifecycle", "portfolio_delta_trigger"), ("lifecycle", "hedge_underlyings")])
    elif lane is LaneId.EARNINGS_CALENDAR:
        paths.append(("lifecycle", "close_only"))
    missing = [".".join(path) for path in paths if _path_value(management, path) in (None, "", {}, [])]
    if missing:
        raise PolicyError(f"{row_name}: management_policy_json missing required fields: {', '.join(missing)}")
    boolean_paths: list[tuple[str, ...]] = []
    if lane is LaneId.SHORT_STRANGLE:
        boolean_paths.append(("lifecycle", "inversion", "allowed"))
    elif lane is LaneId.CALL_VERTICAL:
        boolean_paths.append(("lifecycle", "close_only"))
    elif lane is LaneId.DIRECTIONAL_DIAGONAL:
        boolean_paths.extend(
            [("lifecycle", "short_leg", "roll"), ("lifecycle", "long_only", "requires_approval")]
        )
    elif lane is LaneId.GENERIC_CLOSE_ONLY:
        boolean_paths.append(("lifecycle", "close_only"))
    elif lane is LaneId.EARNINGS_CALENDAR:
        boolean_paths.append(("lifecycle", "close_only"))
    for path in boolean_paths:
        _strict_bool(_path_value(management, path), label=f"{row_name}: {'.'.join(path)}")


def _path_value(values: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = values
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _validate_numeric_policy(row: dict[str, Any], *, lane: LaneId, management: dict[str, Any], row_name: str) -> None:
    numeric_fields = {
        "sizing_value",
        "max_contracts",
        "score_weight_credit",
        "score_weight_pop",
        "score_weight_liquidity",
        "score_weight_spread",
        "max_bid_ask_pct",
        "min_option_oi",
        *_LANE_REQUIRED_FIELDS[lane],
    }
    if lane is LaneId.SHORT_STRANGLE and _as_bool(row.get("universe_expansion_enabled")):
        numeric_fields.update({"underlying_price_min", "underlying_price_max"})
    parsed = {name: _finite_number(row.get(name), label=f"{row_name}: Sheet field {name}") for name in sorted(numeric_fields)}
    for name in ("max_contracts", "min_option_oi"):
        if not parsed[name].is_integer():
            raise PolicyError(f"{row_name}: Sheet field {name} must be an integer")
    for name in ("sizing_value", "max_contracts", "live_max_bpr_per_order"):
        if parsed[name] <= 0:
            raise PolicyError(f"{row_name}: Sheet field {name} must be positive")
    for name in ("score_weight_credit", "score_weight_pop", "score_weight_liquidity", "score_weight_spread", "max_bid_ask_pct", "min_option_oi"):
        if parsed[name] < 0:
            raise PolicyError(f"{row_name}: Sheet field {name} must be nonnegative")
    if sum(parsed[name] for name in ("score_weight_credit", "score_weight_pop", "score_weight_liquidity", "score_weight_spread")) <= 0:
        raise PolicyError(f"{row_name}: Sheet score weights must have positive total")
    if lane is LaneId.DIRECTIONAL_DIAGONAL:
        if not 0 < parsed["max_loss_multiple"] <= 1:
            raise PolicyError(
                f"{row_name}: debit diagonal max_loss_multiple must be a loss fraction in (0, 1]"
            )
        if not 0 < parsed["max_debit_to_width_ratio"] <= 1:
            raise PolicyError(
                f"{row_name}: max_debit_to_width_ratio must be in (0, 1]"
            )
        sizing_method = str(row.get("sizing_method") or "").strip().lower()
        if sizing_method != "fixed_contracts" or parsed["sizing_value"] != 1 or parsed["max_contracts"] != 1:
            raise PolicyError(
                f"{row_name}: directional diagonal currently requires fixed_contracts sizing_value=1 max_contracts=1"
            )
    for low, high in (
        ("dte_min", "dte_max"),
        ("short_delta_min", "short_delta_max"),
        ("long_delta_min", "long_delta_max"),
        ("iv_rank_min", "iv_rank_max"),
        ("underlying_price_min", "underlying_price_max"),
        ("long_dte_min", "long_dte_max"),
    ):
        if low in parsed and high in parsed and parsed[low] > parsed[high]:
            raise PolicyError(f"{row_name}: Sheet range invalid: {low}>{high}")
    numeric_paths = [("lifecycle", "fill", "max_attempts"), ("lifecycle", "fill", "price_increment")]
    if lane is LaneId.SHORT_STRANGLE:
        numeric_paths.extend(
            [
                ("lifecycle", "tested_side_confirmation"),
                ("lifecycle", "roll", "min_credit"),
                ("lifecycle", "roll", "duration_trigger_dte"),
                ("lifecycle", "adjustment_limit"),
                ("lifecycle", "inversion", "max_width"),
                ("lifecycle", "cooldown", "minutes"),
                ("lifecycle", "loss_stages", "watch_multiple"),
                ("lifecycle", "loss_stages", "close_multiple"),
            ]
        )
    elif lane is LaneId.CALL_VERTICAL and source_mode_is_portfolio(management, row):
        numeric_paths.append(("lifecycle", "portfolio_delta_trigger"))
    elif lane is LaneId.DIRECTIONAL_DIAGONAL:
        numeric_paths.append(("lifecycle", "short_leg", "roll_dte"))
    elif lane is LaneId.GENERIC_CLOSE_ONLY and source_mode_is_portfolio(management, row):
        numeric_paths.append(("lifecycle", "portfolio_delta_trigger"))
    for path in numeric_paths:
        number = _finite_number(_path_value(management, path), label=f"{row_name}: {'.'.join(path)}")
        if number < 0:
            raise PolicyError(f"{row_name}: {'.'.join(path)} must be nonnegative")


def source_mode_is_portfolio(_management: dict[str, Any], row: dict[str, Any]) -> bool:
    return str(row.get("source_mode") or "").strip().lower() == SourceMode.PORTFOLIO_HEDGE.value


def _finite_number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or value in (None, ""):
        raise PolicyError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise PolicyError(f"{label} must be finite")
    return number


def _strict_bool(value: Any, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise PolicyError(f"{label} must be boolean")
