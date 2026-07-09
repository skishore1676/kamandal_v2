from datetime import date, timedelta

from kamandal_v2.domain.models import ChainSnapshot, Greeks, Idea, OptionQuote, Playbook, PortfolioState, UniverseEntry, utc_now
from kamandal_v2.market.fixture import FixturePreflightClient
from kamandal_v2.planner.candidate_builder import _call_spread_candidates, _put_spread_candidates, build_candidates
from kamandal_v2.planner.engine import _vertical_gate_unmet_count


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
        "leg_count": 2,
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


def _opt_quote(option_type: str, strike: float, mid: float, delta: float, expiration: str) -> OptionQuote:
    return OptionQuote(
        underlying="NVDA",
        expiration=expiration,
        option_type=option_type,
        strike=strike,
        bid=mid,
        ask=mid,
        delta=delta,
        gamma=0.01,
        theta=-0.02,
        vega=0.03,
        iv=40.0,
        open_interest=1000,
        volume=500,
    )


def _vertical_quotes(
    *,
    option_type: str,
    short_strike: float,
    short_mid: float,
    longs: dict[float, float],
    dte_days: int = 30,
) -> list[OptionQuote]:
    expiration = (date.today() + timedelta(days=dte_days)).isoformat()
    short_delta = -0.20 if option_type == "put" else 0.20
    long_delta = -0.05 if option_type == "put" else 0.05
    quotes = [_opt_quote(option_type, short_strike, short_mid, short_delta, expiration)]
    for strike, mid in longs.items():
        quotes.append(_opt_quote(option_type, strike, mid, long_delta, expiration))
    return quotes


class _SyntheticMarketDataProvider:
    """Deterministic market provider backed by a fixed quotes list, for tests
    that need exact control over strikes/credits (unlike FixtureMarketDataProvider,
    whose synthetic chain is not shaped for width-search scenarios)."""

    def __init__(self, quotes: list[OptionQuote], *, price: float = 120.0, account_size: float = 5000.0) -> None:
        self._quotes = quotes
        self._price = price
        self._account_size = account_size

    def account_state(self) -> PortfolioState:
        return PortfolioState(
            account_size=self._account_size,
            buying_power=self._account_size,
            bpr_used=0.0,
            positions_count=0,
            greeks=Greeks(),
            per_underlying_bpr={},
        )

    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        return ChainSnapshot(
            chain_snapshot_id="synthetic_test_chain",
            underlying=underlying,
            captured_at=utc_now(),
            underlying_price=self._price,
            quotes=self._quotes,
            source="test",
        )

    def iv_percentile(self, underlying: str) -> float | None:
        return 50.0

    def iv_rank(self, underlying: str) -> float | None:
        return 50.0

    def iv_abs(self, underlying: str) -> float | None:
        return 40.0

    def event_status(self, underlying: str) -> str:
        return "clear"


_BPR_CAP_CONFIG = {"live": {"max_bpr_per_order_by_structure": {"put_spread": 500, "call_spread": 500, "default": 500}}}


def _width_search_config(widths: list[float], *, enabled: bool = True, respect_bpr_cap: bool = True) -> dict:
    return {
        "planner": {"vertical_width_search": {"enabled": enabled, "widths": widths, "respect_bpr_cap": respect_bpr_cap}},
        "live": _BPR_CAP_CONFIG["live"],
    }


def test_vertical_width_search_disabled_matches_legacy_builder() -> None:
    idea = _idea()
    playbook = _playbook("put_spread", min_credit_to_width_ratio=0.28)
    quotes = _vertical_quotes(
        option_type="put",
        short_strike=100.0,
        short_mid=3.00,
        longs={95.0: 2.20, 92.5: 0.40, 90.0: 0.05},
    )

    legacy = _put_spread_candidates(idea, playbook, quotes)
    disabled = _put_spread_candidates(idea, playbook, quotes, config=_width_search_config([5.0, 7.5, 10.0], enabled=False))

    assert len(legacy) == 1
    assert [candidate.to_dict() for candidate in legacy] == [candidate.to_dict() for candidate in disabled]
    long_leg, short_leg = legacy[0].legs
    assert (long_leg.strike, short_leg.strike) == (95.0, 100.0)
    assert not any(reason.startswith("widths_tried=") for reason in legacy[0].reasons)


def test_call_spread_width_search_disabled_matches_legacy_builder() -> None:
    idea = _idea()
    playbook = _playbook("call_spread", min_credit_to_width_ratio=0.28)
    quotes = _vertical_quotes(
        option_type="call",
        short_strike=100.0,
        short_mid=3.00,
        longs={105.0: 2.20, 107.5: 0.40, 110.0: 0.05},
    )

    legacy = _call_spread_candidates(idea, playbook, quotes)
    disabled = _call_spread_candidates(idea, playbook, quotes, config=_width_search_config([5.0, 7.5, 10.0], enabled=False))

    assert len(legacy) == 1
    assert [candidate.to_dict() for candidate in legacy] == [candidate.to_dict() for candidate in disabled]
    short_leg, long_leg = legacy[0].legs
    assert (short_leg.strike, long_leg.strike) == (100.0, 105.0)


def test_vertical_width_search_finds_wider_construction_when_narrow_fails_gate() -> None:
    idea = _idea()
    playbook = _playbook("put_spread", min_credit_to_width_ratio=0.28)
    quotes = _vertical_quotes(
        option_type="put",
        short_strike=100.0,
        short_mid=3.00,
        longs={95.0: 2.20, 92.5: 0.40, 90.0: 0.05},
    )

    candidates = _put_spread_candidates(idea, playbook, quotes, config=_width_search_config([5.0, 7.5, 10.0]))

    assert len(candidates) == 1
    chosen = candidates[0]
    long_leg, short_leg = chosen.legs
    # 5-wide: ratio 0.16 fails the 0.28 gate. 7.5-wide: ratio ~0.35, passes, BPR 490 <= 500 cap.
    assert (long_leg.strike, short_leg.strike) == (92.5, 100.0)
    assert round(chosen.net_credit, 2) == 2.6
    assert chosen.estimated_bpr <= 500
    assert "widths_tried=[5.0,7.5,10.0]" in chosen.reasons


def test_vertical_width_search_excludes_construction_over_bpr_cap() -> None:
    # Both widths clear the credit gate; the narrower one (5-wide) would win on
    # "narrowest compliant" alone, but its BPR (350) blows a tighter $300 cap,
    # so the cap must exclude it and force the 7.5-wide (BPR 250) construction.
    idea = _idea()
    playbook = _playbook("put_spread", min_credit_to_width_ratio=0.28)
    quotes = _vertical_quotes(
        option_type="put",
        short_strike=100.0,
        short_mid=6.00,
        longs={95.0: 4.50, 92.5: 1.00},
    )
    config = {
        "planner": {"vertical_width_search": {"enabled": True, "widths": [5.0, 7.5], "respect_bpr_cap": True}},
        "live": {"max_bpr_per_order_by_structure": {"put_spread": 300, "default": 300}},
    }

    candidates = _put_spread_candidates(idea, playbook, quotes, config=config)

    assert len(candidates) == 1
    chosen = candidates[0]
    long_leg, short_leg = chosen.legs
    assert (long_leg.strike, short_leg.strike) == (92.5, 100.0)
    assert chosen.estimated_bpr <= 300


def test_vertical_width_search_rejects_when_all_widths_exceed_bpr_cap() -> None:
    idea = _idea()
    playbook = _playbook("put_spread", min_credit_to_width_ratio=0.28)
    quotes = _vertical_quotes(
        option_type="put",
        short_strike=100.0,
        short_mid=3.00,
        longs={95.0: 1.50, 92.5: 0.50},
    )
    config = {
        "planner": {"vertical_width_search": {"enabled": True, "widths": [5.0, 7.5], "respect_bpr_cap": True}},
        "live": {"max_bpr_per_order_by_structure": {"put_spread": 100, "default": 100}},
    }

    candidates = _put_spread_candidates(idea, playbook, quotes, config=config)

    assert len(candidates) == 1
    chosen = candidates[0]
    assert chosen.rejection_reason == "vertical_bpr_above_cap:350.0>100.0"
    assert "widths_tried=[5.0,7.5]" in chosen.reasons
    assert not chosen.eligible


def test_vertical_width_search_keeps_narrowest_gate_passing_construction() -> None:
    idea = _idea()
    playbook = _playbook("put_spread", min_credit_to_width_ratio=0.28)
    quotes = _vertical_quotes(
        option_type="put",
        short_strike=100.0,
        short_mid=3.00,
        longs={95.0: 1.50, 92.5: 0.375},
    )

    candidates = _put_spread_candidates(idea, playbook, quotes, config=_width_search_config([5.0, 7.5]))

    assert len(candidates) == 1
    chosen = candidates[0]
    long_leg, short_leg = chosen.legs
    # Both 5-wide (ratio 0.30) and 7.5-wide (ratio 0.35) clear the gate and the
    # cap; the narrower one must win even though it has the worse ratio.
    assert (long_leg.strike, short_leg.strike) == (95.0, 100.0)
    assert round(chosen.net_credit, 2) == 1.5


def test_vertical_width_search_gate_unmet_keeps_best_ratio_and_flags_metric() -> None:
    idea = _idea()
    universe = [UniverseEntry(symbol="NVDA", enabled=True, profile="large_stocks")]
    playbook = _playbook("put_spread", min_credit_to_width_ratio=0.28)
    quotes = _vertical_quotes(
        option_type="put",
        short_strike=100.0,
        short_mid=3.00,
        longs={95.0: 2.70, 94.0: 2.00},
    )
    provider = _SyntheticMarketDataProvider(quotes)

    candidates = build_candidates(
        [idea],
        universe,
        [playbook],
        provider,
        FixturePreflightClient(),
        config=_width_search_config([5.0, 6.0]),
    )

    put_spread_candidates = [candidate for candidate in candidates if candidate.structure == "put_spread"]
    assert len(put_spread_candidates) == 1
    chosen = put_spread_candidates[0]
    long_leg, short_leg = chosen.legs
    # Neither 5-wide (ratio 0.06) nor 6-wide (ratio 0.167) clears the 0.28
    # gate; the better-ratio (6-wide) construction is kept so the existing
    # rejection telemetry still fires.
    assert (long_leg.strike, short_leg.strike) == (94.0, 100.0)
    assert not chosen.eligible
    assert chosen.rejection_reason.startswith("credit_width_ratio_below_min")
    assert "widths_tried=[5.0,6.0]" in chosen.reasons
    assert _vertical_gate_unmet_count(candidates) == 1


def test_vertical_gate_unmet_count_ignores_pairs_with_an_eligible_construction() -> None:
    idea = _idea()
    universe = [UniverseEntry(symbol="NVDA", enabled=True, profile="large_stocks")]
    playbook = _playbook("put_spread", min_credit_to_width_ratio=0.28)
    quotes = _vertical_quotes(
        option_type="put",
        short_strike=100.0,
        short_mid=3.00,
        longs={95.0: 2.20, 92.5: 0.40, 90.0: 0.05},
    )
    provider = _SyntheticMarketDataProvider(quotes)

    candidates = build_candidates(
        [idea],
        universe,
        [playbook],
        provider,
        FixturePreflightClient(),
        config=_width_search_config([5.0, 7.5, 10.0]),
    )

    assert any(candidate.eligible for candidate in candidates)
    assert _vertical_gate_unmet_count(candidates) == 0
