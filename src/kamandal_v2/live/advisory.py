"""Strict live advisory planning beside permissive shadow."""

from __future__ import annotations

import copy
import json
import os
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kamandal_v2.domain.models import Candidate, PortfolioState
from kamandal_v2.live.approval import create_live_approval_request
from kamandal_v2.live.orders import APPROVE_LIVE, build_open_ticket
from kamandal_v2.live.reconciliation import reconciliation_blockers_for_group
from kamandal_v2.live.risk_manager import (
    cluster_capped_symbols,
    cluster_for_symbol,
    evaluate_entry_risk,
    underlying_capped_symbols,
)
from kamandal_v2.planner.bpr import structure_bpr_cap
from kamandal_v2.planner.daily_plan import render_daily_plan_rows
from kamandal_v2.planner.engine import PlanRunResult, run_plan
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.sheets import write_daily_plan
from kamandal_v2.stores.audit import AuditWriter
from kamandal_v2.stores.sqlite import LocalStore

def live_config(base: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(base)
    runtime = config.setdefault("runtime", {})
    runtime["mode"] = "live"
    portfolio = config.setdefault("portfolio", {})
    live = config.get("live") or {}
    if live.get("max_bpr_per_underlying_pct") not in (None, ""):
        portfolio["max_bpr_per_underlying_pct"] = live["max_bpr_per_underlying_pct"]
    execution = config.setdefault("execution", {})
    execution["approval_mode"] = "live_plan_only"
    execution["max_contracts_per_order"] = 1
    return config


def run_live_advisory_plan(
    config: dict[str, Any],
    *,
    idea_paths: list[str | Path],
    config_source: str = "sheet",
    provider: str = "public",
    write_sheet: bool = False,
    persist_order_intents: bool = True,
    notify_unplaced_selected: bool = True,
    store: LocalStore | None = None,
    audit: AuditWriter | None = None,
) -> PlanRunResult:
    store = store or LocalStore()
    audit = audit or AuditWriter("data/audit/live")
    config = live_config(config)
    live_cfg = config.get("live") or {}
    result = run_plan(
        config,
        idea_paths=idea_paths,
        config_source=config_source,
        provider=provider,
        write_sheet=False,
        store=store,
        audit=audit,
        candidate_postprocessor=_live_candidate_policy,
        plan_top_n=int(live_cfg.get("max_new_plans_per_day") or live_cfg.get("top_n") or 1),
        plan_max_new_positions=int(live_cfg.get("max_new_positions_per_plan") or 1),
    )
    rows = render_live_plan_rows(result, config, store=store, persist_order_intents=persist_order_intents)
    result.daily_plan_rows[:] = rows
    if write_sheet and rows:
        write_daily_plan(config, rows, DAILY_PLAN_HEADER, replace_lanes={"live_advisory"})
    elif write_sheet:
        store.event("live_daily_plan_write_skipped", {"plan_run_id": result.plan_run_id, "reason": "no_eligible_live_plans"})
        audit.event("live_daily_plan_write_skipped", {"plan_run_id": result.plan_run_id, "reason": "no_eligible_live_plans"})
        if notify_unplaced_selected and _entry_approval_mode(config) == "auto_top_plan":
            from kamandal_v2.live.execution import notify_live_advisory_risk_block

            notify_live_advisory_risk_block(config, store, result.candidates)
    audit.write_json("latest_live_advisory", {
        **result.to_dict(),
        "daily_plan_rows": rows,
    })
    store.event("live_advisory_plan_completed", {
        "plan_run_id": result.plan_run_id,
        "plans": len(result.plans),
        "candidates": len(result.candidates),
    })
    return result


def render_live_plan_rows(result: PlanRunResult, config: dict[str, Any], *, store: LocalStore, persist_order_intents: bool = True) -> list[list[Any]]:
    rows = render_daily_plan_rows(result.plans, mode="live_advisory")
    entry_mode = _entry_approval_mode(config)
    if entry_mode == "disabled":
        return []
    account_json = {
        "account_size": result.metrics.get("account_size_raw"),
        "buying_power": result.metrics.get("buying_power_raw"),
        "account_size_effective": result.metrics.get("account_size_effective"),
        "buying_power_effective": result.metrics.get("buying_power_effective"),
    }
    for index, plan in enumerate(result.plans):
        if not plan.candidates:
            continue
        tickets = [build_open_ticket(plan, candidate) for candidate in plan.candidates]
        if persist_order_intents:
            for ticket in tickets:
                store.save_live_order_intent(ticket)
        candidate = plan.candidates[0]
        ticket = tickets[0]
        row = dict(zip(DAILY_PLAN_HEADER, rows[index], strict=False))
        metrics = _loads(row.get("plan_metrics_json"))
        detail = _loads(row.get("plan_detail_json"))
        metrics["real_account_json"] = account_json
        detail["lane"] = "live_advisory"
        detail["live_gate_status"] = "eligible"
        detail["live_blockers"] = []
        detail["order_ticket_json"] = ticket
        detail["order_tickets_json"] = tickets
        detail["basket_execution_json"] = {
            "mode": "concurrent",
            "ticket_count": len(tickets),
            "submit_default": "all_pending_tickets_up_to_live_limit",
            "requires_resync_between_fills": False,
        }
        detail["public_preflight_json"] = candidate.preflight.to_dict() if candidate.preflight else None
        detail["real_account_json"] = account_json
        row["mode"] = "live_advisory"
        row["operator_action"] = APPROVE_LIVE if entry_mode == "auto_top_plan" and index == 0 else ""
        row["plan_metrics_json"] = json.dumps(metrics, sort_keys=True)
        row["plan_detail_json"] = json.dumps(detail, sort_keys=True)
        if persist_order_intents and entry_mode == "telegram_approval" and index == 0:
            request = create_live_approval_request(config, row=row, plan=plan, candidate=candidate, ticket=ticket, store=store)
            detail["live_approval_request_id"] = request["request_id"]
            detail["live_gate_status"] = "telegram_pending"
            row["plan_detail_json"] = json.dumps(detail, sort_keys=True)
        rows[index] = [row.get(column, "") for column in DAILY_PLAN_HEADER]
    return rows


def _entry_approval_mode(config: dict[str, Any]) -> str:
    raw = str(((config.get("live") or {}).get("entry_approval_mode") or "sheet_approval")).strip().lower()
    allowed = {"sheet_approval", "auto_top_plan", "telegram_approval", "disabled"}
    if raw not in allowed:
        raise ValueError(f"unsupported live.entry_approval_mode={raw!r}; expected one of {sorted(allowed)}")
    return raw


def _live_candidate_policy(candidates: list[Candidate], store: LocalStore, config: dict[str, Any], portfolio: PortfolioState) -> None:
    live_cfg = config.get("live") or {}
    max_contracts = int((config.get("execution") or {}).get("max_contracts_per_order") or 1)
    min_entry_legs = int(live_cfg.get("min_entry_legs") or 1)
    traded_ids = store.live_idea_ids_opened_since(_market_day_start())
    open_ids = store.open_live_idea_ids()
    open_contracts = _open_live_contract_keys(store)
    risk_decision = evaluate_entry_risk(store, config)
    risk_block_reason = ""
    cluster_capped = set[str]()
    underlying_capped = set[str]()
    if risk_decision.enabled:
        if risk_decision.blocked:
            risk_block_reason = "live_risk_manager_blocked:" + ",".join(risk_decision.reason_codes())
        cluster_capped = cluster_capped_symbols(risk_decision)
        underlying_capped = underlying_capped_symbols(risk_decision)
    for candidate in candidates:
        if not candidate.eligible:
            continue
        max_bpr = _candidate_bpr_cap(candidate, portfolio, live_cfg)
        if risk_block_reason:
            candidate.rejection_reason = risk_block_reason
        elif candidate.underlying.upper() in underlying_capped:
            candidate.rejection_reason = f"live_risk_underlying_cap:{candidate.underlying.upper()}"
        elif candidate.underlying.upper() in cluster_capped:
            cluster = cluster_for_symbol(config, candidate.underlying) or "unknown"
            candidate.rejection_reason = f"live_risk_cluster_cap:{cluster}"
        elif len(candidate.legs) < min_entry_legs:
            candidate.rejection_reason = f"live_leg_count_below_min:{len(candidate.legs)}<{min_entry_legs}"
        elif any(int(leg.quantity or 1) > max_contracts for leg in candidate.legs):
            candidate.rejection_reason = "live_contract_limit"
        elif candidate.idea_id in open_ids:
            candidate.rejection_reason = "live_idea_already_open"
        elif candidate.idea_id in traded_ids:
            candidate.rejection_reason = "live_idea_already_traded_today"
        elif overlap := _live_contract_overlap(candidate, open_contracts):
            candidate.rejection_reason = f"live_contract_already_open:{overlap}"
        elif mismatch := _mentioned_strategy_mismatch(candidate, live_cfg):
            candidate.rejection_reason = mismatch
        elif reconciliation_blockers_for_group(store, {"underlying": candidate.underlying, "group_id": ""}, config=config):
            candidate.rejection_reason = "live_reconciliation_blocker"
        elif candidate.estimated_bpr > max_bpr:
            candidate.rejection_reason = f"live_bpr_above_max:{candidate.estimated_bpr}>{max_bpr}"
        elif candidate.preflight is None or not candidate.preflight.ok:
            candidate.rejection_reason = "live_preflight_required"
        elif _preflight_bpr_incomplete(candidate):
            candidate.rejection_reason = "live_preflight_bpr_incomplete"


def _open_live_contract_keys(store: LocalStore) -> set[str]:
    keys: set[str] = set()
    for group in store.open_live_position_groups():
        candidate = group.get("candidate") or {}
        underlying = str(group.get("underlying") or candidate.get("underlying") or "").upper()
        for leg in candidate.get("legs") or []:
            key = _contract_key(underlying, leg)
            if key:
                keys.add(key)
    return keys


def _live_contract_overlap(candidate: Candidate, open_contracts: set[str]) -> str:
    for leg in candidate.legs:
        key = _contract_key(candidate.underlying, leg)
        if key and key in open_contracts:
            return key
    return ""


def _contract_key(underlying: str, leg: Any) -> str:
    getter = leg.get if isinstance(leg, dict) else lambda name, default=None: getattr(leg, name, default)
    expiration = str(getter("expiration", "") or "")
    option_type = str(getter("option_type", "") or "").lower()
    try:
        strike = float(getter("strike", 0.0) or 0.0)
    except (TypeError, ValueError):
        return ""
    if not underlying or not expiration or option_type not in {"call", "put"} or strike <= 0:
        return ""
    return f"{underlying.upper()}|{expiration}|{option_type}|{strike:g}"


def _candidate_bpr_cap(candidate: Candidate, portfolio: PortfolioState, live_cfg: dict[str, Any]) -> float:
    absolute = _candidate_live_bpr_cap(candidate)
    if absolute is None:
        absolute = structure_bpr_cap(candidate.structure, live_cfg)
    pct_raw = live_cfg.get("max_bpr_per_order_pct")
    if pct_raw in (None, ""):
        return absolute
    pct_cap = max(float(portfolio.account_size or 0.0), 1.0) * (float(pct_raw) / 100.0)
    return round(min(absolute, pct_cap), 2)


def _candidate_live_bpr_cap(candidate: Candidate) -> float | None:
    for reason in getattr(candidate, "reasons", []):
        if not reason.startswith("live_max_bpr_per_order="):
            continue
        raw = reason.split("=", 1)[1]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    return None


def _mentioned_strategy_mismatch(candidate: Candidate, live_cfg: dict[str, Any]) -> str:
    policy = str(live_cfg.get("mentioned_strategy_policy") or "soft").strip().lower()
    if policy in {"", "soft", "ignore", "disabled"}:
        return ""
    mentioned = ""
    for reason in candidate.reasons:
        if str(reason).startswith("mentioned_strategy="):
            mentioned = str(reason).split("=", 1)[1].strip().lower()
            break
    if not mentioned:
        return ""
    if _strategy_aliases(mentioned).intersection({candidate.playbook_id.lower(), candidate.structure.lower()}):
        return ""
    return f"live_mentioned_strategy_mismatch:{mentioned}!={candidate.structure}"


def _strategy_aliases(value: str) -> set[str]:
    normalized = value.strip().lower()
    aliases = {normalized}
    if normalized == "strangle":
        aliases.add("short_strangle")
    if normalized == "calendar":
        aliases.update({"call_calendar", "put_calendar"})
    return aliases


def _preflight_bpr_incomplete(candidate: Candidate) -> bool:
    if candidate.preflight is None:
        return True
    raw = candidate.preflight.raw or {}
    if raw.get("source") == "fixture":
        return False
    response = raw.get("response") or {}
    return not any(response.get(key) not in (None, "") for key in ("buyingPowerRequirement", "buyingPowerEffect", "estimatedBuyingPower"))


def _market_day_start() -> str:
    market_tz = os.environ.get("KAMANDAL_MARKET_TZ") or "America/Chicago"
    today = datetime.now(ZoneInfo(market_tz)).date()
    local_start = datetime.combine(today, time.min, tzinfo=ZoneInfo(market_tz))
    return local_start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
