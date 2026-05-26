"""Live close advisory planning."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from kamandal_v2.events.earnings import EarningsStore
from kamandal_v2.live.orders import APPROVE_LIVE_CLOSE, build_close_ticket
from kamandal_v2.live.position_management import live_exit_decision, live_exit_policy, mark_live_group
from kamandal_v2.market.broker import broker_adapter
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
    fresh_underlyings = _refresh_live_group_quotes(config, store, groups)
    exit_mode = _exit_approval_mode(config)
    rows: list[list[Any]] = []
    decisions = []
    marks = []
    for index, group in enumerate(groups, start=1):
        underlying = str(group.get("underlying") or (group.get("candidate") or {}).get("underlying") or "")
        playbook = playbook_by_id.get(str(group.get("playbook_id") or (group.get("candidate") or {}).get("playbook_id") or ""))
        mark = mark_live_group(
            group,
            _latest_chain(store, underlying),
            playbook,
            quote_fresh=underlying in fresh_underlyings,
            config=config,
        )
        marks.append(mark)
        store.record_live_position_mark(str(group.get("group_id")), mark)
        decision = live_exit_decision(mark, playbook, earnings, config)
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
        ticket = build_close_ticket(group, close_net_credit=float(decision.get("recommended_close_net") or 0.0) / 100.0)
        store.save_live_order_intent(ticket, status="pending_close_approval")
        rows.append(_close_plan_row(index, group, decision, ticket, approval_mode=exit_mode))
    if write_sheet and rows:
        write_daily_plan(config, rows, DAILY_PLAN_HEADER, replace_lanes={"live_close_advisory"})
    return {"groups": len(groups), "close_recommendations": len(rows), "marks": marks, "decisions": decisions, "daily_plan_rows": rows}


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


def _refresh_live_group_quotes(config: dict[str, Any], store: LocalStore, groups: list[dict[str, Any]]) -> set[str]:
    if not live_exit_policy(config).require_fresh_quotes:
        return {str(group.get("underlying") or (group.get("candidate") or {}).get("underlying") or "") for group in groups}
    underlyings = sorted({str(group.get("underlying") or (group.get("candidate") or {}).get("underlying") or "") for group in groups if group})
    refreshed: set[str] = set()
    if not underlyings:
        return refreshed
    try:
        adapter = broker_adapter(config)
    except Exception as exc:  # noqa: BLE001
        store.event("live_quote_refresh_failed", {"stage": "adapter", "error": str(exc), "underlyings": underlyings})
        return refreshed
    if hasattr(adapter, "available") and not adapter.available():
        store.event("live_quote_refresh_skipped", {"reason": "broker_unavailable", "underlyings": underlyings})
        return refreshed
    for underlying in underlyings:
        if not underlying:
            continue
        try:
            snapshot = adapter.chain_snapshot(underlying)
            store.save_chain_snapshot(snapshot)
            refreshed.add(underlying)
        except Exception as exc:  # noqa: BLE001
            store.event("live_quote_refresh_failed", {"stage": "chain_snapshot", "underlying": underlying, "error": str(exc)})
    return refreshed


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
