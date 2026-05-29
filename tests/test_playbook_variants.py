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
