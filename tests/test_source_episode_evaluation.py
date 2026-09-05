from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path("scripts/evaluate_source_episode_models.py")
SPEC = importlib.util.spec_from_file_location("source_episode_evaluation", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_packets_preserve_missing_media_as_an_evidence_gate() -> None:
    cases = [
        {
            "source_id": "mike_butler",
            "post_ref": "x-post:1",
            "text": "Downside put diagonal in $LULU",
            "gold_status": "complete_from_operator_media_read",
            "expected_events": [
                {
                    "action": "open",
                    "symbol": "LULU",
                    "structure_hint": "put_diagonal",
                    "legs": [{"ratio": 1}],
                }
            ],
        }
    ]

    packet = MODULE._build_packets(cases)["mike_butler"]

    assert packet["records"][0]["source"]["media"][0]["cache_status"] == "missing"


def test_score_fails_hard_gate_for_false_entry_and_invented_media_package() -> None:
    cases = [
        {
            "source_id": "mike_butler",
            "post_ref": "x-post:1",
            "text": "Closed a prior package",
            "gold_status": "partial_needs_media_and_history",
            "expected_events": [
                {
                    "action": "close",
                    "symbol": "DELL",
                    "structure_hint": "call_calendar",
                    "planner_new_entry": False,
                }
            ],
        }
    ]
    episodes = [
        {
            "post_ref": "x-post:1",
            "events": [
                {
                    "action": "close",
                    "symbol": "DELL",
                    "structure_hint": "call_calendar",
                    "planner_new_entry": True,
                    "exact_packages": [{"complete": True}],
                }
            ],
            "effects": {},
        }
    ]

    score = MODULE._score(cases, episodes)

    assert score["false_new_entry_count"] == 1
    assert score["invented_media_package_count"] == 1
    assert score["hard_gate_pass"] is False


def test_model_evaluation_uses_separate_metering_lane(monkeypatch):
    import pytest
    captured = {}
    class StopBeforeModel(Exception):
        pass
    def client(**kwargs):
        captured.update(kwargs)
        raise StopBeforeModel
    monkeypatch.setattr(MODULE, "BrokerJsonClient", client)
    with pytest.raises(StopBeforeModel):
        MODULE._run_model("gpt-5.6-terra", 1, [], reasoning_effort="medium")
    assert captured["lane_id"] == "kamandal_evaluation"
    from kamandal_v2.intelligence.llm_client import BrokerJsonClient
    assert BrokerJsonClient().lane_id == "kamandal"
