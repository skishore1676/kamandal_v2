"""Live health readback and severity reporting."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kamandal_v2.live.risk_manager import evaluate_entry_risk
from kamandal_v2.stores.sqlite import LocalStore


DEFAULT_STALE_CLOSE_ORDER_MINUTES = 120
DEFAULT_EXIT_PIPELINE_STALLED_MINUTES = 20
DEFAULT_URGENT_CLOSE_ORDER_STALE_MINUTES = 30
APPROVED_CLOSE_PENDING_SUBMIT = "approved_close_pending_submit"
LOCAL_CLOSE_PIPELINE_STATUSES = {"pending_close_approval", APPROVED_CLOSE_PENDING_SUBMIT, "dry_run"}
WORKING_CLOSE_STATUSES = {"submitted", "repriced"}
CLOSED_CLOSE_STATUSES = {"filled", "manual_fill_recorded", "close_filled"}
NON_ACTIONABLE_TERMINAL_CLOSE_STATUSES = {"expired_stale_close_approval", "retired_stale_close_failure", "rejected_by_operator", "expired_eod"}
FAILED_CLOSE_STATUSES = {
    "rejected",
    "failed",
    "expired",
    "canceled",
    "cancelled",
    "BROKER_STATUS_FETCH_FAILED",
}
FAILED_CLOSE_PREFIXES = ("blocked_", "reprice_")
PREFLIGHT_FAIL_CLOSE_STATUSES = {
    "blocked_preflight_failed",
    "blocked_preflight_stale",
    "blocked_same_day_close",
    "submit_failed",
    "reprice_submit_failed",
}
REASON_ORDER = [
    "risk_daily_drawdown_breaker",
    "risk_weekly_drawdown_breaker",
    "risk_consecutive_loss_cooldown",
    "risk_daily_new_position_cap",
    "portfolio_bpr_over_hard_cap",
    "reconciliation_blocker",
    "failed_close_order",
    "failed_preflight_close",
    "exit_pipeline_stalled",
    "urgent_close_order_stale",
    "loss_watch",
    "portfolio_bpr_over_target",
    "close_order_stale",
    "stale_failed_close_order",
    "position_target_reached",
    "working_close_order",
    "exit_pipeline_pending",
    "pending_entry_approvals",
]


def run_live_health(
    store: LocalStore,
    config: dict[str, Any] | None = None,
    *,
    stale_close_order_minutes: int | None = None,
) -> dict[str, Any]:
    config = config or {}
    configured_stale = _int_value(
        config.get("live", {}).get("health", {}).get("stale_close_order_minutes"),
        default=DEFAULT_STALE_CLOSE_ORDER_MINUTES,
    )
    stale_minutes = int(stale_close_order_minutes if stale_close_order_minutes is not None else configured_stale)
    stalled_minutes = _exit_pipeline_stalled_minutes(config)
    urgent_stale_minutes = _urgent_close_order_stale_minutes(config)

    open_groups = store.open_live_position_groups()
    open_group_ids = {str(group.get("group_id") or "") for group in open_groups}
    reconciliation_blockers = [
        issue
        for issue in store.open_live_reconciliation_issues()
        if not _is_pending_confirmation_issue(issue)
    ]
    pending_entry_approvals = store.live_order_intents_by_type("open", statuses={"pending_approval"})
    risk = _risk_overview(store, config)

    events: list[dict[str, Any]] = []
    group_marks: list[dict[str, Any]] = []
    for group in open_groups:
        group_id = str(group.get("group_id") or "")
        if not group_id:
            continue
        mark = store.latest_live_position_mark(group_id)
        if not mark:
            continue
        group_mark = _mark_overview(group_id, mark)
        group_marks.append(group_mark)
        _collect_mark_events(group_mark, events)

    close_orders = [
        order
        for order in store.live_order_intents_by_type("close")
        if str(order.get("group_id") or "") in open_group_ids
    ]
    latest_close_ticket_by_group = _latest_close_ticket_by_group(close_orders)
    close_findings = []
    for order in close_orders:
        finding = _close_order_finding(order)
        finding["group_id"] = finding["group_id"] or str(order.get("group_id") or "")
        if finding["reason"] in {"working_close_order", "close_order_stale", "exit_pipeline_pending"}:
            finding["age_minutes"] = _order_age_minutes(order, now=_utc_now())
            if finding["reason"] == "exit_pipeline_pending" and (finding["age_minutes"] or 0.0) > stalled_minutes:
                finding["reason"] = "exit_pipeline_stalled"
            elif (
                finding["reason"] in {"working_close_order", "close_order_stale"}
                and _is_urgent_close(order)
                and (finding["age_minutes"] or 0.0) > urgent_stale_minutes
            ):
                finding["reason"] = "urgent_close_order_stale"
            elif finding["reason"] in {"working_close_order", "close_order_stale"} and (finding["age_minutes"] or 0.0) > stale_minutes:
                finding["reason"] = "close_order_stale"
        if finding["is_failed_close"]:
            _demote_stale_failed_close(store, finding, order, latest_close_ticket_by_group)
        close_findings.append(finding)

    for issue in reconciliation_blockers:
        events.append(
            {
                "severity": "red",
                "reason": "reconciliation_blocker",
                "detail": issue.get("issue_type") or "open_reconciliation_blocker",
                "group_id": str(issue.get("group_id") or ""),
                "issue_id": str(issue.get("issue_id") or ""),
                "status": str(issue.get("status") or "open"),
                "observed_count": int(issue.get("observed_count") or 1),
            },
        )

    for finding in close_findings:
        if finding["reason"] == "close_order_stale":
            events.append(
                {
                    "severity": "yellow",
                    "reason": "close_order_stale",
                    "detail": _close_order_detail(finding, stale_minutes),
                    "group_id": finding.get("group_id"),
                    "ticket_hash": finding.get("ticket_hash"),
                    "age_minutes": finding.get("age_minutes"),
                },
            )
            continue
        if finding["reason"] == "working_close_order":
            events.append(
                {
                    "severity": "yellow",
                    "reason": "working_close_order",
                    "detail": _close_order_detail(finding, stale_minutes),
                    "group_id": finding.get("group_id"),
                    "ticket_hash": finding.get("ticket_hash"),
                    "age_minutes": finding.get("age_minutes"),
                },
            )
            continue
        if finding["reason"] == "urgent_close_order_stale":
            events.append(
                {
                    "severity": "red",
                    "reason": "urgent_close_order_stale",
                    "detail": _close_order_detail(finding, urgent_stale_minutes),
                    "group_id": finding.get("group_id"),
                    "ticket_hash": finding.get("ticket_hash"),
                    "age_minutes": finding.get("age_minutes"),
                },
            )
            continue
        if finding["reason"] == "exit_pipeline_stalled":
            events.append(
                {
                    "severity": "red",
                    "reason": "exit_pipeline_stalled",
                    "detail": _close_order_detail(finding, stalled_minutes),
                    "group_id": finding.get("group_id"),
                    "ticket_hash": finding.get("ticket_hash"),
                    "age_minutes": finding.get("age_minutes"),
                },
            )
            continue
        if finding["reason"] == "exit_pipeline_pending":
            events.append(
                {
                    "severity": "yellow",
                    "reason": "exit_pipeline_pending",
                    "detail": _close_order_detail(finding, stalled_minutes),
                    "group_id": finding.get("group_id"),
                    "ticket_hash": finding.get("ticket_hash"),
                    "age_minutes": finding.get("age_minutes"),
                },
            )
            continue
        if finding["reason"] == "failed_preflight_close":
            events.append(
                {
                    "severity": "red",
                    "reason": "failed_preflight_close",
                    "detail": finding.get("order_status") or "",
                    "group_id": finding.get("group_id"),
                    "ticket_hash": finding.get("ticket_hash"),
                },
            )
            continue
        if finding["reason"] == "failed_close_order":
            events.append(
                {
                    "severity": "red",
                    "reason": "failed_close_order",
                    "detail": finding.get("order_status") or "",
                    "group_id": finding.get("group_id"),
                    "ticket_hash": finding.get("ticket_hash"),
                },
            )
            continue
        if finding["reason"] == "stale_failed_close_order":
            events.append(
                {
                    "severity": "yellow",
                    "reason": "stale_failed_close_order",
                    "detail": finding.get("order_status") or "",
                    "group_id": finding.get("group_id"),
                    "ticket_hash": finding.get("ticket_hash"),
            },
        )

    if risk["severity"]:
        events.append(
            {
                "severity": risk["severity"],
                "reason": risk["reason"],
                "detail": risk["detail"],
                "snapshot_id": risk.get("snapshot_id") or "",
                "bpr_used_pct": risk.get("bpr_used_pct"),
                "hard_max_bpr_utilization_pct": risk.get("hard_max_bpr_utilization_pct"),
                "target_max_bpr_utilization_pct": risk.get("target_max_bpr_utilization_pct"),
            },
        )

    if pending_entry_approvals:
        events.append(
            {
                "severity": "yellow",
                "reason": "pending_entry_approvals",
                "detail": f"{len(pending_entry_approvals)} unsubmitted open intents need approval/expiry hygiene",
            },
        )

    risk_manager_decision = evaluate_entry_risk(store, config)
    for reason in risk_manager_decision.reasons:
        events.append(
            {
                "severity": str(reason.get("severity") or "yellow"),
                "reason": str(reason.get("code") or "risk_manager"),
                "detail": str(reason.get("detail") or ""),
            },
        )

    status = _coerce_status(events)
    return {
        "checked_at": _utc_now().isoformat(),
        "overall": status,
        "risk_manager": risk_manager_decision.to_dict(),
        "scale": _scale(status, events, risk, pending_entry_approvals),
        "risk": risk,
        "counts": {
            "open_groups": len(open_groups),
            "pending_entry_approvals": len(pending_entry_approvals),
            "reconciliation_blockers": len(reconciliation_blockers),
            "working_close_orders": len([order for order in close_findings if order["reason"] in {"working_close_order", "close_order_stale", "urgent_close_order_stale"}]),
            "exit_pipeline_pending": len([order for order in close_findings if order["reason"] == "exit_pipeline_pending"]),
            "exit_pipeline_stalled": len([order for order in close_findings if order["reason"] == "exit_pipeline_stalled"]),
            "stale_close_orders": len([order for order in close_findings if order["reason"] == "close_order_stale"]),
            "urgent_close_orders": len([order for order in close_findings if order["reason"] == "urgent_close_order_stale"]),
            "failed_close_orders": len([order for order in close_findings if order["is_failed_close"]]),
            "stale_failed_close_orders": len([order for order in close_findings if order["reason"] == "stale_failed_close_order"]),
            "target_reached_groups": len([mark for mark in group_marks if bool(mark.get("target_reached"))]),
            "loss_watch_groups": len([mark for mark in group_marks if bool(mark.get("loss_watch"))]),
        },
        "reasons": _ordered_reason_codes(events),
        "events": events,
        "close_orders": close_findings,
        "group_marks": group_marks,
    }


def entry_health_gate(store: LocalStore, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Decide whether new live entries are allowed given current book health."""
    config = config or {}
    block_on_red = _bool_value(
        (config.get("live") or {}).get("health", {}).get("block_entries_on_red"),
        default=True,
    )
    report = run_live_health(store, config)
    risk_manager = report.get("risk_manager") or {}
    risk_blocked = bool(risk_manager.get("enabled")) and bool(risk_manager.get("blocked"))
    blocked = (block_on_red and report["overall"] == "RED") or risk_blocked
    return {
        "blocked": blocked,
        "block_entries_on_red": block_on_red,
        "overall": report["overall"],
        "reasons": report["reasons"],
        "counts": report["counts"],
        "risk_manager": risk_manager,
    }


def format_live_health(report: dict[str, Any]) -> str:
    counts = report["counts"]
    scale = report.get("scale") or {}
    risk = report.get("risk") or {}
    lines = [
        "LIVE HEALTH "
        f"[{report['overall']}] "
        f"score={scale.get('score', '')} "
        f"groups={counts['open_groups']} "
        f"pending_entries={counts.get('pending_entry_approvals', 0)} "
        f"working_close={counts['working_close_orders']} "
        f"recon_blockers={counts['reconciliation_blockers']} "
        f"stale_close={counts['stale_close_orders']} "
        f"failed_close={counts['failed_close_orders']}"
    ]
    if risk.get("bpr_used_pct") is not None:
        lines.append(
            "- risk "
            f"bpr_used_pct={risk['bpr_used_pct']:.2f} "
            f"target={risk.get('target_max_bpr_utilization_pct') or ''} "
            f"hard={risk.get('hard_max_bpr_utilization_pct') or ''}"
        )
    if not report["events"]:
        lines.append("- no open lifecycle blockers")
        return "\n".join(lines)
    for event in sorted(report["events"], key=_event_sort_key):
        detail = str(event.get("detail") or "")
        group_id = f" group={event.get('group_id')}" if event.get("group_id") else ""
        lines.append(f"- {event['reason']}{group_id}: {detail}")
    return "\n".join(lines)


def _is_pending_confirmation_issue(issue: dict[str, Any]) -> bool:
    decision = issue.get("decision") or {}
    return str(decision.get("tier") or "") == "pending_confirmation"


def _collect_mark_events(group_mark: dict[str, Any], events: list[dict[str, Any]]) -> None:
    if bool(group_mark.get("target_reached")):
        events.append(
            {
                "severity": "yellow",
                "reason": "position_target_reached",
                "detail": _format_mark_summary(group_mark),
                "group_id": group_mark.get("group_id"),
            },
        )
    if bool(group_mark.get("loss_watch")):
        events.append(
            {
                "severity": "red",
                "reason": "loss_watch",
                "detail": _format_mark_summary(group_mark),
                "group_id": group_mark.get("group_id"),
            },
        )


def _mark_overview(group_id: str, mark: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_id": group_id,
        "underlying": str(mark.get("underlying") or ""),
        "target_progress_pct": float(mark.get("target_progress_pct") or 0.0),
        "trigger_progress_pct": float(mark.get("trigger_progress_pct") or 0.0),
        "target_reached": bool(mark.get("target_reached") or _target_reached(mark)),
        "loss_watch": bool(mark.get("loss_watch") or bool(mark.get("max_loss_watch"))),
        "loss_watch_observations": mark.get("loss_watch_observations") or {},
        "updated_at": str(mark.get("marked_at") or mark.get("updated_at") or mark.get("created_at") or ""),
    }


def _latest_close_ticket_by_group(close_orders: list[dict[str, Any]]) -> dict[str, str]:
    latest: dict[str, tuple[str, str]] = {}
    for order in close_orders:
        group_id = str(order.get("group_id") or "")
        if not group_id:
            continue
        stamp = _order_timestamp(order)
        if group_id not in latest or stamp >= latest[group_id][0]:
            latest[group_id] = (stamp, str(order.get("ticket_hash") or ""))
    return {group_id: ticket_hash for group_id, (_, ticket_hash) in latest.items()}


def _demote_stale_failed_close(
    store: LocalStore,
    finding: dict[str, Any],
    order: dict[str, Any],
    latest_close_ticket_by_group: dict[str, str],
) -> None:
    """A failed close only blocks the book while the system still wants that exit.

    Superseded tickets (a newer close intent exists for the group) carry no signal.
    A failed latest ticket is demoted to yellow once a newer management decision
    says hold/no_exit — the exit condition that produced it has passed.
    """
    group_id = str(finding.get("group_id") or "")
    latest_ticket = latest_close_ticket_by_group.get(group_id)
    if latest_ticket and latest_ticket != str(finding.get("ticket_hash") or ""):
        finding["reason"] = "superseded_close_order"
        finding["is_failed_close"] = False
        return
    decision = store.latest_live_management_decision(group_id) or {}
    decision_is_no_exit = (
        str(decision.get("_action") or "") == "hold" and str(decision.get("_reason") or "") == "no_exit"
    )
    decision_stamp = _normalize_stamp(str(decision.get("_created_at") or ""))
    if decision_is_no_exit and decision_stamp >= _order_timestamp(order):
        finding["reason"] = "stale_failed_close_order"
        finding["is_failed_close"] = False


def _order_timestamp(order: dict[str, Any]) -> str:
    return _normalize_stamp(str(order.get("_ledger_updated_at") or order.get("_ledger_created_at") or ""))


def _normalize_stamp(raw: str) -> str:
    return raw.replace("T", " ").replace("Z", "").strip()


def _close_order_finding(order: dict[str, Any]) -> dict[str, Any]:
    status = str(order.get("_ledger_status") or "")
    ticket_hash = str(order.get("ticket_hash") or "")
    group_id = str(order.get("group_id") or "")
    is_failed_close = False
    reason = "working_close_order"
    if status in CLOSED_CLOSE_STATUSES:
        reason = "close_completed"
    elif status in NON_ACTIONABLE_TERMINAL_CLOSE_STATUSES:
        reason = "close_expired"
    elif status in LOCAL_CLOSE_PIPELINE_STATUSES:
        reason = "exit_pipeline_pending"
    elif status in WORKING_CLOSE_STATUSES:
        reason = "working_close_order"
    elif status in PREFLIGHT_FAIL_CLOSE_STATUSES:
        reason = "failed_preflight_close"
        is_failed_close = True
    elif status in FAILED_CLOSE_STATUSES or _status_has_failed_prefix(status):
        reason = "failed_close_order"
        is_failed_close = True
    elif status:
        reason = "failed_close_order"
        is_failed_close = True
    elif status == "":
        reason = "failed_close_order"
        is_failed_close = True
    return {
        "ticket_hash": ticket_hash,
        "group_id": group_id,
        "order_status": status,
        "reason": reason,
        "is_failed_close": is_failed_close,
    }


def _close_order_detail(finding: dict[str, Any], stale_minutes: int) -> str:
    status = str(finding.get("order_status") or "")
    group_id = str(finding.get("group_id") or "unknown_group")
    age = finding.get("age_minutes")
    if finding["reason"] in {"close_order_stale", "urgent_close_order_stale"} and isinstance(age, (int, float)):
        return (
            f"status={status} group={group_id} age={age:.1f}m "
            f"older than {stale_minutes}m"
        )
    if finding["reason"] == "exit_pipeline_stalled" and isinstance(age, (int, float)):
        return (
            f"status={status} group={group_id} age={age:.1f}m "
            f"older than {stale_minutes}m"
        )
    if isinstance(age, (int, float)):
        return f"status={status} group={group_id} age={age:.1f}m"
    return f"status={status} group={group_id}"


def _ordered_reason_codes(events: list[dict[str, Any]]) -> list[str]:
    known: list[str] = []
    seen: set[str] = set()
    for reason in REASON_ORDER:
        if any(event.get("reason") == reason for event in events) and reason not in seen:
            seen.add(reason)
            known.append(reason)
    for event in events:
        reason = str(event.get("reason") or "")
        if reason and reason not in seen:
            seen.add(reason)
            known.append(reason)
    return known


def _coerce_status(events: list[dict[str, Any]]) -> str:
    if any(str(event.get("severity")) == "red" for event in events):
        return "RED"
    if any(str(event.get("severity")) == "yellow" for event in events):
        return "YELLOW"
    return "GREEN"


def _scale(status: str, events: list[dict[str, Any]], risk: dict[str, Any], pending_entry_approvals: list[dict[str, Any]]) -> dict[str, Any]:
    score = {"GREEN": 100, "YELLOW": 70, "RED": 35}.get(status, 50)
    if risk.get("reason") == "portfolio_bpr_over_hard_cap":
        score = min(score, 25)
    if pending_entry_approvals:
        score = min(score, 70)
    return {
        "rating": status,
        "score": score,
        "summary": _scale_summary(status, events),
    }


def _scale_summary(status: str, events: list[dict[str, Any]]) -> str:
    if status == "GREEN":
        return "clean_live_book"
    reasons = _ordered_reason_codes(events)
    return ",".join(reasons) if reasons else "needs_review"


def _risk_overview(store: LocalStore, config: dict[str, Any]) -> dict[str, Any]:
    snapshot = store.latest_account_snapshot()
    portfolio = config.get("portfolio") or {}
    target_pct = _optional_float(portfolio.get("target_max_bpr_utilization_pct"))
    hard_pct = _optional_float(portfolio.get("hard_max_bpr_utilization_pct"))
    result: dict[str, Any] = {
        "snapshot_id": "",
        "account_size": None,
        "bpr_used": None,
        "bpr_used_pct": None,
        "target_max_bpr_utilization_pct": target_pct,
        "hard_max_bpr_utilization_pct": hard_pct,
        "severity": "",
        "reason": "",
        "detail": "no account snapshot",
    }
    if not snapshot:
        return result
    account_size = _optional_float(snapshot.get("account_size"))
    bpr_used = _optional_float(snapshot.get("bpr_used"))
    bpr_pct = _optional_float(snapshot.get("bpr_used_pct"))
    if bpr_pct is None and account_size and bpr_used is not None:
        bpr_pct = (bpr_used / max(account_size, 1.0)) * 100.0
    result.update(
        {
            "snapshot_id": str(snapshot.get("_snapshot_id") or ""),
            "account_size": account_size,
            "bpr_used": bpr_used,
            "bpr_used_pct": round(bpr_pct, 2) if bpr_pct is not None else None,
            "detail": "account snapshot available",
        },
    )
    if bpr_pct is None:
        return result
    if hard_pct is not None and bpr_pct > hard_pct:
        result.update(
            {
                "severity": "red",
                "reason": "portfolio_bpr_over_hard_cap",
                "detail": f"bpr_used_pct={bpr_pct:.2f} exceeds hard cap {hard_pct:.2f}",
            },
        )
    elif target_pct is not None and bpr_pct > target_pct:
        result.update(
            {
                "severity": "yellow",
                "reason": "portfolio_bpr_over_target",
                "detail": f"bpr_used_pct={bpr_pct:.2f} exceeds target cap {target_pct:.2f}",
            },
        )
    return result


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_sort_key(event: dict[str, Any]) -> tuple[int, str]:
    severity_rank = {"red": 0, "yellow": 1}.get(str(event.get("severity")), 2)
    return (severity_rank, str(event.get("reason") or ""))


def _order_age_minutes(order: dict[str, Any], *, now: datetime) -> float | None:
    raw = str(order.get("_ledger_updated_at") or order.get("_ledger_created_at") or "")
    parsed = _parse_timestamp(raw)
    if not parsed:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def _status_has_failed_prefix(status: str) -> bool:
    return any(status.startswith(prefix) for prefix in FAILED_CLOSE_PREFIXES)


def _target_reached(mark: dict[str, Any]) -> bool:
    progress = float(mark.get("target_progress_pct") or 0.0)
    trigger = float(mark.get("trigger_progress_pct") or 0.0)
    return trigger > 0.0 and progress >= trigger


def _format_mark_summary(group_mark: dict[str, Any]) -> str:
    target = float(group_mark.get("target_progress_pct") or 0.0)
    trigger = float(group_mark.get("trigger_progress_pct") or 0.0)
    loss_watch_count = int((group_mark.get("loss_watch_observations") or {}).get("count", 0) or 0)
    return (
        f"group={group_mark.get('group_id')} "
        f"target={target:.1f}/{trigger:.1f}% "
        f"loss_watch={loss_watch_count}"
    )


def _bool_value(raw: Any, *, default: bool) -> bool:
    if raw in (None, ""):
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int_value(raw: Any, *, default: int | None = None) -> int | None:
    if raw in (None, ""):
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _exit_pipeline_stalled_minutes(config: dict[str, Any]) -> int:
    raw = ((config.get("live") or {}).get("health") or {}).get("exit_pipeline_stalled_minutes")
    return int(_int_value(raw, default=DEFAULT_EXIT_PIPELINE_STALLED_MINUTES) or DEFAULT_EXIT_PIPELINE_STALLED_MINUTES)


def _urgent_close_order_stale_minutes(config: dict[str, Any]) -> int:
    raw = ((config.get("live") or {}).get("health") or {}).get("urgent_close_order_stale_minutes")
    return int(_int_value(raw, default=DEFAULT_URGENT_CLOSE_ORDER_STALE_MINUTES) or DEFAULT_URGENT_CLOSE_ORDER_STALE_MINUTES)


def _is_urgent_close(order: dict[str, Any]) -> bool:
    return str(order.get("exit_reason") or "").lower() in {"max_loss", "pre_event"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    normalized = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
