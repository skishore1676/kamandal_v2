from datetime import date, timedelta

from kamandal_v2.market.fixture import FixtureMarketDataProvider
from kamandal_v2.volatility.iv import IvOverlayMarket, snapshot_from_chain
from kamandal_v2.volatility.iv_store import IvSnapshot, IvStore


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
