from kamandal_v2.domain.models import Idea, Playbook, UniverseEntry
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
        [UniverseEntry(symbol="NVDA", enabled=True, profile="large_stocks")],
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
