"""Sheet-gated live order execution and reconciliation."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, time
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from kamandal_v2.live.health import entry_health_gate
from kamandal_v2.live.orders import APPROVE_LIVE, APPROVE_LIVE_CLOSE, LIVE_SUBMIT_CONFIRM
from kamandal_v2.live.orders import ticket_hash as compute_ticket_hash
from kamandal_v2.market.broker import broker_adapter
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.sheets import GoogleSheetClient, pull_sheet_tables
from kamandal_v2.stores.sqlite import LocalStore


TERMINAL_UNFILLED_ORDER_STATUSES = {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"}
COMPLETED_TICKET_STATUSES = {"filled", "close_filled", "manual_fill_recorded"}
APPROVED_CLOSE_PENDING_SUBMIT = "approved_close_pending_submit"
EXPIRED_EOD_STATUS = "expired_eod"
PENDING_TICKET_STATUSES = {"pending_approval", "pending_close_approval", APPROVED_CLOSE_PENDING_SUBMIT, "dry_run"}
ACTIVE_TICKET_STATUSES = {"submitted", "repriced"}
CANCEL_PENDING_TICKET_STATUSES = {"repriced", "expired"}
EXPIRED_BROKER_MISSING_STATUS = "expired_broker_status_missing"
RETIRED_STALE_ENTRY_APPROVAL_STATUS = "retired_stale_entry_approval"
FAILED_TICKET_STATUS_PREFIXES = ("blocked_", "reprice_", "submit_failed")
FAILED_TICKET_STATUSES = {"rejected", "expired", EXPIRED_EOD_STATUS, "failed", "cancelled", "canceled"}
DEFAULT_STALE_ENTRY_APPROVAL_MINUTES = 120


def execute_live_approved(
    config: dict[str, Any],
    *,
    submit: bool = False,
    close: bool = False,
    store: LocalStore | None = None,
) -> dict[str, Any]:
    store = store or LocalStore()
    action = APPROVE_LIVE_CLOSE if close else APPROVE_LIVE
    if close and _exit_submit_source(config) == "ledger":
        promoted = _promote_sheet_approved_closes_to_ledger(config, store)
        promoted += _promote_legacy_auto_rule_closes_to_ledger(config, store)
        tickets = _ledger_approved_close_tickets(store, config)
        if not tickets:
            return {"action": action, "submit": submit, "processed": 0, "results": [], "source": "ledger", "promoted": promoted}
        _assert_submit_allowed(config, submit=submit)
        adapter = broker_adapter(config)
        results = [_execute_ticket(config, adapter, store, ticket, submit=submit, close=True) for ticket in tickets]
        return {"action": action, "submit": submit, "processed": len(results), "results": results, "source": "ledger", "promoted": promoted}

    rows = _approved_rows(config, close=close)
    if not rows:
        return {"action": action, "submit": submit, "processed": 0, "results": []}
    _assert_submit_allowed(config, submit=submit)
    cluster_capped: dict[str, str] = {}
    if submit and not close:
        gate = entry_health_gate(store, config)
        if gate["blocked"]:
            risk_manager = gate.get("risk_manager") or {}
            if bool(risk_manager.get("enabled")) and bool(risk_manager.get("blocked")):
                reason = "blocked_risk_manager:" + ",".join(gate["reasons"])
                store.event("live_entries_blocked_by_risk_manager", {"overall": gate["overall"], "reasons": gate["reasons"], "risk_manager": risk_manager})
            else:
                reason = "blocked_live_health_red:" + ",".join(gate["reasons"])
                store.event("live_entries_blocked_by_health", {"overall": gate["overall"], "reasons": gate["reasons"], "counts": gate["counts"]})
            results = [{"status": "blocked", "reason": reason, "trade_bundle": row.get("trade_bundle")} for row in rows[:1]]
            return {"action": action, "submit": submit, "processed": len(results), "results": results, "health_gate": gate}
        for cluster, symbols in ((gate.get("risk_manager") or {}).get("clusters_at_cap") or {}).items():
            for symbol in symbols:
                cluster_capped[str(symbol).upper()] = str(cluster)
    adapter = broker_adapter(config)
    results = []
    for row in rows[:1]:
        tickets, selection_reason = _tickets_to_execute(config, store, row, submit=submit, close=close)
        if not tickets:
            results.append({"status": "blocked", "reason": selection_reason, "trade_bundle": row.get("trade_bundle")})
            continue
        if submit and not close and not _daily_basket_cap_allows(config, store, row):
            results.append({"status": "blocked", "reason": "max_live_baskets_per_day_reached", "trade_bundle": row.get("trade_bundle")})
            continue
        for ticket in tickets:
            capped_cluster = cluster_capped.get(str(ticket.get("underlying") or "").upper())
            if capped_cluster and submit and not close:
                store.event("live_entry_blocked_by_cluster_cap", {"ticket_hash": ticket.get("ticket_hash"), "underlying": ticket.get("underlying"), "cluster": capped_cluster})
                results.append({"status": "blocked", "reason": f"blocked_risk_cluster_cap:{capped_cluster}", "underlying": ticket.get("underlying"), "ticket_hash": ticket.get("ticket_hash")})
                continue
            results.append(_execute_ticket(config, adapter, store, ticket, submit=submit, close=close))
    return {"action": action, "submit": submit, "processed": len(results), "results": results}


def _execute_ticket(
    config: dict[str, Any],
    adapter: Any,
    store: LocalStore,
    ticket: dict[str, Any],
    *,
    submit: bool,
    close: bool,
) -> dict[str, Any]:
    intent = store.live_order_intent(str(ticket.get("ticket_hash") or ""))
    if not intent:
        return _failure(ticket, "ticket_not_found_in_live_ledger")
    if intent.get("ticket_hash") != ticket.get("ticket_hash"):
        return _failure(ticket, "ticket_hash_mismatch")
    ledger_status = str(intent.get("_ledger_status") or "")
    allowed_statuses = {"dry_run", "pending_approval"}
    if close:
        allowed_statuses = {"dry_run", "pending_close_approval", APPROVED_CLOSE_PENDING_SUBMIT}
    if ledger_status and ledger_status not in allowed_statuses:
        return _failure(ticket, f"ticket_already_{ledger_status}")
    if close and _same_day_close_blocked(config, store, ticket):
        store.update_live_order_intent_status(str(ticket["ticket_hash"]), "blocked_same_day_close")
        return _failure(ticket, "same_day_live_exit_blocked")
    if submit and not _ticket_fresh(config, ticket):
        store.update_live_order_intent_status(str(ticket["ticket_hash"]), "blocked_preflight_stale")
        return _failure(ticket, "ticket_preflight_stale")
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
            return _failure(ticket, fresh_preflight.message or "fresh_preflight_failed")
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
    return {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "status": status, "response": response}


def sync_live_orders(config: dict[str, Any], *, store: LocalStore | None = None, manage_entries: bool = True) -> dict[str, Any]:
    store = store or LocalStore()
    adapter = broker_adapter(config)
    tickets = sorted(
        store.live_order_intents_by_status({"submitted", *CANCEL_PENDING_TICKET_STATUSES}),
        key=lambda item: (0 if str(item.get("_ledger_status") or "") == "submitted" else 1, str(item.get("created_at") or "")),
    )
    results = []
    for ticket in tickets:
        ledger_status = str(ticket.get("_ledger_status") or "submitted")
        try:
            response = adapter.get_order(str(ticket["order_id"]))
        except Exception as exc:  # noqa: BLE001
            error = _safe_broker_error(exc)
            if ledger_status == "expired" and _broker_error_is_404(error):
                status = "BROKER_ORDER_NOT_FOUND"
                response = {"error": error, "reconciled_status": EXPIRED_BROKER_MISSING_STATUS}
                store.record_live_order_status(str(ticket["order_id"]), status, response, ticket_hash=str(ticket["ticket_hash"]))
                store.update_live_order_intent_status_with_payload(
                    str(ticket["ticket_hash"]),
                    EXPIRED_BROKER_MISSING_STATUS,
                    {
                        "order_reconciliation": {
                            "status": EXPIRED_BROKER_MISSING_STATUS,
                            "prior_status": ledger_status,
                            "reason": "expired_order_missing_at_broker",
                            "error": error,
                            "reconciled_at": _now_utc(),
                        }
                    },
                )
                results.append({
                    "ticket_hash": ticket["ticket_hash"],
                    "order_id": ticket["order_id"],
                    "status": status,
                    "ledger_status": ledger_status,
                    "reconciled_status": EXPIRED_BROKER_MISSING_STATUS,
                    "error": error,
                })
                continue
            response = {"error": error}
            status = "BROKER_STATUS_FETCH_FAILED"
            store.record_live_order_status(str(ticket["order_id"]), status, response, ticket_hash=str(ticket["ticket_hash"]))
            results.append({
                "ticket_hash": ticket["ticket_hash"],
                "order_id": ticket["order_id"],
                "status": status,
                "ledger_status": ledger_status,
                "needs_broker_status_review": True,
                "error": error,
            })
            continue
        status = str(response.get("status") or "UNKNOWN").upper()
        intent_type = str(ticket.get("intent_type") or "")
        should_manage_submitted = manage_entries and ledger_status == "submitted"
        store.record_live_order_status(str(ticket["order_id"]), status, response, ticket_hash=str(ticket["ticket_hash"]))
        if status in {"NEW", "OPEN", "WORKING"} and should_manage_submitted and intent_type == "close" and _close_expire_due(store, ticket, response, config):
            expire_result = _expire_live_close_order(adapter, store, config, ticket, response)
            results.append({"ticket_hash": ticket["ticket_hash"], "order_id": ticket["order_id"], "status": status, **expire_result})
            continue
        if status in {"NEW", "OPEN", "WORKING"} and should_manage_submitted and intent_type == "close" and _close_reprice_due(store, ticket, response, config):
            reprice_result = _reprice_live_close_order(adapter, store, config, ticket, response)
            results.append({"ticket_hash": ticket["ticket_hash"], "order_id": ticket["order_id"], "status": status, **reprice_result})
            continue
        if status in {"NEW", "OPEN", "WORKING"} and should_manage_submitted and intent_type == "open" and _entry_expire_due(store, ticket, response, config):
            expire_result = _expire_live_entry_order(adapter, store, config, ticket, response)
            results.append({"ticket_hash": ticket["ticket_hash"], "order_id": ticket["order_id"], "status": status, **expire_result})
            continue
        if status in {"NEW", "OPEN", "WORKING"} and should_manage_submitted and intent_type == "open" and _entry_reprice_due(store, ticket, response, config):
            reprice_result = _reprice_live_entry_order(adapter, store, config, ticket, response)
            results.append({"ticket_hash": ticket["ticket_hash"], "order_id": ticket["order_id"], "status": status, **reprice_result})
            continue
        if status in {"FILLED", "PARTIALLY_FILLED"} and ticket.get("intent_type") == "open":
            _save_live_position_from_ticket(store, ticket, status=status.lower(), order_status=response)
        if status in {"FILLED", "PARTIALLY_FILLED"} and ticket.get("intent_type") == "close":
            store.update_live_order_intent_status(str(ticket["ticket_hash"]), "close_filled")
        if status in TERMINAL_UNFILLED_ORDER_STATUSES:
            store.update_live_order_intent_status(str(ticket["ticket_hash"]), status.lower().replace("canceled", "cancelled"))
        result = {"ticket_hash": ticket["ticket_hash"], "order_id": ticket["order_id"], "status": status, "ledger_status": ledger_status}
        if ledger_status in CANCEL_PENDING_TICKET_STATUSES and status in {"NEW", "OPEN", "WORKING"}:
            result["cancel_pending"] = True
            result["needs_broker_cancel_review"] = True
        if not manage_entries and ledger_status == "submitted" and status in {"NEW", "OPEN", "WORKING"}:
            result["entry_management_skipped"] = True
        results.append(result)
    return {"synced": len(results), "manage_entries": manage_entries, "orders": results}


def _exit_submit_source(config: dict[str, Any]) -> str:
    raw = str((config.get("live") or {}).get("exit_submit_source") or "sheet").strip().lower()
    return raw if raw in {"sheet", "ledger"} else "sheet"


def _exit_approval_mode(config: dict[str, Any]) -> str:
    return str((config.get("live") or {}).get("exit_approval_mode") or "sheet_approval").strip().lower()


def _max_close_submits_per_run(config: dict[str, Any]) -> int:
    return max(int((config.get("live") or {}).get("max_close_submits_per_run") or 10), 1)


def _ledger_approved_close_tickets(store: LocalStore, config: dict[str, Any]) -> list[dict[str, Any]]:
    tickets = store.live_order_intents_by_type("close", statuses={APPROVED_CLOSE_PENDING_SUBMIT})
    tickets.sort(key=lambda item: (str(item.get("_ledger_created_at") or ""), str(item.get("ticket_hash") or "")))
    return tickets[:_max_close_submits_per_run(config)]


def _promote_sheet_approved_closes_to_ledger(config: dict[str, Any], store: LocalStore) -> int:
    if _exit_approval_mode(config) != "sheet_approval":
        return 0
    promoted = 0
    for row in _approved_rows(config, close=True):
        for ticket in _tickets_from_row(row, close=True):
            ticket_hash = str(ticket.get("ticket_hash") or "")
            intent = store.live_order_intent(ticket_hash)
            if not intent:
                continue
            if str(intent.get("_ledger_status") or "") == "pending_close_approval":
                store.update_live_order_intent_status(ticket_hash, APPROVED_CLOSE_PENDING_SUBMIT)
                promoted += 1
    return promoted


def _promote_legacy_auto_rule_closes_to_ledger(config: dict[str, Any], store: LocalStore) -> int:
    if _exit_approval_mode(config) != "auto_rules":
        return 0
    open_group_ids = {str(group.get("group_id") or "") for group in store.open_live_position_groups()}
    promoted = 0
    for ticket in store.live_order_intents_by_type("close", statuses={"pending_close_approval"}):
        group_id = str(ticket.get("group_id") or "")
        if group_id and group_id not in open_group_ids:
            continue
        store.update_live_order_intent_status(str(ticket["ticket_hash"]), APPROVED_CLOSE_PENDING_SUBMIT)
        promoted += 1
    if promoted:
        store.event("live_legacy_auto_close_approvals_promoted", {"promoted": promoted})
    return promoted


def _safe_broker_error(exc: Exception) -> str:
    return re.sub(r"/trading/[^/]+/", "/trading/<account>/", str(exc))


def _broker_error_is_404(error: str) -> bool:
    return "status=404" in error or "status = 404" in error


def _expire_live_close_order(adapter: Any, store: LocalStore, config: dict[str, Any], ticket: dict[str, Any], broker_status: dict[str, Any]) -> dict[str, Any]:
    try:
        _assert_submit_allowed(config, submit=True)
        cancel_response = adapter.cancel_order(str(ticket["order_id"]))
        store.record_live_order_status(str(ticket["order_id"]), "CLOSE_EXPIRE_CANCEL_REQUESTED", cancel_response, ticket_hash=str(ticket["ticket_hash"]))
        store.record_live_order_attempt(
            ticket,
            action="expire_close",
            submit=True,
            ok=True,
            request_payload={"orderId": ticket.get("order_id"), "reason": "exit_reprice_expired_eod"},
            response_payload=cancel_response,
        )
        store.update_live_order_intent_status(str(ticket["ticket_hash"]), EXPIRED_EOD_STATUS)
        store.event("live_order_close_expired_eod", {
            "ticket_hash": ticket.get("ticket_hash"),
            "order_id": ticket.get("order_id"),
            "broker_status": broker_status.get("status"),
            "age_minutes": _close_order_age_minutes(store, ticket, broker_status),
        })
        return {"expire_status": "cancel_requested"}
    except Exception as exc:  # noqa: BLE001
        store.event("live_order_close_expire_failed", {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "error": str(exc)})
        return {"expire_status": "failed", "expire_message": str(exc)}


def _reprice_live_close_order(adapter: Any, store: LocalStore, config: dict[str, Any], ticket: dict[str, Any], broker_status: dict[str, Any]) -> dict[str, Any]:
    try:
        _assert_submit_allowed(config, submit=True)
        cancel_response = adapter.cancel_order(str(ticket["order_id"]))
        store.record_live_order_status(str(ticket["order_id"]), "CLOSE_REPRICE_CANCEL_REQUESTED", cancel_response, ticket_hash=str(ticket["ticket_hash"]))
        new_ticket = _repriced_close_ticket(ticket, config)
        fresh_preflight = adapter.preflight_ticket(new_ticket)
        if not fresh_preflight.ok:
            store.record_live_order_attempt(
                new_ticket,
                action="reprice_preflight_close",
                submit=True,
                ok=False,
                request_payload=dict((fresh_preflight.raw or {}).get("request") or new_ticket.get("submit_payload") or {}),
                response_payload=fresh_preflight.to_dict(),
            )
            store.update_live_order_intent_status(str(ticket["ticket_hash"]), "reprice_blocked_preflight_failed")
            return {"reprice_status": "blocked_preflight_failed", "reprice_message": fresh_preflight.message}
        new_ticket["preflight"] = fresh_preflight.to_dict()
        new_ticket["ticket_hash"] = compute_ticket_hash(new_ticket)
        response = adapter.place_order_ticket(new_ticket)
        ok = bool(response.get("orderId"))
        store.save_live_order_intent(new_ticket, status="submitted" if ok else "submit_failed")
        store.record_live_order_attempt(
            new_ticket,
            action="reprice_submit_close",
            submit=True,
            ok=ok,
            request_payload=dict(new_ticket.get("submit_payload") or {}),
            response_payload=response,
        )
        store.update_live_order_intent_status(str(ticket["ticket_hash"]), "repriced" if ok else "reprice_submit_failed")
        store.event("live_order_close_repriced", {
            "from_ticket_hash": ticket.get("ticket_hash"),
            "to_ticket_hash": new_ticket.get("ticket_hash"),
            "from_order_id": ticket.get("order_id"),
            "to_order_id": new_ticket.get("order_id"),
            "from_limit_price": ticket.get("limit_price"),
            "to_limit_price": new_ticket.get("limit_price"),
            "broker_status": broker_status.get("status"),
            "attempt": new_ticket.get("reprice_attempt"),
            "ok": ok,
        })
        return {
            "reprice_status": "submitted" if ok else "submit_failed",
            "reprice_ticket_hash": new_ticket.get("ticket_hash"),
            "reprice_order_id": new_ticket.get("order_id"),
            "reprice_limit_price": new_ticket.get("limit_price"),
        }
    except Exception as exc:  # noqa: BLE001
        store.event("live_order_close_reprice_failed", {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "error": str(exc)})
        return {"reprice_status": "failed", "reprice_message": str(exc)}


def _expire_live_entry_order(adapter: Any, store: LocalStore, config: dict[str, Any], ticket: dict[str, Any], broker_status: dict[str, Any]) -> dict[str, Any]:
    try:
        _assert_submit_allowed(config, submit=True)
        cancel_response = adapter.cancel_order(str(ticket["order_id"]))
        store.record_live_order_status(str(ticket["order_id"]), "ENTRY_EXPIRE_CANCEL_REQUESTED", cancel_response, ticket_hash=str(ticket["ticket_hash"]))
        store.record_live_order_attempt(
            ticket,
            action="expire_open",
            submit=True,
            ok=True,
            request_payload={"orderId": ticket.get("order_id"), "reason": "entry_reprice_expired"},
            response_payload=cancel_response,
        )
        store.update_live_order_intent_status(str(ticket["ticket_hash"]), "expired")
        store.event("live_order_entry_expired", {
            "ticket_hash": ticket.get("ticket_hash"),
            "order_id": ticket.get("order_id"),
            "broker_status": broker_status.get("status"),
            "age_minutes": _entry_order_age_minutes(store, ticket, broker_status),
        })
        return {"expire_status": "cancel_requested"}
    except Exception as exc:  # noqa: BLE001
        store.event("live_order_entry_expire_failed", {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "error": str(exc)})
        return {"expire_status": "failed", "expire_message": str(exc)}


def _reprice_live_entry_order(adapter: Any, store: LocalStore, config: dict[str, Any], ticket: dict[str, Any], broker_status: dict[str, Any]) -> dict[str, Any]:
    try:
        _assert_submit_allowed(config, submit=True)
        cancel_response = adapter.cancel_order(str(ticket["order_id"]))
        store.record_live_order_status(str(ticket["order_id"]), "REPRICE_CANCEL_REQUESTED", cancel_response, ticket_hash=str(ticket["ticket_hash"]))
        new_ticket = _repriced_open_ticket(ticket, config)
        fresh_preflight = adapter.preflight_ticket(new_ticket)
        if not fresh_preflight.ok:
            store.record_live_order_attempt(
                new_ticket,
                action="reprice_preflight_open",
                submit=True,
                ok=False,
                request_payload=dict((fresh_preflight.raw or {}).get("request") or new_ticket.get("submit_payload") or {}),
                response_payload=fresh_preflight.to_dict(),
            )
            store.update_live_order_intent_status(str(ticket["ticket_hash"]), "reprice_blocked_preflight_failed")
            return {"reprice_status": "blocked_preflight_failed", "reprice_message": fresh_preflight.message}
        new_ticket["preflight"] = _preflight_with_entry_pricing(fresh_preflight.to_dict(), new_ticket)
        new_ticket["ticket_hash"] = compute_ticket_hash(new_ticket)
        response = adapter.place_order_ticket(new_ticket)
        ok = bool(response.get("orderId"))
        store.save_live_order_intent(new_ticket, status="submitted" if ok else "submit_failed")
        store.record_live_order_attempt(
            new_ticket,
            action="reprice_submit_open",
            submit=True,
            ok=ok,
            request_payload=dict(new_ticket.get("submit_payload") or {}),
            response_payload=response,
        )
        store.update_live_order_intent_status(str(ticket["ticket_hash"]), "repriced" if ok else "reprice_submit_failed")
        store.event("live_order_repriced", {
            "from_ticket_hash": ticket.get("ticket_hash"),
            "to_ticket_hash": new_ticket.get("ticket_hash"),
            "from_order_id": ticket.get("order_id"),
            "to_order_id": new_ticket.get("order_id"),
            "from_limit_price": ticket.get("limit_price"),
            "to_limit_price": new_ticket.get("limit_price"),
            "broker_status": broker_status.get("status"),
            "attempt": new_ticket.get("reprice_attempt"),
            "ok": ok,
        })
        return {
            "reprice_status": "submitted" if ok else "submit_failed",
            "reprice_ticket_hash": new_ticket.get("ticket_hash"),
            "reprice_order_id": new_ticket.get("order_id"),
            "reprice_limit_price": new_ticket.get("limit_price"),
        }
    except Exception as exc:  # noqa: BLE001
        store.event("live_order_reprice_failed", {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "error": str(exc)})
        return {"reprice_status": "failed", "reprice_message": str(exc)}


def _entry_expire_due(store: LocalStore, ticket: dict[str, Any], broker_status: dict[str, Any], config: dict[str, Any]) -> bool:
    if str(ticket.get("intent_type") or "") != "open":
        return False
    policy = ((config.get("live") or {}).get("entry_reprice") or {})
    if not _as_bool(policy.get("enabled"), False):
        return False
    expire_after = int(policy.get("expire_after_minutes") or 0)
    if expire_after <= 0:
        return False
    age_minutes = _entry_order_age_minutes(store, ticket, broker_status)
    return age_minutes is not None and age_minutes >= expire_after


def _close_expire_due(store: LocalStore, ticket: dict[str, Any], broker_status: dict[str, Any], config: dict[str, Any]) -> bool:
    if str(ticket.get("intent_type") or "") != "close":
        return False
    policy = ((config.get("live") or {}).get("exit_reprice") or {})
    if not _as_bool(policy.get("enabled"), False):
        return False
    expire_after = int(policy.get("expire_after_minutes") or 0)
    if expire_after <= 0:
        return False
    age_minutes = _close_order_age_minutes(store, ticket, broker_status)
    return age_minutes is not None and age_minutes >= expire_after


def _entry_reprice_due(store: LocalStore, ticket: dict[str, Any], broker_status: dict[str, Any], config: dict[str, Any]) -> bool:
    if str(ticket.get("intent_type") or "") != "open":
        return False
    policy = ((config.get("live") or {}).get("entry_reprice") or {})
    if not _as_bool(policy.get("enabled"), False):
        return False
    max_reprices = int(policy.get("max_reprices") or 0)
    if int(ticket.get("reprice_attempt") or 0) >= max_reprices:
        return False
    after_minutes = max(int(policy.get("after_minutes") or 5), 1)
    age_minutes = _active_order_age_minutes(ticket, broker_status)
    if age_minutes is None:
        return False
    return age_minutes >= after_minutes


def _close_reprice_due(store: LocalStore, ticket: dict[str, Any], broker_status: dict[str, Any], config: dict[str, Any]) -> bool:
    if str(ticket.get("intent_type") or "") != "close":
        return False
    policy = ((config.get("live") or {}).get("exit_reprice") or {})
    if not _as_bool(policy.get("enabled"), False):
        return False
    max_reprices = int(policy.get("max_reprices") or 0)
    if int(ticket.get("reprice_attempt") or 0) >= max_reprices:
        return False
    after_minutes = max(int(policy.get("after_minutes") or 10), 1)
    age_minutes = _active_order_age_minutes(ticket, broker_status)
    if age_minutes is None:
        return False
    return age_minutes >= after_minutes


def _repriced_open_ticket(ticket: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    new_ticket = json.loads(json.dumps(ticket, sort_keys=True, default=str))
    attempt = int(ticket.get("reprice_attempt") or 0) + 1
    new_limit = _repriced_limit_price(ticket, config)
    seed = json.dumps(
        {
            "parent_ticket_hash": ticket.get("ticket_hash"),
            "attempt": attempt,
            "limit_price": new_limit,
        },
        sort_keys=True,
    )
    new_order_id = str(uuid5(NAMESPACE_URL, "kamandal-live-reprice:" + seed))
    new_ticket["order_id"] = new_order_id
    new_ticket["limit_price"] = new_limit
    new_ticket["parent_ticket_hash"] = ticket.get("ticket_hash")
    new_ticket["reprice_attempt"] = attempt
    new_ticket["entry_order_started_at"] = ticket.get("entry_order_started_at") or ticket.get("created_at")
    new_ticket["created_at"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    submit_payload = dict(new_ticket.get("submit_payload") or {})
    submit_payload["orderId"] = new_order_id
    submit_payload["limitPrice"] = new_limit
    new_ticket["submit_payload"] = submit_payload
    new_ticket["ticket_hash"] = compute_ticket_hash(new_ticket)
    return new_ticket


def _repriced_close_ticket(ticket: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    new_ticket = json.loads(json.dumps(ticket, sort_keys=True, default=str))
    attempt = int(ticket.get("reprice_attempt") or 0) + 1
    new_limit = _repriced_close_limit_price(ticket, config)
    seed = json.dumps(
        {
            "parent_ticket_hash": ticket.get("ticket_hash"),
            "attempt": attempt,
            "limit_price": new_limit,
        },
        sort_keys=True,
    )
    new_order_id = str(uuid5(NAMESPACE_URL, "kamandal-live-close-reprice:" + seed))
    new_ticket["order_id"] = new_order_id
    new_ticket["limit_price"] = new_limit
    new_ticket["parent_ticket_hash"] = ticket.get("ticket_hash")
    new_ticket["reprice_attempt"] = attempt
    new_ticket["close_order_started_at"] = ticket.get("close_order_started_at") or ticket.get("created_at")
    new_ticket["created_at"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    submit_payload = dict(new_ticket.get("submit_payload") or {})
    submit_payload["orderId"] = new_order_id
    submit_payload["limitPrice"] = new_limit
    new_ticket["submit_payload"] = submit_payload
    new_ticket["ticket_hash"] = compute_ticket_hash(new_ticket)
    return new_ticket


def _repriced_limit_price(ticket: dict[str, Any], config: dict[str, Any]) -> str:
    metadata = (((ticket.get("preflight") or {}).get("raw") or {}).get("entry_pricing") or {})
    current = abs(float(str(ticket.get("limit_price") or "0").replace("-", "")))
    base = float(metadata.get("base_mid_limit") or current)
    improved = float(metadata.get("improved_limit") or current)
    attempt = int(ticket.get("reprice_attempt") or 0) + 1
    multiplier = _reprice_improvement_multiplier(config, attempt)
    multiplier = max(0.0, min(multiplier, 1.0))
    target = base + ((improved - base) * multiplier)
    signed = str(ticket.get("limit_price") or "")
    if signed.startswith("-"):
        return f"-{_round_cent(target):.2f}"
    return f"{_round_cent(target):.2f}"


def _repriced_close_limit_price(ticket: dict[str, Any], config: dict[str, Any]) -> str:
    current = abs(float(str(ticket.get("limit_price") or "0").replace("-", "")))
    natural = _optional_float(ticket.get("exit_natural_limit_price"))
    if natural is None:
        natural = current
    reason = str(ticket.get("exit_reason") or "").lower()
    floor = _optional_float(ticket.get("exit_profit_floor_limit_price"))
    attempt = int(ticket.get("reprice_attempt") or 0) + 1
    if reason in {"max_loss", "pre_event"}:
        target = natural + (0.05 * attempt)
    elif reason == "profit_target" and floor is not None:
        target = min(natural, floor)
    else:
        target = natural
    step = _exit_reprice_step_multiplier(config, attempt)
    target = current + ((target - current) * step)
    if reason == "profit_target" and floor is not None:
        target = min(target, floor)
    return f"{max(_round_cent(target), 0.01):.2f}"


def _exit_reprice_step_multiplier(config: dict[str, Any], attempt: int) -> float:
    policy = ((config.get("live") or {}).get("exit_reprice") or {})
    raw_sequence = policy.get("step_multipliers") or policy.get("improvement_multipliers")
    if isinstance(raw_sequence, list) and raw_sequence:
        index = max(attempt - 1, 0)
        raw = raw_sequence[index] if index < len(raw_sequence) else raw_sequence[-1]
        return max(0.0, min(float(raw), 1.0))
    raw = policy.get("step_multiplier")
    if raw in (None, ""):
        raw = 1.0
    return max(0.0, min(float(raw), 1.0))


def _reprice_improvement_multiplier(config: dict[str, Any], attempt: int) -> float:
    policy = ((config.get("live") or {}).get("entry_reprice") or {})
    raw_sequence = policy.get("improvement_multipliers")
    if isinstance(raw_sequence, list) and raw_sequence:
        index = max(attempt - 1, 0)
        raw = raw_sequence[index] if index < len(raw_sequence) else raw_sequence[-1]
        return float(raw)
    raw_multiplier = policy.get("improvement_multiplier")
    if raw_multiplier in (None, ""):
        raw_multiplier = 0.5
    return float(raw_multiplier)


def _preflight_with_entry_pricing(preflight: dict[str, Any], source_ticket: dict[str, Any]) -> dict[str, Any]:
    raw = dict(preflight.get("raw") or {})
    if "entry_pricing" not in raw:
        prior_entry_pricing = (((source_ticket.get("preflight") or {}).get("raw") or {}).get("entry_pricing") or {})
        if prior_entry_pricing:
            raw["entry_pricing"] = prior_entry_pricing
            preflight = dict(preflight)
            preflight["raw"] = raw
    return preflight


def _entry_order_age_minutes(store: LocalStore, ticket: dict[str, Any], broker_status: dict[str, Any]) -> float | None:
    started = _entry_order_started_at(store, ticket) or _parse_utc(str(broker_status.get("createdAt") or ticket.get("created_at") or ""))
    if started is None:
        return None
    return (datetime.now(UTC) - started).total_seconds() / 60.0


def _close_order_age_minutes(store: LocalStore, ticket: dict[str, Any], broker_status: dict[str, Any]) -> float | None:
    started = _close_order_started_at(store, ticket) or _parse_utc(str(broker_status.get("createdAt") or ticket.get("created_at") or ""))
    if started is None:
        return None
    return (datetime.now(UTC) - started).total_seconds() / 60.0


def _active_order_age_minutes(ticket: dict[str, Any], broker_status: dict[str, Any]) -> float | None:
    created = _parse_utc(str(broker_status.get("createdAt") or ticket.get("created_at") or ""))
    if created is None:
        return None
    return (datetime.now(UTC) - created).total_seconds() / 60.0


def _entry_order_started_at(store: LocalStore, ticket: dict[str, Any]) -> datetime | None:
    explicit = _parse_utc(str(ticket.get("entry_order_started_at") or ""))
    if explicit is not None:
        return explicit
    current = ticket
    seen: set[str] = set()
    while current:
        parent_hash = str(current.get("parent_ticket_hash") or "")
        if not parent_hash or parent_hash in seen:
            break
        seen.add(parent_hash)
        parent = store.live_order_intent(parent_hash)
        if not parent:
            break
        current = parent
    return _parse_utc(str((current or ticket).get("created_at") or ""))


def _close_order_started_at(store: LocalStore, ticket: dict[str, Any]) -> datetime | None:
    explicit = _parse_utc(str(ticket.get("close_order_started_at") or ""))
    if explicit is not None:
        return explicit
    current = ticket
    seen: set[str] = set()
    while current:
        parent_hash = str(current.get("parent_ticket_hash") or "")
        if not parent_hash or parent_hash in seen:
            break
        seen.add(parent_hash)
        parent = store.live_order_intent(parent_hash)
        if not parent:
            break
        current = parent
    return _parse_utc(str((current or ticket).get("created_at") or ""))


def _round_cent(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_utc(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _ticket_age_minutes(ticket: dict[str, Any], *, now: datetime) -> float | None:
    raw = str(ticket.get("_ledger_updated_at") or ticket.get("_ledger_created_at") or ticket.get("updated_at") or ticket.get("created_at") or "")
    parsed = _parse_utc(raw)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def _now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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
        tickets = _tickets_from_row(row, close=action == APPROVE_LIVE_CLOSE)
        progress = [_ticket_progress(store, ticket) for ticket in tickets]
        states = {item["state"] for item in progress}
        statuses = ",".join(item["status"] for item in progress)
        if not progress or "unknown" in states:
            continue
        if "failed" not in states and ("pending" in states or "active" in states):
            continue
        row["operator_action"] = ""
        row["operator_notes"] = f"auto-cleared stale {action}; ledger_statuses={statuses}"
        row["plan_status"] = "filled" if states == {"done"} else "terminal"
        cleared.append({"statuses": statuses, "trade_bundle": row.get("trade_bundle")})
    if cleared:
        client.replace_tab(title, header=DAILY_PLAN_HEADER, rows=[[row.get(column, "") for column in DAILY_PLAN_HEADER] for row in rows])
    retired = _retire_stale_entry_approvals(config, store, rows)
    store.event("live_approval_cleanup_completed", {"cleared": cleared, "retired_stale_entry_approvals": retired})
    return {"cleared": len(cleared), "rows": cleared, "retired_stale_entry_approvals": len(retired), "retired_rows": retired}


def _retire_stale_entry_approvals(config: dict[str, Any], store: LocalStore, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active_hashes = _live_advisory_ticket_hashes(rows, config)
    now = datetime.now(UTC)
    stale_minutes = _stale_entry_approval_minutes(config)
    retired = []
    for ticket in store.live_order_intents_by_type("open", statuses={"pending_approval"}):
        ticket_hash = str(ticket.get("ticket_hash") or "")
        if ticket_hash in active_hashes:
            continue
        age_minutes = _ticket_age_minutes(ticket, now=now)
        if age_minutes is None or age_minutes <= stale_minutes:
            continue
        store.update_live_order_intent_status_with_payload(
            ticket_hash,
            RETIRED_STALE_ENTRY_APPROVAL_STATUS,
            {
                "order_reconciliation": {
                    "status": RETIRED_STALE_ENTRY_APPROVAL_STATUS,
                    "prior_status": "pending_approval",
                    "reason": "stale_entry_approval_not_in_current_daily_plan",
                    "age_minutes": round(age_minutes, 2),
                    "stale_after_minutes": stale_minutes,
                    "reconciled_at": _now_utc(),
                }
            },
        )
        item = {
            "ticket_hash": ticket_hash,
            "order_id": ticket.get("order_id"),
            "underlying": ticket.get("underlying"),
            "structure": ticket.get("structure"),
            "age_minutes": round(age_minutes, 2),
            "status": RETIRED_STALE_ENTRY_APPROVAL_STATUS,
        }
        retired.append(item)
    return retired


def _live_advisory_ticket_hashes(rows: list[dict[str, Any]], config: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    today = _market_today(config)
    for row in rows:
        detail = _loads(row.get("plan_detail_json"))
        if str(detail.get("lane") or row.get("mode") or "") != "live_advisory":
            continue
        if str(row.get("plan_date") or "") != today:
            continue
        ticket = detail.get("order_ticket_json")
        if isinstance(ticket, dict) and ticket.get("ticket_hash"):
            hashes.add(str(ticket["ticket_hash"]))
        tickets = detail.get("order_tickets_json")
        if isinstance(tickets, list):
            for item in tickets:
                if isinstance(item, dict) and item.get("ticket_hash"):
                    hashes.add(str(item["ticket_hash"]))
    return hashes


def _stale_entry_approval_minutes(config: dict[str, Any]) -> int:
    recon = ((config.get("live") or {}).get("reconciliation") or {})
    return int(recon.get("stale_entry_approval_minutes") or DEFAULT_STALE_ENTRY_APPROVAL_MINUTES)


def _market_today(config: dict[str, Any]) -> str:
    market_tz = str((config.get("runtime") or {}).get("market_timezone") or os.environ.get("KAMANDAL_MARKET_TZ") or "America/Chicago")
    return datetime.now(ZoneInfo(market_tz)).date().isoformat()


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


def _tickets_from_row(row: dict[str, Any], *, close: bool) -> list[dict[str, Any]]:
    if close:
        return [_ticket_from_row(row)]
    detail = _loads(row.get("plan_detail_json"))
    tickets = detail.get("order_tickets_json")
    if isinstance(tickets, list) and tickets:
        return [ticket for ticket in tickets if isinstance(ticket, dict)]
    return [_ticket_from_row(row)]


def _tickets_to_execute(
    config: dict[str, Any],
    store: LocalStore,
    row: dict[str, Any],
    *,
    submit: bool,
    close: bool,
) -> tuple[list[dict[str, Any]], str]:
    tickets = _tickets_from_row(row, close=close)
    if close:
        if not tickets:
            return [], "close_ticket_missing"
        progress = _ticket_progress(store, tickets[0])
        status = progress["status"]
        if progress["state"] == "done":
            return [], f"close_ticket_done:{status}"
        if progress["state"] == "active":
            return [], f"close_ticket_active:{status}"
        if progress["state"] == "failed":
            return [], f"close_ticket_failed:{status}"
        if progress["state"] == "pending":
            return tickets[:1], "close_ticket_pending"
        return [], f"close_ticket_unknown:{status}"
    if not submit:
        limit = _ticket_limit(config, submit=submit, close=close)
        return tickets[:limit], "dry_run"
    selected: list[dict[str, Any]] = []
    limit = _ticket_limit(config, submit=submit, close=close)
    for ticket in tickets:
        progress = _ticket_progress(store, ticket)
        status = progress["status"]
        if progress["state"] == "done":
            continue
        if progress["state"] == "failed":
            return [], f"basket_ticket_failed:{status}"
        if progress["state"] == "active":
            return [], f"basket_ticket_active:{status}"
        if progress["state"] == "pending":
            selected.append(ticket)
            if len(selected) >= limit:
                break
            continue
        return [], f"basket_ticket_unknown:{status}"
    if not selected:
        return [], "basket_complete_or_no_pending_tickets"
    return selected, "pending_basket_tickets"


def _ticket_limit(config: dict[str, Any], *, submit: bool, close: bool) -> int:
    if close:
        return 1
    live_cfg = config.get("live") or {}
    if submit:
        return max(int(live_cfg.get("max_live_entry_submits_per_run") or 1), 1)
    return max(int(live_cfg.get("max_orders_per_plan") or live_cfg.get("max_new_positions_per_plan") or 1), 1)


def _ticket_progress(store: LocalStore, ticket: dict[str, Any]) -> dict[str, str]:
    ticket_hash = str(ticket.get("ticket_hash") or "")
    intent = store.live_order_intent(ticket_hash)
    status = str((intent or {}).get("_ledger_status") or "")
    if not status:
        return {"state": "unknown", "status": "missing_intent"}
    if status in COMPLETED_TICKET_STATUSES:
        return {"state": "done", "status": status}
    if status == "repriced":
        children = store.live_order_child_intents(ticket_hash)
        child_states = [_ticket_progress(store, child) for child in children]
        if any(child["state"] == "active" for child in child_states):
            return {"state": "active", "status": "repriced_child_active"}
        if any(child["state"] == "done" for child in child_states):
            return {"state": "done", "status": "repriced_child_done"}
        if any(child["state"] == "failed" for child in child_states):
            return {"state": "failed", "status": "repriced_child_failed"}
        return {"state": "active", "status": status}
    if status in ACTIVE_TICKET_STATUSES:
        return {"state": "active", "status": status}
    if status in PENDING_TICKET_STATUSES:
        return {"state": "pending", "status": status}
    if status in FAILED_TICKET_STATUSES or any(status.startswith(prefix) for prefix in FAILED_TICKET_STATUS_PREFIXES):
        return {"state": "failed", "status": status}
    return {"state": "unknown", "status": status}


def _daily_basket_cap_allows(config: dict[str, Any], store: LocalStore, row: dict[str, Any]) -> bool:
    raw_cap = (config.get("live") or {}).get("max_live_baskets_per_day")
    if raw_cap in (None, ""):
        return True
    cap = int(raw_cap)
    if cap <= 0:
        return False
    current_plan_id = str(row.get("plan_id") or "")
    opened_plan_ids = store.live_entry_plan_ids_since(_market_day_start(config))
    if current_plan_id in opened_plan_ids:
        return True
    return len(opened_plan_ids) < cap


def _market_day_start(config: dict[str, Any]) -> str:
    market_tz = str((config.get("runtime") or {}).get("market_timezone") or os.environ.get("KAMANDAL_MARKET_TZ") or "America/Chicago")
    today = datetime.now(ZoneInfo(market_tz)).date()
    local_start = datetime.combine(today, time.min, tzinfo=ZoneInfo(market_tz))
    return local_start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


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
        "execution_quality": ticket.get("execution_quality") or {},
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
        "execution_quality": ticket.get("execution_quality") or {},
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
        "execution_quality": ticket.get("execution_quality") or {},
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
