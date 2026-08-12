"""Isolated prospective quote telemetry for short-dated vertical spreads.

This module is intentionally read-only with respect to the broker. It captures
live Public option-chain snapshots, opens synthetic one-lot observation lanes,
and marks those exact contracts over time. It never calls preflight, builds an
order ticket, writes Google Sheets, or submits an order.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from kamandal_v2.config import load_control
from kamandal_v2.domain.models import ChainSnapshot, OptionQuote
from kamandal_v2.market.public import PublicAdapter
from kamandal_v2.paths import resolve_path


CONTRACT_MULTIPLIER = 100.0
DEFAULT_DTES = (1, 3, 7, 14)
DEFAULT_DB = "data/research/vertical_telemetry_v1.db"


@dataclass(frozen=True, slots=True)
class IdeaDirection:
    symbol: str
    direction: str
    source_event_id: str = ""


@dataclass(frozen=True, slots=True)
class SelectedSpread:
    symbol: str
    direction: str
    requested_dte: int
    actual_dte: int
    expiration: str
    option_type: str
    short_quote: OptionQuote
    long_quote: OptionQuote
    mid_credit: float
    natural_credit: float
    width: float

    @property
    def entry_bpr(self) -> float:
        return max(
            (self.width - self.natural_credit) * CONTRACT_MULTIPLIER,
            0.0,
        )


@dataclass(frozen=True, slots=True)
class SpreadMark:
    lane_id: str
    captured_at: str
    underlying_price: float
    close_mid_debit: float
    close_natural_debit: float
    pnl_mid_dollars: float
    pnl_natural_dollars: float
    capture_mid_pct: float
    capture_natural_pct: float
    peak_natural_capture_pct: float
    short_delta_abs: float
    quote_complete: bool
    missing_reason: str = ""


def utc_now() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def safe_telemetry_config(base_config: dict[str, Any]) -> dict[str, Any]:
    """Return a forced shadow/read-only configuration copy."""

    config = copy.deepcopy(base_config)
    runtime = dict(config.get("runtime") or {})
    runtime.update(
        {"mode": "shadow", "trading_enabled": False, "halt": False}
    )
    config["runtime"] = runtime

    execution = dict(config.get("execution") or {})
    execution["submit_to_broker"] = False
    config["execution"] = execution

    live = dict(config.get("live") or {})
    live["auto_submit_entries"] = False
    live["auto_submit_exits"] = False
    live["entry_approval_mode"] = "disabled"
    live["exit_approval_mode"] = "disabled"
    config["live"] = live
    return config


def assert_telemetry_safe(config: dict[str, Any]) -> None:
    runtime = config.get("runtime") or {}
    execution = config.get("execution") or {}
    live = config.get("live") or {}
    failures = []
    if str(runtime.get("mode") or "").lower() != "shadow":
        failures.append("runtime.mode must be shadow")
    if bool(runtime.get("trading_enabled")):
        failures.append("runtime.trading_enabled must be false")
    if bool(execution.get("submit_to_broker")):
        failures.append("execution.submit_to_broker must be false")
    if bool(live.get("auto_submit_entries")) or bool(
        live.get("auto_submit_exits")
    ):
        failures.append("live auto-submit flags must be false")
    if failures:
        raise RuntimeError(
            "unsafe telemetry configuration: " + "; ".join(failures)
        )


def parse_idea(raw: str) -> IdeaDirection:
    """Parse SYMBOL:DIRECTION[:SOURCE_EVENT_ID]."""

    parts = [part.strip() for part in raw.split(":")]
    if len(parts) not in {2, 3}:
        raise ValueError("idea must be SYMBOL:DIRECTION[:SOURCE_EVENT_ID]")
    symbol = parts[0].upper()
    direction = parts[1].upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    return IdeaDirection(
        symbol=symbol,
        direction=direction,
        source_event_id=parts[2] if len(parts) == 3 else "",
    )


def requested_expiration_dates(
    dtes: Sequence[int],
    *,
    as_of: date | None = None,
    tolerance_days: int = 3,
) -> list[str]:
    """Return bounded calendar dates Public can probe for listed expirations."""

    origin = as_of or date.today()
    dates: set[date] = set()
    for dte in dtes:
        if dte < 0:
            raise ValueError("DTE cannot be negative")
        for delta in range(-tolerance_days, tolerance_days + 1):
            offset = dte + delta
            if offset < 0:
                continue
            candidate = origin + timedelta(days=offset)
            if candidate.weekday() < 5:
                dates.add(candidate)
    return [item.isoformat() for item in sorted(dates)]


def _captured_date(snapshot: ChainSnapshot) -> date:
    raw = str(snapshot.captured_at or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return date.today()


def _valid_quote(quote: OptionQuote) -> bool:
    return (
        quote.bid >= 0
        and quote.ask > 0
        and quote.ask >= quote.bid
        and quote.strike > 0
        and abs(float(quote.delta)) > 0
    )


def select_vertical(
    snapshot: ChainSnapshot,
    *,
    direction: str,
    requested_dte: int,
    short_delta_target: float = 0.30,
    long_delta_target: float = 0.15,
    max_dte_error: int = 4,
) -> SelectedSpread | None:
    """Select a same-expiration vertical using actual snapshot quotes."""

    resolved_direction = direction.upper()
    if resolved_direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    option_type = "put" if resolved_direction == "LONG" else "call"
    captured_date = _captured_date(snapshot)
    by_expiration: dict[str, list[OptionQuote]] = {}
    for quote in snapshot.quotes:
        if quote.option_type.lower() != option_type or not _valid_quote(quote):
            continue
        if option_type == "put" and quote.strike >= snapshot.underlying_price:
            continue
        if option_type == "call" and quote.strike <= snapshot.underlying_price:
            continue
        by_expiration.setdefault(quote.expiration, []).append(quote)

    expiration_candidates = []
    for expiration, quotes in by_expiration.items():
        try:
            actual_dte = (
                date.fromisoformat(expiration) - captured_date
            ).days
        except ValueError:
            continue
        if actual_dte < 0 or abs(actual_dte - requested_dte) > max_dte_error:
            continue
        expiration_candidates.append(
            (
                abs(actual_dte - requested_dte),
                actual_dte,
                expiration,
                quotes,
            )
        )
    expiration_candidates.sort(key=lambda row: (row[0], row[1]))

    best: SelectedSpread | None = None
    best_score = float("inf")
    for _dte_error, actual_dte, expiration, quotes in expiration_candidates:
        for short_quote in quotes:
            for long_quote in quotes:
                ordered = (
                    long_quote.strike
                    < short_quote.strike
                    < snapshot.underlying_price
                    if option_type == "put"
                    else snapshot.underlying_price
                    < short_quote.strike
                    < long_quote.strike
                )
                if not ordered:
                    continue
                mid_credit = short_quote.mid - long_quote.mid
                natural_credit = short_quote.bid - long_quote.ask
                if mid_credit <= 0 or natural_credit <= 0:
                    continue
                score = (
                    abs(abs(short_quote.delta) - short_delta_target)
                    + abs(abs(long_quote.delta) - long_delta_target)
                    + 0.001 * abs(actual_dte - requested_dte)
                    + 1e-7
                    * abs(short_quote.strike - long_quote.strike)
                )
                if score < best_score:
                    best_score = score
                    best = SelectedSpread(
                        symbol=snapshot.underlying.upper(),
                        direction=resolved_direction,
                        requested_dte=requested_dte,
                        actual_dte=actual_dte,
                        expiration=expiration,
                        option_type=option_type,
                        short_quote=short_quote,
                        long_quote=long_quote,
                        mid_credit=float(mid_credit),
                        natural_credit=float(natural_credit),
                        width=float(
                            abs(short_quote.strike - long_quote.strike)
                        ),
                    )
        if best is not None:
            break
    return best


def lane_id_for(
    idea: IdeaDirection,
    spread: SelectedSpread,
    captured_at: str,
) -> str:
    payload = "|".join(
        [
            idea.source_event_id or captured_at,
            spread.symbol,
            spread.direction,
            str(spread.requested_dte),
            spread.expiration,
            spread.option_type,
            f"{spread.short_quote.strike:.4f}",
            f"{spread.long_quote.strike:.4f}",
        ]
    )
    return "vtel_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


class TelemetryStore:
    def __init__(self, sqlite_path: str | Path = DEFAULT_DB) -> None:
        self.sqlite_path = resolve_path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS vertical_telemetry_lanes (
                    lane_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    source_event_id TEXT,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    requested_dte INTEGER NOT NULL,
                    actual_dte INTEGER NOT NULL,
                    expiration TEXT NOT NULL,
                    option_type TEXT NOT NULL,
                    short_strike REAL NOT NULL,
                    long_strike REAL NOT NULL,
                    entry_underlying REAL NOT NULL,
                    entry_snapshot_id TEXT NOT NULL,
                    entry_captured_at TEXT NOT NULL,
                    entry_mid_credit REAL NOT NULL,
                    entry_natural_credit REAL NOT NULL,
                    spread_width REAL NOT NULL,
                    estimated_bpr REAL NOT NULL,
                    entry_short_delta REAL,
                    entry_long_delta REAL,
                    entry_short_oi INTEGER,
                    entry_long_oi INTEGER,
                    status TEXT NOT NULL DEFAULT 'open',
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vertical_telemetry_marks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lane_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    snapshot_source TEXT NOT NULL,
                    underlying_price REAL,
                    short_bid REAL,
                    short_ask REAL,
                    long_bid REAL,
                    long_ask REAL,
                    close_mid_debit REAL,
                    close_natural_debit REAL,
                    pnl_mid_dollars REAL,
                    pnl_natural_dollars REAL,
                    capture_mid_pct REAL,
                    capture_natural_pct REAL,
                    peak_natural_capture_pct REAL,
                    short_delta_abs REAL,
                    quote_complete INTEGER NOT NULL,
                    missing_reason TEXT,
                    payload_json TEXT NOT NULL,
                    UNIQUE(lane_id, captured_at),
                    FOREIGN KEY(lane_id)
                        REFERENCES vertical_telemetry_lanes(lane_id)
                );
                CREATE INDEX IF NOT EXISTS idx_vertical_telemetry_open
                    ON vertical_telemetry_lanes(status, symbol);
                CREATE INDEX IF NOT EXISTS idx_vertical_telemetry_marks_lane
                    ON vertical_telemetry_marks(lane_id, captured_at);
                """
            )

    def save_lane(
        self,
        lane_id: str,
        idea: IdeaDirection,
        snapshot: ChainSnapshot,
        spread: SelectedSpread,
    ) -> bool:
        payload = {
            "short_quote": spread.short_quote.to_dict(),
            "long_quote": spread.long_quote.to_dict(),
            "snapshot_source": snapshot.source,
        }
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO vertical_telemetry_lanes (
                    lane_id, created_at, source_event_id, symbol, direction,
                    requested_dte, actual_dte, expiration, option_type,
                    short_strike, long_strike, entry_underlying,
                    entry_snapshot_id, entry_captured_at, entry_mid_credit,
                    entry_natural_credit, spread_width, estimated_bpr,
                    entry_short_delta, entry_long_delta, entry_short_oi,
                    entry_long_oi, status, metadata_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, 'open', ?
                )
                """,
                (
                    lane_id,
                    utc_now(),
                    idea.source_event_id,
                    spread.symbol,
                    spread.direction,
                    spread.requested_dte,
                    spread.actual_dte,
                    spread.expiration,
                    spread.option_type,
                    spread.short_quote.strike,
                    spread.long_quote.strike,
                    snapshot.underlying_price,
                    snapshot.chain_snapshot_id,
                    snapshot.captured_at,
                    spread.mid_credit,
                    spread.natural_credit,
                    spread.width,
                    spread.entry_bpr,
                    spread.short_quote.delta,
                    spread.long_quote.delta,
                    spread.short_quote.open_interest,
                    spread.long_quote.open_interest,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            return cursor.rowcount > 0

    def open_lanes(
        self,
        symbols: Sequence[str] | None = None,
    ) -> list[sqlite3.Row]:
        query = (
            "SELECT * FROM vertical_telemetry_lanes "
            "WHERE status = 'open'"
        )
        params: list[Any] = []
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            query += f" AND symbol IN ({placeholders})"
            params.extend(symbol.upper() for symbol in symbols)
        query += " ORDER BY created_at, lane_id"
        with self._connect() as connection:
            return connection.execute(query, params).fetchall()

    def latest_peak_capture(self, lane_id: str) -> float:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(peak_natural_capture_pct) AS peak
                FROM vertical_telemetry_marks
                WHERE lane_id = ?
                """,
                (lane_id,),
            ).fetchone()
        return float(row["peak"] or 0.0)

    def save_mark(
        self,
        mark: SpreadMark,
        snapshot: ChainSnapshot,
        payload: dict[str, Any],
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO vertical_telemetry_marks (
                    lane_id, captured_at, snapshot_id, snapshot_source,
                    underlying_price, short_bid, short_ask, long_bid, long_ask,
                    close_mid_debit, close_natural_debit, pnl_mid_dollars,
                    pnl_natural_dollars, capture_mid_pct, capture_natural_pct,
                    peak_natural_capture_pct, short_delta_abs, quote_complete,
                    missing_reason, payload_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    mark.lane_id,
                    mark.captured_at,
                    snapshot.chain_snapshot_id,
                    snapshot.source,
                    mark.underlying_price,
                    payload.get("short_bid"),
                    payload.get("short_ask"),
                    payload.get("long_bid"),
                    payload.get("long_ask"),
                    mark.close_mid_debit,
                    mark.close_natural_debit,
                    mark.pnl_mid_dollars,
                    mark.pnl_natural_dollars,
                    mark.capture_mid_pct,
                    mark.capture_natural_pct,
                    mark.peak_natural_capture_pct,
                    mark.short_delta_abs,
                    1 if mark.quote_complete else 0,
                    mark.missing_reason,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            return cursor.rowcount > 0

    def complete_prior_session_lanes(self, current_date: date) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE vertical_telemetry_lanes
                SET status = 'complete'
                WHERE status = 'open'
                  AND substr(entry_captured_at, 1, 10) < ?
                """,
                (current_date.isoformat(),),
            )
            return cursor.rowcount

    def complete_lanes_with_expired_horizon(
        self,
        max_marks: int = 25,
    ) -> int:
        """Complete after the entry snapshot plus 24 future marks."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE vertical_telemetry_lanes
                SET status = 'complete'
                WHERE status = 'open'
                  AND lane_id IN (
                    SELECT lane_id
                    FROM vertical_telemetry_marks
                    WHERE quote_complete = 1
                    GROUP BY lane_id
                    HAVING COUNT(*) >= ?
                  )
                """,
                (max_marks,),
            )
            return cursor.rowcount

    def report(self) -> dict[str, Any]:
        with self._connect() as connection:
            lanes = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM vertical_telemetry_lanes
                    ORDER BY created_at, lane_id
                    """
                ).fetchall()
            ]
            marks = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM vertical_telemetry_marks
                    ORDER BY captured_at, id
                    """
                ).fetchall()
            ]
        latest_by_lane: dict[str, dict[str, Any]] = {}
        for mark in marks:
            latest_by_lane[str(mark["lane_id"])] = mark
        complete_marks = [mark for mark in marks if mark["quote_complete"]]
        return {
            "database": str(self.sqlite_path),
            "lanes": len(lanes),
            "open_lanes": sum(
                1 for lane in lanes if lane["status"] == "open"
            ),
            "marks": len(marks),
            "complete_marks": len(complete_marks),
            "latest": [
                {
                    "lane_id": lane["lane_id"],
                    "symbol": lane["symbol"],
                    "direction": lane["direction"],
                    "requested_dte": lane["requested_dte"],
                    "expiration": lane["expiration"],
                    "status": lane["status"],
                    "latest_mark": latest_by_lane.get(
                        str(lane["lane_id"])
                    ),
                }
                for lane in lanes
            ],
        }


def _quote_map(
    snapshot: ChainSnapshot,
) -> dict[tuple[str, str, float], OptionQuote]:
    return {
        (
            quote.expiration,
            quote.option_type.lower(),
            float(quote.strike),
        ): quote
        for quote in snapshot.quotes
    }


def mark_lane(
    store: TelemetryStore,
    lane: sqlite3.Row,
    snapshot: ChainSnapshot,
) -> SpreadMark:
    quotes = _quote_map(snapshot)
    short_key = (
        str(lane["expiration"]),
        str(lane["option_type"]).lower(),
        float(lane["short_strike"]),
    )
    long_key = (
        str(lane["expiration"]),
        str(lane["option_type"]).lower(),
        float(lane["long_strike"]),
    )
    short_quote = quotes.get(short_key)
    long_quote = quotes.get(long_key)
    if short_quote is None or long_quote is None:
        missing = []
        if short_quote is None:
            missing.append("short_quote")
        if long_quote is None:
            missing.append("long_quote")
        return SpreadMark(
            lane_id=str(lane["lane_id"]),
            captured_at=snapshot.captured_at,
            underlying_price=float(snapshot.underlying_price),
            close_mid_debit=0.0,
            close_natural_debit=0.0,
            pnl_mid_dollars=0.0,
            pnl_natural_dollars=0.0,
            capture_mid_pct=0.0,
            capture_natural_pct=0.0,
            peak_natural_capture_pct=store.latest_peak_capture(
                str(lane["lane_id"])
            ),
            short_delta_abs=0.0,
            quote_complete=False,
            missing_reason=",".join(missing),
        )

    close_mid = max(short_quote.mid - long_quote.mid, 0.0)
    close_natural = max(short_quote.ask - long_quote.bid, 0.0)
    entry_mid = float(lane["entry_mid_credit"])
    entry_natural = float(lane["entry_natural_credit"])
    pnl_mid = (entry_mid - close_mid) * CONTRACT_MULTIPLIER
    pnl_natural = (entry_natural - close_natural) * CONTRACT_MULTIPLIER
    capture_mid = (
        ((entry_mid - close_mid) / entry_mid) * 100.0
        if entry_mid > 0
        else 0.0
    )
    capture_natural = (
        ((entry_natural - close_natural) / entry_natural) * 100.0
        if entry_natural > 0
        else 0.0
    )
    peak = max(
        store.latest_peak_capture(str(lane["lane_id"])),
        capture_natural,
    )
    return SpreadMark(
        lane_id=str(lane["lane_id"]),
        captured_at=snapshot.captured_at,
        underlying_price=float(snapshot.underlying_price),
        close_mid_debit=float(close_mid),
        close_natural_debit=float(close_natural),
        pnl_mid_dollars=float(pnl_mid),
        pnl_natural_dollars=float(pnl_natural),
        capture_mid_pct=float(capture_mid),
        capture_natural_pct=float(capture_natural),
        peak_natural_capture_pct=float(peak),
        short_delta_abs=abs(float(short_quote.delta)),
        quote_complete=True,
    )


def mark_payload(
    lane: sqlite3.Row,
    snapshot: ChainSnapshot,
) -> dict[str, Any]:
    quotes = _quote_map(snapshot)
    short = quotes.get(
        (
            str(lane["expiration"]),
            str(lane["option_type"]).lower(),
            float(lane["short_strike"]),
        )
    )
    long = quotes.get(
        (
            str(lane["expiration"]),
            str(lane["option_type"]).lower(),
            float(lane["long_strike"]),
        )
    )
    return {
        "lane_id": str(lane["lane_id"]),
        "captured_at": snapshot.captured_at,
        "short_bid": short.bid if short else None,
        "short_ask": short.ask if short else None,
        "long_bid": long.bid if long else None,
        "long_ask": long.ask if long else None,
        "short_quote": short.to_dict() if short else None,
        "long_quote": long.to_dict() if long else None,
    }


def _public_adapter(
    config: dict[str, Any],
    expiration_dates: Sequence[str],
) -> PublicAdapter:
    assert_telemetry_safe(config)
    return PublicAdapter(config, expiration_dates=expiration_dates)


def open_observation_lanes(
    *,
    config: dict[str, Any],
    store: TelemetryStore,
    ideas: Sequence[IdeaDirection],
    dtes: Sequence[int],
    short_delta: float,
    long_delta: float,
) -> dict[str, Any]:
    assert_telemetry_safe(config)
    results = []
    expiration_dates = requested_expiration_dates(dtes)
    for idea in ideas:
        snapshot = _public_adapter(
            config,
            expiration_dates,
        ).chain_snapshot(idea.symbol)
        for requested_dte in dtes:
            spread = select_vertical(
                snapshot,
                direction=idea.direction,
                requested_dte=requested_dte,
                short_delta_target=short_delta,
                long_delta_target=long_delta,
            )
            if spread is None:
                results.append(
                    {
                        "symbol": idea.symbol,
                        "direction": idea.direction,
                        "requested_dte": requested_dte,
                        "status": "rejected",
                        "reason": "no_positive_natural_credit_delta_pair",
                    }
                )
                continue
            lane_id = lane_id_for(idea, spread, snapshot.captured_at)
            inserted = store.save_lane(lane_id, idea, snapshot, spread)
            lane_rows = [
                row
                for row in store.open_lanes([idea.symbol])
                if row["lane_id"] == lane_id
            ]
            if lane_rows:
                mark = mark_lane(store, lane_rows[0], snapshot)
                store.save_mark(
                    mark,
                    snapshot,
                    mark_payload(lane_rows[0], snapshot),
                )
            results.append(
                {
                    "lane_id": lane_id,
                    "symbol": idea.symbol,
                    "direction": idea.direction,
                    "requested_dte": requested_dte,
                    "actual_dte": spread.actual_dte,
                    "expiration": spread.expiration,
                    "short_strike": spread.short_quote.strike,
                    "long_strike": spread.long_quote.strike,
                    "mid_credit": spread.mid_credit,
                    "natural_credit": spread.natural_credit,
                    "estimated_bpr": spread.entry_bpr,
                    "status": "opened" if inserted else "already_exists",
                }
            )
    return {
        "mode": "telemetry_only",
        "orders_submitted": 0,
        "results": results,
    }


def mark_open_observation_lanes(
    *,
    config: dict[str, Any],
    store: TelemetryStore,
    symbols: Sequence[str] | None = None,
    max_marks: int = 25,
) -> dict[str, Any]:
    assert_telemetry_safe(config)
    completed_prior_session = store.complete_prior_session_lanes(date.today())
    rows = store.open_lanes(symbols)
    by_symbol: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_symbol.setdefault(str(row["symbol"]), []).append(row)
    results = []
    for symbol, lanes in by_symbol.items():
        expirations = sorted({str(row["expiration"]) for row in lanes})
        snapshot = _public_adapter(config, expirations).chain_snapshot(symbol)
        for lane in lanes:
            mark = mark_lane(store, lane, snapshot)
            inserted = store.save_mark(
                mark,
                snapshot,
                mark_payload(lane, snapshot),
            )
            results.append({**asdict(mark), "inserted": inserted})
    completed = store.complete_lanes_with_expired_horizon(
        max_marks=max_marks
    )
    return {
        "mode": "telemetry_only",
        "orders_submitted": 0,
        "marks": results,
        "lanes_completed": completed,
        "prior_session_lanes_completed": completed_prior_session,
    }


def _csv_ints(raw: str) -> list[int]:
    values = [
        int(item.strip()) for item in raw.split(",") if item.strip()
    ]
    if not values:
        raise ValueError("at least one DTE is required")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kamandal-vertical-telemetry"
    )
    parser.add_argument("--config", default="config/control.yaml")
    parser.add_argument("--db", default=DEFAULT_DB)
    subparsers = parser.add_subparsers(dest="command", required=True)

    open_parser = subparsers.add_parser(
        "open",
        help="Capture chains and open observation-only lanes",
    )
    open_parser.add_argument(
        "--idea",
        action="append",
        required=True,
        help="SYMBOL:LONG|SHORT[:SOURCE_EVENT_ID]",
    )
    open_parser.add_argument("--dtes", default="1,3,7,14")
    open_parser.add_argument("--short-delta", type=float, default=0.30)
    open_parser.add_argument("--long-delta", type=float, default=0.15)

    mark_parser = subparsers.add_parser(
        "mark",
        help="Mark exact contracts in all open observation lanes",
    )
    mark_parser.add_argument("--symbols", nargs="*", default=None)
    mark_parser.add_argument("--max-marks", type=int, default=25)

    subparsers.add_parser(
        "report",
        help="Print telemetry coverage and latest marks",
    )

    args = parser.parse_args()
    config = safe_telemetry_config(load_control(args.config))
    assert_telemetry_safe(config)
    store = TelemetryStore(args.db)

    if args.command == "open":
        result = open_observation_lanes(
            config=config,
            store=store,
            ideas=[parse_idea(item) for item in args.idea],
            dtes=_csv_ints(args.dtes),
            short_delta=args.short_delta,
            long_delta=args.long_delta,
        )
    elif args.command == "mark":
        result = mark_open_observation_lanes(
            config=config,
            store=store,
            symbols=args.symbols,
            max_marks=args.max_marks,
        )
    else:
        result = store.report()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
