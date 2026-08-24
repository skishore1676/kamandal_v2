"""Sheet-gated live order execution and reconciliation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from dataclasses import replace
from datetime import UTC, datetime, time
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from kamandal_v2.live.health import entry_health_gate
from kamandal_v2.live.entry_hygiene import (
    market_today,
    retire_stale_entry_approvals,
    stale_entry_approval_minutes,
)
from kamandal_v2.live.lineage import EntryLineage, resolve_entry_lineage
from kamandal_v2.live.orders import APPROVE_LIVE, APPROVE_LIVE_CLOSE, LIVE_SUBMIT_CONFIRM
from kamandal_v2.live.orders import ticket_hash as compute_ticket_hash
from kamandal_v2.live.option_sessions import submission_window
from kamandal_v2.live.order_identity import broker_order_id, client_order_id, persist_broker_identity
from kamandal_v2.live.plan_fallback import FallbackDecision, PlanFallbackCoordinator, attempt_event_type, fallback_enabled, registered_campaign_ids
from kamandal_v2.market.broker import broker_adapter, ticket_execution_venue
from kamandal_v2.ops.alerts import default_lathi_bus_profile, send_lathi_alert
from kamandal_v2.ops.stage_receipt import reconciliation_stage
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.sheets import GoogleSheetClient, pull_sheet_tables, write_daily_plan
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.models import CsaStage
from kamandal_v2.strategy_lanes.daily_policy import DailyPolicySnapshot, load_daily_policy_snapshot
from kamandal_v2.strategy_lanes.store import CsaStore, strategy_ticket_from_payload
from kamandal_v2.strategy_engine.policy import ExecutionMode, compile_playbook_policies


TERMINAL_UNFILLED_ORDER_STATUSES = {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"}
COMPLETED_TICKET_STATUSES = {
    "filled",
    "filled_via_replacement",
    "partially_filled_terminal",
    "close_filled",
    "manual_fill_recorded",
}
APPROVED_CLOSE_PENDING_SUBMIT = "approved_close_pending_submit"
EXPIRED_EOD_STATUS = "expired_eod"
WAITING_ENTRY_WINDOW = "waiting_entry_window"
PENDING_TICKET_STATUSES = {
    "pending_approval",
    "pending_close_approval",
    APPROVED_CLOSE_PENDING_SUBMIT,
    "stage_approved_pending_submit",
    WAITING_ENTRY_WINDOW,
    "dry_run",
}
ACTIVE_TICKET_STATUSES = {"submitted", "repriced", "partially_filled"}
REPLACE_CANCEL_PENDING = "replace_cancel_pending"
REPLACE_WAITING_CANCEL = "replace_waiting_cancel"
CANCEL_PENDING_TICKET_STATUSES = {"repriced", "expired", REPLACE_CANCEL_PENDING}
LEGACY_REPRICE_TRACKING_STATUSES = {"reprice_blocked_preflight_failed"}
EXPIRED_BROKER_MISSING_STATUS = "expired_broker_status_missing"
FAILED_TICKET_STATUS_PREFIXES = ("blocked_", "reprice_", "submit_failed")
FAILED_TICKET_STATUSES = {
    "rejected",
    "expired",
    EXPIRED_EOD_STATUS,
    "failed",
    "cancelled",
    "canceled",
    "deferred_entry_cutoff",
    "deferred_market_closed",
}
SELECTED_ENTRY_ATTENTION_STATE_EVENT = "live_selected_entry_attention_state"


def _ticket_has_explicit_venue(ticket: dict[str, Any]) -> bool:
    nested = ticket.get("csa_strategy_ticket") or {}
    metadata = nested.get("metadata") or {}
    candidate = ticket.get("candidate") or {}
    return bool(
        ticket.get("execution_venue")
        or candidate.get("execution_venue")
        or metadata.get("execution_venue")
    )


def _broker_for_ticket(config: dict[str, Any], ticket: dict[str, Any], default: Any | None = None) -> Any:
    if not _ticket_has_explicit_venue(ticket):
        return default if default is not None else broker_adapter(config)
    venue = ticket_execution_venue(config, ticket)
    try:
        return broker_adapter(config, execution_venue=venue)
    except TypeError:
        # Compatibility for small injected adapters used by tests and local
        # diagnostics; production broker_adapter accepts the venue keyword.
        return broker_adapter(config)


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
        # Typed lifecycle management is already authorized and persisted by
        # the unified manager.  Consume it before consulting the Sheet-backed
        # legacy close queue so an open lifecycle can always be closed or
        # adjusted from its frozen policy, even when the operator Sheet is
        # unavailable.  Adjustments must retain their own action class rather
        # than inheriting close-window permission from this CLI entrypoint.
        lifecycle_tickets = _staged_lifecycle_management_tickets(store, config)
        if lifecycle_tickets:
            _assert_submit_allowed(config, submit=submit)
            adapter = broker_adapter(config)
            results = []
            for ticket in lifecycle_tickets:
                authorized, reason = _lifecycle_management_authorization(ticket, config=config, store=store)
                if not authorized:
                    store.event(
                        "stage_authorized_lifecycle_management_blocked",
                        {
                            "ticket_hash": ticket.get("ticket_hash"),
                            "lifecycle_id": ticket.get("csa_lifecycle_id"),
                            "reason": reason,
                        },
                    )
                    results.append({"status": "blocked", "reason": reason, "ticket_hash": ticket.get("ticket_hash")})
                    continue
                results.append(
                    _execute_ticket(
                        config,
                        adapter,
                        store,
                        ticket,
                        submit=submit,
                        close=str(ticket.get("intent_type") or "") == "close",
                    )
                )
            return {
                "action": action,
                "submit": submit,
                "processed": len(results),
                "results": results,
                "source": "frozen_lifecycle_ledger",
                "management": True,
            }
        promoted = _promote_sheet_approved_closes_to_ledger(config, store)
        promoted += _promote_legacy_auto_rule_closes_to_ledger(config, store)
        tickets = _ledger_approved_close_tickets(store, config)
        if not tickets:
            return {"action": action, "submit": submit, "processed": 0, "results": [], "source": "ledger", "promoted": promoted}
        _assert_submit_allowed(config, submit=submit)
        adapter = broker_adapter(config)
        results = [_execute_ticket(config, adapter, store, ticket, submit=submit, close=True) for ticket in tickets]
        return {"action": action, "submit": submit, "processed": len(results), "results": results, "source": "ledger", "promoted": promoted}

    sheet_tables = pull_sheet_tables(config)
    rows = _approved_rows(config, close=close, tables=sheet_tables)
    if not rows and not close:
        staged = sorted(
            store.live_order_intents_by_status({"stage_approved_pending_submit", WAITING_ENTRY_WINDOW}),
            key=lambda ticket: (
                0 if str(ticket.get("intent_type") or "") in {"close", "adjust"} else 1,
                str(ticket.get("created_at") or ""),
                str(ticket.get("ticket_hash") or ""),
            ),
        )
        if staged:
            ticket = staged[0]
            is_management = _is_lifecycle_management_ticket(ticket)
            if is_management:
                authorized, authorization_reason = _lifecycle_management_authorization(ticket, config=config, store=store)
            else:
                try:
                    daily_policy = load_daily_policy_snapshot(config)
                except (FileNotFoundError, ValueError) as exc:
                    authorized, authorization_reason = False, f"blocked_daily_policy_snapshot:{type(exc).__name__}"
                else:
                    authorized, authorization_reason = _stage_ticket_authorization(ticket, daily_policy)
            if not authorized:
                store.event(
                    "stage_authorized_live_entry_blocked",
                    {
                        "ticket_hash": ticket.get("ticket_hash"),
                        "playbook_id": ticket.get("csa_playbook_id"),
                        "reason": authorization_reason,
                    },
                )
                return {
                    "action": action,
                    "submit": submit,
                    "processed": 1,
                    "results": [{"status": "blocked", "reason": authorization_reason}],
                    "source": "stage_authorized_ledger",
                }
            _assert_submit_allowed(config, submit=submit)
            if is_management:
                adapter = broker_adapter(config)
                result = _execute_ticket(
                    config,
                    adapter,
                    store,
                    ticket,
                    submit=submit,
                    close=str(ticket.get("intent_type") or "") == "close",
                )
                return {
                    "action": action,
                    "submit": submit,
                    "processed": 1,
                    "results": [result],
                    "source": "stage_authorized_ledger",
                    "management": True,
                }
            gate = entry_health_gate(store, config)
            if submit and gate["blocked"]:
                return {
                    "action": action,
                    "submit": submit,
                    "processed": 1,
                    "results": [{"status": "blocked", "reason": "blocked_live_health_red:" + ",".join(gate["reasons"])}],
                    "source": "stage_authorized_ledger",
                    "health_gate": gate,
                }
            risk_manager = gate.get("risk_manager") or {}
            symbol = str(ticket.get("underlying") or "").upper()
            underlying_cap = int((risk_manager.get("underlyings_at_cap") or {}).get(symbol) or 0)
            cluster_cap = next(
                (
                    str(cluster)
                    for cluster, symbols in (risk_manager.get("clusters_at_cap") or {}).items()
                    if symbol in {str(item).upper() for item in symbols}
                ),
                "",
            )
            if submit and (underlying_cap or cluster_cap):
                reason = "blocked_risk_underlying_cap" if underlying_cap else f"blocked_risk_cluster_cap:{cluster_cap}"
                return {
                    "action": action,
                    "submit": submit,
                    "processed": 1,
                    "results": [{"status": "blocked", "reason": reason, "underlying": symbol}],
                    "source": "stage_authorized_ledger",
                    "health_gate": gate,
                }
            adapter = broker_adapter(config)
            result = _execute_ticket(config, adapter, store, ticket, submit=submit, close=False)
            return {
                "action": action,
                "submit": submit,
                "processed": 1,
                "results": [result],
                "source": "stage_authorized_ledger",
                "health_gate": gate,
            }
    if not rows:
        return {"action": action, "submit": submit, "processed": 0, "results": []}
    _assert_submit_allowed(config, submit=submit)
    cluster_capped: dict[str, str] = {}
    underlying_capped: dict[str, int] = {}
    if submit and not close:
        gate = entry_health_gate(store, config)
        risk_manager = gate.get("risk_manager") or {}
        if bool(risk_manager.get("enabled")):
            store.event("risk_manager_entry_gate_decision", {"gate": gate, "risk_manager": risk_manager})
        if gate["blocked"]:
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
        underlying_capped = {
            str(symbol).upper(): int(count)
            for symbol, count in ((gate.get("risk_manager") or {}).get("underlyings_at_cap") or {}).items()
        }
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
            capped_underlying = underlying_capped.get(str(ticket.get("underlying") or "").upper())
            if capped_underlying and submit and not close:
                store.event(
                    "live_entry_blocked_by_underlying_cap",
                    {
                        "ticket_hash": ticket.get("ticket_hash"),
                        "underlying": ticket.get("underlying"),
                        "open_positions": capped_underlying,
                    },
                )
                results.append(
                    {
                        "status": "blocked",
                        "reason": "blocked_risk_underlying_cap",
                        "underlying": ticket.get("underlying"),
                        "ticket_hash": ticket.get("ticket_hash"),
                    }
                )
                continue
            capped_cluster = cluster_capped.get(str(ticket.get("underlying") or "").upper())
            if capped_cluster and submit and not close:
                store.event("live_entry_blocked_by_cluster_cap", {"ticket_hash": ticket.get("ticket_hash"), "underlying": ticket.get("underlying"), "cluster": capped_cluster})
                results.append({"status": "blocked", "reason": f"blocked_risk_cluster_cap:{capped_cluster}", "underlying": ticket.get("underlying"), "ticket_hash": ticket.get("ticket_hash")})
                continue
            results.append(_execute_ticket(config, adapter, store, ticket, submit=submit, close=close))
    return {"action": action, "submit": submit, "processed": len(results), "results": results}


def execute_live_approved_with_recovery(
    config: dict[str, Any],
    *,
    submit: bool = False,
    recovery_idea_paths: list[str | os.PathLike[str]] | None = None,
    config_source: str = "sheet",
    provider: str = "public",
    store: LocalStore | None = None,
) -> dict[str, Any]:
    """Execute one selected entry, rebuilding a stale ticket at most once."""

    store = store or LocalStore()
    initial = execute_live_approved(config, submit=submit, store=store)
    final = initial
    recovery: dict[str, Any] = {"attempted": False}
    policy = ((config.get("live") or {}).get("stale_entry_recovery") or {})
    stale = _stale_selected_entry_failure(initial)
    recovery_enabled = _as_bool(policy.get("enabled"), True)
    max_rebuilds = max(int(policy.get("max_rebuilds_per_execution") or 1), 0)

    if submit and stale and recovery_enabled and max_rebuilds > 0:
        from kamandal_v2.live.advisory import run_live_advisory_plan

        store.event(
            "live_stale_selected_entry_rebuild_started",
            {
                "ticket_hash": stale.get("ticket_hash"),
                "underlying": stale.get("underlying"),
                "max_rebuilds": min(max_rebuilds, 1),
            },
        )
        try:
            advisory = run_live_advisory_plan(
                config,
                idea_paths=list(recovery_idea_paths or []),
                config_source=config_source,
                provider=provider,
                write_sheet=True,
                persist_order_intents=True,
                notify_unplaced_selected=False,
                store=store,
            )
            recovery = {
                "attempted": True,
                "rebuilds": 1,
                "plan_run_id": advisory.plan_run_id,
                "plans": len(advisory.plans),
                "candidates": len(advisory.candidates),
            }
        except Exception as exc:  # noqa: BLE001
            recovery = {
                "attempted": True,
                "rebuilds": 1,
                "outcome": "stale_rebuild_failed",
                "error": _safe_broker_error(exc),
            }
            final = {
                "action": APPROVE_LIVE,
                "submit": submit,
                "processed": 1,
                "results": [
                    {
                        "status": "blocked",
                        "reason": "stale_rebuild_failed",
                        "failure_code": "stale_rebuild_failed",
                        "ticket_hash": stale.get("ticket_hash"),
                        "underlying": stale.get("underlying"),
                    }
                ],
            }
        else:
            if advisory.daily_plan_rows:
                final = execute_live_approved(config, submit=submit, store=store)
            else:
                final = {
                    "action": APPROVE_LIVE,
                    "submit": submit,
                    "processed": 1,
                    "results": [
                        {
                            "status": "blocked",
                            "reason": "stale_rebuild_no_eligible_current_rank1",
                            "failure_code": "stale_rebuild_no_eligible_current_rank1",
                            "ticket_hash": stale.get("ticket_hash"),
                            "underlying": stale.get("underlying"),
                        }
                    ],
                }
            recovery["outcome"] = _execution_outcome(final)
        store.event(
            "live_stale_selected_entry_rebuild_completed",
            {
                **recovery,
                "original_ticket_hash": stale.get("ticket_hash"),
                "original_underlying": stale.get("underlying"),
            },
        )

    notification = _notify_selected_entry_failure(
        config,
        store,
        final,
        recovery=recovery,
        submit=submit,
    )
    return {
        **final,
        "initial_execution": initial,
        "recovery": recovery,
        "operator_notification": notification,
    }


def _execute_ticket(
    config: dict[str, Any],
    adapter: Any,
    store: LocalStore,
    ticket: dict[str, Any],
    *,
    submit: bool,
    close: bool,
) -> dict[str, Any]:
    # The venue is frozen into the ticket before it reaches this money boundary.
    # Never let the process-wide default silently reroute a persisted order.
    if _ticket_has_explicit_venue(ticket):
        adapter = _broker_for_ticket(config, ticket, adapter)
    intent = store.live_order_intent(str(ticket.get("ticket_hash") or ""))
    if not intent:
        return _failure(ticket, "ticket_not_found_in_live_ledger")
    if intent.get("ticket_hash") != ticket.get("ticket_hash"):
        return _failure(ticket, "ticket_hash_mismatch")
    ledger_status = str(intent.get("_ledger_status") or "")
    allowed_statuses = {"dry_run", "pending_approval", "stage_approved_pending_submit", WAITING_ENTRY_WINDOW}
    if close:
        allowed_statuses = {
            "dry_run",
            "pending_close_approval",
            APPROVED_CLOSE_PENDING_SUBMIT,
            "stage_approved_pending_submit",
        }
    if ledger_status and ledger_status not in allowed_statuses:
        return _failure(ticket, f"ticket_already_{ledger_status}")
    if close and not ticket.get("csa_lifecycle_id") and _same_day_close_blocked(config, store, ticket):
        store.update_live_order_intent_status(str(ticket["ticket_hash"]), "blocked_same_day_close")
        return _failure(ticket, "same_day_live_exit_blocked")
    request_payload = dict(ticket.get("submit_payload") or {})
    if submit:
        window = submission_window(config, ticket, close=close)
        if not window["allowed"]:
            return _defer_ticket_for_window(store, ticket, window)
        if not _ticket_fresh(config, ticket):
            store.update_live_order_intent_status(str(ticket["ticket_hash"]), "blocked_preflight_stale")
            return _failure(ticket, "ticket_preflight_stale", failure_code="ticket_preflight_stale")
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
            return _failure(
                ticket,
                fresh_preflight.message or "fresh_preflight_failed",
                failure_code="fresh_preflight_failed",
            )
        window = submission_window(config, ticket, close=close)
        if not window["allowed"]:
            return _defer_ticket_for_window(store, ticket, window)
        try:
            response = adapter.place_order_ticket(ticket)
            ok = bool(response.get("orderId"))
            status = "submitted" if ok else "submit_failed"
            if ok:
                persist_broker_identity(ticket, response)
        except Exception as exc:  # noqa: BLE001
            response = {"error": _safe_broker_error(exc)}
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
    store.update_live_order_intent_status_with_payload(
        str(ticket["ticket_hash"]),
        status,
        {
            "client_order_id": client_order_id(ticket),
            **({"broker_order_id": broker_order_id(ticket)} if submit and ok else {}),
        },
    )
    store.event("live_order_execution_evaluated", {
        "ticket_hash": ticket.get("ticket_hash"),
        "order_id": ticket.get("order_id"),
        "client_order_id": client_order_id(ticket),
        "broker_order_id": broker_order_id(ticket) if submit and ok else "",
        "submit": submit,
        "close": close,
        "status": status,
        "execution_venue": ticket_execution_venue(config, ticket),
    })
    result = {
        "ticket_hash": ticket.get("ticket_hash"),
        "order_id": ticket.get("order_id"),
        "client_order_id": client_order_id(ticket),
        "broker_order_id": broker_order_id(ticket) if submit and ok else "",
        "underlying": ticket.get("underlying"),
        "status": status,
        "execution_venue": ticket_execution_venue(config, ticket),
        "response": response,
    }
    if status == "submit_failed":
        result["failure_code"] = "submit_failed"
    return result


def sync_live_orders(config: dict[str, Any], *, store: LocalStore | None = None, manage_entries: bool = True) -> dict[str, Any]:
    """Serialize broker order reconciliation across Kamandal launchd jobs."""

    with reconciliation_stage("sync_live_orders"):
        store = store or LocalStore()
        lock_path = store.sqlite_path.parent / "runlocks" / "live_order_sync.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                result = _sync_live_orders_locked(config, store=store, manage_entries=manage_entries)
                if manage_entries and fallback_enabled(config):
                    result["plan_fallback"] = _advance_plan_fallbacks(config, store)
                return result
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _sync_live_orders_locked(config: dict[str, Any], *, store: LocalStore, manage_entries: bool) -> dict[str, Any]:
    default_adapter = broker_adapter(config)
    tickets = store.live_order_intents_by_status(
        {"submitted", "partially_filled", *CANCEL_PENDING_TICKET_STATUSES, *LEGACY_REPRICE_TRACKING_STATUSES}
    )
    known_hashes = {str(ticket.get("ticket_hash") or "") for ticket in tickets}
    for child in store.live_order_intents_by_status({REPLACE_WAITING_CANCEL}):
        parent_hash = str(child.get("parent_ticket_hash") or "")
        if not parent_hash or parent_hash in known_hashes:
            continue
        parent = store.live_order_intent(parent_hash)
        if not parent:
            continue
        parent["_staged_replacement_recovery"] = True
        tickets.append(parent)
        known_hashes.add(parent_hash)
    tickets = sorted(
        tickets,
        key=lambda item: (0 if str(item.get("_ledger_status") or "") == "submitted" else 1, str(item.get("created_at") or "")),
    )
    results = []
    for ticket in tickets:
        adapter = (
            _broker_for_ticket(config, ticket, default_adapter)
            if _ticket_has_explicit_venue(ticket)
            else default_adapter
        )
        ledger_status = str(ticket.get("_ledger_status") or "submitted")
        try:
            response = adapter.get_order(broker_order_id(ticket))
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
        should_manage_submitted = manage_entries and ledger_status in {"submitted", *LEGACY_REPRICE_TRACKING_STATUSES}
        store.record_live_order_status(str(ticket["order_id"]), status, response, ticket_hash=str(ticket["ticket_hash"]))
        if ledger_status == REPLACE_CANCEL_PENDING or bool(ticket.get("_staged_replacement_recovery")):
            if manage_entries:
                replacement_result = _advance_staged_replacement(adapter, store, ticket, response)
                results.append(
                    {
                        "ticket_hash": ticket["ticket_hash"],
                        "order_id": ticket["order_id"],
                        "status": status,
                        **replacement_result,
                    }
                )
            else:
                results.append(
                    {
                        "ticket_hash": ticket["ticket_hash"],
                        "order_id": ticket["order_id"],
                        "status": status,
                        "ledger_status": ledger_status,
                        "cancel_pending": True,
                        "entry_management_skipped": True,
                    }
                )
            continue
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
        position_projection = None
        if status in {"FILLED", "PARTIALLY_FILLED"} and ticket.get("intent_type") == "open":
            position_projection = _save_live_position_from_ticket(store, ticket, status=status.lower(), order_status=response)
        if (
            status in {"FILLED", "PARTIALLY_FILLED"}
            and ticket.get("intent_type") == "close"
            and not ticket.get("csa_lifecycle_id")
        ):
            store.update_live_order_intent_status(str(ticket["ticket_hash"]), "close_filled")
        csa_lifecycle_projection = None
        if status == "FILLED" and ticket.get("csa_lifecycle_id"):
            csa_lifecycle_projection = _adopt_csa_live_fill(
                store,
                ticket,
                response,
                position_projection=position_projection,
                filled_quantity=_filled_quantity(response),
            )
        activity_receipt = None
        terminal_fill_quantity = _filled_quantity(response)
        filled_descendant = _filled_replacement_descendant(store, ticket) if intent_type == "open" else None
        if status in TERMINAL_UNFILLED_ORDER_STATUSES and filled_descendant is not None:
            store.update_live_order_intent_status_with_payload(
                str(ticket["ticket_hash"]),
                "filled_via_replacement",
                {
                    "filled_by_ticket_hash": filled_descendant.get("ticket_hash"),
                    "position_group_id": f"live_group_{_lineage_root_hash(store, ticket)}",
                },
            )
            results.append(
                {
                    "ticket_hash": ticket["ticket_hash"],
                    "order_id": ticket["order_id"],
                    "status": status,
                    "ledger_status": ledger_status,
                    "reconciled_status": "filled_via_replacement",
                    "filled_by_ticket_hash": filled_descendant.get("ticket_hash"),
                }
            )
            continue
        if status in TERMINAL_UNFILLED_ORDER_STATUSES and intent_type == "open" and terminal_fill_quantity > 0:
            position_projection = _save_live_position_from_ticket(
                store,
                ticket,
                status="partially_filled_terminal",
                order_status=response,
            )
            if ticket.get("csa_lifecycle_id"):
                csa_lifecycle_projection = _adopt_csa_live_fill(
                    store,
                    ticket,
                    response,
                    position_projection=position_projection,
                    filled_quantity=terminal_fill_quantity,
                    adopted_ticket_status="partially_filled_terminal",
                )
            result = {
                "ticket_hash": ticket["ticket_hash"],
                "order_id": ticket["order_id"],
                "status": status,
                "ledger_status": ledger_status,
                "partial_fill_preserved": True,
                "filled_quantity": terminal_fill_quantity,
                "position_projection": position_projection,
                "csa_lifecycle_projection": csa_lifecycle_projection,
            }
            results.append(result)
            continue
        if status in TERMINAL_UNFILLED_ORDER_STATUSES:
            normalized_status = status.lower().replace("canceled", "cancelled")
            transitioned = store.transition_live_order_intent_status(
                str(ticket["ticket_hash"]),
                expected_statuses={ledger_status},
                status=normalized_status,
            )
            if transitioned and intent_type == "open" and ledger_status != "repriced":
                activity_receipt = _send_terminal_entry_receipt(
                    config,
                    store,
                    ticket,
                    broker_status=status,
                )
        result = {"ticket_hash": ticket["ticket_hash"], "order_id": ticket["order_id"], "status": status, "ledger_status": ledger_status}
        if position_projection is not None:
            result["position_projection"] = position_projection
        if csa_lifecycle_projection is not None:
            result["csa_lifecycle_projection"] = csa_lifecycle_projection
        if activity_receipt is not None:
            result["activity_receipt"] = activity_receipt
        if ledger_status in CANCEL_PENDING_TICKET_STATUSES and status in {"NEW", "OPEN", "WORKING"}:
            result["cancel_pending"] = True
            result["needs_broker_cancel_review"] = True
        if not manage_entries and ledger_status == "submitted" and status in {"NEW", "OPEN", "WORKING"}:
            result["entry_management_skipped"] = True
        results.append(result)
    return {"synced": len(results), "manage_entries": manage_entries, "orders": results}


def _send_terminal_entry_receipt(
    config: dict[str, Any],
    store: LocalStore,
    ticket: dict[str, Any],
    *,
    broker_status: str,
) -> dict[str, Any] | None:
    policy = (((config.get("live") or {}).get("entry_reprice") or {}).get("terminal_unfilled_receipt") or {})
    if not _as_bool(policy.get("enabled"), False):
        return None

    lineage = _entry_ticket_lineage(store, ticket)
    body = render_terminal_entry_receipt(lineage, broker_status=broker_status)
    underlying = str(ticket.get("underlying") or "entry").upper()
    mode = str(policy.get("mode") or "live").strip().lower()
    if mode not in {"off", "spool", "live"}:
        mode = "live"
    alert = send_lathi_alert(
        title=f"Kamandal entry attempt completed: {underlying} unfilled",
        body=body,
        level="info",
        mode=mode,  # type: ignore[arg-type]
        profile=str(policy.get("profile") or default_lathi_bus_profile()),
    )
    receipt = {
        "attempted": alert.attempted,
        "ok": alert.ok,
        "mode": alert.mode,
        "ticket_hash": ticket.get("ticket_hash"),
        "underlying": underlying,
        "broker_status": broker_status,
        "attempt_count": len(lineage),
    }
    store.event("live_order_terminal_entry_receipt", {**receipt, "delivery": alert.to_dict()})
    return receipt


def render_terminal_entry_receipt(lineage: list[dict[str, Any]], *, broker_status: str) -> str:
    ticket = lineage[-1] if lineage else {}
    underlying = str(ticket.get("underlying") or "UNKNOWN").upper()
    structure = str(ticket.get("structure") or "entry").replace("_", " ")
    limits = " -> ".join(_display_entry_limit(item.get("limit_price")) for item in lineage)
    expiration = next(
        (str(leg.get("expiration")) for leg in (ticket.get("legs") or []) if leg.get("expiration")),
        "unknown",
    )
    return "\n".join(
        [
            f"{underlying} {structure} was attempted but not filled.",
            f"Outcome: {broker_status.lower().replace('_', ' ')}; no live position was opened.",
            f"Broker attempts: {len(lineage)} ({max(len(lineage) - 1, 0)} reprices).",
            f"Limit path: {limits or 'unknown'}.",
            f"Expiration: {expiration}.",
        ]
    )


def _entry_ticket_lineage(store: LocalStore, ticket: dict[str, Any]) -> list[dict[str, Any]]:
    lineage = [ticket]
    current = ticket
    seen = {str(ticket.get("ticket_hash") or "")}
    while current:
        parent_hash = str(current.get("parent_ticket_hash") or "")
        if not parent_hash or parent_hash in seen:
            break
        seen.add(parent_hash)
        parent = store.live_order_intent(parent_hash)
        if not parent:
            break
        lineage.append(parent)
        current = parent
    lineage.reverse()
    return lineage


def _advance_plan_fallbacks(config: dict[str, Any], store: LocalStore) -> list[dict[str, Any]]:
    coordinator = PlanFallbackCoordinator(store, config)
    decisions: list[dict[str, Any]] = []
    for campaign_id in registered_campaign_ids(store):
        decision = coordinator.advance(
            campaign_id,
            replan=lambda context: _fresh_fallback_replan(config, store, context),
        )
        decision_payload = decision.to_dict()
        policy = ((config.get("live") or {}).get("plan_fallback") or {})
        if decision.status == "fallback_ready" and bool(policy.get("auto_submit", True)):
            if not _fallback_basket_cap_allows(config, store, campaign_id, decision.plan_id):
                decisions.append(_blocked_fallback_decision(config, store, decision, decision_payload, "max_live_baskets_per_day_reached"))
                continue
            gate = entry_health_gate(store, config)
            if gate.get("blocked"):
                reason = "blocked_live_health_red:" + ",".join(gate.get("reasons") or [])
                decisions.append(_blocked_fallback_decision(config, store, decision, decision_payload, reason))
                continue
            tickets = [
                ticket
                for ticket_hash in decision.ticket_hashes
                if (ticket := store.live_order_intent(ticket_hash)) is not None
            ]
            if len(tickets) != len(decision.ticket_hashes):
                decisions.append(_blocked_fallback_decision(config, store, decision, decision_payload, "blocked_fallback_tickets_missing"))
                continue
            submission_gate = _fallback_submission_gate(config, tickets, gate=gate)
            if submission_gate:
                decisions.append(_blocked_fallback_decision(config, store, decision, decision_payload, submission_gate))
                continue
            projection = _project_fallback_daily_plan(config, store, decision)
            decision_payload["sheet_projection"] = projection
            if not projection.get("ok"):
                decisions.append(
                    _blocked_fallback_decision(
                        config,
                        store,
                        decision,
                        decision_payload,
                        "blocked_fallback_sheet_projection:" + str(projection.get("reason") or "unknown"),
                        project=False,
                    )
                )
                continue
            adapter = broker_adapter(config)
            submission_results = []
            submit_limit = _ticket_limit(config, submit=True, close=False)
            for ticket_hash in decision.ticket_hashes[:submit_limit]:
                ticket = store.live_order_intent(ticket_hash)
                status = str((ticket or {}).get("_ledger_status") or "")
                if not ticket or status not in PENDING_TICKET_STATUSES:
                    continue
                symbol = str(ticket.get("underlying") or "").upper()
                risk_manager = gate.get("risk_manager") or {}
                if symbol in {str(item).upper() for item in (risk_manager.get("underlyings_at_cap") or {})}:
                    submission_results.append({"status": "blocked", "ticket_hash": ticket_hash, "reason": "blocked_risk_underlying_cap"})
                    continue
                try:
                    submission_results.append(_execute_ticket(config, adapter, store, ticket, submit=True, close=False))
                except Exception as exc:  # noqa: BLE001
                    submission_results.append({"status": "blocked", "ticket_hash": ticket_hash, "reason": _safe_broker_error(exc)})
            if submission_results:
                coordinator.mark_submitted(decision, submission_results)
                decision_payload["submission_results"] = submission_results
        decisions.append(decision_payload)
    return decisions


def _blocked_fallback_decision(
    config: dict[str, Any],
    store: LocalStore,
    decision: FallbackDecision,
    payload: dict[str, Any],
    reason: str,
    *,
    project: bool = True,
) -> dict[str, Any]:
    payload["submission_blocked"] = reason
    if project:
        payload["sheet_projection"] = _project_fallback_daily_plan(config, store, decision, blocked_reason=reason)
    store.event(
        "live_plan_fallback_blocked",
        {"campaign_id": decision.campaign_id, "reason": reason, "plan_id": decision.plan_id},
    )
    return payload


def _project_fallback_daily_plan(
    config: dict[str, Any],
    store: LocalStore,
    decision: FallbackDecision,
    *,
    blocked_reason: str = "",
) -> dict[str, Any]:
    """Make the current fallback portfolio visible before any broker effect."""

    if not decision.daily_plan_rows:
        return {"ok": False, "reason": "daily_plan_rows_missing", "rows": 0}
    rows: list[list[Any]] = []
    for raw_row in decision.daily_plan_rows:
        row = dict(zip(DAILY_PLAN_HEADER, raw_row, strict=False))
        detail = _loads(row.get("plan_detail_json"))
        detail["fallback_attempt"] = decision.attempt
        detail["fallback_campaign_id"] = decision.campaign_id
        detail["fallback_parent_attempt_id"] = decision.campaign_id
        detail["fallback_reason"] = decision.reason
        detail["fallback_submission_blocked"] = blocked_reason
        row["plan_detail_json"] = json.dumps(detail, sort_keys=True)
        row["mode"] = "live_advisory"
        is_selected = str(row.get("plan_id") or "") == decision.plan_id
        row["operator_action"] = APPROVE_LIVE if is_selected and not blocked_reason else ""
        if is_selected:
            row["plan_status"] = "blocked" if blocked_reason else "eligible"
            row["operator_notes"] = (
                f"automatic Plan {decision.attempt} after {decision.reason}"
                + (f"; blocked={blocked_reason}" if blocked_reason else "")
            )
        rows.append([row.get(column, "") for column in DAILY_PLAN_HEADER])
    try:
        written = write_daily_plan(config, rows, DAILY_PLAN_HEADER, replace_lanes={"live_advisory"})
    except Exception as exc:  # noqa: BLE001 - projection failure must block the live fallback.
        reason = f"{type(exc).__name__}:{_safe_broker_error(exc)}"
        store.event(
            "live_plan_fallback_sheet_projection_failed",
            {"campaign_id": decision.campaign_id, "plan_id": decision.plan_id, "reason": reason},
        )
        return {"ok": False, "reason": reason, "rows": 0}
    receipt = {
        "ok": True,
        "campaign_id": decision.campaign_id,
        "plan_id": decision.plan_id,
        "attempt": decision.attempt,
        "blocked_reason": blocked_reason,
        "rows": written,
    }
    store.event("live_plan_fallback_sheet_projected", receipt)
    return receipt


def _fresh_fallback_replan(config: dict[str, Any], store: LocalStore, context: dict[str, Any]) -> dict[str, Any] | None:
    """Re-enter the active unified planner with current portfolio truth."""

    from kamandal_v2.strategy_engine.planning import run_unified_fallback_plan

    idea_paths = [path for path in context.get("idea_paths") or [] if path]
    if not idea_paths:
        store.event("live_unified_fallback_replan_blocked", {"campaign_id": context.get("campaign_id"), "reason": "idea_paths_missing"})
        return None
    try:
        unified = run_unified_fallback_plan(
            config,
            store=store,
            idea_paths=idea_paths,
            provider=str(context.get("provider") or "public"),
            exclude_candidate_ids=set(str(item) for item in context.get("attempted_candidate_ids") or []),
            exclude_contract_keys=set(str(item) for item in context.get("attempted_contract_keys") or []),
            expected_policy_snapshot=dict(context.get("daily_policy_snapshot") or {}),
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        store.event(
            "live_unified_fallback_replan_blocked",
            {"campaign_id": context.get("campaign_id"), "reason": f"{type(exc).__name__}: {exc}"},
        )
        return None
    book = unified.live
    if book.result is None or book.errors or not book.result.plans or not book.result.plans[0].candidates:
        store.event(
            "live_unified_fallback_replan_blocked",
            {
                "campaign_id": context.get("campaign_id"),
                "reason": "unified_live_book_unavailable",
                "errors": list(book.errors),
                "has_result": book.result is not None,
            },
        )
        return None
    plan = book.result.plans[0]
    candidate_ids = {candidate.candidate_id for candidate in plan.candidates}
    tickets = [
        dict(ticket)
        for ticket in store.live_order_intents_by_type("open", statuses=PENDING_TICKET_STATUSES)
        if str(ticket.get("plan_id") or "") == plan.plan_id
        and str(ticket.get("candidate_id") or "") in candidate_ids
    ]
    if {str(ticket.get("candidate_id") or "") for ticket in tickets} != candidate_ids:
        store.event(
            "live_unified_fallback_replan_blocked",
            {"campaign_id": context.get("campaign_id"), "reason": "unified_ticket_handoff_missing", "candidate_ids": sorted(candidate_ids), "ticket_ids": sorted(str(ticket.get("candidate_id") or "") for ticket in tickets)},
        )
        return None
    fresh_session = all(submission_window(config, ticket, close=False).get("allowed") is True for ticket in tickets)
    fresh_quotes = all(
        float(leg.get("bid") or 0.0) >= 0 and float(leg.get("ask") or 0.0) > 0 and float(leg.get("ask") or 0.0) >= float(leg.get("bid") or 0.0)
        for candidate in plan.candidates
        for leg in candidate.to_dict().get("legs") or []
    )
    broker_preflight_valid = all(candidate.preflight is not None and candidate.preflight.ok for candidate in plan.candidates)
    unified_lifecycle_handoff_valid = all(
        bool(ticket.get("csa_lifecycle_id"))
        and bool(ticket.get("csa_compiled_policy_hash"))
        and bool(ticket.get("stage_authorized"))
        and bool(ticket.get("csa_policy_snapshot_hash"))
        and bool(ticket.get("csa_policy_snapshot_date"))
        for ticket in tickets
    )
    validation = {
        "fresh_session": fresh_session,
        "fresh_quotes": fresh_quotes,
        "risk_valid": all(not candidate.rejection_reason for candidate in plan.candidates),
        "bpr_valid": plan.total_bpr > 0 and plan.buying_power_after >= 0,
        "concentration_valid": not plan.blocked_by,
        "overlap_valid": all(candidate.eligible for candidate in plan.candidates),
        "broker_preflight_valid": broker_preflight_valid,
        "unified_lifecycle_handoff": unified_lifecycle_handoff_valid,
    }
    return {
        "plan_id": plan.plan_id,
        "candidate_ids": [candidate.candidate_id for candidate in plan.candidates],
        "tickets": tickets,
        "validation": validation,
        "daily_plan_rows": [list(row) for row in book.result.daily_plan_rows],
    }


def _fallback_submission_gate(
    config: dict[str, Any],
    tickets: list[dict[str, Any]],
    *,
    gate: dict[str, Any],
) -> str:
    """Apply the canonical money and stage gates before inline Plan-2 submit."""

    try:
        _assert_submit_allowed(config, submit=True)
    except RuntimeError as exc:
        return "blocked_live_submit_gate:" + str(exc)
    try:
        daily_policy = load_daily_policy_snapshot(config)
    except (FileNotFoundError, ValueError) as exc:
        return f"blocked_daily_policy_snapshot:{type(exc).__name__}"
    if not tickets:
        return "blocked_fallback_tickets_missing"
    for ticket in tickets:
        authorized, reason = _stage_ticket_authorization(ticket, daily_policy)
        if not authorized:
            return reason
    risk_manager = gate.get("risk_manager") or {}
    underlyings = {str(ticket.get("underlying") or "").upper() for ticket in tickets}
    at_cap = {str(symbol).upper() for symbol in (risk_manager.get("underlyings_at_cap") or {})}
    blocked_underlying = sorted(underlyings & at_cap)
    if blocked_underlying:
        return "blocked_risk_underlying_cap:" + ",".join(blocked_underlying)
    for cluster, symbols in (risk_manager.get("clusters_at_cap") or {}).items():
        if underlyings & {str(symbol).upper() for symbol in symbols}:
            return f"blocked_risk_cluster_cap:{cluster}"
    return ""


def _fallback_basket_cap_allows(config: dict[str, Any], store: LocalStore, campaign_id: str, plan_id: str) -> bool:
    raw_cap = (config.get("live") or {}).get("max_live_baskets_per_day")
    if raw_cap in (None, ""):
        return True
    cap = int(raw_cap)
    if cap <= 0:
        return False
    used_plan_ids = store.live_entry_plan_ids_since(_market_day_start(config))
    for registered_id in registered_campaign_ids(store):
        state = store.latest_event(attempt_event_type(registered_id)) or {}
        state_plan_id = str(state.get("plan_id") or "")
        if str(state.get("status") or "") != "rank_one_active":
            used_plan_ids.update(str(item) for item in state.get("attempted_plan_ids") or [] if item)
        ticket_hashes = {str(item) for item in state.get("ticket_hashes") or [] if item}
        tickets = [store.live_order_intent(ticket_hash) for ticket_hash in ticket_hashes]
        tickets = [ticket for ticket in tickets if ticket]
        submitted = any(
            str(ticket.get("_ledger_status") or "") not in PENDING_TICKET_STATUSES | {"dry_run"}
            or bool(store.live_order_attempts_for_ticket_hashes({str(ticket.get("ticket_hash") or "")}))
            for ticket in tickets
        )
        if submitted and state_plan_id:
            used_plan_ids.add(state_plan_id)
    return str(plan_id or "") in used_plan_ids or len(used_plan_ids) < cap


def _display_entry_limit(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value or "unknown")
    side = "credit" if numeric < 0 else "debit"
    return f"${abs(numeric):.2f} {side}"


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


def _staged_lifecycle_management_tickets(store: LocalStore, config: dict[str, Any]) -> list[dict[str, Any]]:
    tickets = [
        ticket
        for ticket in store.live_order_intents_by_status({"stage_approved_pending_submit", WAITING_ENTRY_WINDOW})
        if _is_lifecycle_management_ticket(ticket)
    ]
    tickets.sort(
        key=lambda item: (
            0 if str(item.get("intent_type") or "") == "close" else 1,
            str(item.get("_ledger_created_at") or item.get("created_at") or ""),
            str(item.get("ticket_hash") or ""),
        )
    )
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
        cancel_response = adapter.cancel_order(broker_order_id(ticket))
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
        window = submission_window(config, ticket, close=True)
        if not window["allowed"]:
            store.event("live_order_close_reprice_deferred", {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "submission_window": window})
            return {"reprice_status": "deferred_market_closed", "submission_window": window}
        new_ticket = _repriced_close_ticket(ticket, config)
        if _staged_replace_required(adapter, new_ticket):
            return _begin_staged_replacement(adapter, store, ticket, new_ticket, broker_status=broker_status, close=True)
        if _atomic_replace_supported(adapter, new_ticket):
            return _replace_live_order_atomically(
                adapter,
                store,
                ticket,
                new_ticket,
                broker_status=broker_status,
                close=True,
            )
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
            store.event(
                "live_order_close_reprice_preflight_deferred",
                {
                    "ticket_hash": ticket.get("ticket_hash"),
                    "order_id": ticket.get("order_id"),
                    "message": fresh_preflight.message,
                },
            )
            return {"reprice_status": "deferred_preflight_failed", "reprice_message": fresh_preflight.message}
        new_ticket["preflight"] = fresh_preflight.to_dict()
        new_ticket["ticket_hash"] = compute_ticket_hash(new_ticket)
        window = submission_window(config, new_ticket, close=True)
        if not window["allowed"]:
            store.event("live_order_close_reprice_deferred", {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "submission_window": window})
            return {"reprice_status": "deferred_market_closed", "submission_window": window}
        cancel_response = adapter.cancel_order(broker_order_id(ticket))
        store.record_live_order_status(str(ticket["order_id"]), "CLOSE_REPRICE_CANCEL_REQUESTED", cancel_response, ticket_hash=str(ticket["ticket_hash"]))
        response = adapter.place_order_ticket(new_ticket)
        ok = bool(response.get("orderId"))
        if ok:
            persist_broker_identity(new_ticket, response)
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
        error = _safe_broker_error(exc)
        store.event("live_order_close_reprice_failed", {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "error": error})
        return {"reprice_status": "failed", "reprice_message": error}


def _expire_live_entry_order(adapter: Any, store: LocalStore, config: dict[str, Any], ticket: dict[str, Any], broker_status: dict[str, Any]) -> dict[str, Any]:
    try:
        _assert_submit_allowed(config, submit=True)
        cancel_response = adapter.cancel_order(broker_order_id(ticket))
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
        window = submission_window(config, ticket, close=False)
        if not window["allowed"]:
            store.event("live_order_reprice_deferred", {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "submission_window": window})
            return {"reprice_status": "deferred_entry_cutoff", "submission_window": window}
        new_ticket = _repriced_open_ticket(ticket, config)
        campaign_metadata = (((new_ticket.get("preflight") or {}).get("raw") or {}).get("entry_pricing") or {}).get("campaign") or {}
        if campaign_metadata.get("enabled"):
            fresh_preflight = adapter.preflight_ticket(new_ticket)
            if not fresh_preflight.ok:
                store.record_live_order_attempt(
                    new_ticket,
                    action="campaign_reprice_preflight_open",
                    submit=True,
                    ok=False,
                    request_payload=dict((fresh_preflight.raw or {}).get("request") or new_ticket.get("submit_payload") or {}),
                    response_payload=fresh_preflight.to_dict(),
                )
                store.event(
                    "live_order_campaign_reprice_blocked",
                    {
                        "ticket_hash": ticket.get("ticket_hash"),
                        "order_id": ticket.get("order_id"),
                        "attempt": new_ticket.get("reprice_attempt"),
                        "reason": "fresh_preflight_failed",
                        "message": fresh_preflight.message,
                    },
                )
                return {"reprice_status": "campaign_preflight_blocked", "reprice_message": fresh_preflight.message}
            new_ticket["preflight"] = _preflight_with_entry_pricing(fresh_preflight.to_dict(), new_ticket)
            new_ticket["ticket_hash"] = compute_ticket_hash(new_ticket)
        if _staged_replace_required(adapter, new_ticket):
            return _begin_staged_replacement(adapter, store, ticket, new_ticket, broker_status=broker_status, close=False)
        if _atomic_replace_supported(adapter, new_ticket):
            return _replace_live_order_atomically(
                adapter,
                store,
                ticket,
                new_ticket,
                broker_status=broker_status,
                close=False,
            )
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
            store.event(
                "live_order_reprice_preflight_deferred",
                {
                    "ticket_hash": ticket.get("ticket_hash"),
                    "order_id": ticket.get("order_id"),
                    "message": fresh_preflight.message,
                },
            )
            return {"reprice_status": "deferred_preflight_failed", "reprice_message": fresh_preflight.message}
        new_ticket["preflight"] = _preflight_with_entry_pricing(fresh_preflight.to_dict(), new_ticket)
        new_ticket["ticket_hash"] = compute_ticket_hash(new_ticket)
        window = submission_window(config, new_ticket, close=False)
        if not window["allowed"]:
            store.event("live_order_reprice_deferred", {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "submission_window": window})
            return {"reprice_status": "deferred_entry_cutoff", "submission_window": window}
        cancel_response = adapter.cancel_order(broker_order_id(ticket))
        store.record_live_order_status(str(ticket["order_id"]), "REPRICE_CANCEL_REQUESTED", cancel_response, ticket_hash=str(ticket["ticket_hash"]))
        response = adapter.place_order_ticket(new_ticket)
        ok = bool(response.get("orderId"))
        if ok:
            persist_broker_identity(new_ticket, response)
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
        error = _safe_broker_error(exc)
        store.event("live_order_reprice_failed", {"ticket_hash": ticket.get("ticket_hash"), "order_id": ticket.get("order_id"), "error": error})
        return {"reprice_status": "failed", "reprice_message": error}


def _replace_live_order_atomically(
    adapter: Any,
    store: LocalStore,
    ticket: dict[str, Any],
    new_ticket: dict[str, Any],
    *,
    broker_status: dict[str, Any],
    close: bool,
) -> dict[str, Any]:
    """Use the broker's atomic cancel-replace operation when available.

    The replacement request id is deterministic, so retrying the same state
    after an indeterminate network response remains idempotent.
    """

    response = adapter.replace_order(broker_order_id(ticket), new_ticket)
    request_id = client_order_id(new_ticket)
    response_order_id = persist_broker_identity(new_ticket, response)
    new_ticket["replace_request_id"] = request_id
    new_ticket["replace_method"] = "broker_atomic"
    new_ticket["replaces_order_id"] = client_order_id(ticket)
    new_ticket["replaces_broker_order_id"] = broker_order_id(ticket)
    new_ticket["replace_response"] = dict(response)
    store.save_live_order_intent(new_ticket, status="submitted")
    store.record_live_order_attempt(
        new_ticket,
        action="atomic_replace_close" if close else "atomic_replace_open",
        submit=True,
        ok=True,
        request_payload={
            "orderId": ticket.get("order_id"),
            "brokerOrderId": broker_order_id(ticket),
            "requestId": request_id,
            "quantity": new_ticket.get("quantity"),
            "limitPrice": new_ticket.get("limit_price"),
        },
        response_payload=response,
    )
    store.update_live_order_intent_status(str(ticket["ticket_hash"]), "repriced")
    event_name = "live_order_close_repriced" if close else "live_order_repriced"
    store.event(
        event_name,
        {
            "from_ticket_hash": ticket.get("ticket_hash"),
            "to_ticket_hash": new_ticket.get("ticket_hash"),
            "from_order_id": ticket.get("order_id"),
            "to_order_id": response_order_id,
            "replace_request_id": request_id,
            "from_limit_price": ticket.get("limit_price"),
            "to_limit_price": new_ticket.get("limit_price"),
            "broker_status": broker_status.get("status"),
            "attempt": new_ticket.get("reprice_attempt"),
            "method": "broker_atomic",
            "ok": True,
        },
    )
    return {
        "reprice_status": "submitted",
        "reprice_method": "broker_atomic",
        "reprice_ticket_hash": new_ticket.get("ticket_hash"),
        "reprice_order_id": response_order_id,
        "reprice_request_id": request_id,
        "reprice_limit_price": new_ticket.get("limit_price"),
    }


def _atomic_replace_supported(adapter: Any, replacement_ticket: dict[str, Any]) -> bool:
    replace = getattr(adapter, "replace_order", None)
    if not callable(replace):
        return False
    supports = getattr(adapter, "supports_atomic_replace", None)
    return bool(supports(replacement_ticket)) if callable(supports) else True


def _staged_replace_required(adapter: Any, replacement_ticket: dict[str, Any]) -> bool:
    supports = getattr(adapter, "supports_atomic_replace", None)
    return (
        callable(getattr(adapter, "cancel_order", None))
        and callable(supports)
        and not bool(supports(replacement_ticket))
    )


def _begin_staged_replacement(
    adapter: Any,
    store: LocalStore,
    ticket: dict[str, Any],
    new_ticket: dict[str, Any],
    *,
    broker_status: dict[str, Any],
    close: bool,
) -> dict[str, Any]:
    """Persist replacement lineage before requesting cancellation."""

    new_ticket["replace_method"] = "staged_cancel"
    new_ticket["replaces_order_id"] = client_order_id(ticket)
    new_ticket["replaces_broker_order_id"] = broker_order_id(ticket)
    store.save_live_order_intent(new_ticket, status=REPLACE_WAITING_CANCEL)
    store.update_live_order_intent_status(str(ticket["ticket_hash"]), REPLACE_CANCEL_PENDING)
    cancel_response = adapter.cancel_order(broker_order_id(ticket))
    store.record_live_order_status(
        str(ticket["order_id"]),
        "REPLACE_CANCEL_REQUESTED",
        cancel_response,
        ticket_hash=str(ticket["ticket_hash"]),
    )
    store.record_live_order_attempt(
        new_ticket,
        action="stage_replace_cancel_close" if close else "stage_replace_cancel_open",
        submit=True,
        ok=True,
        request_payload={"orderId": ticket.get("order_id"), "brokerOrderId": broker_order_id(ticket)},
        response_payload=cancel_response,
    )
    store.event(
        "live_order_replace_cancel_requested",
        {
            "from_ticket_hash": ticket.get("ticket_hash"),
            "to_ticket_hash": new_ticket.get("ticket_hash"),
            "from_order_id": ticket.get("order_id"),
            "to_order_id": new_ticket.get("order_id"),
            "broker_status": broker_status.get("status"),
            "close": close,
        },
    )
    return {
        "reprice_status": "cancel_requested",
        "reprice_method": "staged_cancel",
        "reprice_ticket_hash": new_ticket.get("ticket_hash"),
        "reprice_order_id": new_ticket.get("order_id"),
        "reprice_limit_price": new_ticket.get("limit_price"),
    }


def _advance_staged_replacement(
    adapter: Any,
    store: LocalStore,
    ticket: dict[str, Any],
    broker_status: dict[str, Any],
) -> dict[str, Any]:
    children = [
        child
        for child in store.live_order_child_intents(str(ticket.get("ticket_hash") or ""))
        if str(child.get("_ledger_status") or "") == REPLACE_WAITING_CANCEL
    ]
    if not children:
        return {
            "reprice_status": "waiting_cancel",
            "reprice_method": "staged_cancel",
            "reprice_message": "replacement child missing",
            "needs_replacement_reconciliation": True,
        }
    replacement = max(children, key=lambda child: str(child.get("created_at") or ""))
    status = str(broker_status.get("status") or "UNKNOWN").upper()
    close = str(replacement.get("intent_type") or "") == "close"
    if status in {"FILLED", "PARTIALLY_FILLED"}:
        parent_status = "close_filled" if close else "filled"
        store.update_live_order_intent_status(str(ticket["ticket_hash"]), parent_status)
        store.update_live_order_intent_status(str(replacement["ticket_hash"]), "replace_aborted_parent_filled")
        return {
            "reprice_status": "aborted_parent_filled",
            "reprice_method": "staged_cancel",
            "broker_status": status,
        }

    position_evidence = _replacement_position_evidence(adapter, replacement) if close else {"intact": True, "reason": "entry_order"}
    if position_evidence.get("intact") is not True:
        return {
            "reprice_status": "waiting_position_reconciliation",
            "reprice_method": "staged_cancel",
            "position_evidence": position_evidence,
        }

    if status in {"NEW", "OPEN", "WORKING", "PENDING", "ACCEPTED"}:
        try:
            cancel_response = adapter.cancel_order(broker_order_id(ticket))
            store.record_live_order_status(
                str(ticket["order_id"]),
                "REPLACE_CANCEL_REQUESTED",
                cancel_response,
                ticket_hash=str(ticket["ticket_hash"]),
            )
        except Exception as exc:  # noqa: BLE001
            store.event(
                "live_order_replace_cancel_retry_failed",
                {
                    "ticket_hash": ticket.get("ticket_hash"),
                    "order_id": ticket.get("order_id"),
                    "error": _safe_broker_error(exc),
                },
            )
        return {
            "reprice_status": "waiting_cancel",
            "reprice_method": "staged_cancel",
            "broker_status": status,
            "position_evidence": position_evidence,
        }

    if status not in TERMINAL_UNFILLED_ORDER_STATUSES:
        return {
            "reprice_status": "waiting_cancel",
            "reprice_method": "staged_cancel",
            "broker_status": status,
            "position_evidence": position_evidence,
        }

    fresh_preflight = adapter.preflight_ticket(replacement)
    if not fresh_preflight.ok:
        return {
            "reprice_status": "waiting_cancel",
            "reprice_method": "staged_cancel",
            "position_evidence": position_evidence,
            "reprice_message": fresh_preflight.message,
        }
    replacement["preflight"] = _preflight_with_entry_pricing(fresh_preflight.to_dict(), replacement)
    response = adapter.place_order_ticket(replacement)
    replacement["replace_request_id"] = client_order_id(replacement)
    response_order_id = persist_broker_identity(replacement, response)
    replacement["replace_response"] = dict(response)
    store.save_live_order_intent(replacement, status="submitted")
    store.update_live_order_intent_status(str(ticket["ticket_hash"]), "repriced")
    store.record_live_order_attempt(
        replacement,
        action="submit_staged_replace_close" if close else "submit_staged_replace_open",
        submit=True,
        ok=True,
        request_payload=dict(replacement.get("submit_payload") or {}),
        response_payload=response,
    )
    store.event(
        "live_order_staged_replacement_submitted",
        {
            "from_ticket_hash": ticket.get("ticket_hash"),
            "to_ticket_hash": replacement.get("ticket_hash"),
            "from_order_id": ticket.get("order_id"),
            "to_order_id": response_order_id,
            "broker_status": status,
            "position_evidence": position_evidence,
        },
    )
    return {
        "reprice_status": "submitted",
        "reprice_method": "staged_cancel",
        "reprice_ticket_hash": replacement.get("ticket_hash"),
        "reprice_order_id": response_order_id,
        "reprice_limit_price": replacement.get("limit_price"),
        "position_evidence": position_evidence,
    }


def _replacement_position_evidence(adapter: Any, replacement: dict[str, Any]) -> dict[str, Any]:
    try:
        positions = list(adapter.broker_positions())
    except Exception as exc:  # noqa: BLE001
        return {"intact": None, "reason": "portfolio_fetch_failed", "error": _safe_broker_error(exc)}
    legs = list(replacement.get("legs") or [])
    missing = []
    for leg in legs:
        expected_quantity = max(float(leg.get("quantity") or 1), 0.0)
        side = str(leg.get("side") or "").lower()
        match = next(
            (
                position
                for position in positions
                if str(position.get("underlying") or "").upper() == str(replacement.get("underlying") or "").upper()
                and str(position.get("expiration") or "") == str(leg.get("expiration") or "")
                and str(position.get("option_type") or "").lower() == str(leg.get("option_type") or "").lower()
                and abs(float(position.get("strike") or 0.0) - float(leg.get("strike") or 0.0)) < 0.001
            ),
            None,
        )
        broker_quantity = float((match or {}).get("quantity") or 0.0)
        available = broker_quantity <= -expected_quantity if side == "buy" else broker_quantity >= expected_quantity
        if not available:
            missing.append(
                {
                    "expiration": leg.get("expiration"),
                    "option_type": leg.get("option_type"),
                    "strike": leg.get("strike"),
                    "close_side": side,
                    "expected_quantity": expected_quantity,
                    "broker_quantity": broker_quantity,
                }
            )
    return {
        "intact": not missing,
        "reason": "portfolio_legs_intact" if not missing else "portfolio_quantity_changed",
        "checked_legs": len(legs),
        "missing_or_changed_legs": missing,
    }


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
    pricing_metadata = (((ticket.get("preflight") or {}).get("raw") or {}).get("entry_pricing") or {})
    campaign = pricing_metadata.get("campaign") or {}
    if campaign.get("enabled"):
        prices = campaign.get("prices") or []
        next_index = int(ticket.get("reprice_attempt") or 0) + 1
        if next_index >= len(prices):
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
    new_ticket["client_order_id"] = new_order_id
    new_ticket.pop("broker_order_id", None)
    new_ticket.pop("replace_response", None)
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
    new_ticket["client_order_id"] = new_order_id
    new_ticket.pop("broker_order_id", None)
    new_ticket.pop("replace_response", None)
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
    campaign = metadata.get("campaign") or {}
    base = float(metadata.get("base_mid_limit") or current)
    improved = float(metadata.get("improved_limit") or current)
    attempt = int(ticket.get("reprice_attempt") or 0) + 1
    if campaign.get("enabled"):
        prices = campaign.get("prices") or []
        if attempt >= len(prices):
            raise ValueError("entry campaign has no valid next price")
        return str(prices[attempt])
    multiplier = _reprice_improvement_multiplier(config, attempt)
    multiplier = max(0.0, min(multiplier, 1.0))
    target = base + ((improved - base) * multiplier)
    signed = str(ticket.get("limit_price") or "")
    if signed.startswith("-"):
        return f"-{_round_cent(target):.2f}"
    return f"{_round_cent(target):.2f}"


def _repriced_close_limit_price(ticket: dict[str, Any], config: dict[str, Any]) -> str:
    current_net = _close_net_from_limit_price(ticket)
    natural_net = _optional_float(ticket.get("exit_natural_net"))
    if natural_net is None:
        natural_price = _optional_float(ticket.get("exit_natural_limit_price"))
        natural_net = _close_net_from_price_with_current_side(ticket, natural_price) if natural_price is not None else current_net
    reason = str(ticket.get("exit_reason") or "").lower()
    reason_class = str(ticket.get("exit_reason_class") or ticket.get("csa_action_reason_class") or "").lower()
    floor_net = _optional_float(ticket.get("exit_profit_floor_net"))
    if floor_net is None:
        floor_price = _optional_float(ticket.get("exit_profit_floor_limit_price"))
        if floor_price is not None:
            floor_net = _close_net_from_price_with_current_side(ticket, floor_price)
    attempt = int(ticket.get("reprice_attempt") or 0) + 1
    if reason_class == "executable_profit" or reason == "profit_target":
        target_net = max(natural_net, floor_net) if floor_net is not None else natural_net
    elif reason_class in {"adverse_price_loss", "mandatory_event_exit", "hard_emergency", "time_decision"}:
        target_net = natural_net
    elif reason == "profit_target" and floor_net is not None:
        target_net = max(natural_net, floor_net)
    else:
        target_net = natural_net
    step = _exit_reprice_step_multiplier(config, attempt)
    repriced_net = current_net + ((target_net - current_net) * step)
    lower = min(current_net, natural_net)
    upper = max(current_net, natural_net)
    repriced_net = min(max(repriced_net, lower), upper)
    if (reason_class == "executable_profit" or reason == "profit_target") and floor_net is not None:
        repriced_net = max(repriced_net, floor_net)
    return _close_limit_price_from_net(ticket, repriced_net)


def _close_net_from_limit_price(ticket: dict[str, Any]) -> float:
    raw = str(ticket.get("limit_price") or "0").strip()
    try:
        price = float(raw)
    except ValueError:
        price = 0.0
    legs = list(ticket.get("legs") or [])
    if len(legs) == 1:
        side = str((legs[0] or {}).get("side") or "").lower()
        return abs(price) * 100.0 if side == "sell" else -abs(price) * 100.0
    return -price * 100.0


def _close_net_from_price_with_current_side(ticket: dict[str, Any], price: float) -> float:
    current_net = _close_net_from_limit_price(ticket)
    absolute = abs(float(price)) * 100.0
    if current_net >= 0:
        return absolute
    return -absolute


def _close_limit_price_from_net(ticket: dict[str, Any], close_net: float) -> str:
    per_contract = max(abs(close_net) / 100.0, 0.01)
    rounded = _round_cent(per_contract)
    legs = list(ticket.get("legs") or [])
    if len(legs) == 1:
        return f"{rounded:.2f}"
    if close_net > 0:
        return f"-{rounded:.2f}"
    return f"{rounded:.2f}"


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
    return retire_stale_entry_approvals(config, store, active_hashes=active_hashes, source="live_approval_cleanup")


def _live_advisory_ticket_hashes(rows: list[dict[str, Any]], config: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    today = _market_today(config)
    for row in rows:
        detail = _loads(row.get("plan_detail_json"))
        if str(detail.get("lane") or row.get("mode") or "") not in {"live_advisory", "live"}:
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
    return stale_entry_approval_minutes(config)


def _market_today(config: dict[str, Any]) -> str:
    return market_today(config)


def record_manual_live_fill(ticket_hash: str, *, store: LocalStore | None = None) -> dict[str, Any]:
    store = store or LocalStore()
    ticket = store.live_order_intent(ticket_hash)
    if not ticket:
        raise RuntimeError(f"live order intent not found: {ticket_hash}")
    projection = _save_live_position_from_ticket(store, ticket, status="open", order_status={"manual": True})
    store.update_live_order_intent_status(ticket_hash, "manual_fill_recorded")
    return {
        "ticket_hash": ticket_hash,
        "group_id": projection.get("group_id") or _group_id(ticket),
        "status": "manual_fill_recorded",
    }


def _approved_rows(
    config: dict[str, Any],
    *,
    close: bool,
    tables: dict[str, list[dict[str, str]]] | None = None,
) -> list[dict[str, str]]:
    rows = (tables if tables is not None else pull_sheet_tables(config)).get("daily_plan") or []
    action = APPROVE_LIVE_CLOSE if close else APPROVE_LIVE
    lanes = {"live_close_advisory"} if close else {"live_advisory", "live"}
    approved = []
    for row in rows:
        if str(row.get("operator_action") or "").strip().upper() != action:
            continue
        detail = _loads(row.get("plan_detail_json"))
        if str(detail.get("lane") or row.get("mode") or "") not in lanes:
            continue
        approved.append(row)
    return approved


def _stage_ticket_authorization(
    ticket: dict[str, Any],
    snapshot: DailyPolicySnapshot,
) -> tuple[bool, str]:
    """Validate against the immutable state captured for this trading day."""

    playbook_id = str(ticket.get("csa_playbook_id") or "")
    policy_hash = str(ticket.get("csa_policy_hash") or "")
    if not playbook_id or not policy_hash or not bool(ticket.get("stage_authorized")):
        return False, "blocked_stage_authorization_metadata_missing"
    if str(ticket.get("csa_policy_snapshot_date") or "") != snapshot.trading_date:
        return False, "blocked_stage_authorization_snapshot_date_mismatch"
    if str(ticket.get("csa_policy_snapshot_hash") or "") != snapshot.snapshot_hash:
        return False, "blocked_stage_authorization_snapshot_hash_mismatch"
    if str(ticket.get("csa_authorization_policy") or "") == "unified_strategy_engine":
        compiled = compile_playbook_policies(snapshot.tables.get("playbooks") or [])
        if not compiled.ok:
            return False, "blocked_stage_authorization_policy_invalid"
        policy = next((item for item in compiled.policies if item.playbook_id == playbook_id), None)
        if policy is None:
            return False, "blocked_stage_authorization_policy_unavailable"
        if policy.mode is not ExecutionMode.LIVE:
            return False, "blocked_stage_authorization_revoked"
        if policy.policy_hash != policy_hash:
            return False, "blocked_stage_authorization_policy_changed"
        if str(ticket.get("csa_compiled_policy_hash") or "") != policy_hash:
            return False, "blocked_stage_authorization_compiled_policy_mismatch"
        return True, "stage_authorization_current"
    policy = next((item for item in snapshot.policy.policies if item.playbook_id == playbook_id), None)
    if policy is None:
        return False, "blocked_stage_authorization_policy_unavailable"
    if policy.stage not in {CsaStage.PILOT_LIVE, CsaStage.LIVE}:
        return False, "blocked_stage_authorization_revoked"
    if policy.policy_hash != policy_hash:
        return False, "blocked_stage_authorization_policy_changed"
    if str(ticket.get("csa_stage") or "") != policy.stage.value:
        return False, "blocked_stage_authorization_stage_changed"
    return True, "stage_authorization_current"


def _is_lifecycle_management_ticket(ticket: dict[str, Any]) -> bool:
    return (
        str(ticket.get("intent_type") or "") in {"close", "adjust"}
        and bool(ticket.get("csa_lifecycle_id"))
    )


def _lifecycle_management_authorization(
    ticket: dict[str, Any],
    *,
    config: dict[str, Any],
    store: LocalStore,
) -> tuple[bool, str]:
    """Authorize management from frozen lifecycle state, never today's Sheet.

    Current Sheet policy controls new entries.  This branch deliberately uses
    only the lifecycle and local safety/reconciliation state so a later Sheet
    edit cannot orphan a legitimately open position.
    """
    action_type = str(ticket.get("intent_type") or "")
    lifecycle_id = str(ticket.get("csa_lifecycle_id") or "")
    if action_type not in {"close", "adjust"} or not lifecycle_id:
        return False, "blocked_lifecycle_authorization_metadata_missing"
    if bool((config.get("runtime") or {}).get("halt")):
        return False, "blocked_lifecycle_authorization_runtime_halt"
    payload = ticket.get("csa_strategy_ticket")
    if not isinstance(payload, dict):
        return False, "blocked_lifecycle_authorization_strategy_ticket_missing"
    try:
        strategy_ticket = strategy_ticket_from_payload(payload)
        lifecycle = CsaStore(store.sqlite_path, read_only=True).lifecycle(lifecycle_id)
    except (RuntimeError, TypeError, ValueError):
        return False, "blocked_lifecycle_authorization_lifecycle_unavailable"
    if lifecycle is None or lifecycle.status != "open":
        return False, "blocked_lifecycle_authorization_not_open"
    if str(lifecycle.metadata.get("execution_mode") or "") != "live":
        return False, "blocked_lifecycle_authorization_execution_mode"
    if strategy_ticket.lifecycle_id != lifecycle_id or strategy_ticket.lifecycle_version != lifecycle.version:
        return False, "blocked_lifecycle_authorization_version_mismatch"
    if str(ticket.get("csa_action_type") or "") != action_type or str(strategy_ticket.metadata.get("action_type") or "") != action_type:
        return False, "blocked_lifecycle_authorization_action_mismatch"
    frozen_policy = lifecycle.metadata.get("compiled_management_policy")
    if not isinstance(frozen_policy, dict) or str(frozen_policy.get("policy_hash") or "") != lifecycle.policy_hash:
        return False, "blocked_lifecycle_authorization_frozen_policy_missing"
    if strategy_ticket.policy_hash != lifecycle.policy_hash or str(ticket.get("csa_policy_hash") or "") != lifecycle.policy_hash:
        return False, "blocked_lifecycle_authorization_policy_mismatch"
    if not lifecycle.active_legs:
        return False, "blocked_lifecycle_authorization_ownership_missing"
    from kamandal_v2.live.reconciliation import reconciliation_blockers_for_group

    reconciliation = reconciliation_blockers_for_group(
        store,
        {"underlying": ticket.get("underlying") or lifecycle.metadata.get("underlying")},
        config=config,
    )
    if reconciliation:
        return False, "blocked_lifecycle_authorization_reconciliation"
    active_statuses = {
        *PENDING_TICKET_STATUSES,
        *ACTIVE_TICKET_STATUSES,
        *CANCEL_PENDING_TICKET_STATUSES,
        REPLACE_WAITING_CANCEL,
    }
    conflicts = [
        item
        for item in store.live_order_intents_by_status(active_statuses)
        if str(item.get("csa_lifecycle_id") or "") == lifecycle_id
        and str(item.get("ticket_hash") or "") != str(ticket.get("ticket_hash") or "")
    ]
    if conflicts:
        return False, "blocked_lifecycle_authorization_working_order_conflict"
    return True, "lifecycle_authorization_frozen"


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
    if status in {REPLACE_CANCEL_PENDING, REPLACE_WAITING_CANCEL}:
        return {"state": "active", "status": status}
    if status in LEGACY_REPRICE_TRACKING_STATUSES:
        return {"state": "active", "status": "legacy_reprice_tracking"}
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


def _adopt_csa_live_fill(
    store: LocalStore,
    ticket: dict[str, Any],
    order_status: dict[str, Any],
    *,
    position_projection: dict[str, Any] | None = None,
    reconcile_current_version: bool = False,
    filled_quantity: float | None = None,
    adopted_ticket_status: str | None = None,
) -> dict[str, Any]:
    """Advance the app-owned lifecycle exactly once after a broker fill."""

    from kamandal_v2.strategy_lanes.models import ShadowFill, stable_csa_id
    from kamandal_v2.strategy_lanes.shadow_execution import ShadowExecutionAdapter
    from kamandal_v2.strategy_lanes.store import CsaStore, strategy_ticket_from_payload

    payload = ticket.get("csa_strategy_ticket")
    if not isinstance(payload, dict):
        return {"saved": False, "reason": "csa_strategy_ticket_missing"}
    strategy_ticket = strategy_ticket_from_payload(payload)
    csa_store = CsaStore(store.sqlite_path)
    lifecycle = csa_store.lifecycle(strategy_ticket.lifecycle_id)
    if lifecycle is None:
        return {"saved": False, "reason": "csa_lifecycle_missing"}
    if lifecycle.version > strategy_ticket.lifecycle_version:
        if not reconcile_current_version:
            return {
                "saved": True,
                "idempotent": True,
                "lifecycle_id": lifecycle.lifecycle_id,
                "version": lifecycle.version,
                "status": lifecycle.status,
            }
        strategy_ticket = replace(strategy_ticket, lifecycle_version=lifecycle.version)
    if lifecycle.version != strategy_ticket.lifecycle_version:
        return {
            "saved": False,
            "reason": "csa_lifecycle_version_mismatch",
            "lifecycle_version": lifecycle.version,
            "ticket_version": strategy_ticket.lifecycle_version,
        }
    observed_quantity = filled_quantity
    if observed_quantity in (None, 0):
        observed_quantity = _filled_quantity(order_status)
    if observed_quantity and observed_quantity > 0:
        metadata = dict(lifecycle.metadata)
        metadata["filled_quantity"] = float(observed_quantity)
        metadata["requested_quantity"] = float(ticket.get("quantity") or 1.0)
        metadata["partial_fill"] = bool(float(observed_quantity) < float(ticket.get("quantity") or 1.0))
        lifecycle = replace(lifecycle, metadata=metadata)
    raw_price = order_status.get("averagePrice")
    if raw_price in (None, ""):
        raw_price = order_status.get("average-price")
    filled_price = abs(float(raw_price)) if raw_price not in (None, "") else abs(float(strategy_ticket.limit_price))
    observed_at = str(
        order_status.get("updatedAt")
        or order_status.get("updated-at")
        or order_status.get("filledAt")
        or order_status.get("filled-at")
        or _now_utc()
    )
    fill = ShadowFill(
        fill_id=stable_csa_id("live-fill", [strategy_ticket.ticket_id, ticket.get("order_id"), filled_price]),
        ticket_id=strategy_ticket.ticket_id,
        lifecycle_id=strategy_ticket.lifecycle_id,
        status="filled",
        attempt=0,
        natural_price=filled_price,
        working_price=abs(float(strategy_ticket.limit_price)),
        filled_price=filled_price,
        filled_at=observed_at,
        quote_evidence={"source": "broker_order_status", "order_id": ticket.get("order_id")},
    )
    projection_id = str(
        (position_projection or {}).get("group_id")
        or ticket.get("position_projection_id")
        or ticket.get("group_id")
        or lifecycle.position_projection_id
        or ""
    )
    metadata = dict(lifecycle.metadata)
    if projection_id:
        metadata["position_projection_id"] = projection_id
        lifecycle = replace(lifecycle, metadata=metadata)
    updated = ShadowExecutionAdapter().adopt_fill(lifecycle, strategy_ticket, fill)
    action_type = str(strategy_ticket.metadata.get("action_type") or "")
    committed = store.commit_canonical_lifecycle_fill(
        updated,
        ticket_hash=str(ticket.get("ticket_hash") or ""),
        ticket_status=adopted_ticket_status or ("close_filled" if action_type == "close" else "filled"),
        position_projection_id=projection_id,
        fill_evidence={
            "filled_at": observed_at,
            "fill_id": fill.fill_id,
            "order_id": str(ticket.get("order_id") or ""),
            "action_type": action_type,
        },
    )
    return {**committed, "idempotent": False}


def _save_live_position_from_ticket(
    store: LocalStore,
    ticket: dict[str, Any],
    *,
    status: str,
    order_status: dict[str, Any],
) -> dict[str, Any]:
    lineage = resolve_entry_lineage(store, ticket, broker_order_id=str(ticket.get("order_id") or ""))
    if lineage.ambiguous or not lineage.canonical_ticket:
        blocked = {
            "saved": False,
            "reason": lineage.reason or "ambiguous_entry_lineage",
            "ticket_hash": ticket.get("ticket_hash"),
            "order_id": ticket.get("order_id"),
            "lineage_id": lineage.lineage_id,
        }
        store.event("live_position_projection_blocked", blocked)
        return blocked

    canonical = lineage.canonical_ticket
    group_id = lineage.group_id
    execution_fills = _lineage_execution_fills(store, lineage, ticket, order_status)
    fill_quantity = sum(float(fill.get("filled_quantity") or 0.0) for fill in execution_fills)
    if fill_quantity <= 0 and status in {"filled", "open"}:
        fill_quantity = float(canonical.get("quantity") or 1.0)
        execution_fills = [
            _execution_fill(canonical, order_status, fill_quantity=fill_quantity, source="fallback")
        ]
    if fill_quantity <= 0:
        blocked = {
            "saved": False,
            "reason": "filled_quantity_missing",
            "ticket_hash": canonical.get("ticket_hash"),
            "order_id": canonical.get("order_id"),
            "lineage_id": lineage.lineage_id,
        }
        store.event("live_position_projection_blocked", blocked)
        return blocked

    candidate = _candidate_from_ticket(canonical, fill_quantity=fill_quantity)
    payload = {
        "group_id": group_id,
        "order_id": canonical.get("order_id"),
        "plan_id": canonical.get("plan_id"),
        "candidate_id": canonical.get("candidate_id"),
        "idea_id": canonical.get("idea_id"),
        "underlying": canonical.get("underlying"),
        "playbook_id": canonical.get("playbook_id"),
        "structure": canonical.get("structure"),
        "execution_venue": ticket_execution_venue({}, canonical),
        "candidate": candidate,
        "execution_quality": canonical.get("execution_quality") or {},
        "entry_snapshot": _entry_snapshot_from_lineage(
            canonical,
            order_status,
            execution_fills=execution_fills,
            fill_quantity=fill_quantity,
        ),
        "execution_lineage": {
            "lineage_id": lineage.lineage_id,
            "root_ticket_hash": lineage.root_ticket_hash,
            "canonical_ticket_hash": canonical.get("ticket_hash"),
            "broker_order_id": canonical.get("order_id"),
            "ticket_hashes": sorted(str(member.get("ticket_hash") or "") for member in lineage.members),
            "broker_order_ids": sorted(
                {str(fill.get("order_id") or "") for fill in execution_fills if str(fill.get("order_id") or "")}
            ),
        },
        "order_status": order_status,
    }
    store.save_live_position_group(group_id, payload, status="open")
    store.save_live_position(group_id, group_id, payload, status="open")
    canonical_status = "partially_filled" if status == "partially_filled" else "filled"
    if status == "partially_filled_terminal":
        canonical_status = "partially_filled_terminal"
    store.update_live_order_intent_status_with_payload(
        str(canonical["ticket_hash"]),
        canonical_status,
        {
            "execution_lineage": payload["execution_lineage"],
            "position_group_id": group_id,
            "filled_quantity": fill_quantity,
        },
    )
    if canonical_status == "filled":
        for member in lineage.members:
            member_hash = str(member.get("ticket_hash") or "")
            if member_hash == str(canonical.get("ticket_hash") or ""):
                continue
            if str(member.get("order_id") or "") != str(canonical.get("order_id") or ""):
                continue
            member_status = str(member.get("_ledger_status") or member.get("status") or "")
            if member_status in COMPLETED_TICKET_STATUSES:
                continue
            store.update_live_order_intent_status_with_payload(
                member_hash,
                "filled_via_replacement",
                {
                    "execution_lineage": payload["execution_lineage"],
                    "position_group_id": group_id,
                    "filled_by_ticket_hash": canonical.get("ticket_hash"),
                },
            )
    store.event(
        "live_position_projection_saved",
        {
            "group_id": group_id,
            "lineage_id": lineage.lineage_id,
            "canonical_ticket_hash": canonical.get("ticket_hash"),
            "broker_order_id": canonical.get("order_id"),
            "filled_quantity": fill_quantity,
            "execution_fill_count": len(execution_fills),
            "status": canonical_status,
        },
    )
    return {
        "saved": True,
        "group_id": group_id,
        "lineage_id": lineage.lineage_id,
        "canonical_ticket_hash": canonical.get("ticket_hash"),
        "filled_quantity": fill_quantity,
        "execution_fill_count": len(execution_fills),
        "status": canonical_status,
    }


def _candidate_from_ticket(ticket: dict[str, Any], *, fill_quantity: float = 1.0) -> dict[str, Any]:
    legs = []
    for raw_leg in ticket.get("legs") or []:
        leg = dict(raw_leg)
        leg["quantity"] = abs(float(leg.get("quantity") or 1.0)) * fill_quantity
        legs.append(leg)
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


def _entry_snapshot_from_lineage(
    ticket: dict[str, Any],
    order_status: dict[str, Any],
    *,
    execution_fills: list[dict[str, Any]],
    fill_quantity: float = 1.0,
) -> dict[str, Any]:
    entry_net_cashflow = round(
        sum(
            float(fill.get("net_credit") or 0.0)
            * 100.0
            * float(fill.get("filled_quantity") or 0.0)
            for fill in execution_fills
        ),
        2,
    )
    net_credit = entry_net_cashflow / (100.0 * fill_quantity) if fill_quantity else _net_credit_from_ticket(ticket)
    return {
        "entry_kind": "credit" if entry_net_cashflow > 0 else "debit",
        "entry_net_credit": round(net_credit, 4),
        "entry_net_cashflow": entry_net_cashflow,
        "entry_value": abs(entry_net_cashflow),
        "fill_price": order_status.get("averagePrice"),
        "fill_quantity": fill_quantity,
        "source_order_id": ticket.get("order_id"),
        "source_ticket_hash": ticket.get("ticket_hash"),
        "execution_fills": execution_fills,
        "execution_quality": ticket.get("execution_quality") or {},
    }


def _group_id(ticket: dict[str, Any]) -> str:
    return f"live_group_{ticket.get('ticket_hash')}"


def _filled_quantity(order_status: dict[str, Any]) -> float:
    raw = order_status.get("filledQuantity")
    if raw in (None, ""):
        return 0.0
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _lineage_execution_fills(
    store: LocalStore,
    lineage: EntryLineage,
    current_ticket: dict[str, Any],
    current_status: dict[str, Any],
) -> list[dict[str, Any]]:
    """Aggregate cumulative fills once per broker order in an entry lineage."""

    members_by_hash = {str(member.get("ticket_hash") or ""): member for member in lineage.members}
    members_by_order: dict[str, list[dict[str, Any]]] = {}
    for member in lineage.members:
        order_id = str(member.get("order_id") or "")
        if order_id:
            members_by_order.setdefault(order_id, []).append(member)
    observations = store.live_order_status_history(set(members_by_order))
    current_order_id = str(current_ticket.get("order_id") or "")
    if current_order_id and not any(
        str(item.get("_order_id") or "") == current_order_id for item in observations
    ):
        observations.append(
            {
                **current_status,
                "_order_id": current_order_id,
                "_ticket_hash": current_ticket.get("ticket_hash"),
                "_broker_status": current_status.get("status"),
                "_status_id": 0,
            }
        )

    best_by_order: dict[str, dict[str, Any]] = {}
    for observation in observations:
        order_id = str(observation.get("_order_id") or "")
        quantity = _filled_quantity(observation)
        if not order_id or quantity <= 0:
            continue
        previous = best_by_order.get(order_id)
        previous_quantity = _filled_quantity(previous or {})
        if previous is None or quantity > previous_quantity or (
            quantity == previous_quantity
            and int(observation.get("_status_id") or 0) > int(previous.get("_status_id") or 0)
        ):
            best_by_order[order_id] = observation

    fills = []
    for order_id, observation in sorted(best_by_order.items()):
        observed_ticket = members_by_hash.get(str(observation.get("_ticket_hash") or ""))
        ticket_for_order = observed_ticket or max(
            members_by_order.get(order_id) or [current_ticket],
            key=lambda member: str(member.get("created_at") or ""),
        )
        fills.append(
            _execution_fill(
                ticket_for_order,
                observation,
                fill_quantity=_filled_quantity(observation),
                source="broker_status_history",
            )
        )
    return fills


def _execution_fill(
    ticket: dict[str, Any],
    order_status: dict[str, Any],
    *,
    fill_quantity: float,
    source: str,
) -> dict[str, Any]:
    net_credit = _net_credit_from_ticket(ticket)
    average_price = order_status.get("averagePrice")
    if average_price not in (None, ""):
        observed_price = abs(float(average_price))
        side = str(order_status.get("side") or "").upper()
        if len(ticket.get("legs") or []) == 1 and side:
            net_credit = observed_price if side == "SELL" else -observed_price
        elif net_credit:
            net_credit = observed_price if net_credit > 0 else -observed_price
    return {
        "order_id": str(order_status.get("_order_id") or ticket.get("order_id") or ""),
        "ticket_hash": str(order_status.get("_ticket_hash") or ticket.get("ticket_hash") or ""),
        "filled_quantity": fill_quantity,
        "average_price": average_price,
        "net_credit": round(net_credit, 4),
        "broker_status": str(order_status.get("_broker_status") or order_status.get("status") or ""),
        "observed_at": order_status.get("_observed_at"),
        "source": source,
    }


def _filled_replacement_descendant(store: LocalStore, ticket: dict[str, Any]) -> dict[str, Any] | None:
    resolution = resolve_entry_lineage(store, ticket, broker_order_id=str(ticket.get("order_id") or ""))
    ticket_hash = str(ticket.get("ticket_hash") or "")
    completed = [
        member
        for member in resolution.members
        if str(member.get("ticket_hash") or "") != ticket_hash
        and str(member.get("_ledger_status") or member.get("status") or "")
        in {"filled", "manual_fill_recorded", "partially_filled_terminal"}
    ]
    if not completed:
        return None
    return max(
        completed,
        key=lambda member: (str(member.get("created_at") or ""), str(member.get("ticket_hash") or "")),
    )


def _lineage_root_hash(store: LocalStore, ticket: dict[str, Any]) -> str:
    resolution = resolve_entry_lineage(store, ticket, broker_order_id=str(ticket.get("order_id") or ""))
    return resolution.root_ticket_hash or str(ticket.get("ticket_hash") or "")


def _failure(
    ticket: dict[str, Any],
    reason: str,
    *,
    failure_code: str | None = None,
) -> dict[str, Any]:
    result = {
        "ticket_hash": ticket.get("ticket_hash"),
        "order_id": ticket.get("order_id"),
        "underlying": ticket.get("underlying"),
        "status": "blocked",
        "reason": reason,
    }
    if failure_code:
        result["failure_code"] = failure_code
    return result


def _stale_selected_entry_failure(execution: dict[str, Any]) -> dict[str, Any] | None:
    for result in execution.get("results") or []:
        reason = str(result.get("failure_code") or result.get("reason") or "")
        if reason == "ticket_preflight_stale" or reason.endswith(":blocked_preflight_stale"):
            return result
    return None


def _execution_outcome(execution: dict[str, Any]) -> str:
    results = execution.get("results") or []
    if any(str(result.get("status") or "") == "submitted" for result in results):
        return "submitted"
    if not results:
        return "no_selected_entry"
    return str(results[0].get("failure_code") or results[0].get("reason") or results[0].get("status") or "blocked")


def _selected_entry_failure(execution: dict[str, Any], *, submit: bool) -> dict[str, Any] | None:
    if not submit:
        return None
    results = execution.get("results") or []
    if not results:
        return None
    for result in results:
        status = str(result.get("status") or "")
        reason = str(result.get("reason") or "")
        if status == "submitted":
            continue
        if status == "dry_run":
            continue
        if status in {WAITING_ENTRY_WINDOW, "retired_stale_entry_approval"}:
            continue
        if reason.startswith("basket_ticket_active:"):
            continue
        if reason == "basket_complete_or_no_pending_tickets":
            continue
        return result
    return None


def notify_live_advisory_risk_block(
    config: dict[str, Any],
    store: LocalStore,
    candidates: list[Any],
) -> dict[str, Any]:
    """Notify when auto-selection produced no ticket because its risk gate blocked."""

    blocked = [
        candidate
        for candidate in candidates
        if str(getattr(candidate, "rejection_reason", "") or "").startswith("live_risk_")
    ]
    if not blocked:
        return {"needed": False, "attempted": False, "reason": "no_advisory_risk_block"}
    candidate = max(blocked, key=lambda item: float(getattr(item, "score", 0.0) or 0.0))
    result = {
        "status": "blocked",
        "reason": str(candidate.rejection_reason),
        "failure_code": str(candidate.rejection_reason),
        "underlying": str(candidate.underlying),
        "structure": str(candidate.structure),
        "ticket_hash": f"candidate:{candidate.candidate_id}",
    }
    return _notify_selected_entry_failure(
        config,
        store,
        {"processed": 1, "results": [result]},
        recovery={"attempted": False},
        submit=True,
    )


def _notify_selected_entry_failure(
    config: dict[str, Any],
    store: LocalStore,
    execution: dict[str, Any],
    *,
    recovery: dict[str, Any],
    submit: bool,
) -> dict[str, Any]:
    result = _selected_entry_failure(execution, submit=submit)
    previous = store.latest_event(SELECTED_ENTRY_ATTENTION_STATE_EVENT) or {}
    if result is None:
        if previous.get("status") == "open" and previous.get("intent_type") != "close":
            store.event(
                SELECTED_ENTRY_ATTENTION_STATE_EVENT,
                {
                    "status": "cleared",
                    "fingerprint": previous.get("fingerprint") or "",
                    "reason": previous.get("reason") or "",
                },
            )
        return {"needed": False, "attempted": False, "reason": "no_selected_entry_failure"}

    ticket_hash = str(result.get("ticket_hash") or "")
    ticket = store.live_order_intent(ticket_hash) if ticket_hash else None
    underlying = str(result.get("underlying") or (ticket or {}).get("underlying") or "entry").upper()
    structure = str(result.get("structure") or (ticket or {}).get("structure") or "options entry").replace("_", " ")
    reason = str(result.get("failure_code") or result.get("reason") or result.get("status") or "placement_failed")
    intent_type = str(result.get("intent_type") or (ticket or {}).get("intent_type") or "open")
    operation_label = "close" if intent_type == "close" else "entry"
    incident_subject = str(
        (ticket or {}).get("position_projection_id")
        or (ticket or {}).get("group_id")
        or (ticket or {}).get("csa_lifecycle_id")
        or ticket_hash
        or f"{underlying}:{structure}"
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "incident_subject": incident_subject,
                "underlying": underlying,
                "intent_type": intent_type,
                "reason": reason,
                "recovery_attempted": bool(recovery.get("attempted")),
                "recovery_outcome": str(recovery.get("outcome") or ""),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if previous.get("status") == "open" and previous.get("fingerprint") == fingerprint:
        return {
            "needed": True,
            "attempted": False,
            "reason": "unchanged_selected_entry_failure",
            "fingerprint": fingerprint,
        }

    recovery_line = (
        f"One fresh rank-1 rebuild was attempted; outcome: {recovery.get('outcome')}."
        if recovery.get("attempted")
        else "No stale-ticket rebuild applied to this failure."
    )
    effect_line = (
        "The position remains open; review is needed only if the next canonical management cycle cannot recover."
        if intent_type == "close"
        else "No new position was opened. Review is needed only if you want to override or investigate this failed entry."
    )
    body = "\n".join(
        [
            f"Selected {operation_label}: {underlying} {structure}.",
            f"Placement stopped at: {reason}.",
            recovery_line,
            effect_line,
        ]
    )
    raw_mode = str(
        (((config.get("live") or {}).get("stale_entry_recovery") or {}).get("notification_mode") or "live")
    ).strip().lower()
    mode = raw_mode if raw_mode in {"off", "spool", "live"} else "live"
    alert = send_lathi_alert(
        title=f"Kamandal selected {operation_label} not placed: {underlying}",
        body=body,
        level="error",
        mode=mode,
        profile=default_lathi_bus_profile(),
    )
    store.event(
        SELECTED_ENTRY_ATTENTION_STATE_EVENT,
        {
            "status": "open",
            "fingerprint": fingerprint,
            "reason": reason,
            "ticket_hash": ticket_hash,
            "incident_subject": incident_subject,
            "underlying": underlying,
            "intent_type": intent_type,
            "notification_ok": alert.ok,
            "notification_mode": alert.mode,
        },
    )
    return {
        "needed": True,
        "attempted": alert.attempted,
        "ok": alert.ok,
        "mode": alert.mode,
        "reason": reason,
        "fingerprint": fingerprint,
    }


def _defer_ticket_for_window(
    store: LocalStore,
    ticket: dict[str, Any],
    window: dict[str, Any],
) -> dict[str, Any]:
    if str(window.get("reason") or "") == "entry_not_open":
        status = WAITING_ENTRY_WINDOW
    else:
        status = "deferred_market_closed" if str(window.get("intent_type")) == "close" else "deferred_entry_cutoff"
    ticket_hash = str(ticket.get("ticket_hash") or "")
    store.update_live_order_intent_status_with_payload(
        ticket_hash,
        status,
        {"submission_window": window},
    )
    store.event(
        "live_order_submission_deferred",
        {
            "ticket_hash": ticket_hash,
            "order_id": ticket.get("order_id"),
            "status": status,
            "submission_window": window,
        },
    )
    return {
        "ticket_hash": ticket_hash,
        "order_id": ticket.get("order_id"),
        "status": status,
        "reason": window.get("reason"),
        "submission_window": window,
    }


def _loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
