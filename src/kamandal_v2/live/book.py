"""Per-group live book cockpit."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from kamandal_v2.live.health import entry_health_gate
from kamandal_v2.schemas import LIVE_BOOK_HEADER
from kamandal_v2.stores.sqlite import LocalStore


WORKING_OPEN_STATUSES = {"pending_approval", "dry_run", "submitted", "repriced"}
NONTERMINAL_CLOSE_STATUSES = {"pending_close_approval", "dry_run", "submitted", "repriced"}
HEALTH_ROW_SYMBOL = "_HEALTH_"


def run_live_book(store: LocalStore, config: dict[str, Any] | None = None) -> dict[str, Any]:
    _ = config or {}
    checked_at = datetime.now(UTC)
    checked_at_cst = checked_at.astimezone(ZoneInfo("America/Chicago")).isoformat(timespec="seconds")
    groups = store.open_live_position_groups()
    open_orders = store.live_order_intents_by_type("open")
    close_orders = store.live_order_intents_by_type("close")
    open_reconciliation_issues = store.open_live_reconciliation_issues()
    rows = []
    for group in groups:
        group_id = str(group.get("group_id") or "")
        candidate = group.get("candidate") or {}
        mark = store.latest_live_position_mark(group_id) or {}
        stats = store.live_position_mark_stats(group_id)
        management = store.latest_live_management_decision(group_id) or {}
        group_close_orders = [order for order in close_orders if str(order.get("group_id") or "") == group_id]
        group_open_orders = _open_orders_for_group(open_orders, group)
        attempts = store.live_order_attempts_for_ticket_hashes(
            {str(order.get("ticket_hash") or "") for order in [*group_open_orders, *group_close_orders]}
        )
        reconciliation = _reconciliation_status(open_reconciliation_issues, group_id, str(group.get("underlying") or candidate.get("underlying") or ""))
        row = {
            "updated_at_cst": checked_at_cst,
            "symbol": str(mark.get("underlying") or group.get("underlying") or candidate.get("underlying") or ""),
            "structure": str(mark.get("structure") or group.get("structure") or candidate.get("structure") or ""),
            "playbook_id": str(mark.get("playbook_id") or group.get("playbook_id") or candidate.get("playbook_id") or ""),
            "group_id": group_id,
            "entry_date": str(mark.get("opened_at") or group.get("opened_at") or "")[:10],
            "dte": _dte(mark, group),
            "entry_kind": str(mark.get("entry_kind") or ""),
            "entry_credit_debit": _entry_credit_debit(mark, group),
            "current_mark": _number(mark.get("close_mid_net")),
            "unrealized_pnl": _number(mark.get("pnl_mid")),
            "target_profit": _number(mark.get("target_profit")),
            "loss_threshold": _loss_threshold(mark),
            "mfe": _number(stats.get("mfe")),
            "mae": _number(stats.get("mae")),
            "working_open_orders": _summarize_orders(group_open_orders, WORKING_OPEN_STATUSES),
            "working_close_orders": _summarize_orders(group_close_orders, NONTERMINAL_CLOSE_STATUSES),
            "last_close_attempt": _last_close_attempt(group_close_orders),
            "last_preflight_result": _last_preflight_result(attempts),
            "broker_error_code": _broker_error_code(attempts),
            "reconciliation_status": reconciliation,
            "management_blocker": _management_blocker(management),
            "recommended_action": _recommended_action(management),
            "quote_fresh": bool(mark.get("quote_fresh")) if mark else "",
            "target_progress_pct": _number(mark.get("target_progress_pct")),
        }
        row["report_json"] = json.dumps({**row, "mark": mark, "management": management}, separators=(",", ":"), sort_keys=True)
        rows.append(row)
    rows.sort(key=lambda row: (str(row.get("symbol") or ""), str(row.get("entry_date") or ""), str(row.get("group_id") or "")))
    gate = entry_health_gate(store, config)
    rows.insert(0, _health_row(gate, checked_at_cst))
    return {
        "checked_at": checked_at.isoformat(),
        "updated_at_cst": checked_at_cst,
        "rows": rows,
        "open_groups": len(groups),
        "health_gate": gate,
    }


def live_book_sheet_rows(report: dict[str, Any], header: list[str]) -> list[list[Any]]:
    return [[row.get(column, "") for column in header] for row in report.get("rows") or []]


def format_live_book(report: dict[str, Any]) -> str:
    rows = list(report.get("rows") or [])
    lines = [f"LIVE BOOK groups={report.get('open_groups', len(rows))} checked_at={report.get('checked_at', '')}"]
    if not rows:
        lines.append("- no open live position groups")
        return "\n".join(lines)
    lines.append("symbol | structure | entry | dte | pnl | target | mfe/mae | mgmt | close_orders | recon")
    for row in rows:
        lines.append(
            " | ".join(
                [
                    str(row.get("symbol") or ""),
                    str(row.get("structure") or ""),
                    str(row.get("entry_date") or ""),
                    str(row.get("dte") or ""),
                    _fmt(row.get("unrealized_pnl")),
                    _fmt(row.get("target_profit")),
                    f"{_fmt(row.get('mfe'))}/{_fmt(row.get('mae'))}",
                    str(row.get("recommended_action") or ""),
                    str(row.get("working_close_orders") or ""),
                    str(row.get("reconciliation_status") or ""),
                ]
            )
        )
    if "sheet_rows_written" in report:
        lines.append(f"- sheet_rows_written={report['sheet_rows_written']}")
    return "\n".join(lines)


def _health_row(gate: dict[str, Any], checked_at_cst: str) -> dict[str, Any]:
    counts = gate.get("counts") or {}
    reasons = ",".join(gate.get("reasons") or [])
    row = {column: "" for column in LIVE_BOOK_HEADER}
    row.update(
        {
            "updated_at_cst": checked_at_cst,
            "symbol": HEALTH_ROW_SYMBOL,
            "structure": str(gate.get("overall") or ""),
            "management_blocker": reasons,
            "recommended_action": "entries_blocked:health_red" if gate.get("blocked") else "entries_allowed",
            "working_close_orders": f"working:{counts.get('working_close_orders', 0)},failed:{counts.get('failed_close_orders', 0)}",
            "reconciliation_status": f"blockers:{counts.get('reconciliation_blockers', 0)}",
        }
    )
    row["report_json"] = json.dumps({**gate, "updated_at_cst": checked_at_cst}, separators=(",", ":"), sort_keys=True)
    return row


def _open_orders_for_group(orders: list[dict[str, Any]], group: dict[str, Any]) -> list[dict[str, Any]]:
    # No plan_id fallback: a plan can hold orders for sibling candidates, which would
    # misattribute their orders to this group's row.
    group_id = str(group.get("group_id") or "")
    order_id = str(group.get("order_id") or "")
    candidate_id = str(group.get("candidate_id") or "")
    result = []
    for order in orders:
        if group_id and str(order.get("group_id") or "") == group_id:
            result.append(order)
            continue
        if order_id and str(order.get("order_id") or "") == order_id:
            result.append(order)
            continue
        if candidate_id and str(order.get("candidate_id") or "") == candidate_id:
            result.append(order)
    return result


def _dte(mark: dict[str, Any], group: dict[str, Any]) -> Any:
    dte = mark.get("dte") or {}
    if dte.get("remaining") not in (None, ""):
        return dte.get("remaining")
    expirations = []
    for leg in ((group.get("candidate") or {}).get("legs") or []):
        raw = str(leg.get("expiration") or "")
        if not raw:
            continue
        try:
            expirations.append(date.fromisoformat(raw))
        except ValueError:
            pass
    return (min(expirations) - date.today()).days if expirations else ""


def _entry_credit_debit(mark: dict[str, Any], group: dict[str, Any]) -> Any:
    value = _number(mark.get("entry_net_cashflow"))
    if value not in ("", None):
        return value
    candidate = group.get("candidate") or {}
    net_credit = _number(candidate.get("net_credit"))
    if net_credit in ("", None):
        return ""
    return round(float(net_credit) * 100.0, 2)


def _loss_threshold(mark: dict[str, Any]) -> Any:
    entry_value = _as_float(mark.get("entry_value"))
    multiple = _as_float(mark.get("max_loss_multiple"))
    if entry_value is None or multiple is None:
        return ""
    return round(-(entry_value * multiple), 2)


def _summarize_orders(orders: list[dict[str, Any]], statuses: set[str]) -> str:
    working = [order for order in orders if str(order.get("_ledger_status") or "") in statuses]
    if not working:
        return ""
    counts: dict[str, int] = {}
    for order in working:
        status = str(order.get("_ledger_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return ",".join(f"{status}:{count}" for status, count in sorted(counts.items()))


def _last_close_attempt(orders: list[dict[str, Any]]) -> str:
    if not orders:
        return ""
    order = max(orders, key=lambda item: str(item.get("_ledger_updated_at") or item.get("_ledger_created_at") or ""))
    status = str(order.get("_ledger_status") or "")
    updated = str(order.get("_ledger_updated_at") or order.get("_ledger_created_at") or "")
    return f"{status} {updated}".strip()


def _last_preflight_result(attempts: list[dict[str, Any]]) -> str:
    preflights = [attempt for attempt in attempts if "preflight" in str(attempt.get("action") or "")]
    if not preflights:
        return ""
    attempt = max(preflights, key=lambda item: str(item.get("created_at") or ""))
    response = attempt.get("response_payload") or {}
    ok = bool(response.get("ok", attempt.get("ok")))
    message = str(response.get("message") or "")
    return f"{attempt.get('action')} ok={ok} {message}".strip()


def _broker_error_code(attempts: list[dict[str, Any]]) -> str:
    for attempt in attempts:
        code = _find_error_code(attempt.get("response_payload") or {})
        if code:
            return code
    return ""


def _find_error_code(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("error_code", "errorCode", "code", "statusCode"):
            if value.get(key) not in (None, ""):
                return str(value.get(key))
        for child in value.values():
            found = _find_error_code(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_error_code(child)
            if found:
                return found
    if isinstance(value, str):
        match = re.search(r'"code"\s*:\s*"?([A-Za-z0-9_-]+)"?', value)
        if match:
            return match.group(1)
        match = re.search(r"\bcode[=:]\s*([A-Za-z0-9_-]+)", value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _reconciliation_status(issues: list[dict[str, Any]], group_id: str, underlying: str) -> str:
    matching = [
        issue
        for issue in issues
        if (group_id and str(issue.get("group_id") or "") == group_id)
        or (underlying and str(issue.get("underlying") or "") == underlying)
    ]
    if not matching:
        return "ok"
    return ",".join(sorted({str(issue.get("issue_type") or "open_issue") for issue in matching}))


def _management_blocker(management: dict[str, Any]) -> str:
    reason = str(management.get("_reason") or management.get("reason") or "")
    action = str(management.get("_action") or management.get("action") or "")
    if action == "hold" and reason not in {"", "no_exit"}:
        return reason
    blocked_reason = str(management.get("blocked_reason") or "")
    return blocked_reason


def _recommended_action(management: dict[str, Any]) -> str:
    action = str(management.get("_action") or management.get("action") or "")
    reason = str(management.get("_reason") or management.get("reason") or "")
    urgency = str(management.get("urgency") or "")
    if not action and not reason:
        return ""
    parts = [part for part in [action, reason, urgency] if part]
    return ":".join(parts)


def _number(value: Any) -> Any:
    as_float = _as_float(value)
    return "" if as_float is None else round(as_float, 2)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any) -> str:
    if value in ("", None):
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)
