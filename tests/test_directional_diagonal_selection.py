from __future__ import annotations

from datetime import date, timedelta

import pytest

from kamandal_v2.domain.models import Idea, OptionQuote, Playbook
from kamandal_v2.planner.candidate_builder import (
    _call_diagonal_candidates,
    _put_diagonal_candidates,
)


def _playbook(structure: str) -> Playbook:
    return Playbook(
        playbook_id=f"{structure}_directional",
        enabled=True,
        strategy_family=structure,
        structure=structure,
        variant="directional",
        leg_count=2,
        profiles=["mid_stocks", "large_stocks", "index_etf"],
        applicable_direction=["bullish" if structure == "call_diagonal" else "bearish"],
        dte_min=20,
        dte_max=30,
        long_dte_min=45,
        long_dte_max=60,
        short_delta_min=0.20,
        short_delta_max=0.30,
        long_delta_min=0.45,
        long_delta_max=0.55,
        spread_width=None,
        max_bid_ask_pct=0.20,
        min_option_oi=25,
    )


def _idea(structure: str, underlying: str) -> Idea:
    return Idea.from_dict(
        {
            "idea_id": f"idea-{underlying}-{structure}",
            "source": "test",
            "underlying": underlying,
            "direction": "bullish" if structure == "call_diagonal" else "bearish",
        }
    )


def _quote(
    underlying: str,
    option_type: str,
    strike: float,
    dte: int,
    delta: float,
    mid: float,
) -> OptionQuote:
    return OptionQuote(
        underlying=underlying,
        expiration=(date.today() + timedelta(days=dte)).isoformat(),
        option_type=option_type,
        strike=strike,
        bid=round(mid * 0.96, 2),
        ask=round(mid * 1.04, 2),
        delta=delta,
        gamma=0.01,
        theta=-0.02,
        vega=0.08,
        iv=0.30,
        open_interest=500,
    )


@pytest.mark.parametrize(
    ("underlying", "spot", "long_strike", "short_strike"),
    [
        ("SPCX", 137.95, 140.0, 146.0),
        ("NVDA", 213.05, 215.0, 227.5),
        ("SPY", 765.91, 765.0, 774.0),
    ],
)
def test_call_diagonal_reproduces_operator_price_scale_examples(
    underlying: str,
    spot: float,
    long_strike: float,
    short_strike: float,
) -> None:
    del spot  # The delta targets, not a fixed dollar width, scale the strikes.
    quotes = [
        _quote(underlying, "call", short_strike, 23, 0.25, 2.00),
        _quote(underlying, "call", short_strike - 1, 23, 0.30, 2.40),
        _quote(underlying, "call", short_strike + 1, 23, 0.20, 1.60),
        _quote(underlying, "call", long_strike, 51, 0.50, 7.00),
        _quote(underlying, "call", long_strike - 1, 51, 0.55, 7.80),
        _quote(underlying, "call", long_strike + 1, 51, 0.45, 6.20),
    ]

    candidates = _call_diagonal_candidates(
        _idea("call_diagonal", underlying),
        _playbook("call_diagonal"),
        quotes,
        config={"planner": {"expiry": {"diagonal_calendar_dte_fallback": {"enabled": True}}}},
    )

    assert len(candidates) == 1
    legs = {leg.role: leg for leg in candidates[0].legs}
    assert legs["long_far"].strike == long_strike
    assert legs["short_near"].strike == short_strike
    assert abs(short_strike - long_strike) in {6.0, 9.0, 12.5}
    assert "diagonal_pair_selection=independent_sheet_leg_targets" in candidates[0].reasons


def test_put_diagonal_is_the_exact_bearish_mirror() -> None:
    quotes = [
        _quote("XYZ", "put", 280, 23, -0.25, 2.00),
        _quote("XYZ", "put", 285, 23, -0.30, 2.40),
        _quote("XYZ", "put", 275, 23, -0.20, 1.60),
        _quote("XYZ", "put", 300, 51, -0.50, 7.00),
        _quote("XYZ", "put", 305, 51, -0.55, 7.80),
        _quote("XYZ", "put", 295, 51, -0.45, 6.20),
    ]

    candidates = _put_diagonal_candidates(
        _idea("put_diagonal", "XYZ"),
        _playbook("put_diagonal"),
        quotes,
    )

    assert len(candidates) == 1
    legs = {leg.role: leg for leg in candidates[0].legs}
    assert legs["long_far"].strike == 300
    assert legs["short_near"].strike == 280
    assert legs["long_far"].strike >= legs["short_near"].strike


def test_directional_diagonal_does_not_silently_widen_dte_windows() -> None:
    quotes = [
        _quote("XYZ", "call", 110, 19, 0.25, 2.00),
        _quote("XYZ", "call", 100, 44, 0.50, 7.00),
    ]

    candidates = _call_diagonal_candidates(
        _idea("call_diagonal", "XYZ"),
        _playbook("call_diagonal"),
        quotes,
        config={"planner": {"expiry": {"diagonal_calendar_dte_fallback": {"enabled": True}}}},
    )

    assert candidates == []
