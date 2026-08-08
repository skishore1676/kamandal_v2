"""Read baseline earnings evidence without creating or changing baseline tables."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from kamandal_v2.events.earnings import EarningsSnapshot
from kamandal_v2.paths import resolve_path


def latest_earnings_snapshot(sqlite_path: str | Path, symbol: str) -> EarningsSnapshot | None:
    path = resolve_path(sqlite_path)
    if not path.exists():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT symbol, fetched_date, next_earnings_date, source, confirmed, time_of_day, raw_json
            FROM earnings_snapshots
            WHERE symbol = ?
            ORDER BY fetched_date DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    finally:
        connection.close()
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
