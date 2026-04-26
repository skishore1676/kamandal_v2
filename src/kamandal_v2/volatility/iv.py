"""Capture current option-chain IV and compute local IV percentiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kamandal_v2.domain.models import ChainSnapshot, OptionQuote
from kamandal_v2.market.fixture import FixtureMarketDataProvider
from kamandal_v2.market.interfaces import MarketDataProvider
from kamandal_v2.market.public import PublicAdapter
from kamandal_v2.planner.config_loader import load_planner_config
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.volatility.iv_store import IvSnapshot, IvStore, today_iso


@dataclass(slots=True)
class IvCaptureResult:
    snapshots: list[IvSnapshot]
    failures: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshots": [snapshot.to_dict() for snapshot in self.snapshots],
            "failures": dict(self.failures),
        }


class IvOverlayMarket:
    def __init__(
        self,
        inner: MarketDataProvider,
        iv_store: IvStore,
        *,
        metric: str = "atm_30_45_mean_iv",
        lookback: int = 252,
        min_observations: int = 1,
        missing_policy: str = "strict",
        provisional_percentile: float = 50.0,
    ) -> None:
        self.inner = inner
        self.iv_store = iv_store
        self.metric = metric
        self.lookback = lookback
        self.min_observations = min_observations
        self.missing_policy = missing_policy
        self.provisional_percentile = provisional_percentile

    def account_state(self):
        return self.inner.account_state()

    def chain_snapshot(self, underlying: str):
        return self.inner.chain_snapshot(underlying)

    def iv_percentile(self, underlying: str) -> float | None:
        local = self.iv_store.percentile(
            underlying,
            metric=self.metric,
            lookback=self.lookback,
            min_observations=self.min_observations,
        )
        if local is not None:
            return local
        fallback = self.inner.iv_percentile(underlying)
        if fallback is not None:
            return fallback
        if self.missing_policy == "neutral":
            return self.provisional_percentile
        return None

    def iv_rank(self, underlying: str) -> float | None:
        local = self.iv_store.rank(
            underlying,
            metric=self.metric,
            lookback=self.lookback,
            min_observations=self.min_observations,
        )
        if local is not None:
            return local
        return self.inner.iv_rank(underlying)

    def iv_abs(self, underlying: str) -> float | None:
        latest = self.iv_store.latest(underlying, metric=self.metric)
        if latest is not None:
            return latest.iv
        return self.inner.iv_abs(underlying)

    def event_status(self, underlying: str) -> str:
        return self.inner.event_status(underlying)


def capture_iv_snapshots(
    config: dict[str, Any],
    *,
    symbols: list[str] | None = None,
    config_source: str = "sheet",
    provider: str = "public",
    store: IvStore | None = None,
) -> IvCaptureResult:
    store = store or IvStore()
    symbols = symbols or _universe_symbols(config, source=config_source)
    market = _market_provider(config, provider=provider)
    local_store = LocalStore()
    volatility_config = config.get("volatility") or {}
    metric = str(volatility_config.get("metric") or "atm_30_45_mean_iv")
    dte_min = int(volatility_config.get("dte_min") or 30)
    dte_max = int(volatility_config.get("dte_max") or 45)
    sample_size = int(volatility_config.get("sample_size") or 12)
    snapshots: list[IvSnapshot] = []
    failures: dict[str, str] = {}
    for symbol in symbols:
        try:
            chain = market.chain_snapshot(symbol)
            local_store.save_chain_snapshot(chain)
            snapshot = snapshot_from_chain(
                chain,
                metric=metric,
                dte_min=dte_min,
                dte_max=dte_max,
                sample_size=sample_size,
            )
            store.save(snapshot)
            snapshots.append(snapshot)
        except Exception as exc:  # noqa: BLE001
            failures[symbol] = str(exc)
    return IvCaptureResult(snapshots=snapshots, failures=failures)


def snapshot_from_chain(
    chain: ChainSnapshot,
    *,
    metric: str = "atm_30_45_mean_iv",
    dte_min: int = 30,
    dte_max: int = 45,
    sample_size: int = 12,
) -> IvSnapshot:
    quotes = _metric_quotes(
        chain,
        dte_min=dte_min,
        dte_max=dte_max,
        sample_size=sample_size,
    )
    if not quotes:
        raise ValueError(f"No IV-bearing quotes for {chain.underlying}")
    weighted_sum = 0.0
    weight_total = 0.0
    for quote in quotes:
        spread_weight = max(0.05, 1.0 - quote.spread_pct)
        moneyness_weight = max(0.05, 1.0 - abs((quote.strike / chain.underlying_price) - 1.0) * 10.0)
        weight = spread_weight * moneyness_weight
        weighted_sum += _normal_iv(quote.iv) * weight
        weight_total += weight
    iv = weighted_sum / max(weight_total, 0.0001)
    return IvSnapshot(
        symbol=chain.underlying,
        snapshot_date=today_iso(),
        iv=round(iv, 4),
        source=chain.source,
        metric=metric,
        quote_count=len(quotes),
        raw={
            "underlying_price": chain.underlying_price,
            "captured_at": chain.captured_at,
            "dte_min": dte_min,
            "dte_max": dte_max,
            "sample_size": sample_size,
        },
    )


def _metric_quotes(chain: ChainSnapshot, *, dte_min: int, dte_max: int, sample_size: int) -> list[OptionQuote]:
    candidates = [
        quote for quote in chain.quotes
        if dte_min <= quote.dte <= dte_max
        and quote.bid > 0
        and quote.ask > 0
        and quote.iv > 0
    ]
    if not candidates:
        candidates = [
            quote for quote in chain.quotes
            if quote.bid > 0 and quote.ask > 0 and quote.iv > 0
        ]
    return sorted(candidates, key=lambda quote: (abs((quote.strike / chain.underlying_price) - 1.0), quote.spread_pct))[:sample_size]


def _normal_iv(raw: float) -> float:
    value = float(raw)
    return value * 100.0 if value <= 5.0 else value


def _universe_symbols(config: dict[str, Any], *, source: str) -> list[str]:
    universe, _playbooks = load_planner_config(config, source=source)
    return [entry.symbol for entry in universe if entry.enabled]


def _market_provider(config: dict[str, Any], *, provider: str) -> MarketDataProvider:
    if provider == "public":
        return PublicAdapter(config)
    return FixtureMarketDataProvider()
