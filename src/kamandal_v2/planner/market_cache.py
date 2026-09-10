"""Quote reuse scoped to one planner invocation, never an execution client."""

from kamandal_v2.domain.models import ChainSnapshot
from kamandal_v2.market.interfaces import MarketDataProvider


class PlanningMarketCache:
    def __init__(self, inner: MarketDataProvider) -> None:
        self.inner = inner
        self._chains: dict[str, ChainSnapshot] = {}
        self.chain_requests = 0
        self.chain_cache_hits = 0

    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        self.chain_requests += 1
        if underlying in self._chains:
            self.chain_cache_hits += 1
            return self._chains[underlying]
        # Keep original capture times and complete expiration coverage. Failed
        # fetches are not cached; diagnostics may retry a transient failure.
        snapshot = self.inner.chain_snapshot(underlying)
        self._chains[underlying] = snapshot
        return snapshot

    def __getattr__(self, name):
        # Preserve venue/preflight capabilities; only chain snapshots are cached.
        return getattr(self.inner, name)

    def account_state(self):
        return self.inner.account_state()

    def iv_percentile(self, underlying: str):
        return self.inner.iv_percentile(underlying)

    def iv_rank(self, underlying: str):
        return self.inner.iv_rank(underlying)

    def iv_abs(self, underlying: str):
        return self.inner.iv_abs(underlying)

    def event_status(self, underlying: str):
        return self.inner.event_status(underlying)

    def metrics(self) -> dict[str, int]:
        return {
            "chain_requests": self.chain_requests,
            "chain_cache_hits": self.chain_cache_hits,
            "chain_symbols_fetched": len(self._chains),
        }
