from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from kamandal_v2.intelligence.source_episode_compiler import (
    EPISODE_SCHEMA,
    PROMPT_SCHEMA,
    compile_source_episode_packet,
    load_episode_history,
    write_episode_compilation,
)
from kamandal_v2.intelligence.source_episode_projection import (
    project_source_episode_compilation,
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


def test_roll_retains_literal_resulting_diagonal_instead_of_old_calendar():
    record = _record("roll-shape", "Rolled $MSFT short call in calendar up and out to create a call diagonal.", ["MSFT"])
    response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": record["signal_id"], "events": [
        _event(action="roll", symbol="MSFT", direction="bullish", structure_hint="call_diagonal",
               thesis="Rolled MSFT call calendar into a call diagonal", projections=["residual"])
    ]}]}
    compilation = compile_source_episode_packet(_packet([record]), _profile("mike_butler"), FakeClient(response))
    event = compilation.episodes[0]["events"][0]
    assert event["structure_hint"] == "call_diagonal"
    assert event["planner_new_entry"] is False


@pytest.mark.parametrize("synonym", ["call_vertical", "bull_call_spread"])
def test_greg_call_vertical_synonym_preserves_source_idea(synonym):
    record = _record("vertical-synonym", "added $HAL Sep 38 calls and $COP Sep 138/144 call spreads", ["HAL", "COP"])
    response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": record["signal_id"], "events": [
        _event(action="scale_in", symbol="COP", direction="bullish", structure_hint=synonym,
               thesis="Added September 138/144 calls", projections=["idea"])
    ]}]}
    compilation = compile_source_episode_packet(_packet([record]), _profile("greg_harmon"), FakeClient(response))
    event = compilation.episodes[0]["events"][0]
    assert event["structure_hint"] == "call_spread"
    assert event["planner_new_entry"] is True


@pytest.mark.parametrize("text", [
    "5 Trade Ideas for Monday: Arm and AMD $ARM $AMD",
    "This went live Thursday, opening it for all to see. $META",
])
def test_greg_research_announcements_are_not_confirmed_openings(text):
    record = _record("research-headline", text, ["ARM"])
    response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": record["signal_id"], "events": [
        _event(symbol="ARM", structure_hint="call_spread", projections=["idea"])
    ]}]}

    event = compile_source_episode_packet(_packet([record]), _profile("greg_harmon"), FakeClient(response)).episodes[0]["events"][0]

    assert event["action"] == "discovery"
    assert event["planner_new_entry"] is False


@pytest.mark.parametrize("text", [
    "5 Trade Ideas for Monday: $ARM call spread. I also bought the $AMD Oct 100 call spread today.",
    "5 Trade Ideas for Monday: $ARM call spread. $AMD: bought the Oct 100 call spread today.",
])
def test_greg_research_headline_does_not_suppress_explicit_fill_in_same_post(text):
    record = _record(
        "mixed-headline",
        text,
        ["ARM", "AMD"],
    )
    response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": record["signal_id"], "events": [
        _event(action="open", symbol="ARM", structure_hint="call_spread", projections=["idea"]),
        _event(action="open", symbol="AMD", structure_hint="call_spread", projections=["idea"]),
    ]}]}

    events = compile_source_episode_packet(_packet([record]), _profile("greg_harmon"), FakeClient(response)).episodes[0]["events"]

    assert [event["action"] for event in events] == ["discovery", "open"]
    assert events[0]["planner_new_entry"] is False
    assert events[1]["planner_new_entry"] is True


def test_reinterpreted_post_does_not_link_to_its_old_revision_as_history():
    record = _record("same-post", "Rolled my $LULU put diagonal", ["LULU"])
    old = {
        "schema": EPISODE_SCHEMA, "post_ref": record["signal_id"],
        "published_at": "2026-09-03T14:00:00Z", "profile_version": "3",
        "interpretation_rules_version": "source-evidence-v2",
        "events": [{"event_id": "prior_same_post", "action": "open", "symbol": "LULU", "structure_hint": "put_diagonal"}],
    }
    response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": record["signal_id"], "events": [
        _event(action="roll", symbol="LULU", projections=["residual"])
    ]}]}
    client = FakeClient(response)

    event = compile_source_episode_packet(
        _packet([record]), _profile("mike_butler"), client, history=[old]
    ).episodes[0]["events"][0]

    assert event["link_state"] == "needs_history"
    assert "prior_same_post" not in client.calls[0]["user_prompt"]


def test_reused_opening_advances_lineage_before_new_roll_in_same_packet():
    opening = _record("opening", "New $LULU put diagonal", ["LULU"])
    opened_response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": opening["signal_id"], "events": [
        _event(symbol="LULU", action="open", projections=["idea"])
    ]}]}
    old_episode = compile_source_episode_packet(
        _packet([opening]), _profile("mike_butler"), FakeClient(opened_response)
    ).episodes[0]
    roll = _record("roll", "Rolled my $LULU put diagonal", ["LULU"],
                   published_at="2026-09-03T14:05:00Z")
    roll_response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": roll["signal_id"], "events": [
        _event(symbol="LULU", action="roll", projections=["residual"])
    ]}]}

    client = FakeClient(roll_response)
    compiled = compile_source_episode_packet(
        _packet([opening, roll]), _profile("mike_butler"), client,
        history=[old_episode],
    )

    assert compiled.episodes[0] == old_episode
    assert compiled.episodes[1]["events"][0]["link_state"] == "linked"
    assert compiled.episodes[1]["events"][0]["links_to"] == [old_episode["events"][0]["event_id"]]
    assert old_episode["events"][0]["event_id"] in client.calls[0]["user_prompt"]


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


def test_four_leg_double_calendar_cannot_become_two_leg_exact_calendar() -> None:
    record = _record("double-calendar", "New double put calendar in $ADBE", ["ADBE"])
    legs = [
        {"quantity": 1, "expiration": expiry, "strike": strike, "option_type": "put", "order_code": code}
        for strike in ("220", "230")
        for expiry, code in (("Sep 11 2026", "STO"), ("Sep 18 2026", "BTO"))
    ]
    response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": record["signal_id"], "events": [
        _event(symbol="ADBE", structure_hint="put_calendar", projections=["idea", "exact_package"],
               exact_packages=[{"complete": True, "blocker": None, "displayed_price": None,
                                "legs": legs, "field_provenance": ["text"]}])
    ]}]}

    event = compile_source_episode_packet(_packet([record]), _profile("mike_butler"), FakeClient(response)).episodes[0]["events"][0]

    assert event["exact_packages"][0]["blocker"] == "structure_leg_count_mismatch"
    assert event["exact_packages"][0]["complete"] is False
    assert "exact_package" not in event["projections"]
    assert event["planner_new_entry"] is True  # Adapted idea remains independent.


def test_source_quantity_conflict_holds_exact_long_call() -> None:
    record = _record("quantity-conflict", "New 3x long call LEAPs in $TLT", ["TLT"])
    response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": record["signal_id"], "events": [
        _event(symbol="TLT", direction="bullish", structure_hint="long_call",
               projections=["idea", "exact_package"], exact_packages=[{
                   "complete": True, "blocker": None, "displayed_price": None,
                   "legs": [{"quantity": 1, "expiration": "Jan 21 2028", "strike": "90",
                             "option_type": "call", "order_code": "BTO"}],
                   "field_provenance": ["text"],
               }])
    ]}]}

    event = compile_source_episode_packet(_packet([record]), _profile("mike_butler"), FakeClient(response)).episodes[0]["events"][0]

    assert event["exact_packages"][0]["blocker"] == "source_quantity_conflicts_with_displayed_package"
    assert event["exact_packages"][0]["complete"] is False
    assert "exact_package" not in event["projections"]


def test_incomplete_sibling_retains_complete_shadow_evidence() -> None:
    record = _record("alternatives", "New $SNOW calendars at 330 and 340", ["SNOW"])
    good_legs = [
        {"quantity": 1, "expiration": expiry, "strike": "330", "option_type": "call", "order_code": code}
        for expiry, code in (("Sep 11 2026", "STO"), ("Oct 16 2026", "BTO"))
    ]
    response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": record["signal_id"], "events": [
        _event(symbol="SNOW", direction="bullish", structure_hint="call_calendar",
               projections=["idea", "exact_package"], exact_packages=[
                   {"complete": True, "blocker": None, "displayed_price": None,
                    "legs": good_legs, "field_provenance": ["text"]},
                   {"complete": False, "blocker": "340 expiration missing", "displayed_price": None,
                    "legs": [], "field_provenance": ["text"]},
               ])
    ]}]}

    event = compile_source_episode_packet(_packet([record]), _profile("mike_butler"), FakeClient(response)).episodes[0]["events"][0]

    assert len(event["exact_packages"]) == 2
    assert "exact_package" in event["projections"]  # Complete sibling remains useful in shadow.
    assert event["planner_new_entry"] is True


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


def test_cross_post_image_reference_cannot_project_an_exact_package(tmp_path: Path) -> None:
    records = []
    for ordinal, symbol in enumerate(("SNOW", "LULU"), start=1):
        image = tmp_path / f"post-{ordinal}.jpg"
        image.write_bytes(f"public image fixture {ordinal}".encode())
        records.append(
            _record(
                f"media-{ordinal}",
                f"New ${symbol} call calendar",
                [symbol],
                media=[
                    {
                        "media_index": 1,
                        "type": "photo",
                        "cache_status": "cached",
                        "artifact_path": str(image),
                        "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                    }
                ],
            )
        )

    def package(image_number: int) -> dict:
        return {
            "complete": True,
            "blocker": None,
            "displayed_price": {"amount": "2.00", "effect": "debit"},
            "legs": [
                {
                    "quantity": 1,
                    "expiration": "Sep 18",
                    "strike": "150",
                    "option_type": "call",
                    "order_code": "STO",
                },
                {
                    "quantity": 1,
                    "expiration": "Oct 16",
                    "strike": "150",
                    "option_type": "call",
                    "order_code": "BTO",
                },
            ],
            "field_provenance": [f"image:{image_number}"],
        }

    response = {
        "schema": PROMPT_SCHEMA,
        "episodes": [
            {
                "signal_id": records[0]["signal_id"],
                "events": [
                    _event(
                        symbol="SNOW",
                        structure_hint="call_calendar",
                        projections=["exact_package"],
                        exact_packages=[package(1)],
                    )
                ],
            },
            {
                "signal_id": records[1]["signal_id"],
                "events": [
                    _event(
                        symbol="LULU",
                        structure_hint="call_calendar",
                        projections=["exact_package"],
                        # Image 1 belongs to the first post. This post's image is
                        # prompt-global image 2, even though it is locally media 1.
                        exact_packages=[package(1)],
                    )
                ],
            },
        ],
    }
    packet = _packet(records)
    profile = _profile("mike_butler")
    compilation = compile_source_episode_packet(packet, profile, FakeClient(response))

    valid_event = compilation.episodes[0]["events"][0]
    invalid_event = compilation.episodes[1]["events"][0]
    assert valid_event["exact_packages"][0]["complete"] is True
    assert invalid_event["exact_packages"][0]["complete"] is False
    assert invalid_event["exact_packages"][0]["blocker"] == "image_reference_outside_source_post"
    assert invalid_event["planner_new_entry"] is False

    projection = project_source_episode_compilation(
        compilation,
        packet,
        profile,
        universe_symbols=("SNOW", "LULU"),
    )
    assert len(projection.observed_batches) == 1
    assert [item.symbol for item in projection.observed_batches[0].packages] == ["SNOW"]


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
            "field_provenance": ["text"],
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
            },
            {
                "quantity": 1,
                "expiration": "Sep 11 2026",
                "strike": "120",
                "option_type": "put",
                "order_code": "STO",
            },
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


def test_greg_quoted_entry_inside_management_post_cannot_reopen_trade() -> None:
    record = _record(
        "management-with-quote",
        (
            '$LULU "Trade Idea 1: Buy the September 4 Expiry 118/113-112 1x2 Put Spread '
            'for a 5 cent credit." sell to close a 118/113 put spread near $5 and roll the '
            "remaining put down and out."
        ),
        ["LULU"],
        classification="earnings_idea",
    )
    response = {
        "schema": PROMPT_SCHEMA,
        "episodes": [
            {
                "signal_id": record["signal_id"],
                "events": [
                    _event(
                        action="open",
                        structure_hint="short_put",
                        projections=["idea", "exact_package"],
                        exact_packages=[
                            {
                                "complete": True,
                                "blocker": None,
                                "displayed_price": {"amount": "0.05", "effect": "credit"},
                                "legs": [
                                    {
                                        "quantity": 1,
                                        "expiration": "Sep 4 2026",
                                        "strike": "118",
                                        "option_type": "put",
                                        "order_code": "BTO",
                                    }
                                ],
                                "field_provenance": ["text"],
                            }
                        ],
                    ),
                    _event(
                        action="hold",
                        structure_hint="short_put",
                        projections=["residual"],
                    ),
                ],
            }
        ],
    }

    compilation = compile_source_episode_packet(
        _packet([record]),
        _profile("greg_harmon"),
        FakeClient(response),
    )

    events = compilation.episodes[0]["events"]
    assert events
    assert {event["action"] for event in events} == {"adjust"}
    assert all(event["planner_new_entry"] is False for event in events)
    assert all(
        disposition["disposition"] != "ready_for_source_policy"
        for event in events
        for disposition in event["projection_dispositions"]
    )


def test_opaque_provider_identifier_cannot_be_guessed_without_independent_evidence():
    record = _record('opaque', 'New solana:J3NKxxXZcnNiMjKw9hYb2K4LUxgwB6t1FtPtQVsv3KFr super bull 1DTE', [])
    record['source']['expanded_urls'] = ['https://x.com/example/status/123/photo/1']
    response = {'schema': PROMPT_SCHEMA, 'episodes': [{'signal_id': record['signal_id'], 'events': [
        _event(symbol='SPX', action='open', direction='bullish', structure_hint='super_bull', projections=['idea'])]}]}
    client = FakeClient(response)
    result = compile_source_episode_packet(_packet([record]), _profile('mike_butler'), client)
    event = result.episodes[0]['events'][0]
    assert event['planner_new_entry'] is False
    assert 'source_identifier_unresolved' in event['blockers']
    prompt = json.loads(client.calls[0]['user_prompt'])
    assert prompt['posts'][0]['media_expected_but_unavailable'] is True
    assert prompt['posts'][0]['opaque_identifier_requires_independent_symbol_evidence'] is True


def test_super_bull_is_not_relabelled_as_one_component_spread():
    record = _record('super-bull', 'New $SPX super bull 1DTE', ['SPX'])
    response = {'schema': PROMPT_SCHEMA, 'episodes': [{'signal_id': record['signal_id'], 'events': [
        _event(symbol='SPX', action='open', direction='bullish', structure_hint='super_bull',
               thesis='Super bull combining a short put spread and long call spread', projections=['idea'])]}]}
    result = compile_source_episode_packet(_packet([record]), _profile('mike_butler'), FakeClient(response))
    assert result.episodes[0]['events'][0]['structure_hint'] == 'super_bull'


def test_crab_thesis_enters_existing_idea_path_with_original_shape_retained():
    record = _record('crab', 'Call CRAB trade in $GOOGL', ['GOOGL'], classification='observed_package_open')
    profile = _profile('mike_butler')
    response = {'schema': PROMPT_SCHEMA, 'episodes': [{'signal_id': record['signal_id'], 'events': [
        _event(symbol='GOOGL', action='open', direction='bullish', structure_hint='call_crab', projections=['idea'])]}]}
    packet = _packet([record])
    result = compile_source_episode_packet(packet, profile, FakeClient(response))
    projection = project_source_episode_compilation(result, packet, profile, universe_symbols=('GOOGL',))
    assert len(projection.planner_ideas) == 1
    idea = projection.planner_ideas[0]
    assert idea['allowed_structures'] == ['call_diagonal']
    assert 'source_structure=call_crab' in idea['notes']
    assert 'idea_reexpression=true' in idea['notes']
    assert projection.observed_batches == ()
    from kamandal_v2.intelligence.source_episode_projection import _allowed_structures
    assert _allowed_structures('call_crab', 'bearish', profile) == []


def test_exact_revision_ignores_batch_prompt_but_tracks_trade_terms(tmp_path):
    from types import SimpleNamespace
    from copy import deepcopy
    from kamandal_v2.intelligence.source_episode_projection import _exact_package_projections
    image = tmp_path / 'post.jpg'
    image.write_bytes(b'public fixture')
    record = _record('123', 'New calendar', ['SNOW'], media=[{
        'media_index': 1, 'type': 'photo', 'cache_status': 'cached',
        'artifact_path': str(image), 'sha256': hashlib.sha256(image.read_bytes()).hexdigest()}])
    event = {'event_id': 'event-1', 'opportunity_group_id': 'opp-1', 'action': 'open',
             'symbol': 'SNOW', 'structure_hint': 'call_calendar', 'exact_packages': [{
        'complete': True, 'field_provenance': ['image:1'],
        'displayed_price': {'amount': '2.00', 'effect': 'debit'},
        'legs': [{'order_code': code, 'quantity': 1, 'expiration': expiry,
                  'strike': '150', 'option_type': 'call'}
                 for code, expiry in [('STO', '2026-09-18'), ('BTO', '2026-10-16')]]}]}
    def project(value, prompt):
        packages, failures = _exact_package_projections(value, record,
            compilation=SimpleNamespace(profile_id='mike_butler', prompt_sha256=prompt))
        assert failures == []
        return packages[0]
    first = project(event, 'batch-1')
    again = project(event, 'batch-2-with-other-post')
    assert first.evidence_revision_id == again.evidence_revision_id
    assert first.prompt_sha256 != again.prompt_sha256
    changed = deepcopy(event)
    changed['exact_packages'][0]['displayed_price']['amount'] = '2.50'
    assert project(changed, 'batch-2').evidence_revision_id != first.evidence_revision_id
    changed['exact_packages'][0]['legs'][0]['strike'] = '155'
    changed['exact_packages'][0]['legs'][1]['strike'] = '155'
    assert project(changed, 'batch-2').package_signature != first.package_signature
    assert project(changed, 'batch-2').opportunity_group_id == first.opportunity_group_id
    changed = deepcopy(event)
    changed['exact_packages'].append({'complete': False, 'blocker': 'other package unreadable', 'legs': []})
    assert project(changed, 'batch-2').source_opening_package_count == 2
    assert project(changed, 'batch-2').evidence_revision_id != first.evidence_revision_id


def test_exact_projection_holds_calendar_label_on_vertical_contracts(tmp_path):
    from types import SimpleNamespace
    from kamandal_v2.intelligence.source_episode_projection import _exact_package_projections

    image = tmp_path / 'post.jpg'
    image.write_bytes(b'public fixture')
    record = _record('shape-mismatch', 'New call calendar', ['SNOW'], media=[{
        'media_index': 1, 'type': 'photo', 'cache_status': 'cached',
        'artifact_path': str(image), 'sha256': hashlib.sha256(image.read_bytes()).hexdigest()}])
    event = {'event_id': 'event-shape', 'opportunity_group_id': 'opp-shape', 'action': 'open',
             'symbol': 'SNOW', 'structure_hint': 'call_calendar', 'exact_packages': [{
                 'complete': True, 'field_provenance': ['image:1'],
                 'displayed_price': {'amount': '2.00', 'effect': 'debit'},
                 'legs': [
                     {'order_code': 'BTO', 'quantity': 1, 'expiration': '2026-10-16',
                      'strike': '150', 'option_type': 'call'},
                     {'order_code': 'STO', 'quantity': 1, 'expiration': '2026-10-16',
                      'strike': '160', 'option_type': 'call'},
                 ]}]}
    packages, failures = _exact_package_projections(
        event, record, compilation=SimpleNamespace(profile_id='mike_butler', prompt_sha256='batch'))
    assert packages == []
    assert 'source structure disagrees with exact contracts' in failures[0]['reason']

    event['exact_packages'][0]['legs'] = [
        {'order_code': 'STO', 'quantity': 2, 'expiration': '2026-09-18',
         'strike': '150', 'option_type': 'call'},
        {'order_code': 'BTO', 'quantity': 1, 'expiration': '2026-10-16',
         'strike': '150', 'option_type': 'call'},
    ]
    packages, failures = _exact_package_projections(
        event, record, compilation=SimpleNamespace(profile_id='mike_butler', prompt_sha256='batch'))
    assert packages == []
    assert 'source structure disagrees with exact contracts' in failures[0]['reason']


def test_retained_fly_plus_vertical_is_labeled_composite_not_simple_fly(tmp_path):
    from kamandal_v2.intelligence.source_episode_projection import _exact_package_projections

    image = tmp_path / 'post.jpg'
    image.write_bytes(b'public composite fixture')
    record = _record('meta-composite', 'New META butterfly plus call vertical', ['META'], media=[{
        'media_index': 1, 'type': 'photo', 'cache_status': 'cached',
        'artifact_path': str(image), 'sha256': hashlib.sha256(image.read_bytes()).hexdigest()}])
    raw_legs = [
        ('BTO', 1, '670', 'put'), ('STO', 2, '685', 'put'), ('BTO', 1, '700', 'put'),
        ('STO', 1, '820', 'call'), ('BTO', 1, '830', 'call'),
    ]
    response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": record["signal_id"], "events": [
        _event(symbol='META', structure_hint='butterfly', projections=['idea', 'exact_package'],
               exact_packages=[{
                   'complete': True, 'blocker': None,
                   'displayed_price': {'amount': '1.00', 'effect': 'credit'},
                   'field_provenance': ['image:1'],
                   'legs': [{'order_code': code, 'quantity': quantity,
                             'expiration': '2026-10-16', 'strike': strike, 'option_type': kind}
                            for code, quantity, strike, kind in raw_legs],
               }])
    ]}]}
    compilation = compile_source_episode_packet(_packet([record]), _profile('mike_butler'), FakeClient(response))
    event = compilation.episodes[0]['events'][0]
    assert event['exact_packages'][0]['complete'] is True
    assert event['exact_packages'][0]['deterministic_structure'] == 'put_butterfly_with_call_vertical'
    packages, failures = _exact_package_projections(event, record, compilation=compilation)
    assert failures == []
    assert packages[0].structure == 'put_butterfly_with_call_vertical'


def test_hedge_smarttag_is_not_discarded_and_old_empty_cache_is_invalidated():
    record = _record('hedge-smarttag', 'Downside hedge in ethereum:0x123', [], classification='irrelevant')
    response = {'schema': PROMPT_SCHEMA, 'episodes': [{'signal_id':record['signal_id'], 'events':[
        _event(action='open',symbol='SPX',direction='bearish',structure_hint='butterfly',thesis='Hedge needs source image',projections=['residual'])]}]}
    client=FakeClient(response)
    first=compile_source_episode_packet(_packet([record]),_profile('mike_butler'),client)
    assert len(client.calls)==1
    assert first.episodes[0]['events'][0]['symbol']=='SPX'
    old=dict(first.episodes[0]);old.pop('interpretation_rules_version');old['events']=[]
    repair=FakeClient(response)
    fixed=compile_source_episode_packet(_packet([record]),_profile('mike_butler'),repair,history=[old])
    assert len(repair.calls)==1
    # Current-rule receipts reuse without paying again or changing event identity.
    reused=compile_source_episode_packet(_packet([record]),_profile('mike_butler'),FakeClient(),history=fixed.episodes)
    assert reused.episodes==fixed.episodes
    assert first.episodes[0]['events'][0]['event_id']==fixed.episodes[0]['events'][0]['event_id']


def test_performance_added_does_not_create_scale_in():
    record=_record('performance', 'Portfolio added 1.255% on the week using 70% of capital', [])
    response={'schema':PROMPT_SCHEMA,'episodes':[{'signal_id':record['signal_id'],'events':[
        _event(action='commentary',symbol=None,direction='unknown',structure_hint=None,thesis='Portfolio performance report',projections=['residual'])]}]}
    result=compile_source_episode_packet(_packet([record]),_profile('greg_harmon'),FakeClient(response))
    assert result.episodes[0]['events'][0]['action']=='commentary'
    assert result.episodes[0]['events'][0]['planner_new_entry'] is False
