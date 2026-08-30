from datetime import date, timedelta

from kamandal_v2.domain.models import Idea, Playbook, UniverseEntry
from kamandal_v2.market.fixture import FixtureMarketDataProvider, FixturePreflightClient
from kamandal_v2.planner.candidate_builder import build_candidates, diagnose_idea_matches


def _playbook(**overrides) -> Playbook:
    base = {
        "playbook_id": "put_diagonal_overextended",
        "enabled": True,
        "strategy_family": "put_diagonal",
        "structure": "put_diagonal",
        "variant": "overextended",
        "leg_count": 2,
        "profiles": ["large_stocks"],
        "applicable_direction": ["bearish"],
        "applicable_thesis_tags": ["overextended"],
        "applicable_horizon_min": 15,
        "applicable_horizon_max": 45,
        "iv_percentile_min": 30,
        "iv_percentile_max": 80,
        "requires_iv_percentile": True,
        "dte_min": 25,
        "dte_max": 35,
        "long_dte_min": 55,
        "long_dte_max": 80,
        "short_delta_min": 0.2,
        "short_delta_max": 0.3,
        "long_delta_min": 0.35,
        "long_delta_max": 0.5,
        "max_bid_ask_pct": 0.2,
        "min_option_oi": 100,
    }
    base.update(overrides)
    return Playbook(**base)


class _NoIvFixture(FixtureMarketDataProvider):
    def iv_percentile(self, underlying: str) -> float | None:
        return None


class _LowAbsoluteIvFixture(FixtureMarketDataProvider):
    def iv_abs(self, underlying: str) -> float | None:
        return 20.0


class _LowIvRankFixture(FixtureMarketDataProvider):
    def iv_rank(self, underlying: str) -> float | None:
        return 20.0


class _AbsurdSpreadFixture(FixtureMarketDataProvider):
    def chain_snapshot(self, underlying: str):
        chain = super().chain_snapshot(underlying)
        for quote in chain.quotes:
            mid = quote.mid
            quote.bid = 0.01
            quote.ask = max(mid * 4.0, 1.0)
        return chain


class _PublicLadderFixture(FixtureMarketDataProvider):
    def chain_snapshot(self, underlying: str):
        chain = super().chain_snapshot(underlying)
        quotes = []
        for quote in chain.quotes:
            ladder_dtes = [24, 38] if quote.dte <= 35 else [45] if quote.dte <= 50 else [80]
            for dte in ladder_dtes:
                clone = quote.to_dict()
                clone["expiration"] = (date.today() + timedelta(days=dte)).isoformat()
                quotes.append(type(quote)(**clone))
        chain.quotes = quotes
        return chain


def test_put_diagonal_variant_matches_bearish_overextended_thesis() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    candidates = build_candidates(
        [idea],
        universe,
        [_playbook()],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
    )

    assert candidates
    assert candidates[0].structure == "put_diagonal"
    assert candidates[0].playbook_id == "put_diagonal_overextended"


def test_put_diagonal_does_not_widen_sheet_dte_window_for_public_ladder() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    candidates = build_candidates(
        [idea],
        universe,
        [_playbook(long_dte_min=75, long_dte_max=85)],
        _PublicLadderFixture(),
        FixturePreflightClient(),
    )

    assert candidates == []


def test_matched_playbook_zero_raw_candidates_gets_build_diagnostic() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]
    playbook = _playbook(long_dte_min=75, long_dte_max=85)
    config = {"planner": {"expiry": {"diagonal_calendar_dte_fallback": {"enabled": False}}}}

    candidates = build_candidates(
        [idea],
        universe,
        [playbook],
        _PublicLadderFixture(),
        FixturePreflightClient(),
        config=config,
    )
    diagnostics = diagnose_idea_matches(
        [idea],
        universe,
        [playbook],
        _PublicLadderFixture(),
        config=config,
    )

    assert candidates == []
    diagnostic = diagnostics[0]
    assert diagnostic["status"] == "matched_playbooks"
    zero = diagnostic["zero_candidate_diagnostics"][0]
    assert zero["playbook_id"] == "put_diagonal_overextended"
    assert zero["structure"] == "put_diagonal"
    assert zero["reason"] == "no_near_expiration_in_window"
    assert zero["near_dte_window"] == {"min": 25, "max": 35}
    assert zero["far_dte_window"] == {"min": 75, "max": 85}
    assert zero["delta_windows"]["near"] == {"min": 0.2, "max": 0.3}
    assert zero["delta_windows"]["far"] == {"min": 0.35, "max": 0.5}
    assert [item["dte"] for item in zero["available_expirations"]] == [24, 38, 45, 80]
    assert "built no raw candidates" in diagnostic["summary"]


def test_candidate_filter_warn_mode_logs_without_rejecting() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    candidates = build_candidates(
        [idea],
        universe,
        [_playbook(min_option_oi=999_999)],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
        candidate_filter_mode="warn",
    )

    assert any(candidate.eligible for candidate in candidates)
    assert any(
        reason.startswith("filter_warning=open_interest_below_min")
        for candidate in candidates
        for reason in candidate.reasons
    )


def test_live_low_oi_price_through_warns_without_rejecting() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    candidates = build_candidates(
        [idea],
        universe,
        [_playbook(min_option_oi=999_999, max_bid_ask_pct=10.0)],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
        candidate_filter_mode="strict",
        config={"runtime": {"mode": "live"}, "live": {"liquidity_policy": {"low_oi_mode": "price_through"}}},
    )

    assert any(candidate.eligible for candidate in candidates)
    assert any(
        "low_oi_price_through=true" in candidate.reasons
        for candidate in candidates
    )


def test_shadow_low_oi_price_through_matches_live_candidate_eligibility() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_shadow_low_oi",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    candidates = build_candidates(
        [idea],
        [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")],
        [_playbook(min_option_oi=999_999, max_bid_ask_pct=10.0)],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
        candidate_filter_mode="strict",
        config={"runtime": {"mode": "shadow"}, "live": {"liquidity_policy": {"low_oi_mode": "price_through"}}},
    )

    assert any(candidate.eligible for candidate in candidates)
    assert any("low_oi_price_through=true" in candidate.reasons for candidate in candidates)


def test_live_diagonal_package_spread_stays_hard_when_leg_price_through_is_enabled() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    candidates = build_candidates(
        [idea],
        universe,
        [_playbook(max_bid_ask_pct=0.01, min_option_oi=0)],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
        candidate_filter_mode="strict",
        config={"runtime": {"mode": "live"}, "live": {"liquidity_policy": {"wide_bid_ask_mode": "price_through"}}},
    )

    assert candidates
    assert not any(candidate.eligible for candidate in candidates)
    assert any(
        str(candidate.rejection_reason or "").startswith("package_bid_ask_pct_above_max:")
        for candidate in candidates
    )
    assert any(
        "wide_bid_ask_price_through=true" in candidate.reasons
        for candidate in candidates
    )


def test_live_absurd_bid_ask_stays_hard_rejected() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    candidates = build_candidates(
        [idea],
        universe,
        [_playbook(max_bid_ask_pct=0.01, min_option_oi=0)],
        _AbsurdSpreadFixture(),
        FixturePreflightClient(),
        candidate_filter_mode="strict",
        config={
            "runtime": {"mode": "live"},
            "live": {"liquidity_policy": {"wide_bid_ask_mode": "price_through", "absurd_bid_ask_pct": 1.0}},
        },
    )

    assert candidates
    assert all(not candidate.eligible for candidate in candidates)
    assert any(candidate.rejection_reason.startswith("bad_quote_absurd_bid_ask_pct:") for candidate in candidates)


def test_permissive_match_mode_warns_instead_of_blocking_iv_and_horizon() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 90,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]
    market = _LowAbsoluteIvFixture()

    strict = build_candidates(
        [idea],
        universe,
        [_playbook(applicable_horizon_max=45, iv_abs_min=30.0)],
        market,
        FixturePreflightClient(),
    )
    permissive = build_candidates(
        [idea],
        universe,
        [_playbook(applicable_horizon_max=45, iv_abs_min=30.0)],
        market,
        FixturePreflightClient(),
        match_gate_mode="permissive",
    )

    assert strict == []
    assert any(candidate.eligible for candidate in permissive)
    assert any(
        "match_gate_warning=" in reason and "horizon_above_max" in reason and "iv_abs_below_min" in reason
        for candidate in permissive
        for reason in candidate.reasons
    )


def test_variant_with_iv_bounds_skips_when_iv_percentile_missing() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    candidates = build_candidates(
        [idea],
        universe,
        [_playbook()],
        _NoIvFixture(),
        FixturePreflightClient(),
    )

    assert candidates == []


def test_variant_with_iv_abs_floor_skips_when_absolute_iv_is_too_low() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    candidates = build_candidates(
        [idea],
        universe,
        [_playbook(iv_abs_min=30.0)],
        _LowAbsoluteIvFixture(),
        FixturePreflightClient(),
    )

    assert candidates == []


def test_variant_with_iv_rank_floor_skips_when_rank_is_too_low() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    candidates = build_candidates(
        [idea],
        universe,
        [_playbook(iv_rank_min=30.0)],
        _LowIvRankFixture(),
        FixturePreflightClient(),
    )

    assert candidates == []


def test_universe_allowed_playbooks_accepts_strategy_family_for_variants() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_overextended",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 21,
    })
    universe = [
        UniverseEntry(
            symbol="TSLA",
            enabled=True,
            profile="large_stocks",
            allowed_playbooks=["put_diagonal"],
        )
    ]

    candidates = build_candidates(
        [idea],
        universe,
        [_playbook()],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
    )

    assert candidates


def test_mentioned_strategy_can_satisfy_tag_gate_without_strategy_hint() -> None:
    idea = Idea.from_dict({
        "idea_id": "ge_jade",
        "source": "test",
        "underlying": "GE",
        "direction": "bullish",
        "mentioned_strategy": "jade_lizard",
        "thesis_tags": ["breakout"],
        "horizon_days": 30,
    })
    universe = [
        UniverseEntry(
            symbol="GE",
            enabled=True,
            profile="large_stocks",
            allowed_playbooks=["jade_lizard"],
        )
    ]
    playbook = Playbook(
        playbook_id="jade_lizard_high_iv",
        enabled=True,
        strategy_family="jade_lizard",
        structure="jade_lizard",
        variant="high_iv",
        leg_count=3,
        profiles=["large_stocks"],
        applicable_direction=["bullish", "neutral"],
        applicable_thesis_tags=["theta_harvest", "vol_contraction"],
        applicable_horizon_min=20,
        applicable_horizon_max=45,
        iv_percentile_min=0,
        iv_percentile_max=100,
        requires_iv_percentile=True,
        dte_min=25,
        dte_max=60,
        short_delta_min=0.15,
        short_delta_max=0.30,
        spread_width=5.0,
    )

    candidates = build_candidates(
        [idea],
        universe,
        [playbook],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
    )

    assert any(candidate.structure == "jade_lizard" for candidate in candidates)


def test_idea_allowed_structures_constrain_playbook_matching() -> None:
    idea = Idea.from_dict({
        "idea_id": "profile_constrained_tsla",
        "source": "correspondent:test",
        "underlying": "TSLA",
        "direction": "bullish",
        "allowed_structures": ["call_spread"],
        "thesis_tags": ["breakout"],
        "horizon_days": 30,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]
    call_spread = _playbook(
        playbook_id="call_spread_breakout",
        strategy_family="call_spread",
        structure="call_spread",
        variant="breakout",
        applicable_direction=["bullish"],
        applicable_thesis_tags=["breakout"],
    )
    long_call = _playbook(
        playbook_id="long_call_breakout",
        strategy_family="long_call",
        structure="long_call",
        variant="breakout",
        leg_count=1,
        applicable_direction=["bullish"],
        applicable_thesis_tags=["breakout"],
    )

    diagnostic = diagnose_idea_matches(
        [idea],
        universe,
        [call_spread, long_call],
        FixtureMarketDataProvider(),
    )[0]

    assert diagnostic["matched_playbooks"] == ["call_spread_breakout"]
    long_call_row = next(row for row in diagnostic["playbooks"] if row["playbook_id"] == "long_call_breakout")
    assert "idea_structure_not_allowed" in long_call_row["reasons"]


def test_match_diagnostics_explain_zero_playbook_match() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_too_short",
        "source": "test",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 7,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    diagnostics = diagnose_idea_matches(
        [idea],
        universe,
        [_playbook(applicable_horizon_min=15)],
        FixtureMarketDataProvider(),
    )

    assert diagnostics[0]["status"] == "no_playbook_match"
    assert "No playbook matched" in diagnostics[0]["summary"]
    assert diagnostics[0]["reason_counts"]["horizon_below_min:7<15"] == 1


def test_llm_short_catalyst_horizon_uses_playbook_horizon_for_matching() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_tomorrow_short",
        "source": "llm_transcript:x_timeline.txt",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 1,
        "catalyst_horizon_days": 1,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    candidates = build_candidates(
        [idea],
        universe,
        [_playbook(applicable_horizon_min=15, applicable_horizon_max=45)],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
    )

    assert candidates
    assert all("horizon_below_min" not in candidate.rejection_reason for candidate in candidates)
    assert any(
        "match_horizon_source=playbook_horizon_for_short_catalyst" in candidate.reasons
        for candidate in candidates
    )


def test_explicit_trade_horizon_still_controls_matching() -> None:
    idea = Idea.from_dict({
        "idea_id": "tsla_short_trade_horizon",
        "source": "llm_transcript:x_timeline.txt",
        "underlying": "TSLA",
        "direction": "bearish",
        "thesis_tags": ["overextended"],
        "horizon_days": 1,
        "catalyst_horizon_days": 1,
        "trade_horizon_days": 7,
    })
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")]

    diagnostics = diagnose_idea_matches(
        [idea],
        universe,
        [_playbook(applicable_horizon_min=15)],
        FixtureMarketDataProvider(),
    )

    assert diagnostics[0]["status"] == "no_playbook_match"
    assert diagnostics[0]["reason_counts"]["horizon_below_min:7<15"] == 1
    assert diagnostics[0]["match_context"]["trade_horizon_days"] == 7
