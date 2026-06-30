"""Live broker/local position reconciliation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any

from kamandal_v2.live.book import live_book_sheet_rows, run_live_book
from kamandal_v2.live.operator_review import OperatorReviewError, create_operator_review_request, expire_stale_operator_review_requests
from kamandal_v2.live.order_reconciliation import reconcile_live_orders
from kamandal_v2.market.broker import broker_adapter
from kamandal_v2.market.public import occ_symbol
from kamandal_v2.schemas import DAILY_PLAN_HEADER, LIVE_BOOK_HEADER
from kamandal_v2.sheets import write_daily_plan, write_live_book
from kamandal_v2.stores.sqlite import LocalStore


RECONCILIATION_ACTIONS = ["retire_local", "adopt_broker_position", "hold", "dismiss"]


def reconcile_live_positions(
    config: dict[str, Any],
    *,
    write_sheet: bool = False,
    send_review: bool = False,
    dry_run: bool = False,
    store: LocalStore | None = None,
) -> dict[str, Any]:
    store = store or LocalStore()
    if not _reconciliation_enabled(config):
        return {"status": "disabled", "issues": [], "daily_plan_rows": []}
    adapter = broker_adapter(config)
    if not hasattr(adapter, "broker_positions"):
        raise RuntimeError(f"configured broker adapter {type(adapter).__name__} does not expose broker_positions")
    broker_positions = [position for position in adapter.broker_positions() if str(position.get("asset_type")) == "option"]
    local_groups = store.open_live_position_groups()
    local_index = _local_leg_index(local_groups)
    broker_index = _broker_position_index(broker_positions)
    decision_context = _decision_context(store, local_groups, broker_index)

    issues = []
    for group in local_groups:
        issue = _local_group_issue(group, broker_index)
        if issue:
            stored = _record_issue(config, store, _with_decision(config, issue, decision_context), dry_run=dry_run)
            stored = _apply_reconciliation_decision(config, store, stored, dry_run=dry_run)
            include_issue = _include_issue_in_report(stored)
            if include_issue:
                issues.append(stored)
            if send_review and include_issue:
                _request_review(config, store, stored, dry_run=dry_run)

    for position in broker_positions:
        symbol = str(position.get("occ_symbol") or "")
        if not symbol:
            issue = _issue("unknown_broker_payload", subject_id=f"broker_{position.get('raw_index')}", broker_position=position)
            stored = _record_issue(config, store, _with_decision(config, issue, decision_context), dry_run=dry_run)
            stored = _apply_reconciliation_decision(config, store, stored, dry_run=dry_run)
            issues.append(stored)
            if send_review:
                _request_review(config, store, stored, dry_run=dry_run)
    for position in broker_index.values():
        symbol = str(position.get("occ_symbol") or "")
        if symbol not in local_index:
            issue = _issue(
                "orphan_broker_position",
                subject_id=symbol,
                underlying=str(position.get("underlying") or ""),
                broker_position=position,
            )
            stored = _record_issue(config, store, _with_decision(config, issue, decision_context), dry_run=dry_run)
            stored = _apply_reconciliation_decision(config, store, stored, dry_run=dry_run)
            issues.append(stored)
            if send_review:
                _request_review(config, store, stored, dry_run=dry_run)
            continue
        local_leg = local_index[symbol]
        broker_qty = float(position.get("quantity") or 0.0)
        local_qty = float(local_leg.get("quantity") or 0.0)
        if abs(broker_qty - local_qty) > 0.001:
            issue = _issue(
                "quantity_mismatch",
                subject_id=symbol,
                group_id=str(local_leg.get("group_id") or ""),
                underlying=str(position.get("underlying") or ""),
                broker_position=position,
                local_leg=local_leg,
            )
            stored = _record_issue(config, store, _with_decision(config, issue, decision_context), dry_run=dry_run)
            stored = _apply_reconciliation_decision(config, store, stored, dry_run=dry_run)
            include_issue = _include_issue_in_report(stored)
            if include_issue:
                issues.append(stored)
            if send_review and include_issue:
                _request_review(config, store, stored, dry_run=dry_run)

    _resolve_unobserved_issues(store, {str(issue.get("issue_id") or "") for issue in issues}, dry_run=dry_run)
    order_reconciliation = reconcile_live_orders(config, dry_run=dry_run, store=store, adapter=adapter)
    rows = [_daily_plan_row(index, issue) for index, issue in enumerate(issues, start=1)]
    live_book_rows_written = 0
    if write_sheet and rows:
        write_daily_plan(config, rows, DAILY_PLAN_HEADER, replace_lanes={"live_reconciliation"})
    if write_sheet:
        live_book_report = run_live_book(store, config)
        live_book_rows_written = write_live_book(config, LIVE_BOOK_HEADER, live_book_sheet_rows(live_book_report, LIVE_BOOK_HEADER))
    return {
        "status": "ok",
        "broker_option_positions": len(broker_positions),
        "local_open_groups": len(local_groups),
        "issues": issues,
        "order_reconciliation": order_reconciliation,
        "daily_plan_rows": rows,
        "live_book_rows_written": live_book_rows_written,
    }


def reconciliation_blockers_for_group(
    store: LocalStore,
    group: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    live_cfg = (config or {}).get("live") or {}
    recon_cfg = live_cfg.get("reconciliation") or {}
    if not _as_bool(recon_cfg.get("block_management_on_open_issues"), True):
        return []
    group_id = str(group.get("group_id") or "")
    underlying = str(group.get("underlying") or (group.get("candidate") or {}).get("underlying") or "")
    blockers = store.open_live_reconciliation_issues(group_id=group_id) if group_id else []
    if underlying:
        blockers.extend(
            issue for issue in store.open_live_reconciliation_issues(underlying=underlying)
            if issue.get("issue_id") not in {item.get("issue_id") for item in blockers}
        )
    return blockers


def apply_reconciliation_review_action(
    config: dict[str, Any],
    request: dict[str, Any],
    action: str,
    *,
    note: str = "",
    source: str = "manual",
    decided_by: str = "Suman",
    store: LocalStore | None = None,
) -> dict[str, Any]:
    store = store or LocalStore()
    issue_id = str((request.get("payload") or {}).get("issue_id") or request.get("subject_id") or "")
    issue = store.live_reconciliation_issue(issue_id)
    if not issue:
        raise RuntimeError(f"live reconciliation issue not found: {issue_id}")
    action = action.lower().strip()
    audit = {"note": note, "source": source, "decided_by": decided_by, "decided_at": _now()}
    if action == "retire_local":
        group_id = str(issue.get("group_id") or "")
        if not group_id:
            raise RuntimeError("retire_local requires a local group_id")
        group = store.live_position_group(group_id)
        if not group:
            raise RuntimeError(f"local group not found: {group_id}")
        store.close_live_position_group(group_id, status="reconciled_retired", reason="operator_retire_local", payload={**group, "reconciliation_issue": issue, "operator_review": audit})
        store.update_live_reconciliation_issue_status(issue_id, "retired", {"resolution": "operator_retire_local", **audit})
        return {"request_status": "applied", "issue_status": "retired", "group_id": group_id, "action": action}
    if action == "adopt_broker_position":
        position = (issue.get("broker_position") or issue.get("payload") or {}).get("broker_position") or issue.get("broker_position")
        if not isinstance(position, dict):
            raise RuntimeError("adopt_broker_position requires broker_position payload")
        group_id = _adopt_broker_position(store, issue, position, audit)
        store.update_live_reconciliation_issue_status(issue_id, "adopted", {"resolution": "operator_adopt_broker_position", "adopted_group_id": group_id, **audit})
        return {"request_status": "applied", "issue_status": "adopted", "group_id": group_id, "action": action}
    if action == "hold":
        store.update_live_reconciliation_issue_status(issue_id, "held", {"resolution": "operator_hold", **audit})
        return {"request_status": "held", "issue_status": "held", "issue_id": issue_id, "action": action}
    if action == "dismiss":
        store.update_live_reconciliation_issue_status(issue_id, "dismissed", {"resolution": "operator_dismiss", **audit})
        return {"request_status": "dismissed", "issue_status": "dismissed", "issue_id": issue_id, "action": action}
    raise RuntimeError(f"unsupported reconciliation action: {action}")


def _record_issue(config: dict[str, Any], store: LocalStore, issue: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return issue
    store.save_live_reconciliation_issue(issue)
    stored = store.live_reconciliation_issue(str(issue["issue_id"])) or issue
    if _should_auto_retire(config, stored):
        group_id = str(stored.get("group_id") or "")
        group = store.live_position_group(group_id)
        if group:
            decision = _decision(
                "auto_local_repair",
                "retire_local",
                "high",
                True,
                "broker_flat_confirmations_met",
                group_id=group_id,
                evidence={"observed_count": stored.get("observed_count"), "group_id": group_id},
            )
            stored = {**stored, "decision": {**decision, "applied": True, "applied_at": _now()}}
            store.close_live_position_group(group_id, status="reconciled_retired", reason="broker_flat_auto_confirmed", payload={**group, "reconciliation_issue": stored})
            store.update_live_reconciliation_issue_status(
                str(stored["issue_id"]),
                "retired",
                {"resolution": "auto_retired_after_flat_confirmations", "retired_at": _now(), "decision": stored["decision"]},
            )
            stored = store.live_reconciliation_issue(str(issue["issue_id"])) or stored
    return stored


def _with_decision(config: dict[str, Any], issue: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    issue = dict(issue)
    issue["decision"] = _reconciliation_decision(config, issue, context)
    return issue


def _reconciliation_decision(config: dict[str, Any], issue: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    issue_type = str(issue.get("issue_type") or "")
    if issue_type == "quantity_mismatch":
        repair = _filled_close_repair_candidate(issue, context)
        if repair:
            return repair
        return _decision(
            "human_review",
            "hold",
            "medium",
            False,
            "quantity_mismatch_requires_review",
            evidence={"subject_id": issue.get("subject_id"), "group_ids": (issue.get("local_leg") or {}).get("group_ids") or []},
        )
    if issue_type == "ghost_local_position":
        return _decision(
            "auto_local_repair" if _should_auto_retire(config, issue) else "human_review",
            "retire_local",
            "high" if _should_auto_retire(config, issue) else "medium",
            _should_auto_retire(config, issue),
            "broker_flat_confirmations_met" if _should_auto_retire(config, issue) else "broker_flat_requires_more_confirmations",
            evidence={"observed_count": issue.get("observed_count"), "group_id": issue.get("group_id")},
        )
    if issue_type == "orphan_broker_position":
        return _decision(
            "human_review",
            "adopt_broker_position",
            "medium",
            False,
            "broker_position_not_in_local_ledger",
            evidence={"subject_id": issue.get("subject_id"), "underlying": issue.get("underlying")},
        )
    return _decision(
        "human_review",
        "hold",
        "low",
        False,
        "no_auto_rule_for_issue_type",
        evidence={"issue_type": issue_type, "subject_id": issue.get("subject_id")},
    )


def _filled_close_repair_candidate(issue: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    local_leg = issue.get("local_leg") or {}
    group_ids = [str(group_id) for group_id in local_leg.get("group_ids") or [] if group_id]
    if not group_ids:
        group_id = str(issue.get("group_id") or "")
        group_ids = [group_id] if group_id else []
    candidates = []
    for group_id in group_ids:
        close_ticket = _latest_close_filled_ticket(context["close_tickets_by_group"].get(group_id) or [])
        if not close_ticket:
            continue
        group_legs = context["legs_by_group"].get(group_id) or []
        if not group_legs:
            continue
        affected_symbols = {str(leg.get("occ_symbol") or "") for leg in group_legs}
        affected_symbols = {symbol for symbol in affected_symbols if symbol}
        if _aggregate_matches_after_removing_group(affected_symbols, group_legs, context):
            candidates.append({"group_id": group_id, "close_ticket": close_ticket, "affected_symbols": sorted(affected_symbols)})
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    return _decision(
        "auto_local_repair",
        "retire_local",
        "high",
        True,
        "close_filled_ticket_reconciles_broker_aggregate",
        group_id=candidate["group_id"],
        evidence={
            "issue_id": issue.get("issue_id"),
            "subject_id": issue.get("subject_id"),
            "affected_symbols": candidate["affected_symbols"],
            "ticket_hash": candidate["close_ticket"].get("ticket_hash"),
            "order_id": candidate["close_ticket"].get("order_id"),
            "close_status": candidate["close_ticket"].get("_ledger_status"),
        },
    )


def _aggregate_matches_after_removing_group(affected_symbols: set[str], group_legs: list[dict[str, Any]], context: dict[str, Any]) -> bool:
    for symbol in affected_symbols:
        broker_qty = float((context["broker_index"].get(symbol) or {}).get("quantity") or 0.0)
        local_qty = float((context["local_index"].get(symbol) or {}).get("quantity") or 0.0)
        group_qty = sum(float(leg.get("quantity") or 0.0) for leg in group_legs if str(leg.get("occ_symbol") or "") == symbol)
        if abs(broker_qty - (local_qty - group_qty)) > 0.001:
            return False
    return True


def _decision_context(store: LocalStore, local_groups: list[dict[str, Any]], broker_index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    legs_by_group: dict[str, list[dict[str, Any]]] = {}
    for group in local_groups:
        group_id = str(group.get("group_id") or "")
        if not group_id:
            continue
        legs_by_group[group_id] = _group_leg_symbols(group)
    close_tickets_by_group = {
        group_id: store.live_close_order_intents_for_group(group_id)
        for group_id in legs_by_group
    }
    return {
        "broker_index": broker_index,
        "local_index": _local_leg_index(local_groups),
        "legs_by_group": legs_by_group,
        "close_tickets_by_group": close_tickets_by_group,
    }


def _latest_close_filled_ticket(tickets: list[dict[str, Any]]) -> dict[str, Any] | None:
    for ticket in tickets:
        if str(ticket.get("_ledger_status") or ticket.get("status") or "") == "close_filled":
            return ticket
    return None


def _apply_reconciliation_decision(config: dict[str, Any], store: LocalStore, issue: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    if str(issue.get("status") or "") != "open":
        return issue
    decision = issue.get("decision") or {}
    if str(decision.get("tier") or "") != "auto_local_repair":
        return issue
    if str(decision.get("action") or "") != "retire_local":
        return issue
    if not _auto_local_repair_enabled(config):
        issue["decision"] = {**decision, "auto_allowed": False, "blocked_by": "auto_local_repair_disabled"}
        return issue
    group_id = str(decision.get("group_id") or issue.get("group_id") or "")
    if not group_id:
        issue["decision"] = {**decision, "auto_allowed": False, "blocked_by": "missing_group_id"}
        return issue
    group = store.live_position_group(group_id)
    if not group:
        issue["decision"] = {**decision, "auto_allowed": False, "blocked_by": "group_not_found"}
        return issue
    applied = {**decision, "applied": not dry_run, "applied_at": _now() if not dry_run else ""}
    issue["decision"] = applied
    if dry_run:
        return issue
    store.close_live_position_group(
        group_id,
        status="reconciled_retired",
        reason=str(decision.get("reason") or "auto_local_repair"),
        payload={**group, "reconciliation_issue": issue, "reconciliation_decision": applied},
    )
    store.update_live_reconciliation_issue_status(
        str(issue["issue_id"]),
        "retired",
        {"resolution": str(decision.get("reason") or "auto_local_repair"), "retired_at": _now(), "decision": applied},
    )
    store.event("live_reconciliation_decision_applied", {"issue_id": issue.get("issue_id"), "group_id": group_id, "decision": applied})
    stored = store.live_reconciliation_issue(str(issue["issue_id"]))
    return stored or issue


def _include_issue_in_report(issue: dict[str, Any]) -> bool:
    return str(issue.get("status") or "") == "open"


def _auto_local_repair_enabled(config: dict[str, Any]) -> bool:
    recon = ((config.get("live") or {}).get("reconciliation") or {})
    return _as_bool(recon.get("auto_local_repair_enabled"), True)


def _decision(
    tier: str,
    action: str,
    confidence: str,
    auto_allowed: bool,
    reason: str,
    *,
    group_id: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "tier": tier,
        "action": action,
        "confidence": confidence,
        "auto_allowed": auto_allowed,
        "reason": reason,
        "group_id": group_id,
        "evidence": evidence or {},
    }


def _local_group_issue(group: dict[str, Any], broker_index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    local_legs = _group_leg_symbols(group)
    if not local_legs:
        return _issue(
            "leg_mismatch",
            subject_id=str(group.get("group_id") or ""),
            group_id=str(group.get("group_id") or ""),
            underlying=str(group.get("underlying") or (group.get("candidate") or {}).get("underlying") or ""),
            local_group=group,
        )
    missing = [leg for leg in local_legs if leg["occ_symbol"] not in broker_index]
    if len(missing) == len(local_legs):
        return _issue(
            "ghost_local_position",
            subject_id=str(group.get("group_id") or ""),
            group_id=str(group.get("group_id") or ""),
            underlying=str(group.get("underlying") or (group.get("candidate") or {}).get("underlying") or ""),
            local_group=group,
            missing_legs=missing,
        )
    if missing:
        return _issue(
            "leg_mismatch",
            subject_id=str(group.get("group_id") or ""),
            group_id=str(group.get("group_id") or ""),
            underlying=str(group.get("underlying") or (group.get("candidate") or {}).get("underlying") or ""),
            local_group=group,
            missing_legs=missing,
        )
    return None


def _local_leg_index(groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for group in groups:
        for leg in _group_leg_symbols(group):
            symbol = str(leg["occ_symbol"])
            current = index.setdefault(
                symbol,
                {
                    **leg,
                    "quantity": 0.0,
                    "group_ids": [],
                    "local_legs": [],
                },
            )
            current["quantity"] = float(current.get("quantity") or 0.0) + float(leg.get("quantity") or 0.0)
            current["group_ids"].append(leg.get("group_id"))
            current["local_legs"].append(leg)
            if not current.get("group_id"):
                current["group_id"] = leg.get("group_id")
    return index


def _group_leg_symbols(group: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = group.get("candidate") or {}
    underlying = str(group.get("underlying") or candidate.get("underlying") or "")
    result = []
    for leg in candidate.get("legs") or []:
        try:
            symbol = occ_symbol(underlying, _leg_proxy(leg))
        except Exception:
            continue
        sign = 1 if str(leg.get("side") or "").lower() == "buy" else -1
        quantity = sign * abs(float(leg.get("quantity") or 1.0))
        result.append({**leg, "quantity": quantity, "group_id": group.get("group_id"), "underlying": underlying, "occ_symbol": symbol})
    return result


def _broker_position_index(positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for position in positions:
        symbol = str(position.get("occ_symbol") or "")
        if not symbol:
            continue
        current = index.setdefault(symbol, {**position, "quantity": 0.0, "raw_positions": []})
        current["quantity"] = float(current.get("quantity") or 0.0) + float(position.get("quantity") or 0.0)
        current["raw_positions"].append(position)
    return index


def _resolve_unobserved_issues(store: LocalStore, observed_issue_ids: set[str], *, dry_run: bool) -> None:
    if dry_run:
        return
    for issue in store.open_live_reconciliation_issues():
        if str(issue.get("status") or "") != "open":
            continue
        issue_id = str(issue.get("issue_id") or "")
        if not issue_id or issue_id in observed_issue_ids:
            continue
        store.update_live_reconciliation_issue_status(
            issue_id,
            "resolved",
            {"resolution": "not_observed_on_latest_reconciliation", "resolved_at": _now()},
        )


def _request_review(config: dict[str, Any], store: LocalStore, issue: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    expire_stale_operator_review_requests(store=store)
    existing_request = store.operator_review_request(f"or_{issue['issue_id']}")
    if existing_request and str(existing_request.get("_ledger_status") or existing_request.get("status")) in {"pending", "sent"}:
        return {"request_id": existing_request["request_id"], "status": "already_pending"}
    actions = _allowed_actions(issue)
    request_payload = {"issue_id": issue["issue_id"], "issue": issue}
    if dry_run:
        return {"request_id": f"or_{issue['issue_id']}", "status": "dry_run", "allowed_actions": actions}
    try:
        return create_operator_review_request(
            config,
            request_type="live_reconciliation",
            subject_id=str(issue["issue_id"]),
            title=_issue_title(issue),
            summary=_issue_summary(issue),
            allowed_actions=actions,
            payload=request_payload,
            store=store,
            send=True,
            request_id=f"or_{issue['issue_id']}",
        )
    except OperatorReviewError as exc:
        error = str(exc)
    except Exception as exc:  # External review transport should not stop live reconciliation.
        error = f"{type(exc).__name__}: {exc}"
    store.event(
        "operator_review_request_failed_nonfatal",
        {"issue_id": issue.get("issue_id"), "issue_type": issue.get("issue_type"), "error": error},
    )
    return {"request_id": f"or_{issue['issue_id']}", "status": "review_send_failed_nonfatal", "error": error}


def _allowed_actions(issue: dict[str, Any]) -> list[str]:
    issue_type = str(issue.get("issue_type") or "")
    if issue_type == "ghost_local_position":
        return ["retire_local", "hold", "dismiss"]
    if issue_type == "orphan_broker_position":
        return ["adopt_broker_position", "hold", "dismiss"]
    return ["hold", "dismiss"]


def _daily_plan_row(index: int, issue: dict[str, Any]) -> list[Any]:
    detail = {"lane": "live_reconciliation", "live_gate_status": "blocked", "reconciliation_issue": issue}
    row = {
        "plan_date": date.today().isoformat(),
        "plan_rank": index,
        "plan_id": str(issue.get("issue_id") or ""),
        "plan_status": "needs_manual_review",
        "plan_trade_count": 0,
        "plan_score": 0,
        "plan_summary": _issue_summary(issue),
        "trade_bundle": f"{issue.get('underlying') or ''} {issue.get('issue_type') or ''}".strip(),
        "trade_bundle_json": json.dumps(issue, sort_keys=True),
        "mode": "live_reconciliation",
        "plan_reasons": str(issue.get("issue_type") or ""),
        "blocked_by": str(issue.get("issue_type") or ""),
        "plan_metrics_json": json.dumps({"observed_count": issue.get("observed_count", 1)}, sort_keys=True),
        "plan_detail_json": json.dumps(detail, sort_keys=True),
        "operator_action": "",
        "operator_notes": "",
    }
    return [row.get(column, "") for column in DAILY_PLAN_HEADER]


def _adopt_broker_position(store: LocalStore, issue: dict[str, Any], position: dict[str, Any], audit: dict[str, Any]) -> str:
    group_id = "adopted_" + _hash_key(str(position.get("occ_symbol") or position.get("symbol") or issue.get("issue_id")))
    leg = {
        "role": "adopted",
        "side": "buy" if float(position.get("quantity") or 0) > 0 else "sell",
        "option_type": position.get("option_type"),
        "strike": position.get("strike"),
        "expiration": position.get("expiration"),
        "quantity": abs(int(float(position.get("quantity") or 1))),
        "mid": float(position.get("average_price") or 0.0),
        "bid": 0.0,
        "ask": 0.0,
        "delta": 0.0,
        "gamma": 0.0,
        "theta": 0.0,
        "vega": 0.0,
        "open_interest": 0,
    }
    payload = {
        "group_id": group_id,
        "order_id": "",
        "plan_id": "manual_adoption",
        "candidate_id": group_id,
        "idea_id": "",
        "underlying": position.get("underlying"),
        "playbook_id": "adopted_broker_position",
        "structure": "adopted_option",
        "candidate": {
            "candidate_id": group_id,
            "underlying": position.get("underlying"),
            "playbook_id": "adopted_broker_position",
            "structure": "adopted_option",
            "net_credit": 0.0,
            "legs": [leg],
        },
        "entry_snapshot": {"source": "broker_reconciliation_adoption", "broker_position": position},
        "reconciliation_issue": issue,
        "operator_review": audit,
    }
    store.save_live_position_group(group_id, payload, status="open")
    store.save_live_position(group_id, group_id, payload, status="open")
    return group_id


def _issue(issue_type: str, *, subject_id: str, group_id: str = "", underlying: str = "", **payload: Any) -> dict[str, Any]:
    issue_id = _issue_id(issue_type, subject_id, group_id=group_id, underlying=underlying)
    return {
        "issue_id": issue_id,
        "issue_type": issue_type,
        "subject_id": subject_id,
        "group_id": group_id,
        "underlying": underlying,
        "status": "open",
        "observed_count": 1,
        **payload,
    }


def _issue_id(issue_type: str, subject_id: str, *, group_id: str = "", underlying: str = "") -> str:
    return f"recon_{_hash_key('|'.join([issue_type, subject_id, group_id, underlying]))}"


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:18]


def _issue_title(issue: dict[str, Any]) -> str:
    return f"{issue.get('issue_type')} {issue.get('underlying') or issue.get('group_id') or issue.get('subject_id')}"


def _issue_summary(issue: dict[str, Any]) -> str:
    issue_type = str(issue.get("issue_type") or "")
    if issue_type == "ghost_local_position":
        return f"Local live group {issue.get('group_id')} is open in Kamandal, but broker positions are flat for its legs."
    if issue_type == "orphan_broker_position":
        position = issue.get("broker_position") or {}
        return f"Broker has option position {position.get('occ_symbol') or position.get('symbol')} that is not in Kamandal's live ledger."
    if issue_type == "quantity_mismatch":
        return f"Broker/local quantity mismatch for {issue.get('subject_id')}."
    if issue_type == "leg_mismatch":
        return f"Local group {issue.get('group_id')} has legs that do not reconcile cleanly to broker positions."
    return f"Live reconciliation issue: {issue_type}"


def _should_auto_retire(config: dict[str, Any], issue: dict[str, Any]) -> bool:
    if str(issue.get("issue_type") or "") != "ghost_local_position":
        return False
    recon = ((config.get("live") or {}).get("reconciliation") or {})
    if not _as_bool(recon.get("auto_retire_ghost_after_confirmations"), True):
        return False
    required = int(recon.get("broker_flat_confirmations_required") or 2)
    return int(issue.get("observed_count") or 0) >= required


def _reconciliation_enabled(config: dict[str, Any]) -> bool:
    return _as_bool(((config.get("live") or {}).get("reconciliation") or {}).get("enabled"), True)


def _leg_proxy(payload: dict[str, Any]) -> Any:
    class LegProxy:
        pass

    proxy = LegProxy()
    for key, value in payload.items():
        setattr(proxy, key, value)
    return proxy


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
