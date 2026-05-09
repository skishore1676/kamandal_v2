"""Sheet-gated live order execution and reconciliation."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from kamandal_v2.live.orders import APPROVE_LIVE, APPROVE_LIVE_CLOSE, LIVE_SUBMIT_CONFIRM
from kamandal_v2.market.public import PublicAdapter
from kamandal_v2.sheets import pull_sheet_tables
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
    adapter = PublicAdapter(config)
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
        if submit and not _ticket_fresh(config, ticket):
            results.append(_failure(ticket, "ticket_preflight_stale"))
            continue
        request_payload = dict(ticket.get("submit_payload") or {})
        if submit:
            fresh_preflight = adapter.preflight_ticket(ticket)
            if not fresh_preflight.ok:
                results.append(_failure(ticket, fresh_preflight.message or "fresh_preflight_failed"))
                continue
            response = adapter.place_order_ticket(ticket)
            ok = bool(response.get("orderId"))
            status = "submitted" if ok else "submit_failed"
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
    adapter = PublicAdapter(config)
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
