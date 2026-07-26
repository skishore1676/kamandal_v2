"""Application-owned validation for broker-inert strategy-discovery proposals."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping


RECEIPT_SCHEMA = "kamandal.strategy_discovery_admission_receipt.v1"
_EVIDENCE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
_FORBIDDEN_FIELDS = {
    "option_legs",
    "selected_legs",
    "mala_gate_result",
    "profitability",
    "profitability_score",
    "expected_profit",
    "requested_effect",
    "requested_effects",
}
_IDEA_REQUIRED = {
    "schema",
    "idea_id",
    "source_lead_id",
    "symbol_universe",
    "freshness_window",
    "thesis",
    "catalyst",
    "invalidation",
    "direction",
    "evidence_citations",
    "source_claims",
    "model_interpretation",
    "status",
    "planner_handoff_status",
    "evidence_ids",
    "model_receipt_id",
    "created_at",
}
_PLAYBOOK_REQUIRED = {
    "schema",
    "proposal_id",
    "source_lead_id",
    "rule_kind",
    "proposed_rule",
    "current_rule",
    "applicability",
    "invalidation",
    "planner_contract_compatibility",
    "evidence_requirements",
    "status",
    "evidence_ids",
    "model_receipt_id",
    "created_at",
}


def _hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _find_forbidden(value: Any, path: str = "$") -> list[str]:
    issues: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _FORBIDDEN_FIELDS:
                issues.append(f"{path}.{key}:forbidden_model_authority")
            issues.extend(_find_forbidden(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            issues.extend(_find_forbidden(child, f"{path}[{index}]"))
    return issues


def _valid_string(value: Any, minimum: int = 1, maximum: int | None = None) -> bool:
    return (
        isinstance(value, str)
        and len(value) >= minimum
        and (maximum is None or len(value) <= maximum)
    )


def _valid_string_list(
    value: Any,
    *,
    minimum_items: int = 0,
    minimum_length: int = 1,
) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= minimum_items
        and all(_valid_string(item, minimum_length) for item in value)
    )


def _valid_datetime(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_strategy_discovery_candidate(
    payload: Mapping[str, Any],
    *,
    evidence_status_by_id: Mapping[str, str],
) -> dict[str, Any]:
    """Return a pure validation receipt; never change planner or runtime state."""
    schema = payload.get("schema")
    if schema == "kamandal.idea_candidate.v1":
        kind = "idea"
        required = _IDEA_REQUIRED
    elif schema == "kamandal.playbook_proposal.v1":
        kind = "playbook"
        required = _PLAYBOOK_REQUIRED
    else:
        kind = "unknown"
        required = set()

    issues = _find_forbidden(payload)
    if kind == "unknown":
        issues.append("invalid_schema")
    keys = set(payload)
    for key in sorted(required - keys):
        issues.append(f"missing_required:{key}")
    for key in sorted(keys - required):
        issues.append(f"unknown_field:{key}")
    if not _valid_string(payload.get("source_lead_id"), 6):
        issues.append("invalid_string:source_lead_id")
    if not _valid_string(payload.get("model_receipt_id"), 4):
        issues.append("invalid_string:model_receipt_id")
    if not _valid_datetime(payload.get("created_at")):
        issues.append("invalid_datetime:created_at")

    evidence_ids = payload.get("evidence_ids")
    if not isinstance(evidence_ids, list) or not evidence_ids:
        issues.append("evidence_ids_required")
        evidence_ids = []
    elif len(evidence_ids) != len(set(evidence_ids)):
        issues.append("duplicate_evidence_ids")
    for evidence_id in evidence_ids:
        if not isinstance(evidence_id, str) or not _EVIDENCE_ID.fullmatch(evidence_id):
            issues.append(f"invalid_evidence_id:{evidence_id}")
            continue
        status = evidence_status_by_id.get(evidence_id)
        if status != "available":
            issues.append(f"evidence_not_available:{evidence_id}:{status or 'missing'}")

    if kind == "idea":
        idea_string_limits = {
            "idea_id": (7, None),
            "symbol_universe": (2, 200),
            "thesis": (20, 1200),
            "catalyst": (10, 800),
            "model_interpretation": (5, 1200),
        }
        for key, (minimum, maximum) in idea_string_limits.items():
            if not _valid_string(payload.get(key), minimum, maximum):
                issues.append(f"invalid_string:{key}")
        if not _valid_string_list(
            payload.get("invalidation"),
            minimum_items=1,
            minimum_length=5,
        ):
            issues.append("invalid_invalidation")
        if payload.get("direction") not in {
            "bullish",
            "bearish",
            "neutral",
            "vol_up",
            "vol_down",
        }:
            issues.append("invalid_direction")
        if payload.get("status") != "proposed":
            issues.append("idea_status_must_be_proposed")
        if payload.get("planner_handoff_status") != "needs_review":
            issues.append("planner_handoff_must_be_needs_review")
        citations = payload.get("evidence_citations")
        cited_ids: set[str] = set()
        if not isinstance(citations, list) or not citations:
            issues.append("invalid_evidence_citations")
        else:
            for index, row in enumerate(citations):
                if not isinstance(row, dict) or set(row) != {"evidence_id", "source"}:
                    issues.append(f"invalid_evidence_citation:{index}")
                    continue
                evidence_id = row.get("evidence_id")
                if not isinstance(evidence_id, str) or not _EVIDENCE_ID.fullmatch(
                    evidence_id
                ):
                    issues.append(f"invalid_evidence_citation_id:{index}")
                else:
                    cited_ids.add(evidence_id)
                if not _valid_string(row.get("source"), 3):
                    issues.append(f"invalid_evidence_citation_source:{index}")
        if cited_ids != set(evidence_ids):
            issues.append("evidence_citations_must_match_evidence_ids")
        freshness = payload.get("freshness_window")
        if not isinstance(freshness, dict) or set(freshness) != {
            "as_of",
            "window_days",
        }:
            issues.append("invalid_freshness_window")
        else:
            if not _valid_datetime(freshness.get("as_of")):
                issues.append("invalid_freshness_as_of")
            window_days = freshness.get("window_days")
            if (
                not isinstance(window_days, int)
                or isinstance(window_days, bool)
                or not 1 <= window_days <= 365
            ):
                issues.append("invalid_freshness_window_days")
        source_claims = payload.get("source_claims")
        if not isinstance(source_claims, dict) or set(source_claims) != {
            "from_source",
            "from_transcripts",
        }:
            issues.append("invalid_source_claims")
        else:
            for key in ("from_source", "from_transcripts"):
                if not _valid_string(source_claims.get(key), 5, 1000):
                    issues.append(f"invalid_source_claim:{key}")

    if kind == "playbook":
        playbook_string_limits = {
            "proposal_id": (8, None),
            "proposed_rule": (5, 1200),
            "planner_contract_compatibility": (5, 800),
        }
        for key, (minimum, maximum) in playbook_string_limits.items():
            if not _valid_string(payload.get(key), minimum, maximum):
                issues.append(f"invalid_string:{key}")
        current_rule = payload.get("current_rule")
        if current_rule is not None and not _valid_string(current_rule, 1, 1200):
            issues.append("invalid_current_rule")
        if not _valid_string_list(
            payload.get("invalidation"),
            minimum_items=1,
            minimum_length=5,
        ):
            issues.append("invalid_invalidation")
        if not _valid_string_list(
            payload.get("evidence_requirements"),
            minimum_items=1,
            minimum_length=5,
        ):
            issues.append("invalid_evidence_requirements")
        if payload.get("rule_kind") not in {
            "structure",
            "iv",
            "dte",
            "adjustment",
            "exit",
        }:
            issues.append("invalid_rule_kind")
        if payload.get("status") != "proposed":
            issues.append("playbook_status_must_be_proposed")
        applicability = payload.get("applicability")
        if not isinstance(applicability, dict) or set(applicability) != {
            "instruments",
            "market_conditions",
            "timeframes",
        }:
            issues.append("invalid_applicability")
        else:
            if not _valid_string_list(
                applicability.get("instruments"),
                minimum_items=1,
                minimum_length=2,
            ):
                issues.append("invalid_applicability_instruments")
            if not _valid_string(
                applicability.get("market_conditions"),
                6,
                300,
            ):
                issues.append("invalid_applicability_market_conditions")
            if not _valid_string_list(
                applicability.get("timeframes"),
                minimum_items=1,
                minimum_length=2,
            ):
                issues.append("invalid_applicability_timeframes")

    identifier = payload.get("idea_id") if kind == "idea" else payload.get("proposal_id")
    accepted = not issues
    result = "accepted" if accepted else (
        "needs_evidence"
        if any(issue.startswith("evidence_not_available:") for issue in issues)
        else "rejected"
    )
    return {
        "schema": RECEIPT_SCHEMA,
        "candidate_kind": kind,
        "candidate_id": identifier,
        "source_lead_id": payload.get("source_lead_id"),
        "candidate_sha256": _hash(payload),
        "evidence_ids": list(evidence_ids),
        "result": result,
        "accepted": accepted,
        "issues": sorted(set(issues)),
        "effects": {
            "idea_written": False,
            "playbook_written": False,
            "planner_mutated": False,
            "option_legs_selected": False,
            "shadow_activated": False,
            "live_activated": False,
            "sheet_mutated": False,
            "broker_accessed": False,
        },
    }
