"""Capture current option-chain IV and compute local IV percentiles."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kamandal_v2.domain.models import ChainSnapshot, OptionQuote
from kamandal_v2.market.fixture import FixtureMarketDataProvider
from kamandal_v2.market.interfaces import MarketDataProvider
from kamandal_v2.market.public import PublicAdapter
from kamandal_v2.market.tastytrade import TastytradeAdapter
from kamandal_v2.planner.config_loader import load_planner_config
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.volatility.iv_store import (
    DAILY_IV_ABS_METRIC,
    DAILY_IV_PERCENTILE_METRIC,
    DAILY_IV_RANK_METRIC,
    IvSnapshot,
    IvStore,
    today_iso,
)
from kamandal_v2.volatility.scale import normalize_iv_abs, normalize_iv_percentile, normalize_iv_rank


@dataclass(slots=True)
class IvCaptureResult:
    snapshots: list[IvSnapshot]
    failures: dict[str, str]
    fallbacks: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshots": [snapshot.to_dict() for snapshot in self.snapshots],
            "failures": dict(self.failures),
            "fallbacks": dict(self.fallbacks),
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
        daily = self.iv_store.latest_metric_value(underlying, DAILY_IV_PERCENTILE_METRIC)
        if daily is not None:
            return normalize_iv_percentile(daily)
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
            return normalize_iv_percentile(fallback)
        if self.missing_policy == "neutral":
            return normalize_iv_percentile(self.provisional_percentile)
        return None

    def iv_rank(self, underlying: str) -> float | None:
        daily = self.iv_store.latest_metric_value(underlying, DAILY_IV_RANK_METRIC)
        if daily is not None:
            return normalize_iv_rank(daily)
        local = self.iv_store.rank(
            underlying,
            metric=self.metric,
            lookback=self.lookback,
            min_observations=self.min_observations,
        )
        if local is not None:
            return local
        return normalize_iv_rank(self.inner.iv_rank(underlying))

    def iv_abs(self, underlying: str) -> float | None:
        daily = self.iv_store.latest_metric_value(underlying, DAILY_IV_ABS_METRIC)
        if daily is not None:
            return normalize_iv_abs(daily)
        latest = self.iv_store.latest(underlying, metric=self.metric)
        if latest is not None:
            return latest.iv
        return normalize_iv_abs(self.inner.iv_abs(underlying))

    def event_status(self, underlying: str) -> str:
        return self.inner.event_status(underlying)


class PrimaryIvOverlayMarket:
    def __init__(
        self,
        inner: MarketDataProvider,
        iv_store: IvStore,
        *,
        primary: MarketDataProvider,
        metric: str = "atm_30_45_mean_iv",
        lookback: int = 252,
        min_observations: int = 1,
        missing_policy: str = "strict",
        provisional_percentile: float = 50.0,
    ) -> None:
        self.inner = inner
        self.iv_store = iv_store
        self.primary = primary
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
        primary = self._primary_iv("iv_percentile", underlying)
        if primary is not None:
            return normalize_iv_percentile(primary)
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
            return normalize_iv_percentile(fallback)
        if self.missing_policy == "neutral":
            return normalize_iv_percentile(self.provisional_percentile)
        return None

    def iv_rank(self, underlying: str) -> float | None:
        primary = self._primary_iv("iv_rank", underlying)
        if primary is not None:
            return normalize_iv_rank(primary)
        local = self.iv_store.rank(
            underlying,
            metric=self.metric,
            lookback=self.lookback,
            min_observations=self.min_observations,
        )
        if local is not None:
            return local
        return normalize_iv_rank(self.inner.iv_rank(underlying))

    def iv_abs(self, underlying: str) -> float | None:
        primary = self._primary_iv("iv_abs", underlying)
        if primary is not None:
            return normalize_iv_abs(primary)
        latest = self.iv_store.latest(underlying, metric=self.metric)
        if latest is not None:
            return latest.iv
        return normalize_iv_abs(self.inner.iv_abs(underlying))

    def event_status(self, underlying: str) -> str:
        return self.inner.event_status(underlying)

    def _primary_iv(self, attr: str, underlying: str) -> float | None:
        try:
            value = getattr(self.primary, attr)(underlying)
        except Exception:
            return None
        return value


@dataclass(slots=True)
class MetricSpec:
    metric: str
    dte_min: int
    dte_max: int
    sample_size: int


def _metric_specs(config: dict[str, Any]) -> list[MetricSpec]:
    """Resolve the list of IV metrics to capture per run.

    Prefers ``volatility.metrics`` (a list of {metric, dte_min, dte_max, sample_size}).
    Falls back to the legacy single-metric keys so existing config keeps working.
    """
    volatility_config = config.get("volatility") or {}
    default_metric = str(volatility_config.get("metric") or "atm_30_45_mean_iv")
    default_dte_min = int(volatility_config.get("dte_min") or 30)
    default_dte_max = int(volatility_config.get("dte_max") or 45)
    default_sample_size = int(volatility_config.get("sample_size") or 12)

    raw_metrics = volatility_config.get("metrics")
    specs: list[MetricSpec] = []
    seen: set[str] = set()
    if isinstance(raw_metrics, list):
        for entry in raw_metrics:
            if not isinstance(entry, dict):
                continue
            metric = str(entry.get("metric") or default_metric)
            if metric in seen:
                continue
            seen.add(metric)
            specs.append(
                MetricSpec(
                    metric=metric,
                    dte_min=int(entry.get("dte_min", default_dte_min)),
                    dte_max=int(entry.get("dte_max", default_dte_max)),
                    sample_size=int(entry.get("sample_size", default_sample_size)),
                )
            )
    if not specs:
        specs.append(
            MetricSpec(
                metric=default_metric,
                dte_min=default_dte_min,
                dte_max=default_dte_max,
                sample_size=default_sample_size,
            )
        )
    return specs


def capture_iv_snapshots(
    config: dict[str, Any],
    *,
    symbols: list[str] | None = None,
    config_source: str = "sheet",
    provider: str = "public",
    store: IvStore | None = None,
    primary_market: Any | None = None,
) -> IvCaptureResult:
    store = store or IvStore()
    symbols = symbols or _universe_symbols(config, source=config_source)
    market = _market_provider(config, provider=provider)
    primary_market = primary_market if primary_market is not None else _preferred_volatility_market(config)
    local_store = LocalStore()
    specs = _metric_specs(config)
    snapshots: list[IvSnapshot] = []
    failures: dict[str, str] = {}
    fallbacks: dict[str, str] = {}
    lookback = int((config.get("volatility") or {}).get("lookback_days") or 252)
    primary_metric = str((config.get("volatility") or {}).get("metric") or "atm_30_45_mean_iv")
    for symbol in symbols:
        native: dict[str, Any] = {}
        if primary_market is not None:
            try:
                native = dict(primary_market.volatility_metrics(symbol) or {})
            except Exception as exc:  # noqa: BLE001 - local evidence is the declared fallback.
                fallbacks[symbol] = f"tastytrade_unavailable:{type(exc).__name__}"
        chain: ChainSnapshot | None = None
        try:
            chain = market.chain_snapshot(symbol)
            local_store.save_chain_snapshot(chain)
        except Exception as exc:  # noqa: BLE001
            failures[f"{symbol}:local_chain"] = str(exc)

        local_primary: IvSnapshot | None = None
        if chain is not None:
            for spec in specs:
                try:
                    snapshot = snapshot_from_chain(
                        chain,
                        metric=spec.metric,
                        dte_min=spec.dte_min,
                        dte_max=spec.dte_max,
                        sample_size=spec.sample_size,
                    )
                    store.save(snapshot)
                    snapshots.append(snapshot)
                    if spec.metric == primary_metric:
                        local_primary = snapshot
                except Exception as exc:  # noqa: BLE001
                    failures[f"{symbol}:{spec.metric}"] = str(exc)

        daily = _daily_metric_snapshots(
            symbol,
            native=native,
            local_primary=local_primary,
            store=store,
            lookback=lookback,
        )
        native_missing = [
            field
            for field in ("iv_abs", "iv_rank", "iv_percentile")
            if native.get(field) in (None, "")
        ]
        if native and native_missing:
            fallbacks.setdefault(symbol, "tastytrade_metrics_incomplete")
        if not native and local_primary is not None:
            fallbacks.setdefault(symbol, "tastytrade_metrics_unavailable")
        if not daily:
            failures.setdefault(symbol, "daily IV, IV Rank, and IV percentile unavailable")
        for snapshot in daily:
            store.save(snapshot)
            snapshots.append(snapshot)
    return IvCaptureResult(snapshots=snapshots, failures=failures, fallbacks=fallbacks)


def _daily_metric_snapshots(
    symbol: str,
    *,
    native: dict[str, Any],
    local_primary: IvSnapshot | None,
    store: IvStore,
    lookback: int,
) -> list[IvSnapshot]:
    captured_at = datetime.now(UTC).isoformat()
    history_count = len(store.history(symbol, metric=local_primary.metric, limit=lookback + 1)) if local_primary else 0
    local_values = {
        DAILY_IV_ABS_METRIC: local_primary.iv if local_primary else None,
        DAILY_IV_RANK_METRIC: (
            store.local_rank(symbol, metric=local_primary.metric, lookback=lookback) if local_primary else None
        ),
        DAILY_IV_PERCENTILE_METRIC: (
            store.local_percentile(symbol, metric=local_primary.metric, lookback=lookback) if local_primary else None
        ),
    }
    native_values = {
        DAILY_IV_ABS_METRIC: normalize_iv_abs(native.get("iv_abs")),
        DAILY_IV_RANK_METRIC: normalize_iv_rank(native.get("iv_rank")),
        DAILY_IV_PERCENTILE_METRIC: normalize_iv_percentile(native.get("iv_percentile")),
    }
    snapshots: list[IvSnapshot] = []
    for metric in (DAILY_IV_ABS_METRIC, DAILY_IV_RANK_METRIC, DAILY_IV_PERCENTILE_METRIC):
        native_value = native_values[metric]
        local_value = local_values[metric]
        value = native_value if native_value is not None else local_value
        if value is None:
            continue
        native_source = native_value is not None
        source = "tastytrade" if native_source else "local_fallback"
        if native_source:
            quality = "native"
        elif metric == DAILY_IV_ABS_METRIC:
            quality = "local_observed"
        elif metric == DAILY_IV_PERCENTILE_METRIC:
            quality = "local_full_history" if history_count >= lookback + 1 else "local_provisional_history"
        else:
            quality = "local_full_history" if history_count >= lookback else "local_provisional_history"
        snapshots.append(
            IvSnapshot(
                symbol=symbol,
                snapshot_date=today_iso(),
                iv=round(float(value), 4),
                source=source,
                metric=metric,
                quote_count=0 if native_source else int(local_primary.quote_count if local_primary else 0),
                raw={
                    "captured_at": captured_at,
                    "provider_asof": str(native.get("provider_asof") or "") if native_source else "",
                    "upstream_source": "tastytrade" if native_source else str(local_primary.source if local_primary else ""),
                    "formula_version": "provider_native" if native_source else "thinkscript_252_v1",
                    "lookback_requested": lookback,
                    "history_count": history_count,
                    "quality": quality,
                },
            )
        )
    return snapshots


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


def _preferred_volatility_market(config: dict[str, Any]) -> Any | None:
    broker = config.get("broker") or {}
    provider = str(broker.get("market_metrics_provider") or "").strip().lower()
    if provider not in {"tastytrade", "tasty"}:
        return None
    adapter = TastytradeAdapter(config)
    return adapter if adapter.available() else None
