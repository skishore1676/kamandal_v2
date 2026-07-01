"""Live close advisory planning."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from kamandal_v2.events.earnings import EarningsStore
from kamandal_v2.live.book import live_book_sheet_rows, run_live_book
from kamandal_v2.live.orders import APPROVE_LIVE_CLOSE, build_close_ticket
from kamandal_v2.live.position_management import live_exit_decision, live_exit_policy, mark_live_group
from kamandal_v2.live.reconciliation import reconciliation_blockers_for_group
from kamandal_v2.market.broker import broker_adapter
from kamandal_v2.planner.config_loader import load_planner_config
from kamandal_v2.schemas import DAILY_PLAN_HEADER, LIVE_BOOK_HEADER
from kamandal_v2.sheets import write_daily_plan, write_live_book
from kamandal_v2.stores.sqlite import LocalStore


NONTERMINAL_CLOSE_STATUSES = {"pending_close_approval", "dry_run", "submitted", "repriced"}


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
    review_recommendations = 0
    close_recommendations = 0
    working_close_orders = 0
    for index, group in enumerate(groups, start=1):
        underlying = str(group.get("underlying") or (group.get("candidate") or {}).get("underlying") or "")
        reconciliation_blockers = reconciliation_blockers_for_group(store, group, config=config)
        if reconciliation_blockers:
            decision = {
                "group_id": group.get("group_id"),
                "action": "hold",
                "reason": "live_reconciliation_blocker",
                "reconciliation_blockers": reconciliation_blockers,
            }
            decisions.append(decision)
            store.record_live_management_decision(str(group.get("group_id")), "hold", "live_reconciliation_blocker", decision)
            continue
        playbook = playbook_by_id.get(str(group.get("playbook_id") or (group.get("candidate") or {}).get("playbook_id") or ""))
        chain = _latest_chain(store, underlying)
        mark = mark_live_group(
            group,
            chain["quotes"],
            playbook,
            quote_fresh=underlying in fresh_underlyings,
            config=config,
            underlying_price=chain["underlying_price"],
        )
        marks.append(mark)
        store.record_live_position_mark(str(group.get("group_id")), mark)
        policy = live_exit_policy(config)
        mark["loss_watch_observations"] = store.live_loss_watch_observations(
            str(group.get("group_id")),
            window_minutes=policy.loss_watch_window_minutes,
        )
        mark["loss_watch_confirmations_required"] = policy.loss_watch_confirmations_required
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
        if decision["action"] == "close":
            working_close = _working_close_order(store, group)
            if working_close:
                working_close_orders += 1
                decision = {
                    **decision,
                    "action": "hold",
                    "blocked_action": "close",
                    "blocked_reason": str(decision.get("reason") or ""),
                    "reason": "working_close_order",
                    "working_close_order": _working_close_summary(working_close),
                }
        decisions.append(decision)
        store.record_live_management_decision(str(group.get("group_id")), str(decision["action"]), str(decision["reason"]), decision)
        if decision["action"] == "review":
            review_recommendations += 1
            rows.append(_review_plan_row(index, group, decision))
            continue
        if decision["action"] != "close":
            continue
        if exit_mode == "disabled":
            continue
        ticket = build_close_ticket(
            group,
            close_net_credit=float(decision.get("recommended_close_net") or 0.0) / 100.0,
            seed_salt=_close_seed_salt(store, group, config),
        )
        store.save_live_order_intent(ticket, status="pending_close_approval")
        rows.append(_close_plan_row(index, group, decision, ticket, approval_mode=exit_mode))
        close_recommendations += 1
    live_book_rows_written = 0
    if write_sheet and rows:
        write_daily_plan(config, rows, DAILY_PLAN_HEADER, replace_lanes={"live_close_advisory"})
    if write_sheet:
        live_book_report = run_live_book(store, config)
        live_book_rows_written = write_live_book(config, LIVE_BOOK_HEADER, live_book_sheet_rows(live_book_report, LIVE_BOOK_HEADER))
    return {
        "groups": len(groups),
        "close_recommendations": close_recommendations,
        "review_recommendations": review_recommendations,
        "working_close_orders": working_close_orders,
        "live_book_rows_written": live_book_rows_written,
        "marks": marks,
        "decisions": decisions,
        "daily_plan_rows": rows,
    }


DEAD_CLOSE_STATUSES = {
    "cancelled",
    "canceled",
    "expired",
    "rejected",
    "failed",
    "expired_stale_close_approval",
    "retired_stale_close_failure",
    "BROKER_STATUS_FETCH_FAILED",
}
DEAD_CLOSE_STATUS_PREFIXES = ("blocked_", "reprice_", "submit_failed")


def _close_seed_salt(store: LocalStore, group: dict[str, Any], config: dict[str, Any]) -> str:
    """Give each close attempt a distinct broker identity per day and per prior failure.

    The order id seed is otherwise deterministic, so a same-priced close rebuilt
    after a failed attempt would reuse the dead order's id at the broker and
    silently inherit its terminal state instead of reaching the market.
    """
    group_id = str(group.get("group_id") or "")
    dead = 0
    for ticket in store.live_order_intents_by_type("close"):
        if str(ticket.get("group_id") or "") != group_id:
            continue
        status = str(ticket.get("_ledger_status") or "")
        if status in DEAD_CLOSE_STATUSES or any(status.startswith(prefix) for prefix in DEAD_CLOSE_STATUS_PREFIXES):
            dead += 1
    market_tz = ZoneInfo(str((config.get("runtime") or {}).get("market_timezone") or "America/Chicago"))
    return f"{datetime.now(market_tz).date().isoformat()}:retry{dead}"


def _working_close_order(store: LocalStore, group: dict[str, Any]) -> dict[str, Any] | None:
    group_id = str(group.get("group_id") or "")
    tickets = store.live_close_order_intents_for_group(group_id, statuses=NONTERMINAL_CLOSE_STATUSES)
    return min(tickets, key=_working_close_status_rank) if tickets else None


def _working_close_status_rank(ticket: dict[str, Any]) -> int:
    status = str(ticket.get("_ledger_status") or "")
    if status in {"submitted", "repriced"}:
        return 0
    if status == "pending_close_approval":
        return 1
    return 2


def _working_close_summary(ticket: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticket_hash": ticket.get("ticket_hash"),
        "order_id": ticket.get("order_id"),
        "ledger_status": ticket.get("_ledger_status"),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("_ledger_updated_at"),
        "limit_price": ticket.get("limit_price"),
        "underlying": ticket.get("underlying"),
        "structure": ticket.get("structure"),
    }


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
    required_expirations = _live_group_expiration_dates(groups)
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
    if required_expirations and hasattr(adapter, "expiration_dates"):
        existing = [str(item) for item in getattr(adapter, "expiration_dates", []) if item]
        adapter.expiration_dates = _sorted_expiration_dates([*existing, *required_expirations])
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


def _live_group_expiration_dates(groups: list[dict[str, Any]]) -> list[str]:
    expirations: set[str] = set()
    today = date.today()
    for group in groups:
        candidate = group.get("candidate") or {}
        for leg in candidate.get("legs") or []:
            raw = str(leg.get("expiration") or "")
            try:
                expiration = date.fromisoformat(raw)
            except ValueError:
                continue
            if expiration >= today:
                expirations.add(expiration.isoformat())
    return _sorted_expiration_dates(expirations)


def _sorted_expiration_dates(expirations: Any) -> list[str]:
    valid: list[date] = []
    for raw in expirations or []:
        try:
            valid.append(date.fromisoformat(str(raw)))
        except ValueError:
            continue
    return [item.isoformat() for item in sorted(set(valid))]


def _latest_chain(store: LocalStore, underlying: str) -> dict[str, Any]:
    if not underlying:
        return {"underlying_price": None, "quotes": {}}
    conn = sqlite3.connect(store.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT payload FROM chain_snapshots WHERE underlying = ?", (underlying,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"underlying_price": None, "quotes": {}}
    payloads = [json.loads(row["payload"]) for row in rows]
    payloads.sort(key=lambda item: str(item.get("captured_at") or ""))
    latest = payloads[-1]
    return {
        "underlying_price": _optional_float(latest.get("underlying_price")),
        "quotes": {
            (str(quote.get("expiration")), str(quote.get("option_type")), float(quote.get("strike") or 0.0)): quote
            for quote in latest.get("quotes") or []
        },
    }


def _review_plan_row(index: int, group: dict[str, Any], decision: dict[str, Any]) -> list[Any]:
    detail = {
        "lane": "live_close_advisory",
        "live_gate_status": "review",
        "live_blockers": [],
        "group": group,
        "decision": decision,
    }
    row = {
        "plan_date": date.today().isoformat(),
        "plan_rank": index,
        "plan_id": f"review_{group.get('group_id')}",
        "plan_status": "review",
        "plan_trade_count": 0,
        "plan_score": 0,
        "plan_summary": f"Review {group.get('underlying')} {group.get('structure')} reason={decision.get('reason')}",
        "trade_bundle": f"{group.get('underlying')} review {group.get('structure')}",
        "trade_bundle_json": "[]",
        "plan_total_bpr": 0,
        "plan_bpr_utilization_pct": 0,
        "buying_power_after": "",
        "mode": "live_close_advisory",
        "plan_reasons": str(decision.get("reason") or ""),
        "blocked_by": "operator_review",
        "plan_metrics_json": json.dumps({"decision": decision}, sort_keys=True),
        "plan_detail_json": json.dumps(detail, sort_keys=True),
        "operator_action": "",
        "operator_notes": f"Review only; no close ticket created. urgency={decision.get('urgency')}",
    }
    return [row.get(column, "") for column in DAILY_PLAN_HEADER]


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


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
