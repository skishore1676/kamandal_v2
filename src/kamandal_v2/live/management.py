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
from kamandal_v2.live.orders import APPROVE_LIVE_CLOSE, REJECT_CLOSE, build_close_ticket
from kamandal_v2.live.position_management import live_exit_decision, live_exit_policy, mark_live_group
from kamandal_v2.live.reconciliation import reconciliation_blockers_for_group
from kamandal_v2.market.broker import broker_adapter
from kamandal_v2.planner.config_loader import load_planner_config
from kamandal_v2.schemas import DAILY_PLAN_HEADER, LIVE_BOOK_HEADER
from kamandal_v2.sheets import pull_sheet_tables, write_daily_plan, write_live_book
from kamandal_v2.stores.sqlite import LocalStore


APPROVED_CLOSE_PENDING_SUBMIT = "approved_close_pending_submit"
EXPIRED_EOD_STATUS = "expired_eod"
LOCAL_CLOSE_PIPELINE_STATUSES = {"pending_close_approval", APPROVED_CLOSE_PENDING_SUBMIT, "dry_run"}
BROKER_WORKING_CLOSE_STATUSES = {"submitted", "repriced", "reprice_blocked_preflight_failed"}
NONTERMINAL_CLOSE_STATUSES = {*LOCAL_CLOSE_PIPELINE_STATUSES, *BROKER_WORKING_CLOSE_STATUSES}


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
    exit_submit_source = _exit_submit_source(config)
    operator_commands = _apply_close_operator_commands(config, store) if exit_submit_source == "ledger" else {"retired": 0, "blocked": 0}
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
                working_reason = "working_close_order"
                if str(working_close.get("_ledger_status") or "") in LOCAL_CLOSE_PIPELINE_STATUSES:
                    working_reason = "exit_pipeline_pending"
                decision = {
                    **decision,
                    "action": "hold",
                    "blocked_action": "close",
                    "blocked_reason": str(decision.get("reason") or ""),
                    "reason": working_reason,
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
        _annotate_exit_ticket(ticket, decision)
        close_status = "pending_close_approval"
        if exit_mode == "auto_rules" and exit_submit_source == "ledger":
            close_status = APPROVED_CLOSE_PENDING_SUBMIT
        store.save_live_order_intent(ticket, status=close_status)
        rows.append(_close_plan_row(index, group, decision, ticket, approval_mode=exit_mode, submit_source=exit_submit_source))
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
        "operator_commands": operator_commands,
        "live_book_rows_written": live_book_rows_written,
        "marks": marks,
        "decisions": decisions,
        "daily_plan_rows": rows,
    }


DEAD_CLOSE_STATUSES = {
    "cancelled",
    "canceled",
    "expired",
    EXPIRED_EOD_STATUS,
    "deferred_market_closed",
    "rejected",
    "failed",
    "expired_stale_close_approval",
    "retired_stale_close_failure",
    "rejected_by_operator",
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


def _apply_close_operator_commands(config: dict[str, Any], store: LocalStore) -> dict[str, Any]:
    try:
        rows = pull_sheet_tables(config).get("daily_plan") or []
    except Exception as exc:  # noqa: BLE001
        store.event("live_close_operator_commands_read_failed", {"error": str(exc)})
        return {"retired": 0, "blocked": 0, "error": str(exc)}
    retired: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("operator_action") or "").strip().upper() != REJECT_CLOSE:
            continue
        detail = _loads(row.get("plan_detail_json"))
        if str(detail.get("lane") or row.get("mode") or "") != "live_close_advisory":
            continue
        ticket = detail.get("order_ticket_json")
        if not isinstance(ticket, dict):
            continue
        ticket_hash = str(ticket.get("ticket_hash") or "")
        intent = store.live_order_intent(ticket_hash)
        if not intent:
            continue
        status = str(intent.get("_ledger_status") or "")
        if status in LOCAL_CLOSE_PIPELINE_STATUSES:
            store.update_live_order_intent_status_with_payload(
                ticket_hash,
                "rejected_by_operator",
                {
                    "operator_command": {
                        "action": REJECT_CLOSE,
                        "reason": str(row.get("operator_notes") or "operator_rejected_close"),
                        "prior_status": status,
                        "applied_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    }
                },
            )
            retired.append({"ticket_hash": ticket_hash, "prior_status": status})
        else:
            blocked.append({"ticket_hash": ticket_hash, "status": status, "reason": "broker_cancel_required"})
    if retired or blocked:
        store.event("live_close_operator_commands_applied", {"retired": retired, "blocked": blocked})
    return {"retired": len(retired), "blocked": len(blocked), "retired_tickets": retired, "blocked_tickets": blocked}


def _working_close_order(store: LocalStore, group: dict[str, Any]) -> dict[str, Any] | None:
    group_id = str(group.get("group_id") or "")
    tickets = store.live_close_order_intents_for_group(group_id, statuses=NONTERMINAL_CLOSE_STATUSES)
    return min(tickets, key=_working_close_status_rank) if tickets else None


def _working_close_status_rank(ticket: dict[str, Any]) -> int:
    status = str(ticket.get("_ledger_status") or "")
    if status in {"submitted", "repriced"}:
        return 0
    if status == APPROVED_CLOSE_PENDING_SUBMIT:
        return 1
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


def _exit_submit_source(config: dict[str, Any]) -> str:
    raw = str(((config.get("live") or {}).get("exit_submit_source") or "sheet")).strip().lower()
    return raw if raw in {"sheet", "ledger"} else "sheet"


def _annotate_exit_ticket(ticket: dict[str, Any], decision: dict[str, Any]) -> None:
    ticket["exit_reason"] = str(decision.get("reason") or "")
    ticket["exit_decision"] = {
        key: value
        for key, value in decision.items()
        if key in {
            "reason",
            "urgency",
            "entry_kind",
            "entry_value",
            "entry_net_cashflow",
            "current_value",
            "pnl_mid",
            "recommended_close_net",
            "close_natural_net",
            "target_profit",
            "profit_target_pct",
            "min_profit_to_trigger",
            "profit_floor_pct",
        }
    }
    natural = _optional_float(decision.get("close_natural_net"))
    if natural is not None:
        ticket["exit_natural_net"] = round(natural, 2)
        ticket["exit_natural_limit_price"] = f"{abs(natural) / 100.0:.2f}"
    ticket["exit_entry_kind"] = str(decision.get("entry_kind") or "")
    entry_net = _optional_float(decision.get("entry_net_cashflow"))
    if entry_net is not None:
        ticket["exit_entry_net_cashflow"] = round(entry_net, 2)
    min_profit = _optional_float(decision.get("min_profit_to_trigger"))
    target_profit = _optional_float(decision.get("target_profit"))
    floor_pct = _optional_float(decision.get("profit_floor_pct"))
    if entry_net is not None and min_profit is not None:
        retained_target_profit = (target_profit or 0.0) * (floor_pct if floor_pct is not None else 50.0) / 100.0
        floor_profit = max(min_profit, retained_target_profit)
        floor_net = floor_profit - entry_net
        ticket["exit_min_profit_to_trigger"] = round(min_profit, 2)
        ticket["exit_profit_floor_pct"] = round(floor_pct if floor_pct is not None else 50.0, 2)
        ticket["exit_profit_floor_net"] = round(floor_net, 2)
        ticket["exit_profit_floor_limit_price"] = _close_limit_price_from_net(ticket, floor_net)


def _close_limit_price_from_net(ticket: dict[str, Any], close_net: float) -> str:
    per_contract = abs(close_net) / 100.0
    legs = list(ticket.get("legs") or [])
    if len(legs) == 1:
        return f"{max(per_contract, 0.01):.2f}"
    if close_net > 0:
        return f"-{max(per_contract, 0.01):.2f}"
    return f"{max(per_contract, 0.01):.2f}"


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


def _close_plan_row(index: int, group: dict[str, Any], decision: dict[str, Any], ticket: dict[str, Any], *, approval_mode: str, submit_source: str = "sheet") -> list[Any]:
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
        "operator_action": APPROVE_LIVE_CLOSE if approval_mode == "auto_rules" and submit_source == "sheet" else "",
        "operator_notes": "ledger-approved close pending submit" if approval_mode == "auto_rules" and submit_source == "ledger" else "",
    }
    return [row.get(column, "") for column in DAILY_PLAN_HEADER]


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
