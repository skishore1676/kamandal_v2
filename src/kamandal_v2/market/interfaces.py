"""Provider interfaces."""

from __future__ import annotations

from typing import Protocol

from kamandal_v2.domain.models import Candidate, ChainSnapshot, PortfolioState, PreflightResult


class MarketDataProvider(Protocol):
    def account_state(self) -> PortfolioState:
        ...

    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        ...

    def iv_percentile(self, underlying: str) -> float | None:
        ...

    def iv_rank(self, underlying: str) -> float | None:
        ...

    def iv_abs(self, underlying: str) -> float | None:
        ...

    def event_status(self, underlying: str) -> str:
        ...


class PreflightClient(Protocol):
    def preflight(self, candidate: Candidate) -> PreflightResult:
        ...
