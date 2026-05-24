"""Live close advisory planning."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kamandal_v2.events.earnings import EarningsStore
from kamandal_v2.live.orders import APPROVE_LIVE_CLOSE, build_close_ticket
from kamandal_v2.management.shadow import _decision_for_mark_row
from kamandal_v2.planner.config_loader import load_planner_config
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.sheets import write_daily_plan
from kamandal_v2.stores.sqlite import LocalStore


def run_live_management_plan(
    config: dict[str, Any],
    *,
    config_source: str = "sheet",
    write_sheet: bool = False,
    store: LocalStore | None = None,
) -> dict[str, Any]:
    store = store or LocalStore()
    _universe, playbooks = load_planner_config(config, source=config_source)
    playbook_by_id = {playbook.playbook_id: playbook for playbook in playbooks}
    earnings = EarningsStore()
    groups = store.open_live_position_groups()
    exit_mode = _exit_approval_mode(config)
    rows: list[list[Any]] = []
    decisions = []
    for index, group in enumerate(groups, start=1):
        mark_row = _mark_live_group(store, group)
        decision = _decision_for_mark_row(mark_row, playbook_by_id, earnings)
        decision["group_id"] = group.get("group_id")
        if decision["action"] == "close" and _same_day_exit_blocked(config, group):
            decision = {
                **decision,
                "action": "hold",
                "blocked_action": "close",
                "blocked_reason": str(decision.get("reason") or ""),
                "reason": "same_day_live_exit_blocked",
            }
        decisions.append(decision)
        store.record_live_management_decision(str(group.get("group_id")), str(decision["action"]), str(decision["reason"]), decision)
        if decision["action"] != "close":
            continue
        if exit_mode == "disabled":
            continue
        ticket = build_close_ticket(group)
        store.save_live_order_intent(ticket, status="pending_close_approval")
        rows.append(_close_plan_row(index, group, decision, ticket, approval_mode=exit_mode))
    if write_sheet and rows:
        write_daily_plan(config, rows, DAILY_PLAN_HEADER, replace_lanes={"live_close_advisory"})
    return {"groups": len(groups), "close_recommendations": len(rows), "decisions": decisions, "daily_plan_rows": rows}


def _exit_approval_mode(config: dict[str, Any]) -> str:
    raw = str(((config.get("live") or {}).get("exit_approval_mode") or "sheet_approval")).strip().lower()
    allowed = {"sheet_approval", "auto_rules", "disabled"}
    if raw not in allowed:
        raise ValueError(f"unsupported live.exit_approval_mode={raw!r}; expected one of {sorted(allowed)}")
    return raw


def _same_day_exit_blocked(config: dict[str, Any], group: dict[str, Any]) -> bool:
    if str(os.environ.get("KAMANDAL_ALLOW_SAME_DAY_LIVE_EXITS") or "").lower() in {"1", "true", "yes", "on"}:
        return False
    if bool((config.get("live") or {}).get("allow_same_day_exits")):
        return False
    market_tz = ZoneInfo(str((config.get("runtime") or {}).get("market_timezone") or os.environ.get("KAMANDAL_MARKET_TZ") or "America/Chicago"))
    allow_after = (config.get("live") or {}).get("allow_same_day_exits_after")
    if allow_after:
        try:
            if datetime.now(market_tz).date() >= date.fromisoformat(str(allow_after)):
                return False
        except ValueError:
            pass
    opened_at = str(group.get("opened_at") or "")
    if not opened_at:
        return True
    opened = _parse_db_timestamp(opened_at).astimezone(market_tz).date()
    return opened == datetime.now(market_tz).date()


def _parse_db_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _mark_live_group(store: LocalStore, group: dict[str, Any]) -> dict[str, Any]:
    candidate = group.get("candidate") or {}
    legs = candidate.get("legs") or []
    entry_credit = float(candidate.get("net_credit") or 0.0) * 100.0
    chain = _latest_chain(store, str(group.get("underlying") or candidate.get("underlying") or ""))
    mid_close_value = 0.0
    natural_close_value = 0.0
    missing_quotes = []
    for leg in legs:
        quote = None
        if chain:
            quote = chain.get((str(leg.get("expiration")), str(leg.get("option_type")), float(leg.get("strike") or 0.0)))
        if quote is None:
            missing_quotes.append(f"{leg.get('expiration')}:{leg.get('option_type')}:{leg.get('strike')}")
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
    mid_pnl = entry_credit + mid_close_value
    natural_pnl = entry_credit + natural_close_value
    return {
        "fill_id": group.get("group_id"),
        "underlying": group.get("underlying") or candidate.get("underlying"),
        "playbook_id": group.get("playbook_id") or candidate.get("playbook_id"),
        "structure": group.get("structure") or candidate.get("structure"),
        "entry_credit": round(entry_credit, 2),
        "mid_pnl": round(mid_pnl, 2),
        "natural_pnl": round(natural_pnl, 2),
        "pnl_pct_of_credit": round((mid_pnl / abs(entry_credit)) * 100.0, 2) if abs(entry_credit) >= 0.01 else 0.0,
        "missing_quotes": missing_quotes,
        "legs": legs,
        "mark_time": "",
    }


def _latest_chain(store: LocalStore, underlying: str) -> dict[tuple[str, str, float], dict[str, Any]]:
    if not underlying:
        return {}
    conn = sqlite3.connect(store.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT payload FROM chain_snapshots WHERE underlying = ?", (underlying,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return {}
    payloads = [json.loads(row["payload"]) for row in rows]
    payloads.sort(key=lambda item: str(item.get("captured_at") or ""))
    latest = payloads[-1]
    return {
        (str(quote.get("expiration")), str(quote.get("option_type")), float(quote.get("strike") or 0.0)): quote
        for quote in latest.get("quotes") or []
    }


def _close_plan_row(index: int, group: dict[str, Any], decision: dict[str, Any], ticket: dict[str, Any], *, approval_mode: str) -> list[Any]:
    detail = {
        "lane": "live_close_advisory",
        "live_gate_status": "eligible",
        "live_blockers": [],
        "group": group,
        "decision": decision,
        "order_ticket_json": ticket,
    }
    row = {
        "plan_date": date.today().isoformat(),
        "plan_rank": index,
        "plan_id": f"close_{group.get('group_id')}",
        "plan_status": "eligible",
        "plan_trade_count": 1,
        "plan_score": 0,
        "plan_summary": f"Close {group.get('underlying')} {group.get('structure')} reason={decision.get('reason')}",
        "trade_bundle": f"{group.get('underlying')} close {group.get('structure')}",
        "trade_bundle_json": json.dumps([ticket], sort_keys=True),
        "plan_total_bpr": 0,
        "plan_bpr_utilization_pct": 0,
        "buying_power_after": "",
        "mode": "live_close_advisory",
        "plan_reasons": str(decision.get("reason") or ""),
        "blocked_by": "",
        "plan_metrics_json": json.dumps({"decision": decision}, sort_keys=True),
        "plan_detail_json": json.dumps(detail, sort_keys=True),
        "operator_action": APPROVE_LIVE_CLOSE if approval_mode == "auto_rules" else "",
        "operator_notes": "",
    }
    return [row.get(column, "") for column in DAILY_PLAN_HEADER]
