from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from kamandal_v2.intelligence import market_questions
from kamandal_v2.intelligence.correspondent_signals import load_correspondent_profile
from kamandal_v2.intelligence.market_questions import (
    build_market_question_request,
    build_range_regime_request,
    run_market_question_exchange,
    validate_market_question_response,
)

PROFILE = Path("config/correspondents/greg_harmon.yaml").resolve()
AS_OF = "2026-08-21T14:00:00Z"


def _record(post_id: str, family: str, symbols: list[str]) -> dict:
    return {
        "schema": "birdclaw.correspondent_signal.v1",
        "signal_id": f"x-post:{post_id}",
        "profile_id": "greg_harmon",
        "source": {
            "kind": "public_x_post",
            "source_id": f"x-post:{post_id}",
            "source_url": f"https://x.com/harmongreg/status/{post_id}",
            "published_at": "2026-08-21T12:00:00Z",
            "author_handle": "harmongreg",
            "observation_sources": ["timeline"],
        },
        "classification": {"type": family},
        "literal": {
            "text": "Five trade ideas for the week "
            + " ".join(f"${item}" for item in symbols),
            "symbols": [
                {"symbol": item, "origin": "literal_cashtag"} for item in symbols
            ],
        },
    }


def _packet() -> dict:
    return {
        "schema": "birdclaw.correspondent_signals.v1",
        "generated_at": AS_OF,
        "records": [
            _record("weekly", "weekly_ideas", ["SPY", "QQQ"]),
            _record("earnings", "earnings_idea", ["TSLA"]),
        ],
    }


def _response(request: dict, *, direction: str = "bearish") -> dict:
    answers = []
    for question in request["questions"]:
        above = direction == "bullish"
        answers.append(
            {
                "question_id": question["question_id"],
                "question": "directional_setup",
                "symbol": question["symbol"],
                "answer_status": "evaluated",
                "direction": direction,
                "direction_hint": "neutral",
                "source_alignment": "not_requested",
                "setup_state": "triggered",
                "trigger": {
                    "rule": "fixture",
                    "direction": "ABOVE" if above else "BELOW",
                    "price": 100.0,
                    "status": "triggered",
                },
                "invalidation": {
                    "rule": "fixture",
                    "direction": "BELOW" if above else "ABOVE",
                    "price": 95.0 if above else 105.0,
                },
                "observed_at": request["as_of"],
                "trend_score": 3 if above else -3,
                "reasons": ["fixture"],
                "evidence_refs": [f"{question['symbol']}:daily:trend"],
                "source_context": {
                    **question["source"],
                    "source_claim": question["source_claim"],
                },
                "planner_eligible": False,
            }
        )
    return {
        "schema": "market_cartographer.question_response.v1",
        "status": "succeeded",
        "run_id": "question-run-1234",
        "as_of": request["as_of"],
        "algorithm_version": "fixture-v1",
        "data": {"provider": "fixture", "mode": "DEMO DATA"},
        "answers": answers,
        "effects": {
            "broker": False,
            "orders": False,
            "auth": False,
            "schedule": False,
            "external_send": False,
            "planner_admission": False,
        },
    }


def test_profile_builds_only_source_neutral_chart_questions() -> None:
    profile, _ = load_correspondent_profile(PROFILE)
    request = build_market_question_request(_packet(), profile)

    assert request is not None
    assert request["schema"] == "market_cartographer.question_request.v1"
    assert [item["symbol"] for item in request["questions"]] == ["SPY", "QQQ"]
    assert all(item["direction_hint"] == "neutral" for item in request["questions"])
    assert all("greg" not in item["question"] for item in request["questions"])


def test_market_question_exchange_writes_current_response_without_trading_authority(
    tmp_path: Path,
) -> None:
    profile, _ = load_correspondent_profile(PROFILE)
    binary = tmp_path / "market-cartographer"
    binary.write_text("fixture", encoding="utf-8")

    def runner(args: list[str], _cwd: Path) -> str:
        request = json.loads(
            Path(args[args.index("--input") + 1]).read_text(encoding="utf-8")
        )
        output = Path(args[args.index("--output") + 1])
        output.mkdir(parents=True)
        output.joinpath("question-response.json").write_text(
            json.dumps(_response(request)), encoding="utf-8"
        )
        return "{}"

    result = run_market_question_exchange(
        _packet(),
        profile,
        {
            "enabled": True,
            "request_dir": str(tmp_path / "requests"),
            "evaluation_dir": str(tmp_path / "responses"),
            "cartographer_bin": str(binary),
            "provider": "fixture",
        },
        command_runner=runner,
    )

    assert result.status == "succeeded"
    assert result.question_count == 2
    assert (
        result.response_path
        == tmp_path / "responses" / "greg_harmon" / "question-response.json"
    )
    payload = validate_market_question_response(
        json.loads(result.response_path.read_text(encoding="utf-8"))
    )
    assert all(answer["planner_eligible"] is False for answer in payload["answers"])


def test_market_question_failure_parks_chart_ideas_without_throwing(
    tmp_path: Path,
) -> None:
    profile, _ = load_correspondent_profile(PROFILE)
    binary = tmp_path / "market-cartographer"
    binary.write_text("fixture", encoding="utf-8")
    result = run_market_question_exchange(
        _packet(),
        profile,
        {
            "enabled": True,
            "request_dir": str(tmp_path / "requests"),
            "evaluation_dir": str(tmp_path / "responses"),
            "cartographer_bin": str(binary),
            "provider": "fixture",
        },
        command_runner=lambda _args, _cwd: (_ for _ in ()).throw(
            OSError("unavailable")
        ),
    )

    assert result.status == "failed"
    assert result.response_path is None
    assert "unavailable" in str(result.error)


def test_cartographer_exit_two_is_valid_negative_evidence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        market_questions.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2,
            stdout='{"status":"failed"}',
            stderr="insufficient evidence",
        ),
    )

    assert market_questions._run_command(["market-cartographer", "answer"], tmp_path) == '{"status":"failed"}'


def test_cartographer_other_nonzero_exit_remains_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        market_questions.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="transport failed",
        ),
    )

    try:
        market_questions._run_command(["market-cartographer", "answer"], tmp_path)
    except subprocess.CalledProcessError as exc:
        assert exc.returncode == 1
    else:
        raise AssertionError("exit one must remain a hard failure")


def test_range_request_is_source_neutral_and_deterministic() -> None:
    first = build_range_regime_request(
        ["SPY", "QQQ", "SPY"], as_of=AS_OF, playbook_id="short_strangle_high_iv"
    )
    second = build_range_regime_request(
        ["QQQ", "SPY"], as_of=AS_OF, playbook_id="short_strangle_high_iv"
    )

    assert first == second
    assert [item["symbol"] for item in first["questions"]] == ["QQQ", "SPY"]
    assert all(item["question"] == "range_regime" for item in first["questions"])
    assert all(item["direction_hint"] == "neutral" for item in first["questions"])
