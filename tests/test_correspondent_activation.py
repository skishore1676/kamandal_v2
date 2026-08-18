from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from kamandal_v2.intelligence.correspondent_activation import _chart_evaluation_paths, activate_correspondent_sources
from kamandal_v2.planner.idea_loader import load_ideas
from kamandal_v2.stores.sqlite import LocalStore


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
                    "type": "earnings_idea",
                    "rule_id": "test_earnings",
                    "interpretation_status": "deterministic_profile",
                },
                "literal": {
                    "text": "Took $TSLA Trade Idea 4",
                    "symbols": [{"symbol": "TSLA", "origin": "literal_cashtag"}],
                    "idea_number": 4,
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

    with pytest.raises(RuntimeError, match="failed closed"):
        activate_correspondent_sources(
            settings,
            universe_symbols={"TSLA"},
            command_runner=failed_runner,
            store=LocalStore(tmp_path / "kamandal.db"),
        )

    assert load_ideas([active_path]) == []
    receipt = json.loads(
        (Path(settings["output_dir"]) / "activation" / "latest.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "failed_closed"
    assert receipt["effects"]["orders"] is False


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


def test_production_x_job_uses_sibling_cartographer_venv() -> None:
    script = Path("scripts/run_x_bookmark_extraction.sh").read_text(encoding="utf-8")

    assert "KAMANDAL_MARKET_CARTOGRAPHER_BIN" in script
    assert "$REPO_ROOT/../market-cartographer/.venv/bin/market-cartographer" in script
    assert "leaving the request pending without treating it as an evaluation" in script
