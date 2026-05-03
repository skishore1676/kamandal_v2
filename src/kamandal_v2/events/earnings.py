"""Earnings/event-risk capture and market overlay."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from kamandal_v2.market.interfaces import MarketDataProvider
from kamandal_v2.paths import resolve_path
from kamandal_v2.planner.config_loader import load_planner_config


@dataclass(slots=True)
class EarningsSnapshot:
    symbol: str
    fetched_date: str
    next_earnings_date: str | None
    source: str
    confirmed: bool = False
    time_of_day: str = ""
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EarningsCaptureResult:
    captured: list[EarningsSnapshot]
    failed: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "captured": [snapshot.to_dict() for snapshot in self.captured],
            "failed": dict(self.failed),
            "captured_count": len(self.captured),
            "failed_count": len(self.failed),
        }


class EarningsProvider(Protocol):
    def next_earnings(self, symbol: str) -> EarningsSnapshot:
        ...


class EarningsStore:
    def __init__(self, sqlite_path: str | Path = "data/kamandal_v2.db") -> None:
        self.sqlite_path = resolve_path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS earnings_snapshots (
                    symbol TEXT NOT NULL,
                    fetched_date TEXT NOT NULL,
                    next_earnings_date TEXT,
                    source TEXT NOT NULL,
                    confirmed INTEGER NOT NULL DEFAULT 0,
                    time_of_day TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (symbol, fetched_date, source)
                )
                """
            )

    def save(self, snapshot: EarningsSnapshot) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO earnings_snapshots
                (symbol, fetched_date, next_earnings_date, source, confirmed, time_of_day, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.symbol.upper(),
                    snapshot.fetched_date,
                    snapshot.next_earnings_date,
                    snapshot.source,
                    1 if snapshot.confirmed else 0,
                    snapshot.time_of_day,
                    json.dumps(_json_tree_safe(snapshot.raw or {}), sort_keys=True, default=str, allow_nan=False),
                ),
            )

    def latest(self, symbol: str, *, source: str | None = None) -> EarningsSnapshot | None:
        params: list[Any] = [symbol.upper()]
        source_clause = ""
        if source:
            source_clause = "AND source = ?"
            params.append(source)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT symbol, fetched_date, next_earnings_date, source, confirmed, time_of_day, raw_json
                FROM earnings_snapshots
                WHERE symbol = ? {source_clause}
                ORDER BY fetched_date DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        if row is None:
            return None
        return EarningsSnapshot(
            symbol=str(row["symbol"]),
            fetched_date=str(row["fetched_date"]),
            next_earnings_date=str(row["next_earnings_date"]) if row["next_earnings_date"] else None,
            source=str(row["source"]),
            confirmed=bool(row["confirmed"]),
            time_of_day=str(row["time_of_day"] or ""),
            raw=json.loads(row["raw_json"] or "{}"),
        )


class YFinanceEarningsProvider:
    def next_earnings(self, symbol: str) -> EarningsSnapshot:
        try:
            import yfinance as yf  # type: ignore
        except ImportError as exc:
            raise RuntimeError("yfinance is required for earnings capture") from exc

        ticker = yf.Ticker(symbol.upper())
        frame = ticker.get_earnings_dates(limit=12)
        next_date: str | None = None
        confirmed = False
        raw: dict[str, Any] = {}
        if frame is not None and not frame.empty:
            rows = []
            for index, row in frame.iterrows():
                event_date = _date_from_any(row.get("Earnings Date") if "Earnings Date" in row else index)
                if event_date is None:
                    continue
                item = {str(key): _json_safe(value) for key, value in row.to_dict().items()}
                item["date"] = event_date.isoformat()
                rows.append(item)
            raw = {"rows": rows[:12]}
            today = date.today()
            future = sorted(date.fromisoformat(item["date"]) for item in rows if date.fromisoformat(item["date"]) >= today)
            if future:
                next_date = future[0].isoformat()
                confirmed = True
        return EarningsSnapshot(
            symbol=symbol.upper(),
            fetched_date=date.today().isoformat(),
            next_earnings_date=next_date,
            source="yfinance",
            confirmed=confirmed,
            raw=raw,
        )


class FixtureEarningsProvider:
    def __init__(self, dates: dict[str, str | None] | None = None) -> None:
        self.dates = {key.upper(): value for key, value in (dates or {}).items()}

    def next_earnings(self, symbol: str) -> EarningsSnapshot:
        return EarningsSnapshot(
            symbol=symbol.upper(),
            fetched_date=date.today().isoformat(),
            next_earnings_date=self.dates.get(symbol.upper()),
            source="fixture",
            confirmed=self.dates.get(symbol.upper()) is not None,
            raw={"source": "fixture"},
        )


class EarningsOverlayMarket:
    def __init__(self, inner: MarketDataProvider, store: EarningsStore, *, source: str | None = None) -> None:
        self.inner = inner
        self.store = store
        self.source = source

    def account_state(self):
        return self.inner.account_state()

    def chain_snapshot(self, underlying: str):
        return self.inner.chain_snapshot(underlying)

    def iv_percentile(self, underlying: str) -> float | None:
        return self.inner.iv_percentile(underlying)

    def iv_rank(self, underlying: str) -> float | None:
        return self.inner.iv_rank(underlying)

    def iv_abs(self, underlying: str) -> float | None:
        return self.inner.iv_abs(underlying)

    def event_status(self, underlying: str) -> str:
        status = earnings_event_status(self.store.latest(underlying, source=self.source))
        if status != "unknown":
            return status
        return self.inner.event_status(underlying)


def capture_earnings_snapshots(
    config: dict[str, Any],
    *,
    symbols: list[str] | None = None,
    config_source: str = "sheet",
    provider: str = "yfinance",
    store: EarningsStore | None = None,
) -> EarningsCaptureResult:
    store = store or EarningsStore()
    selected = [symbol.upper() for symbol in (symbols or _universe_symbols(config, source=config_source))]
    earnings_provider = _provider(provider)
    captured: list[EarningsSnapshot] = []
    failed: dict[str, str] = {}
    for symbol in selected:
        try:
            snapshot = earnings_provider.next_earnings(symbol)
            store.save(snapshot)
            captured.append(snapshot)
        except Exception as exc:  # noqa: BLE001
            failed[symbol] = str(exc)
    return EarningsCaptureResult(captured=captured, failed=failed)


def earnings_event_status(snapshot: EarningsSnapshot | None, *, today: date | None = None) -> str:
    if snapshot is None or not snapshot.next_earnings_date:
        return "unknown"
    today = today or date.today()
    event_date = date.fromisoformat(snapshot.next_earnings_date)
    days = (event_date - today).days
    if days == 0:
        return "earnings_today"
    if 0 < days <= 7:
        return f"earnings_soon:{days}d"
    if -1 <= days < 0:
        return "post_earnings"
    return "clear"


def _provider(name: str) -> EarningsProvider:
    if name == "fixture":
        return FixtureEarningsProvider()
    if name == "yfinance":
        return YFinanceEarningsProvider()
    raise ValueError(f"Unsupported earnings provider: {name}")


def _universe_symbols(config: dict[str, Any], *, source: str) -> list[str]:
    universe, _playbooks = load_planner_config(config, source=source)
    return [entry.symbol for entry in universe if entry.enabled]


def _date_from_any(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    text = str(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_tree_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_tree_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_tree_safe(item) for item in value]
    return _json_safe(value)
