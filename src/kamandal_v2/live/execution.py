"""Sheet-gated live order execution and reconciliation."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from kamandal_v2.live.orders import APPROVE_LIVE, APPROVE_LIVE_CLOSE, LIVE_SUBMIT_CONFIRM
from kamandal_v2.market.broker import broker_adapter
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.sheets import GoogleSheetClient, pull_sheet_tables
from kamandal_v2.stores.sqlite import LocalStore


def execute_live_approved(
    config: dict[str, Any],
    *,
    submit: bool = False,
    close: bool = False,
    store: LocalStore | None = None,
) -> dict[str, Any]:
    store = store or LocalStore()
    rows = _approved_rows(config, close=close)
    action = APPROVE_LIVE_CLOSE if close else APPROVE_LIVE
    if not rows:
        return {"action": action, "submit": submit, "processed": 0, "results": []}
    _assert_submit_allowed(config, submit=submit)
    adapter = broker_adapter(config)
    results = []
    for row in rows[:1]:
        ticket = _ticket_from_row(row)
        intent = store.live_order_intent(str(ticket.get("ticket_hash") or ""))
        if not intent:
            results.append(_failure(ticket, "ticket_not_found_in_live_ledger"))
            continue
        if intent.get("ticket_hash") != ticket.get("ticket_hash"):
            results.append(_failure(ticket, "ticket_hash_mismatch"))
            continue
        ledger_status = str(intent.get("_ledger_status") or "")
        allowed_statuses = {"dry_run", "pending_close_approval" if close else "pending_approval"}
        if ledger_status and ledger_status not in allowed_statuses:
            results.append(_failure(ticket, f"ticket_already_{ledger_status}"))
            continue
        if close and _same_day_close_blocked(config, store, ticket):
            store.update_live_order_intent_status(str(ticket["ticket_hash"]), "blocked_same_day_close")
            results.append(_failure(ticket, "same_day_live_exit_blocked"))
            continue
        if submit and not _ticket_fresh(config, ticket):
            store.update_live_order_intent_status(str(ticket["ticket_hash"]), "blocked_preflight_stale")
            results.append(_failure(ticket, "ticket_preflight_stale"))
            continue
        request_payload = dict(ticket.get("submit_payload") or {})
        if submit:
            fresh_preflight = adapter.preflight_ticket(ticket)
            if not fresh_preflight.ok:
                store.record_live_order_attempt(
                    ticket,
                    action="preflight_close" if close else "preflight_open",
                    submit=submit,
                    ok=False,
                    request_payload=dict((fresh_preflight.raw or {}).get("request") or ticket.get("submit_payload") or {}),
                    response_payload=fresh_preflight.to_dict(),
                )
                store.update_live_order_intent_status(str(ticket["ticket_hash"]), "blocked_preflight_failed")
                results.append(_failure(ticket, fresh_preflight.message or "fresh_preflight_failed"))
                continue
            try:
                response = adapter.place_order_ticket(ticket)
                ok = bool(response.get("orderId"))
                status = "submitted" if ok else "submit_failed"
            except Exception as exc:  # noqa: BLE001
                response = {"error": str(exc)}
                ok = False
                status = "submit_failed"
        else:
            response = {"dry_run": True, "orderId": ticket.get("order_id"), "request": request_payload}
            ok = True
            status = "dry_run"
        store.record_live_order_attempt(
            ticket,
            action="submit_close" if close else "submit_open",
            submit=submit,
            ok=ok,
            request_payload=request_payload,
            response_payload=response,
        )
        store.update_live_order_intent_status(str(ticket["ticket_hash"]), status)
        store.event("live_order_execution_evaluated", {
            "ticket_hash": ticket.get("ticket_hash"),
            "order_id": ticket.get("order_id"),
            "submit": submit,
            "close": close,
            "status": status,
        })
        results.append({"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "status": status, "response": response})
    return {"action": action, "submit": submit, "processed": len(results), "results": results}


def sync_live_orders(config: dict[str, Any], *, store: LocalStore | None = None) -> dict[str, Any]:
    store = store or LocalStore()
    adapter = broker_adapter(config)
    tickets = store.live_order_intents_by_status({"submitted"})
    results = []
    for ticket in tickets:
        response = adapter.get_order(str(ticket["order_id"]))
        status = str(response.get("status") or "UNKNOWN").upper()
        store.record_live_order_status(str(ticket["order_id"]), status, response, ticket_hash=str(ticket["ticket_hash"]))
        if status in {"FILLED", "PARTIALLY_FILLED"} and ticket.get("intent_type") == "open":
            _save_live_position_from_ticket(store, ticket, status=status.lower(), order_status=response)
        if status in {"FILLED", "PARTIALLY_FILLED"} and ticket.get("intent_type") == "close":
            store.update_live_order_intent_status(str(ticket["ticket_hash"]), "close_filled")
        results.append({"ticket_hash": ticket["ticket_hash"], "order_id": ticket["order_id"], "status": status})
    return {"synced": len(results), "orders": results}


def cleanup_live_approvals(config: dict[str, Any], *, store: LocalStore | None = None) -> dict[str, Any]:
    store = store or LocalStore()
    client = GoogleSheetClient.from_config(config)
    tab_names = ((config.get("google_sheets") or {}).get("tabs") or {})
    title = str(tab_names.get("daily_plan") or "daily_plan")
    rows = client.read_tab(title)
    cleared = []
    for row in rows:
        action = str(row.get("operator_action") or "").strip().upper()
        if action not in {APPROVE_LIVE, APPROVE_LIVE_CLOSE}:
            continue
        detail = _loads(row.get("plan_detail_json"))
        ticket = detail.get("order_ticket_json") or {}
        ticket_hash = str(ticket.get("ticket_hash") or "")
        intent = store.live_order_intent(ticket_hash) if ticket_hash else None
        status = str((intent or {}).get("_ledger_status") or "")
        if status in {"pending_approval", "pending_close_approval", "dry_run"}:
            continue
        if not status:
            continue
        row["operator_action"] = ""
        row["operator_notes"] = f"auto-cleared stale {action}; ledger_status={status}"
        row["plan_status"] = status
        cleared.append({"ticket_hash": ticket_hash, "status": status, "trade_bundle": row.get("trade_bundle")})
    if cleared:
        client.replace_tab(title, header=DAILY_PLAN_HEADER, rows=[[row.get(column, "") for column in DAILY_PLAN_HEADER] for row in rows])
    store.event("live_approval_cleanup_completed", {"cleared": cleared})
    return {"cleared": len(cleared), "rows": cleared}


def record_manual_live_fill(ticket_hash: str, *, store: LocalStore | None = None) -> dict[str, Any]:
    store = store or LocalStore()
    ticket = store.live_order_intent(ticket_hash)
    if not ticket:
        raise RuntimeError(f"live order intent not found: {ticket_hash}")
    _save_live_position_from_ticket(store, ticket, status="open", order_status={"manual": True})
    store.update_live_order_intent_status(ticket_hash, "manual_fill_recorded")
    return {"ticket_hash": ticket_hash, "group_id": _group_id(ticket), "status": "manual_fill_recorded"}


def _approved_rows(config: dict[str, Any], *, close: bool) -> list[dict[str, str]]:
    rows = pull_sheet_tables(config).get("daily_plan") or []
    action = APPROVE_LIVE_CLOSE if close else APPROVE_LIVE
    lane = "live_close_advisory" if close else "live_advisory"
    approved = []
    for row in rows:
        if str(row.get("operator_action") or "").strip().upper() != action:
            continue
        detail = _loads(row.get("plan_detail_json"))
        if str(detail.get("lane") or row.get("mode") or "") != lane:
            continue
        approved.append(row)
    return approved


def _ticket_from_row(row: dict[str, Any]) -> dict[str, Any]:
    detail = _loads(row.get("plan_detail_json"))
    ticket = detail.get("order_ticket_json") or {}
    if not ticket:
        raise RuntimeError("approved daily_plan row missing order_ticket_json")
    return ticket


def _assert_submit_allowed(config: dict[str, Any], *, submit: bool) -> None:
    if not submit:
        return
    runtime = config.get("runtime") or {}
    if str(runtime.get("mode") or "").lower() != "live":
        raise RuntimeError("live submit requires runtime.mode=live")
    if not bool(runtime.get("trading_enabled")):
        raise RuntimeError("live submit requires trading_enabled=true")
    if bool(runtime.get("halt")):
        raise RuntimeError("live submit blocked by runtime.halt=true")
    if os.environ.get("KAMANDAL_LIVE_SUBMIT_CONFIRM") != LIVE_SUBMIT_CONFIRM:
        raise RuntimeError("live submit requires KAMANDAL_LIVE_SUBMIT_CONFIRM=I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")


def _ticket_fresh(config: dict[str, Any], ticket: dict[str, Any]) -> bool:
    max_minutes = int((config.get("live") or {}).get("preflight_max_age_minutes") or 10)
    created_at = str(ticket.get("created_at") or "")
    if not created_at:
        return False
    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (datetime.now(UTC) - parsed).total_seconds() <= max_minutes * 60


def _same_day_close_blocked(config: dict[str, Any], store: LocalStore, ticket: dict[str, Any]) -> bool:
    if str(os.environ.get("KAMANDAL_ALLOW_SAME_DAY_LIVE_EXITS") or "").lower() in {"1", "true", "yes", "on"}:
        return False
    if bool((config.get("live") or {}).get("allow_same_day_exits")):
        return False
    group = store.live_position_group(str(ticket.get("group_id") or ""))
    opened_at = str((group or {}).get("opened_at") or "")
    market_tz = ZoneInfo(str((config.get("runtime") or {}).get("market_timezone") or os.environ.get("KAMANDAL_MARKET_TZ") or "America/Chicago"))
    allow_after = (config.get("live") or {}).get("allow_same_day_exits_after")
    if allow_after:
        try:
            if datetime.now(market_tz).date() >= datetime.fromisoformat(str(allow_after)).date():
                return False
        except ValueError:
            pass
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


def _save_live_position_from_ticket(store: LocalStore, ticket: dict[str, Any], *, status: str, order_status: dict[str, Any]) -> None:
    group_id = _group_id(ticket)
    payload = {
        "group_id": group_id,
        "order_id": ticket.get("order_id"),
        "plan_id": ticket.get("plan_id"),
        "candidate_id": ticket.get("candidate_id"),
        "idea_id": ticket.get("idea_id"),
        "underlying": ticket.get("underlying"),
        "playbook_id": ticket.get("playbook_id"),
        "structure": ticket.get("structure"),
        "candidate": _candidate_from_ticket(ticket),
        "entry_snapshot": _entry_snapshot_from_ticket(ticket, order_status),
        "order_status": order_status,
    }
    store.save_live_position_group(group_id, payload, status="open")
    store.save_live_position(group_id, group_id, payload, status="open" if status in {"open", "filled"} else status)
    store.update_live_order_intent_status(str(ticket["ticket_hash"]), "filled")


def _candidate_from_ticket(ticket: dict[str, Any]) -> dict[str, Any]:
    legs = list(ticket.get("legs") or [])
    return {
        "candidate_id": ticket.get("candidate_id"),
        "idea_id": ticket.get("idea_id"),
        "underlying": ticket.get("underlying"),
        "playbook_id": ticket.get("playbook_id"),
        "structure": ticket.get("structure"),
        "net_credit": _net_credit_from_ticket(ticket),
        "legs": legs,
    }


def _net_credit_from_ticket(ticket: dict[str, Any]) -> float:
    limit_price = float(ticket.get("limit_price") or 0.0)
    return abs(limit_price) if limit_price < 0 else -abs(limit_price)


def _entry_snapshot_from_ticket(ticket: dict[str, Any], order_status: dict[str, Any]) -> dict[str, Any]:
    net_credit = _net_credit_from_ticket(ticket)
    if len(ticket.get("legs") or []) == 1 and order_status.get("averagePrice") not in (None, ""):
        fill_price = float(order_status.get("averagePrice") or abs(net_credit))
        side = str(order_status.get("side") or "").upper()
        net_credit = fill_price if side == "SELL" else -fill_price
    entry_net_cashflow = round(net_credit * 100.0, 2)
    return {
        "entry_kind": "credit" if entry_net_cashflow > 0 else "debit",
        "entry_net_credit": round(net_credit, 4),
        "entry_net_cashflow": entry_net_cashflow,
        "entry_value": abs(entry_net_cashflow),
        "fill_price": order_status.get("averagePrice"),
        "fill_quantity": order_status.get("filledQuantity"),
        "source_order_id": ticket.get("order_id"),
        "source_ticket_hash": ticket.get("ticket_hash"),
    }


def _group_id(ticket: dict[str, Any]) -> str:
    return f"live_group_{ticket.get('ticket_hash')}"


def _failure(ticket: dict[str, Any], reason: str) -> dict[str, Any]:
    return {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "status": "blocked", "reason": reason}


def _loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
