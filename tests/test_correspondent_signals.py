from __future__ import annotations

import json
from pathlib import Path

import yaml

from kamandal_v2.intelligence.correspondent_signals import import_correspondent_signals
from kamandal_v2.planner.idea_loader import load_ideas


PROFILE = Path("config/correspondents/greg_harmon.yaml")
AS_OF = "2026-08-01T15:00:00Z"


def _record(
    post_id: str,
    tweet_type: str,
    text: str,
    symbols: list[str],
    *,
    idea_number: int | None = None,
    published_at: str = "2026-08-01T14:00:00Z",
    profile_id: str = "greg_harmon",
) -> dict:
    source_id = f"x-post:{post_id}"
    return {
        "schema": "birdclaw.correspondent_signal.v1",
        "signal_id": source_id,
        "profile_id": profile_id,
        "source": {
            "kind": "public_x_post",
            "source_id": source_id,
            "source_url": f"https://x.com/example/status/{post_id}",
            "published_at": published_at,
            "author_handle": "example",
            "expanded_urls": [],
            "observation_sources": ["timeline"],
        },
        "classification": {
            "type": tweet_type,
            "rule_id": f"test_{tweet_type}",
            "interpretation_status": "deterministic_profile",
        },
        "literal": {
            "text": text,
            "symbols": [{"symbol": symbol, "origin": "literal_cashtag"} for symbol in symbols],
            "idea_number": idea_number,
        },
    }


def _packet(records: list[dict], *, profile_id: str = "greg_harmon") -> dict:
    return {
        "schema": "birdclaw.correspondent_signals.v1",
        "generated_at": AS_OF,
        "profile": {
            "schema": "birdclaw.correspondent_profile.v1",
            "profile_id": profile_id,
            "version": "1",
            "author_handles": ["example"],
            "profile_sha256": "a" * 64,
        },
        "records": records,
        "counts": {},
        "safety": {
            "visibility": "public",
            "sanitization": "sanitized",
            "read_only": True,
            "network_call_performed": False,
            "x_mutation_performed": False,
            "raw_payload_returned": False,
            "database_handle_exposed": False,
        },
    }


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _chart(source_id: str, symbol: str, *, trigger_status: str = "triggered") -> dict:
    return {
        "schema": "market_cartographer.seed_evaluation.v1",
        "status": "succeeded",
        "run_id": "chart-run-1234",
        "as_of": AS_OF,
        "algorithm_version": "test-v1",
        "source": {"source_id": source_id},
        "data": {"mode": "DEMO DATA", "freshness": "sufficient_at_observation"},
        "evaluations": [
            {
                "symbol": symbol,
                "evaluation_status": "evaluated",
                "planner_eligible": False,
                "source_context": {"source_id": source_id},
                "requested_setup_family": "unspecified",
                "observed_setup_family": "breakout_continuation",
                "source_alignment": "confirms",
                "signal_state": "holding",
                "primary_boundary": {"lower": 100.0, "upper": 101.0},
                "confirmation_trigger": {
                    "rule": "close_above_buffered_resistance",
                    "price": 101.5,
                    "status": trigger_status,
                },
                "failure_condition": {"rule": "close_below_support", "price": 97.0},
                "reasons": ["test evidence"],
                "counter_evidence": [],
                "evidence_refs": [f"{symbol}:daily:zone:0"],
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


def test_all_greg_families_are_preserved_and_only_openings_reach_planner(tmp_path: Path) -> None:
    records = [
        _record("101", "earnings_idea", "Took TSLA trade idea #4", ["TSLA"], idea_number=4),
        _record("102", "weekly_ideas", "Five trade ideas $SPY", ["SPY"]),
        _record("103", "trade_journal", "Bought $IWM call spread for 1.25", ["IWM"]),
        _record("104", "trade_journal", "Closed $IWM call spread", ["IWM"]),
        _record("105", "unknown", "Watching $QQQ here", ["QQQ"]),
        _record("106", "irrelevant", "Great game last night", []),
    ]
    input_path = _write_json(tmp_path / "signals.json", _packet(records))
    output_dir = tmp_path / "output"

    first = import_correspondent_signals(
        input_path,
        profile_path=PROFILE,
        universe_symbols={"TSLA", "SPY", "IWM", "QQQ"},
        output_dir=output_dir,
    )
    second = import_correspondent_signals(
        input_path,
        profile_path=PROFILE,
        universe_symbols={"TSLA", "SPY", "IWM", "QQQ"},
        output_dir=output_dir,
    )

    assert first.record_count == 6
    assert first.planner_idea_count == 2
    assert first.created is True
    assert second.created is False
    translation = json.loads(first.translation_path.read_text(encoding="utf-8"))
    by_source = {record["signal_id"]: record for record in translation["records"]}
    assert by_source["x-post:101"]["strategy_family"] == "short_strangle"
    assert by_source["x-post:101"]["planner_eligible"] is True
    assert by_source["x-post:102"]["planner_blockers"] == ["chart_evaluation_missing"]
    assert by_source["x-post:103"]["planner_eligible"] is True
    assert "journal_close_is_not_new_entry" in by_source["x-post:104"]["planner_blockers"]
    assert by_source["x-post:105"]["status"] == "needs_review"
    assert by_source["x-post:106"]["status"] == "ignored"
    ideas = load_ideas([first.planner_ideas_path])
    assert {idea.underlying for idea in ideas} == {"TSLA", "IWM"}
    assert {tuple(idea.allowed_structures) for idea in ideas} == {("short_strangle",), ("call_spread",)}
    lifecycle = json.loads(first.lifecycle_path.read_text(encoding="utf-8"))
    iwm = next(item for item in lifecycle["lifecycles"] if item["key"] == "greg_harmon:IWM:call_spread")
    assert [event["action"] for event in iwm["events"]] == ["open", "close"]
    assert iwm["events"][1]["linked"] is True


def test_triggered_weekly_chart_signal_becomes_constrained_planner_idea(tmp_path: Path) -> None:
    source_id = "x-post:201"
    input_path = _write_json(
        tmp_path / "signals.json",
        _packet([_record("201", "weekly_ideas", "Five trade ideas $SPY", ["SPY"])]),
    )
    chart_path = _write_json(tmp_path / "chart.json", _chart(source_id, "SPY"))

    result = import_correspondent_signals(
        input_path,
        profile_path=PROFILE,
        universe_symbols={"SPY"},
        chart_evaluation_paths=[chart_path],
        output_dir=tmp_path / "output",
    )

    assert result.planner_idea_count == 1
    idea = load_ideas([result.planner_ideas_path])[0]
    assert idea.underlying == "SPY"
    assert idea.allowed_structures == ["call_spread", "call_diagonal"]
    translation = json.loads(result.translation_path.read_text(encoding="utf-8"))
    assert translation["records"][0]["activation"]["status"] == "triggered"


def test_birdclaw_acquisition_receipt_is_preserved_in_translation_and_review(tmp_path: Path) -> None:
    packet = _packet([_record("251", "earnings_idea", "Took TSLA trade idea #4", ["TSLA"], idea_number=4)])
    packet["acquisition"] = {
        "schema": "birdclaw.correspondent_acquisition_reference.v1",
        "status": "succeeded",
        "receipt_generated_at": AS_OF,
        "receipt_run_id": "birdclaw-run-1",
        "attempts": [{"profile_id": "greg_harmon", "status": "succeeded", "coverage_status": "continuous"}],
    }
    input_path = _write_json(tmp_path / "signals.json", packet)
    result = import_correspondent_signals(
        input_path,
        profile_path=PROFILE,
        universe_symbols={"TSLA"},
        output_dir=tmp_path / "output",
    )

    translation = json.loads(result.translation_path.read_text(encoding="utf-8"))
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    review = result.review_path.read_text(encoding="utf-8")
    assert translation["source_acquisition"]["status"] == "succeeded"
    assert receipt["source_acquisition"]["receipt_run_id"] == "birdclaw-run-1"
    assert "Birdclaw acquisition: `succeeded`" in review


def test_unsupported_and_out_of_universe_signals_remain_visible_but_parked(tmp_path: Path) -> None:
    input_path = _write_json(
        tmp_path / "signals.json",
        _packet([
            _record("301", "earnings_idea", "Took ABC trade idea #1", ["ABC"], idea_number=1),
            _record("302", "earnings_idea", "Took OUT trade idea #4", ["OUT"], idea_number=4),
        ]),
    )
    result = import_correspondent_signals(
        input_path,
        profile_path=PROFILE,
        universe_symbols={"ABC"},
        output_dir=tmp_path / "output",
    )
    translation = json.loads(result.translation_path.read_text(encoding="utf-8"))
    by_symbol = {record["symbol"]: record for record in translation["records"]}
    assert "planner_structure_unsupported" in by_symbol["ABC"]["planner_blockers"]
    assert "outside_configured_universe" in by_symbol["OUT"]["planner_blockers"]
    assert result.planner_idea_count == 0
    assert len(load_ideas([result.planner_ideas_path])) == 0


def test_second_correspondent_uses_profile_configuration_without_new_code(tmp_path: Path) -> None:
    profile_path = tmp_path / "sample.yaml"
    profile_path.write_text(
        yaml.safe_dump({
            "schema": "kamandal.correspondent_profile.v1",
            "profile_id": "sample_person",
            "version": "1",
            "source_profile_id": "sample_person",
            "families": {
                "swing_entry": {
                    "mode": "trade_journal",
                    "max_age_hours": 24,
                    "planner": {"supported": True, "thesis_tags": ["sample_swing"], "horizon_days": 21},
                }
            },
            "strategy_rules": [
                {
                    "id": "call_spread",
                    "regex": r"\bcall spread\b",
                    "strategy_family": "call_spread",
                    "direction": "bullish",
                    "allowed_structures": ["call_spread"],
                }
            ],
            "journal_actions": [{"action": "open", "regex": r"\bbought\b"}],
        }, sort_keys=False),
        encoding="utf-8",
    )
    record = _record(
        "401",
        "swing_entry",
        "Bought $SPY call spread",
        ["SPY"],
        profile_id="sample_person",
    )
    input_path = _write_json(tmp_path / "signals.json", _packet([record], profile_id="sample_person"))

    result = import_correspondent_signals(
        input_path,
        profile_path=profile_path,
        universe_symbols={"SPY"},
        output_dir=tmp_path / "output",
    )

    assert result.profile_id == "sample_person"
    assert result.planner_idea_count == 1
    assert load_ideas([result.planner_ideas_path])[0].allowed_structures == ["call_spread"]


def test_journal_direction_uses_profile_action_language(tmp_path: Path) -> None:
    input_path = _write_json(
        tmp_path / "signals.json",
        _packet([
            _record("501", "trade_journal", "Bought $IWM call spread", ["IWM"]),
            _record("502", "trade_journal", "Sold $QQQ call spread", ["QQQ"]),
        ]),
    )
    result = import_correspondent_signals(
        input_path,
        profile_path=PROFILE,
        universe_symbols={"IWM", "QQQ"},
        output_dir=tmp_path / "output",
    )
    translation = json.loads(result.translation_path.read_text(encoding="utf-8"))
    by_symbol = {record["symbol"]: record for record in translation["records"]}
    assert by_symbol["IWM"]["direction"] == "bullish"
    assert by_symbol["QQQ"]["direction"] == "bearish"


def test_profile_revision_replaces_latest_lifecycle_projection_without_duplicate_event(tmp_path: Path) -> None:
    input_path = _write_json(
        tmp_path / "signals.json",
        _packet([_record("601", "trade_journal", "Bought $IWM call spread", ["IWM"])]),
    )
    output_dir = tmp_path / "output"
    import_correspondent_signals(
        input_path,
        profile_path=PROFILE,
        universe_symbols={"IWM"},
        output_dir=output_dir,
    )
    revised_profile = tmp_path / "greg-v2.yaml"
    revised_profile.write_text(
        PROFILE.read_text(encoding="utf-8").replace('version: "1"', 'version: "2"', 1),
        encoding="utf-8",
    )
    revised = import_correspondent_signals(
        input_path,
        profile_path=revised_profile,
        universe_symbols={"IWM"},
        output_dir=output_dir,
    )
    lifecycle = json.loads(revised.lifecycle_path.read_text(encoding="utf-8"))
    assert len(lifecycle["lifecycles"]) == 1
    assert len(lifecycle["lifecycles"][0]["events"]) == 1
