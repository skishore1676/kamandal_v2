"""Deterministic market/preflight fixture adapter."""

from __future__ import annotations

import math
from datetime import date, timedelta

from kamandal_v2.domain.models import Candidate, ChainSnapshot, Greeks, OptionQuote, PortfolioState, PreflightResult, utc_now


UNDERLYING_FIXTURES = {
    "TSLA": {"price": 250.0, "iv": 62.0, "iv_pct": 44.0},
    "NVDA": {"price": 900.0, "iv": 58.0, "iv_pct": 72.0},
    "SPY": {"price": 520.0, "iv": 18.0, "iv_pct": 38.0},
    "QQQ": {"price": 440.0, "iv": 22.0, "iv_pct": 42.0},
    "IWM": {"price": 205.0, "iv": 25.0, "iv_pct": 55.0},
    "TLT": {"price": 92.0, "iv": 20.0, "iv_pct": 48.0},
}


class FixtureMarketDataProvider:
    def __init__(self, *, account_size: float = 5000.0) -> None:
        self.account_size = account_size

    def account_state(self) -> PortfolioState:
        return PortfolioState(
            account_size=self.account_size,
            buying_power=self.account_size,
            bpr_used=0.0,
            positions_count=0,
            greeks=Greeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
            per_underlying_bpr={},
        )

    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        symbol = underlying.upper()
        fixture = UNDERLYING_FIXTURES.get(symbol, {"price": 100.0, "iv": 35.0, "iv_pct": 50.0})
        price = float(fixture["price"])
        expirations = [
            (date.today() + timedelta(days=30)).isoformat(),
            (date.today() + timedelta(days=45)).isoformat(),
            (date.today() + timedelta(days=60)).isoformat(),
        ]
        quotes: list[OptionQuote] = []
        for expiration in expirations:
            for option_type in ("call", "put"):
                for abs_delta in (0.15, 0.16, 0.22, 0.28, 0.30, 0.45, 0.55):
                    strike = _strike_for_delta(price, option_type, abs_delta)
                    quotes.append(_quote(symbol, expiration, option_type, strike, price, abs_delta, float(fixture["iv"])))
        return ChainSnapshot(
            chain_snapshot_id=f"fixture_chain_{symbol}_{date.today().isoformat()}",
            underlying=symbol,
            captured_at=utc_now(),
            underlying_price=price,
            quotes=quotes,
            source="fixture",
        )

    def iv_percentile(self, underlying: str) -> float | None:
        fixture = UNDERLYING_FIXTURES.get(underlying.upper())
        return float(fixture["iv_pct"]) if fixture else 50.0

    def iv_rank(self, underlying: str) -> float | None:
        fixture = UNDERLYING_FIXTURES.get(underlying.upper())
        return float(fixture["iv_pct"]) if fixture else 50.0

    def iv_abs(self, underlying: str) -> float | None:
        fixture = UNDERLYING_FIXTURES.get(underlying.upper())
        return float(fixture["iv"]) if fixture else 35.0

    def event_status(self, underlying: str) -> str:
        return "clear"


class FixturePreflightClient:
    def preflight(self, candidate: Candidate) -> PreflightResult:
        bpr = candidate.estimated_bpr if candidate.estimated_bpr > 0 else _estimate_bpr(candidate)
        return PreflightResult(ok=True, bpr=round(bpr, 2), message="fixture preflight ok", raw={"source": "fixture"})


def _strike_for_delta(price: float, option_type: str, abs_delta: float) -> float:
    if option_type == "call":
        factor = {0.15: 1.11, 0.16: 1.10, 0.22: 1.07, 0.28: 1.06, 0.30: 1.05, 0.45: 1.00, 0.55: 0.98}[abs_delta]
    else:
        factor = {0.15: 0.89, 0.16: 0.90, 0.22: 0.93, 0.28: 0.94, 0.30: 0.95, 0.45: 1.00, 0.55: 1.02}[abs_delta]
    increment = 5.0 if price > 150 else 1.0
    return round(round((price * factor) / increment) * increment, 2)


def _quote(
    symbol: str,
    expiration: str,
    option_type: str,
    strike: float,
    price: float,
    abs_delta: float,
    iv: float,
) -> OptionQuote:
    dte = max((date.fromisoformat(expiration) - date.today()).days, 1)
    intrinsic = max(price - strike, 0.0) if option_type == "call" else max(strike - price, 0.0)
    extrinsic = max(price * (iv / 100.0) * math.sqrt(dte / 365.0) * abs_delta * 0.16, 0.25)
    mid = round(intrinsic + extrinsic, 2)
    spread = max(round(mid * 0.06, 2), 0.05)
    signed_delta = abs_delta if option_type == "call" else -abs_delta
    return OptionQuote(
        underlying=symbol,
        expiration=expiration,
        option_type=option_type,
        strike=strike,
        bid=max(round(mid - spread / 2.0, 2), 0.01),
        ask=round(mid + spread / 2.0, 2),
        delta=signed_delta,
        gamma=round(0.01 * abs_delta, 4),
        theta=round(-0.03 * mid, 4),
        vega=round(0.04 * abs_delta, 4),
        iv=iv,
        open_interest=2500,
        volume=750,
    )


def _estimate_bpr(candidate: Candidate) -> float:
    if candidate.structure in {"put_spread", "call_spread"} and len(candidate.legs) == 2:
        width = abs(candidate.legs[0].strike - candidate.legs[1].strike)
        return max(width * 100 - max(candidate.net_credit, 0) * 100, 1.0)
    if candidate.structure == "iron_condor" and len(candidate.legs) == 4:
        put_width = abs(candidate.legs[0].strike - candidate.legs[1].strike)
        call_width = abs(candidate.legs[2].strike - candidate.legs[3].strike)
        return max(max(put_width, call_width) * 100 - max(candidate.net_credit, 0) * 100, 1.0)
    if candidate.structure == "short_put":
        short_put = candidate.legs[0]
        return max(short_put.strike * 100 * 0.2, 1.0)
    if candidate.structure == "short_strangle":
        short_strikes = [leg.strike for leg in candidate.legs if leg.side == "sell"]
        return max(max(short_strikes) * 100 * 0.2, 1.0)
    if candidate.structure == "jade_lizard":
        short_puts = [leg for leg in candidate.legs if leg.option_type == "put" and leg.side == "sell"]
        calls = [leg for leg in candidate.legs if leg.option_type == "call"]
        put_risk = short_puts[0].strike * 100 * 0.2 if short_puts else 0.0
        call_width = abs(calls[0].strike - calls[1].strike) if len(calls) == 2 else 0.0
        return max(put_risk + call_width * 100 - max(candidate.net_credit, 0) * 100, 1.0)
    if candidate.structure in {"call_calendar", "put_calendar", "put_diagonal", "call_diagonal", "long_call", "long_put"}:
        return max(abs(candidate.net_credit) * 100, 1.0)
    return max(abs(candidate.net_credit) * 100, 1.0)
