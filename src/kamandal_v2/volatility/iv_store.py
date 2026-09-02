"""SQLite-backed IV snapshot history."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from kamandal_v2.paths import resolve_path


DAILY_IV_ABS_METRIC = "daily_iv_abs"
DAILY_IV_RANK_METRIC = "daily_iv_rank"
DAILY_IV_PERCENTILE_METRIC = "daily_iv_percentile"


@dataclass(slots=True)
class IvSnapshot:
    symbol: str
    snapshot_date: str
    iv: float
    source: str
    metric: str
    quote_count: int
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IvStore:
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
                CREATE TABLE IF NOT EXISTS iv_snapshots (
                    symbol TEXT NOT NULL,
                    snapshot_date TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    iv REAL NOT NULL,
                    source TEXT NOT NULL,
                    quote_count INTEGER NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (symbol, snapshot_date, metric)
                )
                """
            )

    def save(self, snapshot: IvSnapshot) -> None:
        import json

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO iv_snapshots
                (symbol, snapshot_date, metric, iv, source, quote_count, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.symbol.upper(),
                    snapshot.snapshot_date,
                    snapshot.metric,
                    snapshot.iv,
                    snapshot.source,
                    snapshot.quote_count,
                    json.dumps(snapshot.raw, sort_keys=True),
                ),
            )

    def history(self, symbol: str, *, metric: str = "atm_30_45_mean_iv", limit: int = 252) -> list[IvSnapshot]:
        import json

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, snapshot_date, iv, source, metric, quote_count, raw_json
                FROM iv_snapshots
                WHERE symbol = ? AND metric = ?
                ORDER BY snapshot_date DESC
                LIMIT ?
                """,
                (symbol.upper(), metric, limit),
            ).fetchall()
        result: list[IvSnapshot] = []
        for row in reversed(rows):
            result.append(
                IvSnapshot(
                    symbol=str(row["symbol"]),
                    snapshot_date=str(row["snapshot_date"]),
                    iv=float(row["iv"]),
                    source=str(row["source"]),
                    metric=str(row["metric"]),
                    quote_count=int(row["quote_count"]),
                    raw=json.loads(row["raw_json"] or "{}"),
                )
            )
        return result

    def latest(self, symbol: str, *, metric: str = "atm_30_45_mean_iv") -> IvSnapshot | None:
        items = self.history(symbol, metric=metric, limit=1)
        return items[-1] if items else None

    def latest_metric_value(self, symbol: str, metric: str) -> float | None:
        snapshot = self.latest(symbol, metric=metric)
        return snapshot.iv if snapshot is not None else None

    def metric_evidence(self, symbol: str, metric: str) -> dict[str, Any] | None:
        snapshot = self.latest(symbol, metric=metric)
        if snapshot is None:
            return None
        return {
            "value": snapshot.iv,
            "source": snapshot.source,
            "snapshot_date": snapshot.snapshot_date,
            "metric": snapshot.metric,
            "raw": dict(snapshot.raw),
        }

    def percentile(self, symbol: str, *, metric: str = "atm_30_45_mean_iv", lookback: int = 252, min_observations: int = 1) -> float | None:
        native = self.latest_metric_value(symbol, DAILY_IV_PERCENTILE_METRIC)
        if native is not None:
            return native
        return self.local_percentile(symbol, metric=metric, lookback=lookback, min_observations=min_observations)

    def local_percentile(self, symbol: str, *, metric: str = "atm_30_45_mean_iv", lookback: int = 252, min_observations: int = 1) -> float | None:
        # ThinkScript compares today's IV with the preceding TimePeriod bars.
        # Exclude the current observation and use a strict-below comparison so
        # ties and the current row do not inflate the percentile.
        items = self.history(symbol, metric=metric, limit=lookback + 1)
        if len(items) < min_observations:
            return None
        if len(items) == 1:
            return 50.0
        latest = items[-1].iv
        prior = items[:-1][-lookback:]
        counts_below = sum(1 for item in prior if item.iv < latest)
        return round((counts_below / len(prior)) * 100.0, 2)

    def rank(self, symbol: str, *, metric: str = "atm_30_45_mean_iv", lookback: int = 252, min_observations: int = 1) -> float | None:
        native = self.latest_metric_value(symbol, DAILY_IV_RANK_METRIC)
        if native is not None:
            return native
        return self.local_rank(symbol, metric=metric, lookback=lookback, min_observations=min_observations)

    def local_rank(self, symbol: str, *, metric: str = "atm_30_45_mean_iv", lookback: int = 252, min_observations: int = 1) -> float | None:
        items = self.history(symbol, metric=metric, limit=lookback)
        if len(items) < min_observations:
            return None
        values = [item.iv for item in items]
        low = min(values)
        high = max(values)
        if high == low:
            return 50.0
        return round(((values[-1] - low) / (high - low)) * 100.0, 2)


def today_iso() -> str:
    return date.today().isoformat()
