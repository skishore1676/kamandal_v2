"""Separate market-data ownership from per-candidate execution venues."""

from __future__ import annotations

from typing import Any

from kamandal_v2.domain.models import Candidate, Greeks, PortfolioState, PreflightResult
from kamandal_v2.market.broker import broker_adapter, execution_venue_registry


class VenueAwareMarket:
    def __init__(
        self,
        inner: Any,
        primary_preflight: Any,
        config: dict[str, Any],
        *,
        mode: str,
        venues: set[str],
        provider: str,
    ) -> None:
        self.inner = inner
        self.primary_preflight = primary_preflight
        self.config = config
        self.mode = mode
        self.venues = set(venues)
        self.provider = provider
        self._adapters: dict[str, Any] = {}

    def account_state(self) -> PortfolioState:
        primary = self.inner.account_state()
        if self.mode != "live" or self.provider != "public" or self.venues <= {"public_primary"}:
            if self.mode == "live":
                self.config.setdefault("runtime", {})["venue_portfolios"] = {
                    "public_primary": primary.to_dict()
                }
            return primary
        states: dict[str, PortfolioState] = {}
        for venue in sorted(self.venues):
            adapter = self._adapter(venue)
            if hasattr(adapter, "available") and not adapter.available():
                raise RuntimeError(f"execution venue {venue} is unavailable")
            states[venue] = adapter.account_state()
        self.config.setdefault("runtime", {})["venue_portfolios"] = {
            venue: state.to_dict() for venue, state in states.items()
        }
        return _aggregate_portfolios(states)

    def preflight(self, candidate: Candidate) -> PreflightResult:
        venue = str(candidate.execution_venue or "public_primary")
        if self.provider != "public" or venue == "public_primary":
            result = self.primary_preflight.preflight(candidate)
        else:
            adapter = self._adapter(venue)
            if hasattr(adapter, "available") and not adapter.available():
                if self.mode == "shadow":
                    result = self.primary_preflight.preflight(candidate)
                else:
                    return PreflightResult(
                        ok=False,
                        bpr=0.0,
                        message=f"execution venue {venue} is unavailable",
                        raw={"execution_venue": venue, "live_eligible": False},
                    )
            else:
                result = adapter.preflight(candidate)
        raw = dict(result.raw or {})
        raw["execution_venue"] = venue
        raw["execution_broker"] = execution_venue_registry(self.config).get(venue, "")
        return PreflightResult(ok=result.ok, bpr=result.bpr, message=result.message, raw=raw)

    def chain_snapshot(self, underlying: str) -> Any:
        return self.inner.chain_snapshot(underlying)

    def iv_percentile(self, underlying: str) -> float | None:
        return self.inner.iv_percentile(underlying)

    def iv_rank(self, underlying: str) -> float | None:
        return self.inner.iv_rank(underlying)

    def iv_abs(self, underlying: str) -> float | None:
        return self.inner.iv_abs(underlying)

    def event_status(self, underlying: str) -> str:
        return self.inner.event_status(underlying)

    def _adapter(self, venue: str) -> Any:
        if venue not in self._adapters:
            self._adapters[venue] = broker_adapter(self.config, execution_venue=venue)
        return self._adapters[venue]


def _aggregate_portfolios(states: dict[str, PortfolioState]) -> PortfolioState:
    greeks = Greeks()
    per_underlying: dict[str, float] = {}
    for state in states.values():
        greeks = greeks + state.greeks
        for symbol, value in state.per_underlying_bpr.items():
            per_underlying[symbol] = round(per_underlying.get(symbol, 0.0) + value, 2)
    return PortfolioState(
        account_size=round(sum(state.account_size for state in states.values()), 2),
        buying_power=round(sum(state.buying_power for state in states.values()), 2),
        bpr_used=round(sum(state.bpr_used for state in states.values()), 2),
        positions_count=sum(state.positions_count for state in states.values()),
        greeks=greeks,
        per_underlying_bpr=per_underlying,
    )
