from datetime import date, timedelta

from kamandal_v2.domain.models import ChainSnapshot, OptionQuote
from kamandal_v2.market.fixture import FixtureMarketDataProvider
from kamandal_v2.volatility.iv import (
    IvOverlayMarket,
    PrimaryIvOverlayMarket,
    capture_iv_snapshots,
    snapshot_from_chain,
)
from kamandal_v2.volatility.iv_store import IvSnapshot, IvStore


def _quote(*, strike: float, dte: int, option_type: str, iv: float) -> OptionQuote:
    return OptionQuote(
        underlying="TSLA",
        expiration=(date.today() + timedelta(days=dte)).isoformat(),
        option_type=option_type,
        strike=strike,
        bid=1.00,
        ask=1.10,
        delta=0.5 if option_type == "call" else -0.5,
        gamma=0.01,
        theta=-0.03,
        vega=0.04,
        iv=iv,
    )


class _ShortDatedChainMarket:
    """Mocked adapter: weeklies (short DTE) carry higher IV than the 30-45 tenor."""

    def __init__(self) -> None:
        self.calls = 0

    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        self.calls += 1
        price = 250.0
        quotes: list[OptionQuote] = []
        # Near-the-money strikes across two tenors with distinct IV per tenor.
        for option_type in ("call", "put"):
            for strike in (245.0, 250.0, 255.0):
                quotes.append(_quote(strike=strike, dte=4, option_type=option_type, iv=0.70))
                quotes.append(_quote(strike=strike, dte=37, option_type=option_type, iv=0.50))
        return ChainSnapshot(
            chain_snapshot_id=f"mock_{underlying}",
            underlying=underlying,
            captured_at="2026-06-13T00:00:00Z",
            underlying_price=price,
            quotes=quotes,
            source="mock",
        )


def test_short_dated_metric_uses_only_near_term_quotes() -> None:
    chain = _ShortDatedChainMarket().chain_snapshot("TSLA")

    short = snapshot_from_chain(chain, metric="atm_2_10_mean_iv", dte_min=2, dte_max=10, sample_size=8)
    long_ = snapshot_from_chain(chain, metric="atm_30_45_mean_iv", dte_min=30, dte_max=45, sample_size=12)

    assert short.metric == "atm_2_10_mean_iv"
    assert short.raw["dte_min"] == 2 and short.raw["dte_max"] == 10
    # IV is normalized to percentage points (0.70 -> 70.0); only the weeklies feed it.
    assert short.iv == 70.0
    assert long_.iv == 50.0
    assert short.iv != long_.iv


def test_capture_iv_snapshots_writes_both_metrics(tmp_path) -> None:
    config = {
        "volatility": {
            "metrics": [
                {"metric": "atm_30_45_mean_iv", "dte_min": 30, "dte_max": 45, "sample_size": 12},
                {"metric": "atm_2_10_mean_iv", "dte_min": 2, "dte_max": 10, "sample_size": 8},
            ]
        }
    }
    store = IvStore(tmp_path / "iv.db")
    market = _ShortDatedChainMarket()

    # Patch the provider factory (no live adapter) and the chain store (no real DB write).
    import kamandal_v2.volatility.iv as iv_module

    class _NullLocalStore:
        def save_chain_snapshot(self, snapshot) -> None:  # noqa: ANN001
            return None

    original_provider = iv_module._market_provider
    original_local_store = iv_module.LocalStore
    iv_module._market_provider = lambda config, *, provider: market
    iv_module.LocalStore = _NullLocalStore
    try:
        result = capture_iv_snapshots(config, symbols=["TSLA"], provider="public", store=store)
    finally:
        iv_module._market_provider = original_provider
        iv_module.LocalStore = original_local_store

    assert result.failures == {}
    metrics = sorted(snap.metric for snap in result.snapshots)
    assert metrics == ["atm_2_10_mean_iv", "atm_30_45_mean_iv"]
    assert market.calls == 1  # chain fetched once, reused for both metrics
    assert store.latest("TSLA", metric="atm_2_10_mean_iv").iv == 70.0
    assert store.latest("TSLA", metric="atm_30_45_mean_iv").iv == 50.0


def test_snapshot_from_chain_uses_near_atm_iv_quotes() -> None:
    chain = FixtureMarketDataProvider().chain_snapshot("TSLA")

    snapshot = snapshot_from_chain(chain)

    assert snapshot.symbol == "TSLA"
    assert snapshot.metric == "atm_30_45_mean_iv"
    assert snapshot.iv > 0
    assert snapshot.quote_count > 0
    assert snapshot.raw["dte_min"] == 30


def test_iv_store_percentile_uses_local_history(tmp_path) -> None:
    store = IvStore(tmp_path / "iv.db")
    for offset, iv in enumerate([20.0, 30.0, 40.0]):
        store.save(
            IvSnapshot(
                symbol="TSLA",
                snapshot_date=(date.today() - timedelta(days=2 - offset)).isoformat(),
                iv=iv,
                source="test",
                metric="atm_30_45_mean_iv",
                quote_count=10,
                raw={},
            )
        )

    assert store.percentile("TSLA") == 100.0
    assert store.rank("TSLA") == 100.0


def test_iv_store_single_observation_is_neutral_bootstrap(tmp_path) -> None:
    store = IvStore(tmp_path / "iv.db")
    store.save(
        IvSnapshot(
            symbol="TSLA",
            snapshot_date=date.today().isoformat(),
            iv=40.0,
            source="test",
            metric="atm_30_45_mean_iv",
            quote_count=10,
            raw={},
        )
    )

    assert store.percentile("TSLA") == 50.0
    assert store.rank("TSLA") == 50.0


def test_iv_overlay_prefers_local_percentile(tmp_path) -> None:
    store = IvStore(tmp_path / "iv.db")
    for offset, iv in enumerate([40.0, 30.0]):
        store.save(
            IvSnapshot(
                symbol="TSLA",
                snapshot_date=(date.today() - timedelta(days=1 - offset)).isoformat(),
                iv=iv,
                source="test",
                metric="atm_30_45_mean_iv",
                quote_count=10,
                raw={},
            )
        )
    overlay = IvOverlayMarket(FixtureMarketDataProvider(), store)

    assert overlay.iv_percentile("TSLA") == 50.0
    assert overlay.iv_rank("TSLA") == 0.0
    assert overlay.iv_abs("TSLA") == 30.0


def test_iv_overlay_can_use_neutral_policy_when_history_is_missing(tmp_path) -> None:
    store = IvStore(tmp_path / "iv.db")
    overlay = IvOverlayMarket(
        FixtureMarketDataProvider(),
        store,
        missing_policy="neutral",
        provisional_percentile=50.0,
    )

    assert overlay.iv_percentile("UNKNOWN") == 50.0


def test_primary_iv_overlay_prefers_primary_market_metrics(tmp_path) -> None:
    class PrimaryMarket:
        def iv_percentile(self, underlying: str) -> float | None:
            return 71.2

        def iv_rank(self, underlying: str) -> float | None:
            return 0.0

        def iv_abs(self, underlying: str) -> float | None:
            return 42.0

    store = IvStore(tmp_path / "iv.db")
    store.save(
        IvSnapshot(
            symbol="TSLA",
            snapshot_date=date.today().isoformat(),
            iv=30.0,
            source="test",
            metric="atm_30_45_mean_iv",
            quote_count=10,
            raw={},
        )
    )
    overlay = PrimaryIvOverlayMarket(FixtureMarketDataProvider(), store, primary=PrimaryMarket())

    assert overlay.iv_percentile("TSLA") == 71.2
    assert overlay.iv_rank("TSLA") == 0.0
    assert overlay.iv_abs("TSLA") == 42.0


def test_primary_iv_overlay_normalizes_fractional_primary_market_metrics(tmp_path) -> None:
    class FractionalPrimaryMarket:
        def iv_percentile(self, underlying: str) -> float | None:
            return 0.226038885

        def iv_rank(self, underlying: str) -> float | None:
            return 0.1063

        def iv_abs(self, underlying: str) -> float | None:
            return 0.317353744

    overlay = PrimaryIvOverlayMarket(FixtureMarketDataProvider(), IvStore(tmp_path / "iv.db"), primary=FractionalPrimaryMarket())

    assert overlay.iv_percentile("AMZN") == 22.6039
    assert overlay.iv_rank("AMZN") == 10.63
    assert overlay.iv_abs("AMZN") == 31.7354


def test_iv_overlay_normalizes_fractional_fallback_market_metrics(tmp_path) -> None:
    class FractionalFallbackMarket(FixtureMarketDataProvider):
        def iv_percentile(self, underlying: str) -> float | None:
            return 0.488159524

        def iv_rank(self, underlying: str) -> float | None:
            return 0.42

        def iv_abs(self, underlying: str) -> float | None:
            return 0.5652961

    overlay = IvOverlayMarket(FractionalFallbackMarket(), IvStore(tmp_path / "iv.db"))

    assert overlay.iv_percentile("SHOP") == 48.816
    assert overlay.iv_rank("SHOP") == 42.0
    assert overlay.iv_abs("SHOP") == 56.5296


def test_primary_iv_overlay_falls_back_to_local_history(tmp_path) -> None:
    class MissingPrimaryMarket:
        def iv_percentile(self, underlying: str) -> float | None:
            return None

        def iv_rank(self, underlying: str) -> float | None:
            raise RuntimeError("primary temporarily unavailable")

        def iv_abs(self, underlying: str) -> float | None:
            return None

    store = IvStore(tmp_path / "iv.db")
    for offset, iv in enumerate([40.0, 30.0]):
        store.save(
            IvSnapshot(
                symbol="TSLA",
                snapshot_date=(date.today() - timedelta(days=1 - offset)).isoformat(),
                iv=iv,
                source="test",
                metric="atm_30_45_mean_iv",
                quote_count=10,
                raw={},
            )
        )
    overlay = PrimaryIvOverlayMarket(FixtureMarketDataProvider(), store, primary=MissingPrimaryMarket())

    assert overlay.iv_percentile("TSLA") == 50.0
    assert overlay.iv_rank("TSLA") == 0.0
    assert overlay.iv_abs("TSLA") == 30.0
