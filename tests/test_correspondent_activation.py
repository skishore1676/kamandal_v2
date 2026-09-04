from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest
import yaml

from kamandal_v2.intelligence.correspondent_activation import _chart_evaluation_paths, activate_correspondent_sources
from kamandal_v2.intelligence.observed_packages import load_observed_package_feed
from kamandal_v2.planner.idea_loader import load_ideas
from kamandal_v2.stores.sqlite import LocalStore
from tests.test_market_questions import _response as _market_response


PROFILE = Path("config/correspondents/greg_harmon.yaml").resolve()
AS_OF = "2026-08-01T15:00:00Z"


def _packet(*, actionable: bool) -> dict:
    records = []
    if actionable:
        records.append(
            {
                "schema": "birdclaw.correspondent_signal.v1",
                "signal_id": "x-post:live-idea-4",
                "profile_id": "greg_harmon",
                "source": {
                    "kind": "public_x_post",
                    "source_id": "x-post:live-idea-4",
                    "source_url": "https://x.com/harmongreg/status/live-idea-4",
                    "published_at": "2026-08-01T14:00:00Z",
                    "author_handle": "harmongreg",
                    "expanded_urls": [],
                    "observation_sources": ["correspondent:greg_harmon:harmongreg"],
                },
                "classification": {
                    "type": "earnings_bundle",
                    "rule_id": "test_earnings",
                    "interpretation_status": "deterministic_profile",
                },
                "literal": {
                    "text": "4 Trade Ideas for Tesla $TSLA",
                    "symbols": [{"symbol": "TSLA", "origin": "literal_cashtag"}],
                    "idea_number": None,
                },
            }
        )
    return {
        "schema": "birdclaw.correspondent_signals.v1",
        "generated_at": AS_OF,
        "profile": {
            "schema": "birdclaw.correspondent_profile.v1",
            "profile_id": "greg_harmon",
            "version": "1",
            "author_handles": ["harmongreg"],
            "profile_sha256": "a" * 64,
        },
        "records": records,
        "counts": {"earnings_idea": len(records)},
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


def _settings(tmp_path: Path) -> dict:
    birdclaw_root = tmp_path / "birdclaw"
    birdclaw_root.mkdir()
    (birdclaw_root / "birdclawctl").write_text("test executable placeholder\n", encoding="utf-8")
    return {
        "enabled": True,
        "mode": "active_planner",
        "trial_root": str(birdclaw_root),
        "output_dir": str(tmp_path / "research"),
        "active_ideas_dir": str(tmp_path / "active"),
        "since_hours": 336,
        "limit": 200,
        "profiles": [
            {
                "profile_id": "greg_harmon",
                "source_profile_id": "greg_harmon",
                "profile_path": str(PROFILE),
                "enabled": True,
            }
        ],
    }


def test_production_x_job_refuses_retired_bookmark_fallback() -> None:
    script = Path("scripts/run_x_bookmark_extraction.sh").read_text(encoding="utf-8")

    assert "import-x-bookmarks" not in script
    assert "retired bookmark fallback is disabled" in script


def test_activation_publishes_eligible_ideas_and_clears_them_when_no_longer_eligible(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalStore(tmp_path / "kamandal.db")
    commands: list[list[str]] = []

    def actionable_runner(args: list[str], _cwd: Path) -> str:
        commands.append(args)
        return json.dumps(_packet(actionable=True))

    activated = activate_correspondent_sources(
        settings,
        universe_symbols={"TSLA"},
        command_runner=actionable_runner,
        store=store,
    )

    assert activated.status == "succeeded"
    assert activated.planner_idea_count == 1
    assert commands[0][commands[0].index("--profile") + 1] == "greg_harmon"
    assert commands[0][commands[0].index("--since-hours") + 1] == "336"
    active_path = activated.active_idea_paths[0]
    ideas = load_ideas([active_path])
    assert len(ideas) == 1
    assert ideas[0].underlying == "TSLA"
    assert ideas[0].allowed_structures == ["short_strangle"]
    receipt = json.loads(activated.receipt_path.read_text(encoding="utf-8"))
    assert receipt["effects"]["active_idea_publication"] is True
    assert all(
        receipt["effects"][key] is False
        for key in ("planner_run", "live_admission", "sheet_write", "broker", "orders")
    )

    cleared = activate_correspondent_sources(
        settings,
        universe_symbols={"TSLA"},
        command_runner=lambda _args, _cwd: json.dumps(_packet(actionable=False)),
        store=store,
    )

    assert cleared.planner_idea_count == 0
    assert load_ideas([active_path]) == []


def test_activation_failure_clears_previous_active_idea_and_writes_failure_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    active_path = Path(settings["active_ideas_dir"]) / "correspondent_greg_harmon.yaml"
    active_path.parent.mkdir(parents=True)
    active_path.write_text(
        yaml.safe_dump({"ideas": [{"idea_id": "stale", "underlying": "TSLA", "operator_status": "pending"}]}),
        encoding="utf-8",
    )

    def failed_runner(_args: list[str], _cwd: Path) -> str:
        raise OSError("Birdclaw unavailable")

    result = activate_correspondent_sources(
        settings,
        universe_symbols={"TSLA"},
        command_runner=failed_runner,
        store=LocalStore(tmp_path / "kamandal.db"),
    )

    assert result.status == "degraded"
    assert result.source_failure_count == 1
    assert load_ideas([active_path]) == []
    receipt = json.loads(
        (Path(settings["output_dir"]) / "activation" / "latest.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "degraded"
    assert receipt["source_failures"][0]["profile_id"] == "greg_harmon"
    assert receipt["effects"]["orders"] is False


def test_one_source_failure_does_not_clear_a_healthy_sibling(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings["profiles"].append(
        {
            "profile_id": "mike_butler",
            "source_profile_id": "mike_butler",
            "profile_path": str(Path("config/correspondents/mike_butler.yaml").resolve()),
            "enabled": True,
        }
    )
    mike_packet = _packet(actionable=False)
    mike_packet["profile"]["profile_id"] = "mike_butler"
    rows = [
        {"source_id": "greg_harmon", "output_kind": "idea", "mode": "live"},
        {"source_id": "greg_harmon", "output_kind": "exact_package", "mode": "observe"},
        {"source_id": "mike_butler", "output_kind": "idea", "mode": "off"},
        {"source_id": "mike_butler", "output_kind": "exact_package", "mode": "shadow"},
    ]

    def runner(args: list[str], _cwd: Path) -> str:
        profile = args[args.index("--profile") + 1]
        if profile == "greg_harmon":
            raise OSError("Greg fetch unavailable")
        return json.dumps(mike_packet)

    result = activate_correspondent_sources(
        settings,
        universe_symbols={"TSLA"},
        command_runner=runner,
        store=LocalStore(tmp_path / "kamandal.db"),
        trade_source_rows=rows,
    )

    assert result.status == "degraded"
    assert result.profile_count == 2
    assert result.source_failure_count == 1
    assert load_ideas([Path(settings["active_ideas_dir"]) / "correspondent_greg_harmon.yaml"]) == []
    assert load_ideas([Path(settings["active_ideas_dir"]) / "correspondent_mike_butler.yaml"]) == []
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    assert [profile["profile_id"] for profile in receipt["profiles"]] == ["mike_butler"]
    assert receipt["source_failures"][0]["profile_id"] == "greg_harmon"


def test_observed_package_profile_publishes_typed_feed_and_reuses_cache(tmp_path: Path) -> None:
    fixture_root = Path("tests/fixtures/mike_observed_packages")
    manifest = json.loads((fixture_root / "ground-truth.json").read_text(encoding="utf-8"))
    fixture = manifest["fixtures"][0]
    image = fixture["images"][0]
    image_path = (fixture_root / image["path"]).resolve()
    settings = _settings(tmp_path)
    settings["profiles"] = [
        {
            "profile_id": "mike_butler",
            "source_profile_id": "mike_butler",
            "profile_path": str(Path("config/correspondents/mike_butler.yaml").resolve()),
            "source_mode": "observed_package",
            "enabled": True,
        }
    ]
    packet = {
        "schema": "birdclaw.correspondent_signals.v1",
        "generated_at": fixture["published_at"],
        "profile": {"profile_id": "mike_butler"},
        "records": [
            {
                "schema": "birdclaw.correspondent_signal.v1",
                "signal_id": f"x-post:{fixture['post_id']}",
                "profile_id": "mike_butler",
                "source": {
                    "kind": "public_x_post",
                    "source_id": f"x-post:{fixture['post_id']}",
                    "published_at": fixture["published_at"],
                    "media": [{
                        "media_index": 1,
                        "type": "photo",
                        "cache_status": "cached",
                        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                        "artifact_path": str(image_path),
                    }],
                },
                "classification": {
                    "type": "observed_package_open",
                    "rule_id": "explicit_new_package",
                    "interpretation_status": "deterministic_profile",
                },
                "literal": {
                    "text": fixture["post_text"],
                    "symbols": [{"symbol": "ADSK", "origin": "literal_cashtag"}],
                    "idea_number": None,
                },
            }
        ],
        "counts": {"observed_package_open": 1},
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

    class FakeClient:
        calls = 0

        def chat_json(self, _system: str, _user: str, *, images: tuple[str, ...] = ()) -> dict:
            self.calls += 1
            assert images == (str(image_path),)
            package = fixture["expected_extraction"]["packages"][0]
            return {
                "schema": "kamandal.source_episode_interpretation.v1",
                "episodes": [{
                    "signal_id": f"x-post:{fixture['post_id']}",
                    "events": [{
                        "action": "open",
                        "symbol": "ADSK",
                        "direction": "bullish",
                        "structure_hint": "call_calendar",
                        "thesis": "New ADSK call calendar for earnings",
                        "semantic_confidence": 0.99,
                        "evidence_status": "complete",
                        "projections": ["idea", "exact_package"],
                        "exact_packages": [{
                            "complete": True,
                            "blocker": None,
                            "displayed_price": package["displayed_price"],
                            "legs": package["legs"],
                            "field_provenance": ["image:1"],
                        }],
                        "blockers": [],
                        "template_number": None,
                    }],
                }],
            }

    client = FakeClient()
    runner = lambda _args, _cwd: json.dumps(packet)
    result = activate_correspondent_sources(
        settings,
        universe_symbols=set(),
        command_runner=runner,
        store=LocalStore(tmp_path / "kamandal.db"),
        source_episode_client=client,
    )
    second = activate_correspondent_sources(
        settings,
        universe_symbols=set(),
        command_runner=runner,
        store=LocalStore(tmp_path / "kamandal.db"),
        source_episode_client=client,
    )

    assert result.planner_idea_count == 0
    assert result.observed_package_batch_count == 1
    assert second.observed_package_batch_count == 1
    assert client.calls == 1
    assert load_observed_package_feed(result.observed_package_feed_path)[0].packages[0].symbol == "ADSK"
    assert load_ideas([result.active_idea_paths[0]]) == []


def test_activation_records_outside_universe_mentions_for_weekly_review(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalStore(tmp_path / "kamandal.db")

    result = activate_correspondent_sources(
        settings,
        universe_symbols=set(),
        command_runner=lambda _args, _cwd: json.dumps(_packet(actionable=True)),
        store=store,
    )

    assert result.planner_idea_count == 0
    candidates = store.discovery_candidates()
    assert [item["symbol"] for item in candidates] == ["TSLA"]
    assert candidates[0]["source_profiles"] == ["greg_harmon"]


def test_production_x_job_invokes_correspondent_activation_before_llm_extraction() -> None:
    script = Path("scripts/run_x_bookmark_extraction.sh").read_text(encoding="utf-8")
    activation = script.index("activate-correspondent-signals")
    llm = script.index("extract-ideas-llm")
    assert activation < llm
    assert "--config-source sheet" in script


def test_chart_discovery_ignores_seed_requests_and_unrelated_json(tmp_path: Path) -> None:
    chart_root = tmp_path / "chart_seeds"
    request_dir = chart_root / "requests"
    request_dir.mkdir(parents=True)
    request = request_dir / "pending.json"
    request.write_text(json.dumps({"schema": "market_cartographer.seed_request.v1"}), encoding="utf-8")
    (chart_root / "receipt.json").write_text(
        json.dumps({"schema": "kamandal.chart_seed_import_receipt.v1"}),
        encoding="utf-8",
    )
    evaluation = chart_root / "evaluation.json"
    evaluation.write_text(
        json.dumps({"schema": "market_cartographer.seed_evaluation.v1"}),
        encoding="utf-8",
    )

    paths = _chart_evaluation_paths(
        {"chart_seeds": {"enabled": True, "evaluation_dir": str(chart_root)}}
    )

    assert paths == [evaluation]


def test_production_x_job_delegates_current_post_enrichment_to_activation() -> None:
    script = Path("scripts/run_x_bookmark_extraction.sh").read_text(encoding="utf-8")

    assert "Activating configured correspondent signals" in script
    assert "chart_seed_request" not in script
    assert "evaluate-seeds" not in script


def test_activation_asks_current_packet_then_publishes_bearish_diagonal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    binary = tmp_path / "market-cartographer"
    binary.write_text("fixture", encoding="utf-8")
    settings["market_questions"] = {
        "enabled": True,
        "request_dir": str(tmp_path / "questions"),
        "evaluation_dir": str(tmp_path / "answers"),
        "cartographer_bin": str(binary),
        "provider": "fixture",
    }
    packet = _packet(actionable=False)
    packet["records"] = [
        {
            "schema": "birdclaw.correspondent_signal.v1",
            "signal_id": "x-post:weekly-current",
            "profile_id": "greg_harmon",
            "source": {
                "kind": "public_x_post",
                "source_id": "x-post:weekly-current",
                "source_url": "https://x.com/harmongreg/status/weekly-current",
                "published_at": "2026-08-01T14:00:00Z",
                "author_handle": "harmongreg",
                "expanded_urls": [],
                "observation_sources": ["timeline"],
            },
            "classification": {
                "type": "weekly_ideas",
                "rule_id": "weekly",
                "interpretation_status": "deterministic_profile",
            },
            "literal": {
                "text": "Five trade ideas $SPY",
                "symbols": [{"symbol": "SPY", "origin": "literal_cashtag"}],
                "idea_number": None,
            },
        }
    ]

    def market_runner(args: list[str], _cwd: Path) -> str:
        request = json.loads(Path(args[args.index("--input") + 1]).read_text(encoding="utf-8"))
        output = Path(args[args.index("--output") + 1])
        output.mkdir(parents=True)
        output.joinpath("question-response.json").write_text(
            json.dumps(_market_response(request, direction="bearish")), encoding="utf-8"
        )
        return "{}"

    class EpisodeClient:
        def chat_json(self, _system: str, _user: str, *, images: tuple[str, ...] = ()) -> dict:
            assert not images
            return {
                "schema": "kamandal.source_episode_interpretation.v1",
                "episodes": [{
                    "signal_id": "x-post:weekly-current",
                    "events": [{
                        "action": "open",
                        "symbol": "SPY",
                        "direction": "bullish",
                        "structure_hint": "call_diagonal",
                        "thesis": "Directional SPY setup",
                        "semantic_confidence": 0.9,
                        "evidence_status": "complete",
                        "projections": ["idea"],
                        "exact_packages": [],
                        "blockers": [],
                        "template_number": None,
                    }],
                }],
            }

    result = activate_correspondent_sources(
        settings,
        universe_symbols={"SPY"},
        command_runner=lambda _args, _cwd: json.dumps(packet),
        market_command_runner=market_runner,
        store=LocalStore(tmp_path / "kamandal.db"),
        source_episode_client=EpisodeClient(),
    )

    ideas = load_ideas([result.active_idea_paths[0]])
    assert len(ideas) == 1
    assert ideas[0].direction == "bearish"
    assert ideas[0].allowed_structures == ["put_diagonal"]
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    assert receipt["profiles"][0]["market_questions"]["status"] == "succeeded"
