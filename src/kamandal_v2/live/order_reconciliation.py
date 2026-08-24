"""Live order lifecycle reconciliation."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from kamandal_v2.live.execution import TERMINAL_UNFILLED_ORDER_STATUSES
from kamandal_v2.live.book import live_book_sheet_rows, run_live_book
from kamandal_v2.live.health import run_live_health
from kamandal_v2.live.order_identity import broker_order_id
from kamandal_v2.market.broker import broker_adapter, default_execution_venue, ticket_execution_venue
from kamandal_v2.schemas import LIVE_BOOK_HEADER
from kamandal_v2.sheets import write_live_book
from kamandal_v2.stores.sqlite import LocalStore


DEFAULT_STALE_CLOSE_APPROVAL_MINUTES = 120
APPROVED_CLOSE_PENDING_SUBMIT = "approved_close_pending_submit"
LOCAL_ONLY_CLOSE_STATUSES = {"pending_close_approval", APPROVED_CLOSE_PENDING_SUBMIT}
BROKER_OBSERVED_STATUSES = {
    "submitted",
    "repriced",
    "reprice_blocked_preflight_failed",
    "replace_cancel_pending",
}
WORKING_BROKER_STATUSES = {"NEW", "OPEN", "WORKING", "PENDING", "ACCEPTED"}


def reconcile_live_orders(
    config: dict[str, Any],
    *,
    write_sheet: bool = False,
    dry_run: bool = False,
    expire_stale_close_approvals: bool | None = None,
    store: LocalStore | None = None,
    adapter: Any | None = None,
) -> dict[str, Any]:
    store = store or LocalStore()
    if not _order_reconciliation_enabled(config):
        return {"status": "disabled", "checked": 0, "results": [], "live_book_rows_written": 0}

    now = datetime.now(UTC)
    stale_minutes = _stale_close_approval_minutes(config)
    expire_stale = _expire_stale_close_approvals_enabled(config) if expire_stale_close_approvals is None else expire_stale_close_approvals
    tickets = sorted(
        store.live_order_intents_by_type("close", statuses={*LOCAL_ONLY_CLOSE_STATUSES, *BROKER_OBSERVED_STATUSES}),
        key=lambda item: (str(item.get("_ledger_updated_at") or ""), str(item.get("_ledger_created_at") or "")),
    )
    broker = adapter
    results = []
    for ticket in tickets:
        status = str(ticket.get("_ledger_status") or "")
        if status in LOCAL_ONLY_CLOSE_STATUSES:
            results.append(
                _reconcile_local_close_approval(
                    store,
                    ticket,
                    now=now,
                    stale_minutes=stale_minutes,
                    expire_stale=expire_stale,
                    dry_run=dry_run,
                )
            )
            continue
        if status in BROKER_OBSERVED_STATUSES:
            ticket_broker = broker
            if ticket_broker is None:
                try:
                    venue = ticket_execution_venue(config, ticket)
                    ticket_broker = (
                        broker_adapter(config)
                        if venue == default_execution_venue(config)
                        else broker_adapter(config, execution_venue=venue)
                    )
                except Exception as exc:  # noqa: BLE001 - report venue outage without cross-routing.
                    results.append(_broker_venue_unavailable_result(ticket, exc))
                    continue
            results.append(_reconcile_broker_close_order(store, ticket_broker, ticket, dry_run=dry_run))

    results.extend(_retire_stale_close_failures(store, config, dry_run=dry_run))

    live_book_rows_written = 0
    if write_sheet:
        live_book_report = run_live_book(store, config)
        live_book_rows_written = write_live_book(config, LIVE_BOOK_HEADER, live_book_sheet_rows(live_book_report, LIVE_BOOK_HEADER))
    expired = [result for result in results if result.get("reconciled_status") == "expired_stale_close_approval"]
    stale = [result for result in results if result.get("reconciled_status") in {"stale_close_approval", "expired_stale_close_approval"}]
    retired = [result for result in results if result.get("reconciled_status") == "retired_stale_close_failure"]
    return {
        "status": "ok",
        "checked": len(results),
        "stale_close_approvals": len(stale),
        "expired_stale_close_approvals": len(expired),
        "retired_stale_close_failures": len(retired),
        "results": results,
        "live_book_rows_written": live_book_rows_written,
        "dry_run": dry_run,
        "expire_stale_close_approvals": expire_stale,
    }


def _broker_venue_unavailable_result(ticket: dict[str, Any], exc: Exception) -> dict[str, Any]:
    result = _base_result(ticket, source="broker_order")
    result.update(
        {
            "execution_venue": ticket_execution_venue({}, ticket),
            "reconciled_status": "broker_venue_unavailable",
            "needs_broker_status_review": True,
            "error": _safe_broker_error(exc),
        }
    )
    return result


def _retire_stale_close_failures(store: LocalStore, config: dict[str, Any], *, dry_run: bool) -> list[dict[str, Any]]:
    """Retire failed close tickets whose exit decision has since lapsed to hold/no_exit.

    These tickets never reached the broker, so there is no broker state to
    reconcile — without retirement they hold the health gate down forever.
    """
    if not _retire_stale_close_failures_enabled(config):
        return []
    health = run_live_health(store, config)
    results = []
    for finding in health.get("close_orders") or []:
        if str(finding.get("reason") or "") != "stale_failed_close_order":
            continue
        ticket_hash = str(finding.get("ticket_hash") or "")
        if not ticket_hash:
            continue
        result = {
            "ticket_hash": ticket_hash,
            "group_id": str(finding.get("group_id") or ""),
            "ledger_status": str(finding.get("order_status") or ""),
            "source": "stale_close_failure",
            "reason": "exit_decision_lapsed_to_no_exit",
            "reconciled_status": "retired_stale_close_failure",
        }
        if not dry_run:
            store.update_live_order_intent_status_with_payload(
                ticket_hash,
                "retired_stale_close_failure",
                {
                    "order_reconciliation": {
                        "status": "retired_stale_close_failure",
                        "prior_status": result["ledger_status"],
                        "reason": result["reason"],
                        "reconciled_at": _now(),
                    }
                },
            )
            store.event("live_order_reconciled", {"ticket_hash": ticket_hash, **result})
        results.append(result)
    return results


def _retire_stale_close_failures_enabled(config: dict[str, Any]) -> bool:
    recon = ((config.get("live") or {}).get("reconciliation") or {})
    return _as_bool(recon.get("retire_stale_close_failures"), True)


def _reconcile_local_close_approval(
    store: LocalStore,
    ticket: dict[str, Any],
    *,
    now: datetime,
    stale_minutes: int,
    expire_stale: bool,
    dry_run: bool,
) -> dict[str, Any]:
    age_minutes = _ticket_age_minutes(ticket, now=now)
    result = _base_result(ticket, source="local_approval")
    result["age_minutes"] = round(age_minutes, 2) if age_minutes is not None else None
    result["stale_after_minutes"] = stale_minutes
    if age_minutes is None or age_minutes <= stale_minutes:
        result["reconciled_status"] = "still_pending_close_approval"
        return result
    result["reason"] = "local_close_approval_stale"
    if not expire_stale:
        result["reconciled_status"] = "stale_close_approval"
        result["action_required"] = "expire_stale_close_approval"
        return result
    result["reconciled_status"] = "expired_stale_close_approval"
    if not dry_run:
        _expire_ticket(store, ticket, result)
    return result


def _reconcile_broker_close_order(store: LocalStore, adapter: Any, ticket: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    result = _base_result(ticket, source="broker_order")
    try:
        response = adapter.get_order(broker_order_id(ticket))
    except Exception as exc:  # noqa: BLE001
        error = _safe_broker_error(exc)
        result.update({"broker_status": "BROKER_STATUS_FETCH_FAILED", "needs_broker_status_review": True, "error": error})
        if not dry_run:
            store.record_live_order_status(str(ticket["order_id"]), "BROKER_STATUS_FETCH_FAILED", {"error": error}, ticket_hash=str(ticket["ticket_hash"]))
        return result

    broker_status = str(response.get("status") or "UNKNOWN").upper()
    result["broker_status"] = broker_status
    if not dry_run:
        store.record_live_order_status(str(ticket["order_id"]), broker_status, response, ticket_hash=str(ticket["ticket_hash"]))
    if str(ticket.get("_ledger_status") or "") == "replace_cancel_pending":
        result["reconciled_status"] = "staged_replace_parent_observed"
        result["staged_replace_parent_status"] = broker_status
        return result
    if broker_status in {"FILLED", "PARTIALLY_FILLED"}:
        result["reconciled_status"] = "close_filled"
        if not dry_run:
            store.update_live_order_intent_status(str(ticket["ticket_hash"]), "close_filled")
        return result
    if broker_status in TERMINAL_UNFILLED_ORDER_STATUSES:
        ledger_status = broker_status.lower().replace("canceled", "cancelled")
        result["reconciled_status"] = ledger_status
        if not dry_run:
            store.update_live_order_intent_status(str(ticket["ticket_hash"]), ledger_status)
        return result
    if broker_status in WORKING_BROKER_STATUSES:
        result["reconciled_status"] = "still_working_at_broker"
        return result
    result["reconciled_status"] = "broker_status_unknown"
    result["needs_broker_status_review"] = True
    return result


def _expire_ticket(store: LocalStore, ticket: dict[str, Any], result: dict[str, Any]) -> None:
    ticket_hash = str(ticket["ticket_hash"])
    store.update_live_order_intent_status_with_payload(
        ticket_hash,
        "expired_stale_close_approval",
        {
            "order_reconciliation": {
                "status": "expired_stale_close_approval",
                "reason": result.get("reason"),
                "age_minutes": result.get("age_minutes"),
                "stale_after_minutes": result.get("stale_after_minutes"),
                "reconciled_at": _now(),
            }
        },
    )
    store.record_live_order_status(
        str(ticket["order_id"]),
        "EXPIRED_STALE_CLOSE_APPROVAL",
        {
            "reason": "local_close_approval_stale",
            "ticket_hash": ticket_hash,
            "age_minutes": result.get("age_minutes"),
            "stale_after_minutes": result.get("stale_after_minutes"),
        },
        ticket_hash=ticket_hash,
    )
    store.event("live_order_reconciled", {"ticket_hash": ticket_hash, **result})


def _base_result(ticket: dict[str, Any], *, source: str) -> dict[str, Any]:
    return {
        "ticket_hash": str(ticket.get("ticket_hash") or ""),
        "order_id": str(ticket.get("order_id") or ""),
        "group_id": str(ticket.get("group_id") or ""),
        "underlying": str(ticket.get("underlying") or ""),
        "ledger_status": str(ticket.get("_ledger_status") or ""),
        "source": source,
    }


def _ticket_age_minutes(ticket: dict[str, Any], *, now: datetime) -> float | None:
    raw = str(ticket.get("_ledger_updated_at") or ticket.get("_ledger_created_at") or ticket.get("updated_at") or ticket.get("created_at") or "")
    if not raw:
        return None
    try:
        parsed = _parse_timestamp(raw)
    except ValueError:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _order_reconciliation_enabled(config: dict[str, Any]) -> bool:
    recon = ((config.get("live") or {}).get("reconciliation") or {})
    return _as_bool(recon.get("order_reconciliation_enabled"), True)


def _stale_close_approval_minutes(config: dict[str, Any]) -> int:
    recon = ((config.get("live") or {}).get("reconciliation") or {})
    health = ((config.get("live") or {}).get("health") or {})
    return int(recon.get("stale_close_approval_minutes") or health.get("stale_close_order_minutes") or DEFAULT_STALE_CLOSE_APPROVAL_MINUTES)


def _expire_stale_close_approvals_enabled(config: dict[str, Any]) -> bool:
    recon = ((config.get("live") or {}).get("reconciliation") or {})
    return _as_bool(recon.get("expire_stale_close_approvals"), False)


def _safe_broker_error(exc: Exception) -> str:
    return re.sub(r"/trading/[^/]+/", "/trading/<account>/", str(exc))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
