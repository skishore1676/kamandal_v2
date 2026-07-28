from __future__ import annotations

from pathlib import Path

import pytest

from kamandal_v2.domain.models import ChainSnapshot, OptionQuote
from kamandal_v2.research.vertical_telemetry import (
    IdeaDirection,
    TelemetryStore,
    assert_telemetry_safe,
    lane_id_for,
    mark_lane,
    mark_payload,
    safe_telemetry_config,
    select_vertical,
)


def quote(
    expiration: str,
    option_type: str,
    strike: float,
    bid: float,
    ask: float,
    delta: float,
) -> OptionQuote:
    return OptionQuote(
        underlying="IWM",
        expiration=expiration,
        option_type=option_type,
        strike=strike,
        bid=bid,
        ask=ask,
        delta=delta,
        gamma=0.01,
        theta=-0.02,
        vega=0.05,
        iv=0.25,
        open_interest=500,
        volume=100,
    )


def snapshot(
    captured_at: str = "2026-07-28T15:00:00Z",
) -> ChainSnapshot:
    expiration = "2026-07-31"
    return ChainSnapshot(
        chain_snapshot_id="snap-1",
        underlying="IWM",
        captured_at=captured_at,
        underlying_price=200.0,
        quotes=[
            quote(expiration, "put", 198.0, 1.00, 1.05, -0.30),
            quote(expiration, "put", 196.0, 0.40, 0.45, -0.15),
            quote(expiration, "call", 202.0, 1.00, 1.05, 0.30),
            quote(expiration, "call", 204.0, 0.40, 0.45, 0.15),
        ],
        source="fixture",
    )


def test_safe_config_is_fail_closed() -> None:
    base = {
        "runtime": {"mode": "live", "trading_enabled": True},
        "execution": {"submit_to_broker": True},
        "live": {
            "auto_submit_entries": True,
            "auto_submit_exits": True,
        },
    }
    safe = safe_telemetry_config(base)
    assert_telemetry_safe(safe)
    assert safe["runtime"]["mode"] == "shadow"
    assert not safe["runtime"]["trading_enabled"]
    assert not safe["execution"]["submit_to_broker"]
    assert not safe["live"]["auto_submit_entries"]
    assert not safe["live"]["auto_submit_exits"]


def test_selects_strict_bull_and_bear_verticals() -> None:
    snap = snapshot()
    bullish = select_vertical(
        snap,
        direction="LONG",
        requested_dte=3,
    )
    bearish = select_vertical(
        snap,
        direction="SHORT",
        requested_dte=3,
    )
    assert bullish is not None
    assert (
        bullish.long_quote.strike
        < bullish.short_quote.strike
        < snap.underlying_price
    )
    assert bullish.natural_credit == pytest.approx(1.00 - 0.45)
    assert bearish is not None
    assert (
        snap.underlying_price
        < bearish.short_quote.strike
        < bearish.long_quote.strike
    )
    assert bearish.natural_credit == pytest.approx(1.00 - 0.45)


def test_store_persists_exact_contracts_and_natural_mark(
    tmp_path: Path,
) -> None:
    store = TelemetryStore(tmp_path / "telemetry.db")
    snap = snapshot()
    idea = IdeaDirection("IWM", "LONG", "event-1")
    spread = select_vertical(
        snap,
        direction="LONG",
        requested_dte=3,
    )
    assert spread is not None
    lane_id = lane_id_for(idea, spread, snap.captured_at)
    assert store.save_lane(lane_id, idea, snap, spread)
    lane = store.open_lanes()[0]
    assert lane["short_strike"] == spread.short_quote.strike
    assert lane["long_strike"] == spread.long_quote.strike

    mark = mark_lane(store, lane, snap)
    assert mark.quote_complete
    expected = ((1.00 - 0.45) - (1.05 - 0.40)) * 100.0
    assert mark.pnl_natural_dollars == pytest.approx(expected)
    assert store.save_mark(mark, snap, mark_payload(lane, snap))
    report = store.report()
    assert report["lanes"] == 1
    assert report["marks"] == 1
    assert report["latest"][0]["latest_mark"]["quote_complete"] == 1
