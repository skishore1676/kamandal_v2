from kamandal_v2.domain.models import Idea, Playbook, PreflightResult, UniverseEntry
from kamandal_v2.market.fixture import FixtureMarketDataProvider, FixturePreflightClient
from kamandal_v2.planner.candidate_builder import build_candidates


def _idea(**overrides) -> Idea:
    base = {
        "idea_id": "idea_1",
        "source": "test",
        "underlying": "NVDA",
        "direction": "neutral",
        "strategy_hint": "",
        "thesis_tags": ["theta_harvest", "vol_contraction"],
        "horizon_days": 30,
        "operator_status": "approved",
    }
    base.update(overrides)
    return Idea.from_dict(base)


def _playbook(structure: str, **overrides) -> Playbook:
    base = {
        "playbook_id": f"{structure}_test",
        "enabled": True,
        "strategy_family": structure,
        "structure": structure,
        "variant": "test",
        "leg_count": {"short_strangle": 2, "jade_lizard": 3}.get(structure, 1),
        "profiles": ["large_stocks"],
        "applicable_direction": ["neutral"],
        "applicable_thesis_tags": ["theta_harvest", "vol_contraction"],
        "iv_percentile_min": 0,
        "iv_percentile_max": 100,
        "requires_iv_percentile": True,
        "dte_min": 25,
        "dte_max": 60,
        "short_delta_min": 0.15,
        "short_delta_max": 0.30,
        "long_delta_min": 0.35,
        "long_delta_max": 0.65,
        "spread_width": 5.0,
    }
    base.update(overrides)
    return Playbook(**base)


def _candidates(playbook: Playbook, idea: Idea | None = None):
    return build_candidates(
        [idea or _idea()],
        [UniverseEntry(symbol='NVDA', enabled=True)],
        [playbook],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
    )


def test_short_strangle_builder_supports_strangle_alias() -> None:
    candidates = _candidates(_playbook("short_strangle"), _idea(strategy_hint="strangle"))

    eligible = [candidate for candidate in candidates if candidate.eligible]
    assert eligible
    assert eligible[0].structure == "short_strangle"
    assert {leg.option_type for leg in eligible[0].legs} == {"put", "call"}
    assert all(leg.side == "sell" for leg in eligible[0].legs)


def test_minimal_universe_ignores_retired_filters_but_preserves_strategy_iv_and_enablement() -> None:
    # An old snapshot must not silently restore retired symbol-level controls.
    entry = UniverseEntry.from_row({
        "symbol": "NVDA", "enabled": "TRUE", "profile": "never_matches",
        "allowed_playbooks": "unsupported", "tradable_iv_percentile_min": "99",
        "tradable_iv_percentile_max": "100", "max_positions": "0", "notes": "operator note",
    })
    assert entry == UniverseEntry("NVDA", True, "operator note")
    playbook = _playbook("short_strangle", profiles=["index_etf"])
    def build(row, policy):
        return build_candidates([_idea(strategy_hint="strangle")], [row], [policy],
                                FixtureMarketDataProvider(), FixturePreflightClient())
    assert any(c.eligible for c in build(entry, playbook))
    assert not build(UniverseEntry.from_row({"symbol": "NVDA"}), playbook)
    assert not build(entry, _playbook("short_strangle", iv_percentile_min=99))
    assert not build(entry, _playbook("short_strangle", enabled=False))


def test_short_strangle_uses_broker_preflight_bpr_instead_of_local_formula() -> None:
    class BrokerPreflight:
        def preflight(self, _candidate):  # noqa: ANN001
            return PreflightResult(
                ok=True,
                bpr=1_250.0,
                message="broker ok",
                raw={"response": {"buyingPowerRequirement": "1250.00"}},
            )

    candidates = build_candidates(
        [_idea(strategy_hint="strangle")],
        [UniverseEntry(symbol='NVDA', enabled=True)],
        [_playbook("short_strangle")],
        FixtureMarketDataProvider(),
        BrokerPreflight(),
    )

    candidate = next(candidate for candidate in candidates if candidate.eligible)
    assert candidate.estimated_bpr == 1_250.0
    assert "bpr_source=broker_preflight" in candidate.reasons
    assert any(reason.startswith("local_bpr_fallback=") for reason in candidate.reasons)


def test_strangle_price_iv_overlay_expands_only_enabled_sheet_universe() -> None:
    playbook = _playbook(
        "short_strangle",
        profiles=["large_stocks"],
        universe_expansion_enabled=True,
        underlying_price_min=61,
        underlying_price_max=181,
        iv_rank_min=41,
        iv_rank_max=91,
    )
    entry = UniverseEntry(symbol='TLT', enabled=True)
    idea = _idea(underlying="TLT", strategy_hint="strangle")

    candidates = build_candidates(
        [idea],
        [entry],
        [playbook],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
    )

    candidate = next(candidate for candidate in candidates if candidate.eligible)
    assert "strangle_eligibility=sheet_playbook_overlay" in candidate.reasons

    disabled = UniverseEntry(symbol='TLT', enabled=False)
    assert build_candidates(
        [idea],
        [disabled],
        [playbook],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
    ) == []


def test_strangle_universe_expansion_fails_closed_without_sheet_ranges() -> None:
    playbook = _playbook("short_strangle", profiles=["large_stocks"], universe_expansion_enabled=True)
    entry = UniverseEntry(symbol='TLT', enabled=True)

    assert build_candidates(
        [_idea(underlying="TLT", strategy_hint="strangle")],
        [entry],
        [playbook],
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
    ) == []


def test_playbook_parses_universe_expansion_policy_from_sheet_row() -> None:
    playbook = Playbook.from_row(
        {
            "playbook_id": "short_strangle_sheet",
            "enabled": "TRUE",
            "strategy_family": "short_strangle",
            "structure": "short_strangle",
            "leg_count": "2",
            "universe_expansion_enabled": "TRUE",
            "underlying_price_min": "60",
            "underlying_price_max": "180",
            "iv_rank_min": "40",
            "iv_rank_max": "90",
        }
    )

    assert playbook.universe_expansion_enabled is True
    assert playbook.underlying_price_min == 60.0
    assert playbook.underlying_price_max == 180.0
    assert playbook.iv_rank_min == 40.0
    assert playbook.iv_rank_max == 90.0


def test_jade_lizard_builder_creates_short_put_and_call_spread() -> None:
    candidates = _candidates(_playbook("jade_lizard"))

    assert candidates
    assert candidates[0].structure == "jade_lizard"
    assert len(candidates[0].legs) == 3
    assert any(leg.role == "short_put" for leg in candidates[0].legs)
    assert any(leg.role == "short_call" for leg in candidates[0].legs)
    assert any(leg.role == "long_call" for leg in candidates[0].legs)


def test_long_call_and_long_put_builders_create_debit_candidates() -> None:
    call_candidates = _candidates(
        _playbook("long_call", applicable_direction=["bullish"], applicable_thesis_tags=[]),
        _idea(direction="bullish", strategy_hint="long_call"),
    )
    put_candidates = _candidates(
        _playbook("long_put", applicable_direction=["bearish"], applicable_thesis_tags=[]),
        _idea(direction="bearish", strategy_hint="long_put"),
    )

    assert any(candidate.eligible and candidate.structure == "long_call" for candidate in call_candidates)
    assert any(candidate.eligible and candidate.structure == "long_put" for candidate in put_candidates)


def test_per_idea_cap_preserves_structure_diversity() -> None:
    idea = _idea(direction="bullish", thesis_tags=["catalyst"], horizon_days=30)
    playbooks = [
        _playbook(
            "put_spread",
            playbook_id="put_spread_default",
            applicable_direction=["bullish"],
            applicable_thesis_tags=["catalyst"],
            leg_count=2,
        ),
        _playbook(
            "call_diagonal",
            playbook_id="call_diagonal_oversold",
            applicable_direction=["bullish"],
            applicable_thesis_tags=["catalyst"],
            leg_count=2,
            dte_min=25,
            dte_max=35,
            long_dte_min=55,
            long_dte_max=80,
            short_delta_min=0.2,
            short_delta_max=0.3,
            long_delta_min=0.35,
            long_delta_max=0.5,
        ),
    ]

    candidates = build_candidates(
        [idea],
        [UniverseEntry(symbol='NVDA', enabled=True)],
        playbooks,
        FixtureMarketDataProvider(),
        FixturePreflightClient(),
        per_idea_cap=2,
    )

    assert {candidate.structure for candidate in candidates if candidate.eligible} == {"put_spread", "call_diagonal"}
