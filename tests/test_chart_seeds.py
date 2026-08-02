from __future__ import annotations

import json
from pathlib import Path

import pytest

from kamandal_v2.intelligence.chart_seeds import import_chart_seed_evaluation


def _payload() -> dict[str, object]:
    source_id = "x-post:greg-weekly-fixture"
    return {
        "schema": "market_cartographer.seed_evaluation.v1",
        "status": "succeeded",
        "run_id": "7028f86b4cd26ad2",
        "as_of": "2026-07-30T20:00:00+00:00",
        "algorithm_version": "seeded-chart-v1+chart-funnel-v0.3",
        "source": {
            "kind": "greg_harmon_weekly",
            "source_id": source_id,
            "source_url": "https://x.com/harmongreg/status/fixture",
        },
        "data": {
            "provider": "deterministic-fixture",
            "mode": "DEMO DATA",
            "freshness": "sufficient_at_observation",
            "input_fingerprints": {"DE": {"daily": "abc"}},
        },
        "evaluations": [
            {
                "symbol": "DE",
                "source_context": {"source_id": source_id, "source_claim": "push over resistance"},
                "requested_setup_family": "approaching_resistance",
                "observed_setup_family": "breakout_continuation",
                "source_alignment": "partially_confirms",
                "signal_state": "retesting",
                "evaluation_status": "evaluated",
                "planner_eligible": False,
                "primary_boundary": {"lower": 250.0, "upper": 251.0},
                "confirmation_trigger": {"price": 251.5, "status": "triggered"},
                "failure_condition": {"price": 248.0},
                "reasons": ["daily breakout retest"],
                "counter_evidence": [],
                "evidence_refs": ["DE:daily:structure:breakout"],
            }
        ],
        "effects": {
            "broker": False,
            "orders": False,
            "auth": False,
            "schedule": False,
            "external_send": False,
            "planner_admission": False,
        },
    }


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "seed-evaluation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_chart_seed_import_is_research_only_and_idempotent(tmp_path: Path) -> None:
    source = _write(tmp_path, _payload())
    output = tmp_path / "research"

    first = import_chart_seed_evaluation(source, output_dir=output)
    second = import_chart_seed_evaluation(source, output_dir=output)

    assert first.created is True
    assert second.created is False
    assert first.import_id == second.import_id
    watch = json.loads(first.watch_path.read_text(encoding="utf-8"))
    assert watch["schema"] == "kamandal.chart_seed_watch.v1"
    assert watch["status"] == "research_only"
    assert watch["planner_eligible"] is False
    assert watch["effects"]["planner_admission"] is False
    assert watch["effects"]["shadow_admission"] is False
    assert watch["watches"][0]["source_id"] == "x-post:greg-weekly-fixture"
    assert watch["watches"][0]["chart_run_id"] == "7028f86b4cd26ad2"
    assert "not an idea YAML" in first.review_path.read_text(encoding="utf-8")


def test_chart_seed_import_rejects_planner_eligibility(tmp_path: Path) -> None:
    payload = _payload()
    payload["evaluations"][0]["planner_eligible"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="planner_eligible=false"):
        import_chart_seed_evaluation(_write(tmp_path, payload), output_dir=tmp_path / "out")


def test_chart_seed_import_rejects_source_identity_mismatch(tmp_path: Path) -> None:
    payload = _payload()
    payload["evaluations"][0]["source_context"]["source_id"] = "x-post:other"  # type: ignore[index]
    with pytest.raises(ValueError, match="source identity"):
        import_chart_seed_evaluation(_write(tmp_path, payload), output_dir=tmp_path / "out")


def test_chart_seed_import_rejects_any_protected_effect(tmp_path: Path) -> None:
    payload = _payload()
    payload["effects"]["orders"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="protected effects"):
        import_chart_seed_evaluation(_write(tmp_path, payload), output_dir=tmp_path / "out")
