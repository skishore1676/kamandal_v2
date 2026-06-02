from datetime import date, timedelta
import json
import math
import sys
from types import SimpleNamespace

from kamandal_v2.events.earnings import (
    EarningsOverlayMarket,
    EarningsSnapshot,
    EarningsStore,
    FixtureEarningsProvider,
    capture_earnings_snapshots,
    earnings_event_status,
)
from kamandal_v2.market.fixture import FixtureMarketDataProvider


def test_earnings_event_status_buckets() -> None:
    today = date(2026, 5, 3)

    assert earnings_event_status(None, today=today) == "unknown"
    assert earnings_event_status(_snapshot(today), today=today) == "earnings_today"
    assert earnings_event_status(_snapshot(today + timedelta(days=3)), today=today) == "earnings_soon:3d"
    assert earnings_event_status(_snapshot(today + timedelta(days=30)), today=today) == "clear"
    assert earnings_event_status(_snapshot(today - timedelta(days=1)), today=today) == "post_earnings"


def test_capture_earnings_snapshots_saves_to_store(tmp_path) -> None:
    store = EarningsStore(tmp_path / "kamandal.db")
    config = {}

    result = capture_earnings_snapshots(
        config,
        symbols=["TSLA"],
        provider="fixture",
        store=store,
    )

    assert result.failed == {}
    assert result.captured[0].symbol == "TSLA"
    assert store.latest("TSLA").source == "fixture"


def test_store_writes_strict_json_for_nan_values(tmp_path) -> None:
    store = EarningsStore(tmp_path / "kamandal.db")
    store.save(EarningsSnapshot(
        symbol="AAPL",
        fetched_date="2026-05-03",
        next_earnings_date="2026-07-30",
        source="fixture",
        raw={"estimate": math.nan},
    ))

    loaded = store.latest("AAPL")

    assert loaded.raw == {"estimate": None}
    json.dumps(loaded.raw, allow_nan=False)


def test_earnings_overlay_uses_cached_event_status(tmp_path) -> None:
    store = EarningsStore(tmp_path / "kamandal.db")
    upcoming = _snapshot(date.today() + timedelta(days=4), symbol="AAPL")
    store.save(upcoming)
    market = EarningsOverlayMarket(FixtureMarketDataProvider(), store, source="fixture")

    assert market.event_status("AAPL") == "earnings_soon:4d"
    assert market.event_status("TSLA") == "clear"


def test_fixture_provider_can_record_missing_date() -> None:
    snapshot = FixtureEarningsProvider({"SPY": None}).next_earnings("SPY")

    assert snapshot.next_earnings_date is None
    assert snapshot.confirmed is False


def test_yfinance_provider_captures_noisy_stderr(monkeypatch, capsys) -> None:
    from kamandal_v2.events.earnings import YFinanceEarningsProvider

    class FakeTicker:
        def __init__(self, _symbol: str) -> None:
            pass

        def get_earnings_dates(self, *, limit: int):
            print("SPY: No earnings dates found, symbol may be delisted", file=sys.stderr)
            return None

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=FakeTicker))

    snapshot = YFinanceEarningsProvider().next_earnings("SPY")

    assert snapshot.next_earnings_date is None
    assert snapshot.raw["provider_messages"] == ["SPY: No earnings dates found, symbol may be delisted"]
    assert capsys.readouterr().err == ""


def _snapshot(value: date, *, symbol: str = "TSLA") -> EarningsSnapshot:
    return EarningsSnapshot(
        symbol=symbol,
        fetched_date="2026-05-03",
        next_earnings_date=value.isoformat(),
        source="fixture",
        confirmed=True,
    )
