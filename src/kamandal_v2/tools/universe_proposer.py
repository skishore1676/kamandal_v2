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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
from typing import Any, Callable

from kamandal_v2.domain.models import UniverseEntry
from kamandal_v2.paths import resolve_path
from kamandal_v2.schemas import UNIVERSE_HEADER
from kamandal_v2.stores.sqlite import LocalStore

MICRO_DENYLIST = {"", "USD", "USDT", "BTC", "ETH"}

DEFAULT_MAX_PROPOSALS_PER_DAY = 5
DEFAULT_BOOTSTRAP_COMPLETED_SESSIONS = 5
DEFAULT_MIN_PRICE = 10.0
DEFAULT_MIN_AVG_DOLLAR_VOLUME = 20_000_000.0
DEFAULT_MIN_MARKET_CAP = 2_000_000_000.0
LARGE_STOCK_MARKET_CAP = 10_000_000_000.0
# Explicit proposal policy, not a change to existing operator-owned rows.
PROPOSAL_SETTINGS = {
    "tradable_iv_percentile_min": "0",
    "tradable_iv_percentile_max": "100",
    "max_bpr_pct": "25",
    "max_positions": "1",
    "earnings_sensitive": "TRUE",
    "event_avoid_days_before": "7",
    "event_avoid_days_after": "1",
    "allowed_playbooks": "put_spread, call_spread, put_diagonal, call_diagonal, short_strangle",
}


def complete_universe_proposal(
    proposal: dict[str, Any], *, market_cap: float | None = None,
) -> dict[str, Any]:
    """Complete a disabled machine proposal without overwriting operator settings.

    Unknown capitalization uses the narrower mid_stocks routing template, not
    an assertion of market-cap classification. Enabled/held/rejected rows are
    outside this repair contract. The caller owns publication and readback.
    """
    row = dict(proposal)
    if str(row.get("enabled", "")).strip().lower() not in {"false", "0"}:
        return row
    if str(row.get("tier", "")).strip().lower() != "proposed":
        return row
    if row.get("proposal_source") not in {"durable_discovery", "recent_plans"}:
        return row
    if str(row.get("profile") or "").strip() in {"", "satellite"}:
        cap = _positive_float(market_cap)
        row["profile"] = "large_stocks" if cap is not None and cap >= LARGE_STOCK_MARKET_CAP else "mid_stocks"
        basis = "market cap unavailable; mid_stocks routing fallback" if cap is None else f"market cap={cap:.0f}; 10B profile boundary"
        note = f"proposal policy v1: {basis}; review settings, then enabled=TRUE to approve; retain FALSE to defer/reject"
        row["notes"] = "; ".join(filter(None, [str(row.get("notes") or ""), note]))
    for key, value in PROPOSAL_SETTINGS.items():
        if row.get(key) is None or str(row[key]).strip() == "":
            row[key] = value
    return row


@dataclass(frozen=True, slots=True)
class WeeklyUniverseReviewResult:
    review_id: str
    cutoff: str
    proposal_count: int
    published_count: int
    committed: bool


def run_weekly_universe_review(
    store: LocalStore,
    *,
    universe_rows: list[dict[str, Any]],
    publish: Callable[[list[dict[str, str]]], int] | None,
    cutoff: datetime,
    market_facts_loader: Callable[[str], dict[str, float | None]] | None = None,
    limit: int = DEFAULT_MAX_PROPOSALS_PER_DAY,
) -> WeeklyUniverseReviewResult:
    """Aggregate one committed discovery window and advance it exactly once.

    A zero-row review is still a completed weekly boundary.  Any publication
    exception or an inexact append count leaves the boundary untouched so the
    same evidence remains eligible for a retry.
    """
    cutoff = _as_utc(cutoff)
    existing_symbols = {str(row.get("symbol") or "").upper() for row in universe_rows}
    proposals = collect_out_of_universe_symbols(
        store,
        limit=limit,
        existing_symbols=existing_symbols,
        market_facts_loader=market_facts_loader,
        cutoff=cutoff,
    )
    rows = proposals_to_universe_rows(proposals)
    published = 0
    if publish is not None and rows:
        published = int(publish(rows))
        if published != len(rows):
            raise RuntimeError(f"universe proposal publication was inexact: expected={len(rows)} actual={published}")
    review_id = "universe-review:" + hashlib.sha256(
        (cutoff.isoformat() + "|" + "|".join(sorted(str(row.get("symbol") or "") for row in rows))).encode("utf-8")
    ).hexdigest()[:24]
    store.record_universe_review_commit(
        review_id=review_id,
        committed_at=cutoff.isoformat(),
        payload={"proposal_count": len(rows), "published_count": published, "symbols": [row["symbol"] for row in rows]},
    )
    return WeeklyUniverseReviewResult(review_id, cutoff.isoformat(), len(rows), published, True)


def collect_out_of_universe_symbols(
    store: LocalStore,
    *,
    lookback_days: int | None = None,
    limit: int = DEFAULT_MAX_PROPOSALS_PER_DAY,
    existing_symbols: set[str] | None = None,
    market_facts_loader: Callable[[str], dict[str, float | None]] | None = None,
    audit_path: str | Path = "data/audit/live/latest_plan_run.json",
    cutoff: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return a deterministic, bounded weekly discovery queue.

    The interval starts immediately after the last committed weekly review.  On
    bootstrap it covers the five most recently *completed* weekday sessions.
    ``lookback_days`` is retained only as an explicit compatibility override for
    historical callers; production ranking never infers a moving three-day
    window.  ``cutoff`` makes fixture replays independent of wall-clock time.
    """
    limit = max(1, min(int(limit), DEFAULT_MAX_PROPOSALS_PER_DAY))
    cutoff = _as_utc(cutoff or datetime.now(UTC))
    window_start = _discovery_window_start(store, cutoff=cutoff, compatibility_lookback_days=lookback_days)

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
        and _parse_time(str(row.get("last_seen_at") or "")) is not None
        and _parse_time(str(row.get("last_seen_at") or "")) > window_start
        and _parse_time(str(row.get("last_seen_at") or "")) <= cutoff
    ]
    for row in discovery:
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in MICRO_DENYLIST:
            counter[symbol] = max(counter[symbol], int(row.get("mention_count") or 0))
    # Recent plan diagnostics are retained as compatibility input until all
    # source normalizers write discovery evidence, but never replace ledger rows.
    # store events; scan store's recent ideas and plan diagnostics
    try:
        for row in _recent_plan_diagnostics(audit_path, cutoff=window_start):
            idea_underlying = str(row.get("underlying") or "").upper().strip()
            status = str(row.get("status") or "")
            seen_at = _parse_time(row.get("seen_at") or row.get("created_at") or "")
            if seen_at is not None and (seen_at <= window_start or seen_at > cutoff):
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
                if seen_at is not None and (seen_at <= window_start or seen_at > cutoff):
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
            -_timestamp_sort_value(discovery_by_symbol.get(symbol, {}).get("last_seen_at")),
            symbol,
        ),
        reverse=False,
    )
    results: list[dict[str, Any]] = []
    today = cutoff.date().isoformat()
    facts_loader = market_facts_loader or _yfinance_market_facts
    for symbol in ranked:
        try:
            market_facts = facts_loader(symbol)
        except Exception:
            continue
        if not micro_stock_guard(symbol, market_facts=market_facts):
            continue
        results.append(complete_universe_proposal(
            {
                "symbol": symbol,
                "enabled": "FALSE",
                "tier": "proposed",
                "proposal_source": "durable_discovery" if symbol in discovery_by_symbol else "recent_plans",
                "proposal_reason": (
                    f"out_of_universe {counter[symbol]}x from {window_start.date().isoformat()} "
                    f"through {cutoff.date().isoformat()}"
                ),
                "proposal_date": today,
                "notes": (
                    f"auto-proposed {today}: verified price={market_facts['price']:.2f}, "
                    f"avg_dollar_volume={market_facts['avg_dollar_volume']:.0f}, "
                    f"market_cap={market_facts.get('market_cap') or 'unavailable'}"
                ),
            }, market_cap=market_facts.get("market_cap")
        ))
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
        # Proposals may never arrive armed, even if a caller supplies TRUE.
        row["enabled"] = "FALSE"
        row["tier"] = "proposed"
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


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _discovery_window_start(
    store: LocalStore,
    *,
    cutoff: datetime,
    compatibility_lookback_days: int | None,
) -> datetime:
    if compatibility_lookback_days is not None:
        return cutoff - timedelta(days=max(1, int(compatibility_lookback_days)))
    committed_at = store.latest_universe_review_commit_at()
    if committed_at:
        parsed = _parse_time(committed_at)
        if parsed is None:
            raise ValueError("latest universe review commit has an invalid committed_at timestamp")
        return parsed
    sessions: list[datetime] = []
    cursor = cutoff.date() - timedelta(days=1)
    while len(sessions) < DEFAULT_BOOTSTRAP_COMPLETED_SESSIONS:
        if cursor.weekday() < 5:
            sessions.append(datetime.combine(cursor, datetime.min.time(), tzinfo=UTC))
        cursor -= timedelta(days=1)
    return sessions[-1]


def _timestamp_sort_value(value: Any) -> float:
    parsed = _parse_time(str(value or ""))
    return parsed.timestamp() if parsed is not None else float("-inf")
