"""Shadow portfolio marking, management, and reporting."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import Playbook, utc_now
from kamandal_v2.events.earnings import EarningsStore
from kamandal_v2.paths import resolve_path
from kamandal_v2.planner.config_loader import load_planner_config
from kamandal_v2.stores.sqlite import LocalStore


@dataclass(slots=True)
class ShadowManagementResult:
    mark: dict[str, Any]
    decisions: list[dict[str, Any]]
    closed_count: int
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "mark": self.mark,
            "decisions": list(self.decisions),
            "closed_count": self.closed_count,
            "dry_run": self.dry_run,
        }


def mark_shadow_portfolio(store: LocalStore | None = None) -> dict[str, Any]:
    store = store or LocalStore()
    sqlite_path = store.sqlite_path
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        fills = conn.execute(
            """
            SELECT id, opened_at, plan_run_id, plan_id, candidate_id, idea_id, underlying, playbook_id,
                   structure, net_credit, estimated_bpr, delta, gamma, theta, vega, payload
            FROM shadow_fills
            WHERE status = 'open'
            ORDER BY opened_at
            """
        ).fetchall()
        chain_by_underlying = _latest_chain_quote_maps(conn, {str(fill["underlying"]) for fill in fills})
    finally:
        conn.close()

    rows = []
    total_entry = 0.0
    total_mid_pnl = 0.0
    total_natural_pnl = 0.0
    for fill in fills:
        payload = json.loads(fill["payload"] or "{}")
        legs = payload.get("legs") or []
        chain = chain_by_underlying.get(str(fill["underlying"]))
        entry_credit = float(fill["net_credit"] if fill["net_credit"] is not None else payload.get("net_credit") or 0.0) * 100.0
        mid_close_value = 0.0
        natural_close_value = 0.0
        missing_quotes = []
        if chain is None:
            missing_quotes = [f"{leg.get('expiration')}:{leg.get('option_type')}:{leg.get('strike')}" for leg in legs]
        else:
            _captured_at, _underlying_price, quote_map = chain
            for leg in legs:
                key = (str(leg.get("expiration")), str(leg.get("option_type")), float(leg.get("strike") or 0.0))
                quote = quote_map.get(key)
                if quote is None:
                    missing_quotes.append(":".join(map(str, key)))
                    continue
                qty = int(leg.get("quantity") or 1)
                bid = float(quote.get("bid") or 0.0)
                ask = float(quote.get("ask") or 0.0)
                mid = (bid + ask) / 2.0
                if leg.get("side") == "sell":
                    mid_close_value += -mid * qty * 100.0
                    natural_close_value += -ask * qty * 100.0
                else:
                    mid_close_value += mid * qty * 100.0
                    natural_close_value += bid * qty * 100.0
        captured_at, underlying_price = (chain[0], chain[1]) if chain is not None else ("", None)
        mid_pnl = entry_credit + mid_close_value
        natural_pnl = entry_credit + natural_close_value
        total_entry += entry_credit
        total_mid_pnl += mid_pnl
        total_natural_pnl += natural_pnl
        rows.append({
            "fill_id": fill["id"],
            "opened_at": fill["opened_at"],
            "underlying": fill["underlying"],
            "structure": fill["structure"],
            "idea_id": fill["idea_id"] or payload.get("idea_id"),
            "playbook_id": fill["playbook_id"] or payload.get("playbook_id"),
            "estimated_bpr": fill["estimated_bpr"] if fill["estimated_bpr"] is not None else payload.get("estimated_bpr"),
            "entry_credit": round(entry_credit, 2),
            "mid_pnl": round(mid_pnl, 2),
            "natural_pnl": round(natural_pnl, 2),
            "pnl_pct_of_credit": _pnl_pct(mid_pnl, entry_credit),
            "mark_time": captured_at,
            "underlying_price": underlying_price,
            "missing_quotes": missing_quotes,
            "legs": legs,
        })
    mark = {
        "mark_id": "shadow_mark_" + utc_now().replace(":", "").replace("-", ""),
        "marked_at": utc_now(),
        "position_count": len(rows),
        "total_entry_credit": round(total_entry, 2),
        "total_mid_pnl": round(total_mid_pnl, 2),
        "total_natural_pnl": round(total_natural_pnl, 2),
        "rows": rows,
    }
    store.save_shadow_mark(str(mark["mark_id"]), mark)
    store.event("shadow_portfolio_marked", {
        "mark_id": mark["mark_id"],
        "position_count": mark["position_count"],
        "total_mid_pnl": mark["total_mid_pnl"],
        "total_natural_pnl": mark["total_natural_pnl"],
    })
    return mark


def manage_shadow_positions(
    config: dict[str, Any],
    *,
    config_source: str = "sheet",
    dry_run: bool = False,
    store: LocalStore | None = None,
) -> ShadowManagementResult:
    store = store or LocalStore()
    _universe, playbooks = load_planner_config(config, source=config_source)
    playbook_by_id = {playbook.playbook_id: playbook for playbook in playbooks}
    earnings = EarningsStore()
    mark = mark_shadow_portfolio(store)
    decisions = [_decision_for_mark_row(row, playbook_by_id, earnings) for row in mark.get("rows") or []]
    closed = [decision for decision in decisions if decision["action"] == "close"]
    if not dry_run:
        for decision in closed:
            store.close_shadow_fill(
                str(decision["fill_id"]),
                reason=str(decision["reason"]),
                pnl=float(decision["mid_pnl"] or 0.0),
                payload=decision,
            )
            store.event("shadow_position_closed", decision)
    store.event("shadow_management_evaluated", {
        "mark_id": mark["mark_id"],
        "decisions": len(decisions),
        "closed_count": len(closed) if not dry_run else 0,
        "close_recommendations": len(closed),
        "dry_run": dry_run,
    })
    return ShadowManagementResult(mark=mark, decisions=decisions, closed_count=0 if dry_run else len(closed), dry_run=dry_run)


def write_shadow_eod_report(
    config: dict[str, Any],
    *,
    config_source: str = "sheet",
    output_dir: str | Path = "data/reports/eod",
    store: LocalStore | None = None,
) -> dict[str, Any]:
    result = manage_shadow_positions(config, config_source=config_source, dry_run=True, store=store)
    output_root = resolve_path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    report_date = date.today().isoformat()
    json_path = output_root / f"{report_date}_shadow_eod.json"
    md_path = output_root / f"{report_date}_shadow_eod.md"
    payload = result.to_dict()
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_report_markdown(payload), encoding="utf-8")
    (store or LocalStore()).event("shadow_eod_report_written", {"json_path": str(json_path), "markdown_path": str(md_path)})
    return {"json_path": str(json_path), "markdown_path": str(md_path), **payload}


def _decision_for_mark_row(row: dict[str, Any], playbook_by_id: dict[str, Playbook], earnings: EarningsStore) -> dict[str, Any]:
    playbook = playbook_by_id.get(str(row.get("playbook_id") or ""))
    profit_target_pct = _normalize_pct(playbook.profit_target_pct if playbook else 50.0)
    exit_dte_min = int(playbook.exit_dte_min if playbook else 21)
    half_time_exit = bool(playbook.half_time_exit) if playbook else True
    exit_pre_event_days = playbook.exit_pre_event_days if playbook else None
    dte = _position_dte(row)
    pnl_pct = float(row.get("pnl_pct_of_credit") or 0.0)
    missing_quotes = bool(row.get("missing_quotes"))
    reason = "no_exit"
    action = "hold"
    if missing_quotes:
        reason = "missing_quotes"
    elif exit_pre_event_days is not None and _earnings_days(row, earnings) is not None and _earnings_days(row, earnings) <= exit_pre_event_days:
        action = "close"
        reason = "pre_event"
    elif pnl_pct >= profit_target_pct:
        action = "close"
        reason = "profit_target"
    elif dte["remaining"] is not None and dte["remaining"] <= exit_dte_min:
        action = "close"
        reason = "dte_target"
    elif half_time_exit and dte["remaining"] is not None and dte["half_time_threshold"] is not None and dte["remaining"] <= dte["half_time_threshold"]:
        action = "close"
        reason = "half_time"
    return {
        "fill_id": row.get("fill_id"),
        "underlying": row.get("underlying"),
        "playbook_id": row.get("playbook_id"),
        "structure": row.get("structure"),
        "action": action,
        "reason": reason,
        "mid_pnl": row.get("mid_pnl"),
        "natural_pnl": row.get("natural_pnl"),
        "entry_credit": row.get("entry_credit"),
        "pnl_pct_of_credit": pnl_pct,
        "profit_target_pct": profit_target_pct,
        "dte_remaining": dte["remaining"],
        "entry_dte": dte["entry"],
        "half_time_threshold": dte["half_time_threshold"],
        "exit_dte_min": exit_dte_min,
        "exit_pre_event_days": exit_pre_event_days,
        "missing_quotes": row.get("missing_quotes") or [],
        "mark_time": row.get("mark_time"),
    }


def _latest_chain_quote_maps(conn: sqlite3.Connection, underlyings: set[str]) -> dict[str, tuple[str, float | None, dict[tuple[str, str, float], dict]]]:
    result = {}
    for underlying in underlyings:
        rows = conn.execute("SELECT payload FROM chain_snapshots WHERE underlying = ?", (underlying,)).fetchall()
        if not rows:
            continue
        payloads = [json.loads(row["payload"]) for row in rows]
        payloads.sort(key=lambda item: str(item.get("captured_at") or ""))
        latest = payloads[-1]
        quote_map = {}
        for quote in latest.get("quotes") or []:
            key = (str(quote.get("expiration")), str(quote.get("option_type")), float(quote.get("strike") or 0.0))
            quote_map[key] = quote
        result[underlying] = (str(latest.get("captured_at") or ""), latest.get("underlying_price"), quote_map)
    return result


def _pnl_pct(pnl: float, entry_credit: float) -> float:
    if abs(entry_credit) < 0.01:
        return 0.0
    return round((pnl / abs(entry_credit)) * 100.0, 2)


def _normalize_pct(raw: float) -> float:
    value = float(raw)
    if 0 < value <= 1:
        return round(value * 100.0, 2)
    return value


def _position_dte(row: dict[str, Any]) -> dict[str, int | None]:
    expirations = []
    for leg in row.get("legs") or []:
        raw = str(leg.get("expiration") or "")
        if raw:
            try:
                expirations.append(date.fromisoformat(raw))
            except ValueError:
                pass
    if not expirations:
        return {"entry": None, "remaining": None, "half_time_threshold": None}
    short_expiration = min(expirations)
    opened = _parse_opened_date(str(row.get("opened_at") or ""))
    today = date.today()
    entry_dte = max((short_expiration - opened).days, 0) if opened else None
    remaining = (short_expiration - today).days
    half_time = entry_dte // 2 if entry_dte is not None else None
    return {"entry": entry_dte, "remaining": remaining, "half_time_threshold": half_time}


def _parse_opened_date(raw: str) -> date | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt).date()
        except ValueError:
            continue
    return None


def _earnings_days(row: dict[str, Any], earnings: EarningsStore) -> int | None:
    latest = earnings.latest(str(row.get("underlying") or ""))
    if latest is None or not latest.next_earnings_date:
        return None
    try:
        event_date = date.fromisoformat(latest.next_earnings_date)
    except ValueError:
        return None
    return (event_date - date.today()).days


def _report_markdown(payload: dict[str, Any]) -> str:
    mark = payload.get("mark") or {}
    decisions = payload.get("decisions") or []
    closes = [item for item in decisions if item.get("action") == "close"]
    lines = [
        f"# Shadow EOD Report {date.today().isoformat()}",
        "",
        f"- positions: {mark.get('position_count', 0)}",
        f"- entry_credit: {mark.get('total_entry_credit', 0)}",
        f"- mid_pnl: {mark.get('total_mid_pnl', 0)}",
        f"- natural_pnl: {mark.get('total_natural_pnl', 0)}",
        f"- close_recommendations: {len(closes)}",
        "",
        "## Close Candidates",
    ]
    if not closes:
        lines.append("- none")
    for item in closes:
        lines.append(
            f"- {item.get('underlying')} {item.get('structure')} {item.get('playbook_id')} "
            f"reason={item.get('reason')} mid_pnl={item.get('mid_pnl')} pnl_pct={item.get('pnl_pct_of_credit')}"
        )
    lines.extend(["", "## Holds"])
    for item in decisions:
        if item.get("action") == "hold":
            lines.append(
                f"- {item.get('underlying')} {item.get('structure')} reason={item.get('reason')} "
                f"mid_pnl={item.get('mid_pnl')} dte={item.get('dte_remaining')}"
            )
    return "\n".join(lines).strip() + "\n"
