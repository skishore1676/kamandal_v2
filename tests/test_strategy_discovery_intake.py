from __future__ import annotations

from copy import deepcopy

from kamandal_v2.intelligence.strategy_discovery_intake import (
    RECEIPT_SCHEMA,
    validate_strategy_discovery_candidate,
)


EVIDENCE_ID = "sha256:" + ("a" * 64)


def _idea() -> dict:
    return {
        "schema": "kamandal.idea_candidate.v1",
        "idea_id": "kamandal-idea:fixture",
        "source_lead_id": "lead:fixture",
        "symbol_universe": "SPX",
        "freshness_window": {"as_of": "2026-07-26T18:00:00Z", "window_days": 5},
        "thesis": "A current source-backed catalyst warrants planner review.",
        "catalyst": "The cited event is current and bounded.",
        "invalidation": ["The source thesis reverses before admission."],
        "direction": "neutral",
        "evidence_citations": [
            {"evidence_id": EVIDENCE_ID, "source": f"tradelab:origin:{EVIDENCE_ID}"}
        ],
        "source_claims": {
            "from_source": "The source describes a bounded current thesis.",
            "from_transcripts": "Source text remains in hashed evidence.",
        },
        "model_interpretation": "Structure and risk remain planner-owned.",
        "status": "proposed",
        "planner_handoff_status": "needs_review",
        "evidence_ids": [EVIDENCE_ID],
        "model_receipt_id": "agent-run:fixture",
        "created_at": "2026-07-26T18:00:00Z",
    }


def _playbook() -> dict:
    return {
        "schema": "kamandal.playbook_proposal.v1",
        "proposal_id": "kamandal-playbook:fixture",
        "source_lead_id": "lead:fixture",
        "rule_kind": "iv",
        "proposed_rule": "Require IV percentile above 65 before long-vol review.",
        "current_rule": None,
        "applicability": {
            "instruments": ["SPX"],
            "market_conditions": "normalized skew and current option liquidity",
            "timeframes": ["1D"],
        },
        "invalidation": ["Planner replay contradicts the proposed filter."],
        "planner_contract_compatibility": "Review-time filter only; no leg selection.",
        "evidence_requirements": ["planner replay", "historical IV coverage"],
        "status": "proposed",
        "evidence_ids": [EVIDENCE_ID],
        "model_receipt_id": "agent-run:fixture",
        "created_at": "2026-07-26T18:00:00Z",
    }


def test_accepts_idea_without_effects() -> None:
    receipt = validate_strategy_discovery_candidate(
        _idea(),
        evidence_status_by_id={EVIDENCE_ID: "available"},
    )
    assert receipt["schema"] == RECEIPT_SCHEMA
    assert receipt["accepted"] is True
    assert receipt["candidate_kind"] == "idea"
    assert not any(receipt["effects"].values())


def test_accepts_playbook_without_effects() -> None:
    receipt = validate_strategy_discovery_candidate(
        _playbook(),
        evidence_status_by_id={EVIDENCE_ID: "available"},
    )
    assert receipt["accepted"] is True
    assert receipt["candidate_kind"] == "playbook"
    assert not any(receipt["effects"].values())


def test_rejects_stale_evidence_and_citation_loss() -> None:
    idea = _idea()
    idea["evidence_citations"] = []
    receipt = validate_strategy_discovery_candidate(
        idea,
        evidence_status_by_id={EVIDENCE_ID: "stale"},
    )
    assert receipt["accepted"] is False
    assert receipt["result"] == "needs_evidence"
    assert "evidence_citations_must_match_evidence_ids" in receipt["issues"]


def test_rejects_model_selected_legs_and_planner_advance() -> None:
    idea = deepcopy(_idea())
    idea["option_legs"] = [{"kind": "call"}]
    idea["planner_handoff_status"] = "proposed"
    receipt = validate_strategy_discovery_candidate(
        idea,
        evidence_status_by_id={EVIDENCE_ID: "available"},
    )
    assert receipt["accepted"] is False
    assert "$.option_legs:forbidden_model_authority" in receipt["issues"]
    assert "planner_handoff_must_be_needs_review" in receipt["issues"]


def test_rejects_malformed_nested_idea_fields() -> None:
    idea = deepcopy(_idea())
    idea["freshness_window"]["window_days"] = 0
    idea["evidence_citations"][0]["source"] = ""
    idea["source_claims"]["from_source"] = ""
    receipt = validate_strategy_discovery_candidate(
        idea,
        evidence_status_by_id={EVIDENCE_ID: "available"},
    )
    assert receipt["accepted"] is False
    assert "invalid_freshness_window_days" in receipt["issues"]
    assert "invalid_evidence_citation_source:0" in receipt["issues"]
    assert "invalid_source_claim:from_source" in receipt["issues"]


def test_rejects_malformed_playbook_applicability() -> None:
    proposal = deepcopy(_playbook())
    proposal["applicability"]["instruments"] = []
    proposal["evidence_requirements"] = []
    proposal["created_at"] = "later"
    receipt = validate_strategy_discovery_candidate(
        proposal,
        evidence_status_by_id={EVIDENCE_ID: "available"},
    )
    assert receipt["accepted"] is False
    assert "invalid_applicability_instruments" in receipt["issues"]
    assert "invalid_evidence_requirements" in receipt["issues"]
    assert "invalid_datetime:created_at" in receipt["issues"]
