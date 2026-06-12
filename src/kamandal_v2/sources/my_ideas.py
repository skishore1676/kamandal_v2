"""Import operator ideas from the my_ideas Google Sheet tab.

The operator fills rows (date, ticker, type_of_trade, direction, horizon_days,
notes, conviction) before 7:00 AM market time. This importer converts today's
rows into idea YAML under data/ideas/active so the existing advisory/shadow
cycles consume them like any other idea source, then writes per-row import
status back to the sheet.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from kamandal_v2.intelligence.transcripts import CONTROLLED_THESIS_TAGS
from kamandal_v2.paths import resolve_path
from kamandal_v2.schemas import MY_IDEAS_HEADER
from kamandal_v2.sheets import GoogleSheetClient

SOURCE_NAME = "operator_sheet"

DIRECTION_ALIASES = {
    "bull": "bullish",
    "bullish": "bullish",
    "bear": "bearish",
    "bearish": "bearish",
    "neutral": "neutral",
    "vol_up": "vol_up",
    "vol up": "vol_up",
    "vol_down": "vol_down",
    "vol down": "vol_down",
}

TYPE_OF_TRADE_TAGS = {
    "anti news euphoria": ["mean_revert", "capitulation"],
    "contrarian": ["mean_revert"],
    "breakout": ["breakout", "momentum"],
    "neutral high iv": ["high_iv", "range_bound", "theta_harvest"],
    "earnings": ["earnings", "pre_event_anticipation", "high_iv"],
    "lt - researched": ["catalyst"],
    "lt researched": ["catalyst"],
    "feel": ["momentum"],
    "feel (rs, news, insider)": ["momentum"],
}

CONVICTIONS = {"low", "medium", "high"}
DEFAULT_HORIZON_DAYS = 45
SKIP_STATUSES = {"example"}


def import_my_ideas(
    config: dict[str, Any],
    *,
    ideas_dir: str | Path = "data/ideas/active",
    write_sheet: bool = True,
    bootstrap: bool = False,
    client: Any | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    client = client or GoogleSheetClient.from_config(config)
    tab_names = ((config.get("google_sheets") or {}).get("tabs") or {})
    title = str(tab_names.get("my_ideas") or "my_ideas")
    today = today or _market_today(config)

    rows = client.read_tab(title)
    if not rows and bootstrap:
        client.replace_tab(title, header=MY_IDEAS_HEADER, rows=_bootstrap_rows(today))
        return {"status": "bootstrapped", "tab": title, "rows": 0, "imported": 0, "ideas_path": None}

    universe = _enabled_universe(client, tab_names)
    ideas, statuses = convert_rows(rows, universe_symbols=universe, today=today)

    ideas_path = None
    if ideas:
        ideas_path = _write_ideas_file(ideas, ideas_dir=ideas_dir, today=today)

    rows_written = 0
    if write_sheet and rows:
        merged = []
        for row, status in zip(rows, statuses, strict=True):
            out = {column: row.get(column, "") for column in MY_IDEAS_HEADER}
            out["status"] = status["status"]
            out["idea_id"] = status.get("idea_id", "")
            out["imported_at"] = status.get("imported_at", "")
            merged.append([out.get(column, "") for column in MY_IDEAS_HEADER])
        rows_written = client.replace_tab(title, header=MY_IDEAS_HEADER, rows=merged)

    return {
        "status": "ok",
        "tab": title,
        "rows": len(rows),
        "imported": len(ideas),
        "statuses": [status["status"] for status in statuses],
        "ideas_path": str(ideas_path) if ideas_path else None,
        "sheet_rows_written": rows_written,
    }


def convert_rows(
    rows: list[dict[str, str]],
    *,
    universe_symbols: set[str],
    today: date,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    ideas: list[dict[str, Any]] = []
    statuses: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    imported_at = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")
    for row in rows:
        idea, status = _convert_row(row, universe_symbols=universe_symbols, today=today)
        if idea is None:
            statuses.append({"status": status})
            continue
        if idea["idea_id"] in seen_ids:
            statuses.append({"status": "duplicate_row_skipped"})
            continue
        seen_ids.add(idea["idea_id"])
        ideas.append(idea)
        statuses.append({"status": status, "idea_id": idea["idea_id"], "imported_at": imported_at})
    return ideas, statuses


def _convert_row(
    row: dict[str, str],
    *,
    universe_symbols: set[str],
    today: date,
) -> tuple[dict[str, Any] | None, str]:
    existing_status = str(row.get("status") or "").strip().lower()
    if existing_status in SKIP_STATUSES:
        return None, "example"

    row_date = _parse_row_date(str(row.get("date") or ""), today=today)
    if row_date is None:
        return None, "invalid_date"
    if row_date != today:
        return None, existing_status or "skipped_not_today"

    ticker = str(row.get("ticker") or "").strip().upper()
    if not ticker:
        return None, "empty_ticker"
    if ticker not in universe_symbols:
        return None, "not_in_universe_add_to_universe_tab"

    raw_type = str(row.get("type_of_trade") or "").strip()
    if "naked" in raw_type.lower():
        return None, "rejected_naked_structures_not_allowed"

    direction = DIRECTION_ALIASES.get(str(row.get("direction") or "").strip().lower())
    if direction is None:
        return None, "invalid_direction_use_bull_bear_neutral"

    tags, tag_status = _thesis_tags(raw_type, direction)
    horizon = _parse_int(str(row.get("horizon_days") or ""), default=DEFAULT_HORIZON_DAYS)
    conviction = str(row.get("conviction") or "").strip().lower()
    if conviction not in CONVICTIONS:
        conviction = "high"

    idea_id = f"op_{today.isoformat()}_{ticker}_{direction[:4]}"
    idea = {
        "idea_id": idea_id,
        "source": SOURCE_NAME,
        "underlying": ticker,
        "direction": direction,
        "strategy_hint": "",
        "mentioned_strategy": "",
        "thesis_tags": tags,
        "horizon_days": horizon,
        "trade_horizon_days": horizon,
        "confidence": conviction,
        "extraction_confidence": "high",
        "quote_evidence": "",
        "extraction_notes": f"operator my_ideas row type_of_trade={raw_type or 'none'}",
        "operator_status": "approved",
        "notes": str(row.get("notes") or "").strip(),
    }
    return idea, tag_status


def _thesis_tags(raw_type: str, direction: str) -> tuple[list[str], str]:
    normalized = raw_type.strip().lower()
    if not normalized:
        return [], "imported_untagged_add_type_of_trade"
    tags = list(TYPE_OF_TRADE_TAGS.get(normalized) or [])
    if not tags:
        direct = [token.strip().lower() for token in normalized.replace(";", ",").split(",") if token.strip()]
        if direct and all(token in CONTROLLED_THESIS_TAGS for token in direct):
            return direct, "imported"
        return [], "imported_untagged_unknown_type"
    if "mean_revert" in tags:
        if direction == "bullish" and "oversold" not in tags:
            tags.append("oversold")
        if direction == "bearish" and "overextended" not in tags:
            tags.append("overextended")
    if direction == "bearish" and "breakout" in tags:
        tags = ["breakdown" if tag == "breakout" else tag for tag in tags]
    return tags, "imported"


def _write_ideas_file(ideas: list[dict[str, Any]], *, ideas_dir: str | Path, today: date) -> Path:
    directory = resolve_path(ideas_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{today.isoformat()}_my_ideas.yaml"
    path.write_text(yaml.safe_dump({"ideas": ideas}, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def _enabled_universe(client: Any, tab_names: dict[str, Any]) -> set[str]:
    title = str(tab_names.get("universe") or "universe")
    rows = client.read_tab(title)
    symbols = set()
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        enabled = str(row.get("enabled") or "").strip().lower() in {"true", "1", "yes"}
        if symbol and enabled:
            symbols.add(symbol)
    return symbols


def _bootstrap_rows(today: date) -> list[list[Any]]:
    sample = {
        "date": today.isoformat(),
        "ticker": "SPY",
        "type_of_trade": "Breakout",
        "direction": "Bull",
        "horizon_days": "45",
        "notes": "Example row. Replace with a real idea; importer skips rows with status=example.",
        "conviction": "high",
        "status": "example",
        "idea_id": "",
        "imported_at": "",
    }
    return [[sample.get(column, "") for column in MY_IDEAS_HEADER]]


def _parse_row_date(raw: str, *, today: date) -> date | None:
    text = raw.strip()
    if not text:
        return today
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m/%d", "%d-%b", "%d-%b-%Y", "%b %d"):
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if parsed.year == 1900:
            parsed = parsed.replace(year=today.year)
        return parsed
    return None


def _parse_int(raw: str, *, default: int) -> int:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _market_today(config: dict[str, Any]) -> date:
    tz = str(((config.get("runtime") or {}).get("market_timezone")) or "America/Chicago")
    return datetime.now(ZoneInfo(tz)).date()
