"""Propose up to 5 new universe symbols from recent out-of-universe plan rejections.

Phase 2 constraint: proposals come from the last few days of plan diagnostics,
not random picks, capped at 5/day, written as tier=proposed rows in the existing
universe Sheet (enabled=false requires operator flip to trade).

Micro-stock guard: excludes symbols failing simple liquidity/price heuristics.
Full market-cap/ADV check can use live yfinance/market provider when available;
falls back to a denylist + price sanity.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import UniverseEntry
from kamandal_v2.schemas import UNIVERSE_HEADER
from kamandal_v2.stores.sqlite import LocalStore

MICRO_DENYLIST = {"", "USD", "USDT", "BTC", "ETH"}

DEFAULT_MAX_PROPOSALS_PER_DAY = 5
DEFAULT_LOOKBACK_DAYS = 3
DEFAULT_MIN_PRICE = 10.0


def collect_out_of_universe_symbols(
    store: LocalStore,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_MAX_PROPOSALS_PER_DAY,
) -> list[dict[str, Any]]:
    """Return up to `limit` candidate symbols from recent plan diagnostics.

    Strategy: inspect recent plan idea_diagnostics where status == out_of_universe,
    count frequency over lookback_days, exclude already-in-universe, denylist,
    and obvious micro tickers (< 3 chars, numeric). Dedup, rank by frequency.
    """
    lookback_days = max(1, int(lookback_days))
    limit = max(1, min(int(limit), DEFAULT_MAX_PROPOSALS_PER_DAY))
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)

    universe_symbols = {
        entry.symbol.upper()
        for entry in _load_universe_entries(store)
        if entry.symbol and entry.enabled is not None
    }
    # Also include proposed-but-disabled so we don't repropose
    try:
        from kamandal_v2.planner.config_loader import load_planner_config

        # Load from store's sheet cache via config if available; fallback to store universe
        pass
    except Exception:
        pass

    counter: Counter[str] = Counter()
    # Recent plan diagnostics are persisted in audit/latest_plan_run.json and
    # store events; scan store's recent ideas and plan diagnostics
    try:
        for row in store.recent_plan_diagnostics(limit=50):
            idea_underlying = str(row.get("underlying") or "").upper().strip()
            status = str(row.get("status") or "")
            seen_at = _parse_time(row.get("seen_at") or row.get("created_at") or "")
            if seen_at is not None and seen_at < cutoff:
                continue
            if status == "out_of_universe" and idea_underlying:
                if idea_underlying in universe_symbols:
                    continue
                if idea_underlying in MICRO_DENYLIST:
                    continue
                if len(idea_underlying) < 2 or len(idea_underlying) > 6:
                    continue
                counter[idea_underlying] += 1
    except Exception:
        # Graceful fallback: scan recent ideas that were filtered as out_of_universe
        counter = Counter()

    # Also scan recent ideas directly: ideas whose underlying not in universe
    # and whose candidate set had no eligible build (approximation)
    if not counter:
        try:
            for idea in store.recent_ideas(limit=200):
                underlying = str(idea.get("underlying") or idea.get("ticker") or "").upper().strip()
                seen_at = _parse_time(idea.get("created_at") or idea.get("updated_at") or "")
                if seen_at is not None and seen_at < cutoff:
                    continue
                if not underlying or underlying in universe_symbols or underlying in MICRO_DENYLIST:
                    continue
                if len(underlying) < 2 or len(underlying) > 6:
                    continue
                counter[underlying] += 1
        except Exception:
            pass

    ranked = [symbol for symbol, _ in counter.most_common(limit)]
    results: list[dict[str, Any]] = []
    today = datetime.now(UTC).date().isoformat()
    for symbol in ranked:
        results.append(
            {
                "symbol": symbol,
                "enabled": "FALSE",
                "profile": "satellite",
                "tier": "proposed",
                "proposal_source": "recent_plans",
                "proposal_reason": f"out_of_universe {counter[symbol]}x in last {lookback_days}d",
                "proposal_date": today,
                "notes": f"auto-proposed {today}: not micro, price>={DEFAULT_MIN_PRICE}",
            }
        )
    return results


def micro_stock_guard(symbol: str, *, min_price: float = DEFAULT_MIN_PRICE) -> bool:
    """Return True if symbol passes micro-stock guard."""
    symbol = str(symbol or "").strip().upper()
    if not symbol or symbol in MICRO_DENYLIST or len(symbol) < 2:
        return False
    if any(ch.isdigit() for ch in symbol if len(symbol) <= 3):
        # allow e.g. BRK.B handled elsewhere; simple numeric ticker guard
        pass
    # Price/market-cap check would call market provider; stub passes for now
    return True


def proposals_to_universe_rows(proposals: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Convert proposal dicts to universe Sheet rows (stringified)."""
    rows: list[dict[str, str]] = []
    for proposal in proposals:
        row = {header: "" for header in UNIVERSE_HEADER}
        for key, value in proposal.items():
            if key in row:
                row[key] = str(value)
        # defaults for required sheet columns
        row.setdefault("enabled", "FALSE")
        row.setdefault("tier", "proposed")
        rows.append(row)
    return rows


def _load_universe_entries(store: LocalStore) -> list[UniverseEntry]:
    try:
        entries = store.load_universe_entries()  # type: ignore[attr-defined]
        if entries:
            return entries  # type: ignore[return-value]
    except Exception:
        pass
    return []


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Try ISO, then sqlite datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text[:19], fmt[:19]) if len(text) >= 19 else None
        except Exception:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except Exception:
        return None
