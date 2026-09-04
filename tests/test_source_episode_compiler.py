from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from kamandal_v2.intelligence.source_episode_compiler import (
    EPISODE_SCHEMA,
    PROMPT_SCHEMA,
    compile_source_episode_packet,
    load_episode_history,
    write_episode_compilation,
)


class FakeClient:
    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.last_receipt_summary = {"status": "succeeded", "provider_id": "fake"}

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        images: tuple[str, ...] = (),
    ) -> dict:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "images": images,
            }
        )
        return self.responses.pop(0)


def _profile(name: str) -> dict:
    return yaml.safe_load(Path(f"config/correspondents/{name}.yaml").read_text(encoding="utf-8"))


def _record(
    post_id: str,
    text: str,
    symbols: list[str],
    *,
    classification: str = "unknown",
    published_at: str = "2026-09-03T14:00:00Z",
    media: list[dict] | None = None,
) -> dict:
    signal_id = f"x-post:{post_id}"
    source = {
        "kind": "public_x_post",
        "source_id": signal_id,
        "source_url": f"https://x.com/example/status/{post_id}",
        "published_at": published_at,
        "author_handle": "example",
        "expanded_urls": [],
        "observation_sources": ["timeline"],
    }
    if media is not None:
        source["media"] = media
    return {
        "schema": "birdclaw.correspondent_signal.v1",
        "signal_id": signal_id,
        "profile_id": "test",
        "source": source,
        "classification": {
            "type": classification,
            "rule_id": f"test_{classification}",
            "interpretation_status": "deterministic_profile",
        },
        "literal": {
            "text": text,
            "symbols": [
                {"symbol": symbol, "origin": "literal_cashtag"} for symbol in symbols
            ],
            "idea_number": None,
        },
    }


def _packet(records: list[dict]) -> dict:
    return {
        "schema": "birdclaw.correspondent_signals.v1",
        "generated_at": "2026-09-03T15:00:00Z",
        "records": records,
    }


def _event(**overrides: object) -> dict:
    result = {
        "action": "open",
        "symbol": "LULU",
        "direction": "bearish",
        "structure_hint": "put_diagonal",
        "thesis": "Downside earnings diagonal",
        "semantic_confidence": 0.93,
        "evidence_status": "complete",
        "projections": ["idea"],
        "exact_packages": [],
        "blockers": [],
        "template_number": None,
    }
    result.update(overrides)
    return result


def test_greg_bundle_and_confirmation_are_deterministic_and_deduplicated() -> None:
    packet = _packet(
        [
            _record(
                "confirm",
                "took $AVGO trade idea #4",
                ["AVGO"],
                classification="earnings_idea",
                published_at="2026-09-03T14:05:00Z",
            ),
            _record(
                "bundle",
                "Premium Earnings 9-3-26: Broadcom, on the blog and here $AVGO",
                ["AVGO"],
                classification="earnings_bundle",
                published_at="2026-09-03T14:00:00Z",
            ),
        ]
    )
    client = FakeClient()

    compilation = compile_source_episode_packet(
        packet,
        _profile("greg_harmon"),
        client,
    )

    assert client.calls == []
    bundle, confirmation = compilation.episodes
    assert [event["template_number"] for event in bundle["events"]] == [1, 2, 3, 4]
    assert [event["planner_new_entry"] for event in bundle["events"]] == [
        False,
        False,
        False,
        True,
    ]
    confirmed = confirmation["events"][0]
    assert confirmed["planner_new_entry"] is False
    assert confirmed["projections"] == ["residual"]
    assert confirmed["link_state"] == "linked"
    assert confirmed["links_to"] == [bundle["events"][3]["event_id"]]


def test_mixed_post_decomposes_events_and_follow_up_cannot_become_entry() -> None:
    record = _record(
        "mixed",
        "Closed $DELL calendar and opened a new $LULU downside diagonal",
        ["DELL", "LULU"],
    )
    response = {
        "schema": PROMPT_SCHEMA,
        "episodes": [
            {
                "signal_id": record["signal_id"],
                "events": [
                    _event(
                        action="close",
                        symbol="DELL",
                        direction="bullish",
                        structure_hint="call_calendar",
                        projections=["idea", "residual"],
                        thesis="Closed prior calendar",
                    ),
                    _event(),
                ],
            }
        ],
    }
    client = FakeClient(response)

    compilation = compile_source_episode_packet(
        _packet([record]),
        _profile("mike_butler"),
        client,
    )

    close, opening = compilation.episodes[0]["events"]
    assert close["projections"] == ["residual"]
    assert close["planner_new_entry"] is False
    assert close["link_state"] == "needs_history"
    assert opening["planner_new_entry"] is True
    assert opening["opportunity_group_id"] != close["opportunity_group_id"]
    assert compilation.to_dict()["effects"] == {
        "sheet_write": False,
        "active_idea_publication": False,
        "planner_run": False,
        "shadow_admission": False,
        "live_admission": False,
        "broker_effects": False,
        "order_effects": False,
        "external_send": False,
    }


def test_invalid_model_shape_gets_exactly_one_repair_pass() -> None:
    record = _record("repair", "Watching $LULU for earnings", ["LULU"])
    invalid = {"schema": "wrong", "episodes": []}
    valid = {
        "schema": PROMPT_SCHEMA,
        "episodes": [{"signal_id": record["signal_id"], "events": [_event()]}],
    }
    client = FakeClient(invalid, valid)

    compilation = compile_source_episode_packet(
        _packet([record]),
        _profile("mike_butler"),
        client,
    )

    assert len(client.calls) == 2
    assert "failed deterministic validation" in client.calls[1]["user_prompt"]
    assert [receipt["pass"] for receipt in compilation.model_receipts] == [
        "interpret",
        "repair",
    ]


def test_incomplete_exact_package_is_parked_before_projection() -> None:
    record = _record(
        "image-missing",
        "Downside put diagonal in $LULU",
        ["LULU"],
        media=[
            {
                "media_index": 1,
                "type": "photo",
                "cache_status": "missing",
                "artifact_path": "",
                "sha256": "",
            }
        ],
    )
    incomplete = {
        "complete": False,
        "blocker": "screenshot is unavailable",
        "displayed_price": None,
        "legs": [],
        "field_provenance": ["text"],
    }
    response = {
        "schema": PROMPT_SCHEMA,
        "episodes": [
            {
                "signal_id": record["signal_id"],
                "events": [
                    _event(
                        projections=["idea", "exact_package"],
                        exact_packages=[incomplete],
                    )
                ],
            }
        ],
    }

    compilation = compile_source_episode_packet(
        _packet([record]),
        _profile("mike_butler"),
        FakeClient(response),
    )

    event = compilation.episodes[0]["events"][0]
    assert event["projections"] == ["idea"]
    assert event["evidence_status"] == "complete"
    assert event["planner_new_entry"] is True
    assert event["projection_dispositions"] == [
        {
            "projection": "idea",
            "disposition": "ready_for_source_policy",
            "reason": "idea_evidence_complete",
        }
    ]
    assert "exact_package_incomplete" in event["blockers"]


def test_verified_media_is_hashed_and_history_round_trips(tmp_path: Path) -> None:
    image = tmp_path / "post.jpg"
    image.write_bytes(b"public image fixture")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    record = _record(
        "media",
        "New $LULU downside diagonal",
        ["LULU"],
        media=[
            {
                "media_index": 1,
                "type": "photo",
                "cache_status": "cached",
                "artifact_path": str(image),
                "sha256": digest,
            }
        ],
    )
    response = {
        "schema": PROMPT_SCHEMA,
        "episodes": [{"signal_id": record["signal_id"], "events": [_event()]}],
    }
    client = FakeClient(response)
    compilation = compile_source_episode_packet(
        _packet([record]),
        _profile("mike_butler"),
        client,
    )

    assert client.calls[0]["images"] == (str(image.resolve()),)
    run_path = write_episode_compilation(compilation, tmp_path / "episodes")
    assert run_path.is_file()
    history = load_episode_history(tmp_path / "episodes", "mike_butler")
    assert len(history) == 1
    assert history[0]["schema"] == EPISODE_SCHEMA
    assert json.loads(run_path.read_text(encoding="utf-8"))["effects"]["broker_effects"] is False


def test_same_thesis_package_variants_share_one_idea_event() -> None:
    record = _record(
        "variants",
        "New $SNOW call calendars at 330 and 340",
        ["SNOW"],
    )

    def package(strike: str) -> dict:
        return {
            "complete": True,
            "blocker": None,
            "displayed_price": {"amount": "2.00", "effect": "debit"},
            "legs": [
                {
                    "quantity": 1,
                    "expiration": "Sep 18 2026",
                    "strike": strike,
                    "option_type": "call",
                    "order_code": "STO",
                },
                {
                    "quantity": 1,
                    "expiration": "Oct 16 2026",
                    "strike": strike,
                    "option_type": "call",
                    "order_code": "BTO",
                },
            ],
            "field_provenance": ["image:1"],
        }

    response = {
        "schema": PROMPT_SCHEMA,
        "episodes": [
            {
                "signal_id": record["signal_id"],
                "events": [
                    _event(
                        symbol="SNOW",
                        direction="bullish",
                        structure_hint="call_calendar",
                        projections=["idea", "exact_package"],
                        exact_packages=[package("330")],
                    ),
                    _event(
                        symbol="SNOW",
                        direction="bullish",
                        structure_hint="call_calendar",
                        projections=["idea", "exact_package"],
                        exact_packages=[package("340")],
                    ),
                ],
            }
        ],
    }

    compilation = compile_source_episode_packet(
        _packet([record]),
        _profile("mike_butler"),
        FakeClient(response),
    )

    assert len(compilation.episodes[0]["events"]) == 1
    event = compilation.episodes[0]["events"][0]
    assert event["projections"] == ["idea", "exact_package"]
    assert len(event["exact_packages"]) == 2
    assert event["planner_new_entry"] is True


def test_optional_non_numeric_display_price_is_dropped_without_weakening_legs() -> None:
    record = _record("price", "New $LULU put diagonal", ["LULU"])
    package = {
        "complete": True,
        "blocker": None,
        "displayed_price": {"amount": "N/A", "effect": "unknown"},
        "legs": [
            {
                "quantity": 1,
                "expiration": "Sep 18 2026",
                "strike": "115",
                "option_type": "put",
                "order_code": "BTO",
            }
        ],
        "field_provenance": ["text"],
    }
    response = {
        "schema": PROMPT_SCHEMA,
        "episodes": [
            {
                "signal_id": record["signal_id"],
                "events": [
                    _event(
                        projections=["idea", "exact_package"],
                        exact_packages=[package],
                    )
                ],
            }
        ],
    }

    compilation = compile_source_episode_packet(
        _packet([record]),
        _profile("mike_butler"),
        FakeClient(response),
    )

    exact = compilation.episodes[0]["events"][0]["exact_packages"][0]
    assert exact["complete"] is True
    assert exact["displayed_price"] is None


def test_multi_symbol_structure_fallback_cannot_borrow_another_symbols_phrase() -> None:
    record = _record(
        "mixed-structures",
        "New $SNOW calendars and new $GLD call CRAB trade",
        ["SNOW", "GLD"],
    )
    response = {
        "schema": PROMPT_SCHEMA,
        "episodes": [
            {
                "signal_id": record["signal_id"],
                "events": [
                    _event(
                        symbol="SNOW",
                        direction="bullish",
                        structure_hint=None,
                        thesis="New SNOW call calendars",
                    ),
                    _event(
                        symbol="GLD",
                        direction="bullish",
                        structure_hint=None,
                        thesis="New GLD call CRAB trade",
                    ),
                ],
            }
        ],
    }

    compilation = compile_source_episode_packet(
        _packet([record]),
        _profile("mike_butler"),
        FakeClient(response),
    )

    assert [event["structure_hint"] for event in compilation.episodes[0]["events"]] == [
        "call_calendar",
        "call_crab",
    ]


def test_greg_added_trade_remains_a_new_idea_without_prior_position_history() -> None:
    record = _record(
        "added",
        "also added $AAPL Nov 325/Oct 345 call diagonals and selling October 295 put",
        ["AAPL"],
        classification="trade_journal",
    )
    response = {
        "schema": PROMPT_SCHEMA,
        "episodes": [
            {
                "signal_id": record["signal_id"],
                "events": [
                    _event(
                        action="open",
                        symbol="AAPL",
                        direction="bullish",
                        structure_hint="call_diagonal",
                        evidence_status="needs_history",
                        projections=["residual"],
                        blockers=["prior AAPL position is unavailable"],
                    )
                ],
            }
        ],
    }

    compilation = compile_source_episode_packet(
        _packet([record]),
        _profile("greg_harmon"),
        FakeClient(response),
    )

    event = compilation.episodes[0]["events"][0]
    assert event["action"] == "scale_in"
    assert event["structure_hint"] == "call_diagonal_with_short_put"
    assert event["evidence_status"] == "complete"
    assert "idea" in event["projections"]
    assert event["planner_new_entry"] is True
