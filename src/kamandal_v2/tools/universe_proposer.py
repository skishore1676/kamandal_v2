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
from typing import Any, Callable

from kamandal_v2.domain.models import UniverseEntry
from kamandal_v2.paths import resolve_path
from kamandal_v2.schemas import UNIVERSE_HEADER
from kamandal_v2.stores.sqlite import LocalStore

MICRO_DENYLIST = {"", "USD", "USDT", "BTC", "ETH"}

DEFAULT_MAX_PROPOSALS_PER_DAY = 5
DEFAULT_LOOKBACK_DAYS = 3
DEFAULT_MIN_PRICE = 10.0
DEFAULT_MIN_AVG_DOLLAR_VOLUME = 20_000_000.0
DEFAULT_MIN_MARKET_CAP = 2_000_000_000.0


def collect_out_of_universe_symbols(
    store: LocalStore,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_MAX_PROPOSALS_PER_DAY,
    existing_symbols: set[str] | None = None,
    market_facts_loader: Callable[[str], dict[str, float | None]] | None = None,
    audit_path: str | Path = "data/audit/live/latest_plan_run.json",
) -> list[dict[str, Any]]:
    """Return up to `limit` candidate symbols from recent plan diagnostics.

    Strategy: inspect recent plan idea_diagnostics where status == out_of_universe,
    count frequency over lookback_days, exclude already-in-universe, denylist,
    and obvious micro tickers (< 3 chars, numeric). Dedup, rank by frequency.
    """
    lookback_days = max(1, int(lookback_days))
    limit = max(1, min(int(limit), DEFAULT_MAX_PROPOSALS_PER_DAY))
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)

    universe_symbols = {str(symbol).upper() for symbol in (existing_symbols or set())}
    universe_symbols.update({
        entry.symbol.upper()
        for entry in _load_universe_entries(store)
        if entry.symbol and entry.enabled is not None
    })

    counter: Counter[str] = Counter()
    discovery = [
        row for row in store.discovery_candidates()
        if str(row.get("symbol") or "").upper() not in universe_symbols
        and _parse_time(str(row.get("last_seen_at") or "")) is not None
        and _parse_time(str(row.get("last_seen_at") or "")) >= cutoff
    ]
    for row in discovery:
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in MICRO_DENYLIST:
            counter[symbol] = max(counter[symbol], int(row.get("mention_count") or 0))
    # Recent plan diagnostics are retained as compatibility input until all
    # source normalizers write discovery evidence, but never replace ledger rows.
    # store events; scan store's recent ideas and plan diagnostics
    try:
        for row in _recent_plan_diagnostics(audit_path, cutoff=cutoff):
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
            for idea in _stored_ideas(store, limit=200):
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

    discovery_by_symbol = {str(row.get("symbol") or "").upper(): row for row in discovery}
    ranked = sorted(
        counter,
        key=lambda symbol: (
            -counter[symbol],
            -len(discovery_by_symbol.get(symbol, {}).get("source_profiles") or []),
            str(discovery_by_symbol.get(symbol, {}).get("last_seen_at") or ""),
            symbol,
        ),
        reverse=False,
    )
    results: list[dict[str, Any]] = []
    today = datetime.now(UTC).date().isoformat()
    facts_loader = market_facts_loader or _yfinance_market_facts
    for symbol in ranked:
        try:
            market_facts = facts_loader(symbol)
        except Exception:
            continue
        if not micro_stock_guard(symbol, market_facts=market_facts):
            continue
        results.append(
            {
                "symbol": symbol,
                "enabled": "FALSE",
                "profile": "satellite",
                "tier": "proposed",
                "proposal_source": "durable_discovery" if symbol in discovery_by_symbol else "recent_plans",
                "proposal_reason": f"out_of_universe {counter[symbol]}x in last {lookback_days}d",
                "proposal_date": today,
                "notes": (
                    f"auto-proposed {today}: verified price={market_facts['price']:.2f}, "
                    f"avg_dollar_volume={market_facts['avg_dollar_volume']:.0f}, "
                    f"market_cap={market_facts.get('market_cap') or 'unavailable'}"
                ),
            }
        )
        if len(results) >= limit:
            break
    return results


def micro_stock_guard(
    symbol: str,
    *,
    market_facts: dict[str, float | None],
    min_price: float = DEFAULT_MIN_PRICE,
    min_avg_dollar_volume: float = DEFAULT_MIN_AVG_DOLLAR_VOLUME,
    min_market_cap: float = DEFAULT_MIN_MARKET_CAP,
) -> bool:
    """Fail closed unless sourced price/liquidity facts exclude micro/illiquid names."""
    symbol = str(symbol or "").strip().upper()
    if not symbol or symbol in MICRO_DENYLIST or len(symbol) < 2:
        return False
    price = _positive_float(market_facts.get("price"))
    avg_dollar_volume = _positive_float(market_facts.get("avg_dollar_volume"))
    market_cap = _positive_float(market_facts.get("market_cap"))
    if price is None or avg_dollar_volume is None:
        return False
    if price < min_price or avg_dollar_volume < min_avg_dollar_volume:
        return False
    return market_cap is None or market_cap >= min_market_cap


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


def _recent_plan_diagnostics(path: str | Path, *, cutoff: datetime) -> list[dict[str, Any]]:
    resolved = resolve_path(path)
    if not resolved.is_file():
        return []
    modified = datetime.fromtimestamp(resolved.stat().st_mtime, UTC)
    if modified < cutoff:
        return []
    try:
        payload = __import__("json").loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = payload.get("idea_diagnostics") or []
    return [dict(row, seen_at=modified.isoformat()) for row in rows if isinstance(row, dict)]


def _stored_ideas(store: LocalStore, *, limit: int) -> list[dict[str, Any]]:
    try:
        with store._connect() as conn:  # noqa: SLF001 - store owns this local read model.
            rows = conn.execute("SELECT payload FROM ideas ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [__import__("json").loads(str(row["payload"])) for row in rows]
    except Exception:
        return []


def _yfinance_market_facts(symbol: str) -> dict[str, float | None]:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    history = ticker.history(period="1mo", auto_adjust=False)
    if history is None or history.empty:
        raise RuntimeError(f"no market history for {symbol}")
    closes = history["Close"].dropna()
    volumes = history["Volume"].dropna()
    if closes.empty or volumes.empty:
        raise RuntimeError(f"incomplete market history for {symbol}")
    price = float(closes.iloc[-1])
    aligned = history[["Close", "Volume"]].dropna()
    avg_dollar_volume = float((aligned["Close"] * aligned["Volume"]).mean())
    try:
        market_cap = _positive_float(ticker.fast_info.get("market_cap"))
    except Exception:
        market_cap = None
    return {"price": price, "avg_dollar_volume": avg_dollar_volume, "market_cap": market_cap}


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


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
