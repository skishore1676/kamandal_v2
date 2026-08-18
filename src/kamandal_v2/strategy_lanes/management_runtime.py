"""Shared shadow and guarded-live lifecycle management orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from typing import Any

from kamandal_v2.domain.models import OptionLeg, utc_now
from kamandal_v2.live.execution import (
    ACTIVE_TICKET_STATUSES,
    CANCEL_PENDING_TICKET_STATUSES,
    PENDING_TICKET_STATUSES,
    REPLACE_WAITING_CANCEL,
)
from kamandal_v2.live.orders import build_csa_live_ticket
from kamandal_v2.market.public import occ_symbol
from kamandal_v2.planner.engine import _market_provider
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.action_arbiter import arbitrate_actions
from kamandal_v2.strategy_lanes.earnings_read import latest_earnings_snapshot
from kamandal_v2.strategy_lanes.lane_common import lifecycle_number
from kamandal_v2.strategy_lanes.models import ActionType, CsaStage, LaneId, LifecycleState, SourceMode
from kamandal_v2.strategy_lanes.policy import CsaPolicy
from kamandal_v2.strategy_lanes.registry import lifecycle_registry
from kamandal_v2.strategy_lanes.shadow_execution import ShadowExecutionAdapter
from kamandal_v2.strategy_lanes.store import CsaStore
from kamandal_v2.strategy_lanes.strangle import build_strangle_adjustment_ticket
from kamandal_v2.strategy_lanes.tickets import mixed_ticket
from kamandal_v2.strategy_engine.lifecycle import observe_strangle_episode, strangle_adjustment_eligible
from kamandal_v2.strategy_engine.event_timing import event_exit_due


@dataclass(frozen=True, slots=True)
class ManagementRunResult:
    run_id: str
    started_at: str
    completed_at: str
    lifecycle_count: int
    selected_actions: dict[str, int]
    filled_actions: int
    live_intent_count: int
    errors: tuple[str, ...]
    execution_mode: str
    policy_snapshot_date: str
    policy_snapshot_hash: str

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


def run_shadow_lifecycle_management(
    config: dict[str, Any],
    *,
    sqlite_path: str = "data/kamandal_v2.db",
    provider: str = "public",
    tables: dict[str, list[dict[str, Any]]] | None = None,
    market: Any | None = None,
    observed_at: str | None = None,
) -> ManagementRunResult:
    return _run_lifecycle_management(
        config,
        sqlite_path=sqlite_path,
        provider=provider,
        tables=tables,
        market=market,
        observed_at=observed_at,
        execution_mode="shadow",
    )


def run_live_lifecycle_management(
    config: dict[str, Any],
    *,
    sqlite_path: str = "data/kamandal_v2.db",
    provider: str = "public",
    tables: dict[str, list[dict[str, Any]]] | None = None,
    market: Any | None = None,
    observed_at: str | None = None,
) -> ManagementRunResult:
    """Stage reusable live close, roll, and adjustment tickets for guarded execution."""

    return _run_lifecycle_management(
        config,
        sqlite_path=sqlite_path,
        provider=provider,
        tables=tables,
        market=market,
        observed_at=observed_at,
        execution_mode="live",
    )


def _run_lifecycle_management(
    config: dict[str, Any],
    *,
    sqlite_path: str,
    provider: str,
    tables: dict[str, list[dict[str, Any]]] | None,
    market: Any | None,
    observed_at: str | None,
    execution_mode: str,
) -> ManagementRunResult:
    started_at = observed_at or utc_now()
    run_id = f"lifecycle:{execution_mode}-management:{started_at}"
    store = CsaStore(sqlite_path)
    baseline_store = LocalStore(sqlite_path, read_only=True)
    writable_live_store = LocalStore(sqlite_path)
    lifecycles = [
        lifecycle
        for lifecycle in store.open_lifecycles()
        if lifecycle.status == "open"
        and str(lifecycle.metadata.get("execution_mode") or "shadow") == execution_mode
    ]
    # Entry eligibility is Sheet-controlled, but an open lifecycle is governed
    # by its immutable entry/adoption snapshot.  Do not reload a current row or
    # stage here: an edit, disable, or restage must never rewrite an open trade.
    policy_snapshot_date = "frozen_lifecycle"
    policy_snapshot_hash = "frozen_lifecycle"
    errors: list[str] = []
    if market is None:
        wrapper = _market_provider(
            config,
            provider=provider,
            store=baseline_store,
            required_expiration_dates=_active_lifecycle_expirations(lifecycles),
        )
        market = getattr(wrapper, "inner", wrapper)
    active_statuses = {
        *PENDING_TICKET_STATUSES,
        *ACTIVE_TICKET_STATUSES,
        *CANCEL_PENDING_TICKET_STATUSES,
        REPLACE_WAITING_CANCEL,
    }
    working_underlyings = (
        {
            str(ticket.get("underlying") or "").upper()
            for ticket in baseline_store.live_order_intents_by_status(active_statuses)
            if str(ticket.get("underlying") or "").strip()
        }
        if execution_mode == "live"
        else set()
    )
    registry = lifecycle_registry()
    counts: dict[str, int] = {}
    filled_actions = 0
    live_intent_count = 0
    for lifecycle in lifecycles:
        try:
            policy = _frozen_lifecycle_policy(lifecycle)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{lifecycle.lifecycle_id}: frozen policy unavailable: {exc}")
            continue
        underlying = str(lifecycle.metadata.get("underlying") or "")
        try:
            snapshot = market.chain_snapshot(underlying)
            active_legs = _active_option_legs(lifecycle, snapshot)
            context, plans, observed_lifecycle = _management_context(
                lifecycle,
                policy,
                active_legs,
                snapshot,
                market,
                sqlite_path,
                observed_at=started_at,
                ownership_clear=True,
                working_order_conflict=underlying.upper() in working_underlyings,
            )
            store.save_lifecycle(observed_lifecycle)
            proposals = registry.resolve(lifecycle.lane)(observed_lifecycle, policy, context, proposed_at=started_at)
            selected = arbitrate_actions(proposals).selected
            store.save_action(selected)
            counts[selected.action_type.value] = counts.get(selected.action_type.value, 0) + 1
            if selected.action_type in {ActionType.HOLD, ActionType.BLOCK}:
                continue
            ticket = _management_ticket(selected, observed_lifecycle, policy, active_legs, plans, underlying, started_at)
            if execution_mode == "shadow":
                store.save_shadow_order_intent(ticket)
                quotes = _ticket_quote_map(ticket, snapshot)
                fill_policy = dict((policy.management.get("lifecycle") or {}).get("fill") or {})
                adapter = ShadowExecutionAdapter()
                final_fill = None
                for attempt in range(int(float(fill_policy["max_attempts"])) + 1):
                    final_fill = adapter.simulate_fill(ticket, quotes, fill_policy, observed_at=started_at, attempt=attempt)
                    if final_fill.status in {"filled", "missed"}:
                        break
                if final_fill is not None:
                    store.save_shadow_fill(final_fill)
                    if final_fill.status == "filled":
                        store.save_lifecycle(adapter.adopt_fill(observed_lifecycle, ticket, final_fill))
                        filled_actions += 1
            else:
                live_ticket = build_csa_live_ticket(ticket)
                live_ticket.update(
                    {
                        "csa_policy_snapshot_date": str(lifecycle.metadata.get("policy_snapshot_date") or "frozen_lifecycle"),
                        "csa_policy_snapshot_hash": str(lifecycle.metadata.get("policy_snapshot_hash") or lifecycle.policy_hash),
                    }
                )
                if writable_live_store.live_order_intent(str(live_ticket["ticket_hash"])) is None:
                    writable_live_store.save_live_order_intent(
                        live_ticket,
                        status="stage_approved_pending_submit",
                    )
                    live_intent_count += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{lifecycle.lifecycle_id}: {type(exc).__name__}: {' '.join(str(exc).split())[:200]}")
    completed_at = utc_now()
    result = ManagementRunResult(
        run_id,
        started_at,
        completed_at,
        len(lifecycles),
        counts,
        filled_actions,
        live_intent_count,
        tuple(errors),
        execution_mode,
        policy_snapshot_date,
        policy_snapshot_hash,
    )
    store.save_run_receipt({"id": run_id, "command": f"unified-{execution_mode}-lifecycle-management", "status": "completed" if result.ok else "completed_with_errors", "started_at": started_at, "completed_at": completed_at, "result": result.to_dict()})
    return result


# Explicit compatibility aliases for retired CLIs and historical tests.  Normal
# scheduled operation imports the generic owners above.
run_csa_shadow_management = run_shadow_lifecycle_management
run_csa_live_management = run_live_lifecycle_management


def _active_lifecycle_expirations(lifecycles: list[LifecycleState]) -> tuple[str, ...]:
    """Keep management quotes independent from the new-entry DTE window."""
    observed_date = date.today()
    expirations: set[str] = set()
    for lifecycle in lifecycles:
        for leg in lifecycle.active_legs:
            raw = str(leg.get("expiration") or "")
            try:
                expiration = date.fromisoformat(raw)
            except ValueError:
                continue
            if expiration >= observed_date:
                expirations.add(expiration.isoformat())
    return tuple(sorted(expirations))


def _frozen_lifecycle_policy(lifecycle: LifecycleState) -> CsaPolicy:
    raw = lifecycle.metadata.get("compiled_management_policy") or lifecycle.metadata.get("policy")
    if not isinstance(raw, dict):
        raise ValueError("compiled_management_policy missing")
    lane = LaneId(str(raw.get("lane") or lifecycle.lane.value))
    stage_raw = str(raw.get("stage") or ("shadow" if lifecycle.metadata.get("execution_mode") == "shadow" else "live"))
    stage = CsaStage(stage_raw)
    source_mode = SourceMode(str(raw.get("source_mode") or "idea"))
    management = raw.get("management")
    resolved_fields = raw.get("resolved_fields")
    if not isinstance(management, dict) or not isinstance(resolved_fields, dict):
        raise ValueError("compiled policy is incomplete")
    return CsaPolicy(
        playbook_id=str(raw.get("playbook_id") or lifecycle.metadata.get("playbook_id") or ""),
        lane=lane,
        stage=stage,
        source_mode=source_mode,
        management=dict(management),
        resolved_fields=dict(resolved_fields),
        policy_hash=str(raw.get("policy_hash") or lifecycle.policy_hash),
        source=str(raw.get("source") or "frozen_lifecycle"),
        read_at=str(raw.get("read_at") or lifecycle.opened_at),
    )


def _active_option_legs(lifecycle: LifecycleState, snapshot: Any) -> tuple[OptionLeg, ...]:
    quotes = {(quote.expiration, quote.option_type, float(quote.strike)): quote for quote in snapshot.quotes}
    result = []
    for item in lifecycle.active_legs:
        key = (str(item["expiration"]), str(item["option_type"]), float(item["strike"]))
        quote = quotes.get(key)
        if quote is None:
            raise ValueError(f"active leg quote missing: {key}")
        result.append(OptionLeg.from_quote(quote, role=str(item["role"]), side=str(item["side"]), quantity=int(item["quantity"])))
    return tuple(result)


def _management_context(
    lifecycle: LifecycleState,
    policy: CsaPolicy,
    legs: tuple[OptionLeg, ...],
    snapshot: Any,
    market: Any,
    sqlite_path: str,
    *,
    observed_at: str,
    ownership_clear: bool,
    working_order_conflict: bool,
):
    cumulative = float(lifecycle.metadata.get("cumulative_cashflow") or 0.0)
    liquidation = sum((leg.bid if leg.side == "buy" else -leg.ask) * leg.quantity for leg in legs)
    entry = float(lifecycle.cashflow_ledger[0]["amount"]) if lifecycle.cashflow_ledger else cumulative
    pnl = cumulative + liquidation
    profit_pct = (pnl / abs(entry) * 100.0) if entry else 0.0
    loss_multiple = (abs(liquidation) / entry) if entry > 0 else max(-pnl / max(abs(entry), 0.01), 0.0)
    observed_date = _parse_timestamp(observed_at).date()
    dtes = [max((date.fromisoformat(leg.expiration) - observed_date).days, 0) for leg in legs]
    event_status = str(market.event_status(snapshot.underlying))
    common = {"working_order_conflict": working_order_conflict, "ownership_clear": ownership_clear, "hard_emergency": False, "event_exit_due": False, "profit_pct": profit_pct, "loss_multiple": loss_multiple}
    plans: dict[str, Any] = {"liquidation": liquidation}
    metadata = dict(lifecycle.metadata)
    metadata.update(
        {
            "last_marked_at": observed_at,
            "mark_liquidation_price": round(liquidation, 6),
            "mark_pnl_price": round(pnl, 6),
            "mark_profit_pct": round(profit_pct, 6),
            "mark_source": "natural_close_quote",
            "contract_multiplier": 100,
        }
    )
    if lifecycle.lane is LaneId.SHORT_STRANGLE:
        put = next(leg for leg in legs if leg.role == "short_put")
        call = next(leg for leg in legs if leg.role == "short_call")
        tested = "put" if snapshot.underlying_price <= put.strike else ("call" if snapshot.underlying_price >= call.strike else "")
        breached_strike = put.strike if tested == "put" else (call.strike if tested == "call" else None)
        observed = observe_strangle_episode(
            replace(lifecycle, metadata=metadata),
            tested_side=tested,
            breached_strike=breached_strike,
            required_confirmations=int(lifecycle_number(policy, "tested_side_confirmation")),
            rearm_inside_confirmations=2,
        )
        metadata = dict(observed.metadata)
        episode = dict(metadata.get("strangle_test_episode") or {})
        confirmations = int(episode.get("confirmations") or 0)
        metadata["tested_side_confirmations"] = confirmations
        roll_plan, _inversion_plan = _strangle_roll_plans(tested, put, call, snapshot, policy)
        plans["roll"] = roll_plan
        context = {
            **common,
            "dte": min(dtes),
            "tested_side": tested,
            "breached_strike": breached_strike,
            "strangle_episode_id": str(episode.get("episode_id") or ""),
            "strangle_episode_eligible": strangle_adjustment_eligible(observed),
            "cooldown_elapsed": _cooldown_elapsed(metadata, policy, observed_at),
            "tested_side_confirmations": confirmations,
            "adjustment_count": int(metadata.get("adjustment_count") or 0),
            "same_expiry_roll_credit": roll_plan["credit"] if roll_plan else 0.0,
        }
    elif lifecycle.lane in {LaneId.CALL_VERTICAL, LaneId.GENERIC_CLOSE_ONLY}:
        context = {**common, "dte": min(dtes)}
    elif lifecycle.lane is LaneId.DIRECTIONAL_DIAGONAL:
        short = next((leg for leg in legs if leg.role == "short_near"), None)
        long = next(leg for leg in legs if leg.role == "long_far")
        context = {**common, "far_dte": max((date.fromisoformat(long.expiration) - observed_date).days, 0), "short_leg_present": short is not None, "paired_position_complete": short is not None and len(legs) == 2}
    else:
        frozen_event = dict(lifecycle.metadata.get("event_context") or {})
        event = latest_earnings_snapshot(sqlite_path, snapshot.underlying) if not frozen_event else None
        event_date = frozen_event.get("event_date") if frozen_event else (event.next_earnings_date if event else None)
        timing = frozen_event.get("time_of_day") if frozen_event else (event.time_of_day if event else "")
        confirmed = str(frozen_event.get("state") or "") == "confirmed" if frozen_event else bool(event and event.confirmed)
        due = False
        if event_date and confirmed:
            try:
                due = event_exit_due(event_date=date.fromisoformat(str(event_date)), time_of_day=str(timing), observed_at=observed_at)
            except ValueError:
                confirmed = False
        context = {
            "working_order_conflict": working_order_conflict,
            "ownership_clear": ownership_clear,
            "event_state": "confirmed" if confirmed else "unknown",
            "hard_emergency": False,
            "event_exit_due": due,
            "profit_pct": profit_pct,
            "near_leg_expired": min(dtes) <= 0,
        }
    return context, plans, replace(lifecycle, updated_at=observed_at, metadata=metadata)


def _strangle_roll_plans(tested: str, put: OptionLeg, call: OptionLeg, snapshot: Any, policy: CsaPolicy):
    if not tested:
        return None, None
    old = call if tested == "put" else put
    eligible = [
        q
        for q in snapshot.quotes
        if q.option_type == old.option_type
        and q.expiration == old.expiration
        and abs(q.delta) <= 0.40
    ]
    ordinary = [
        q
        for q in eligible
        if put.strike < q.strike < call.strike
        and ((tested == "put" and q.strike < call.strike) or (tested == "call" and q.strike > put.strike))
    ]

    def plan(candidates: list[Any]):
        if not candidates:
            return None
        new = min(candidates, key=lambda q: (abs(abs(q.delta) - 0.30), -(q.bid - old.ask), q.strike))
        return {"old": old, "new": OptionLeg.from_quote(new, role=old.role, side="sell"), "credit": new.bid - old.ask}

    return plan(ordinary), None


def _management_ticket(action: Any, lifecycle: LifecycleState, policy: CsaPolicy, legs: tuple[OptionLeg, ...], plans: dict[str, Any], underlying: str, created_at: str):
    if action.action_type is ActionType.CLOSE:
        return mixed_ticket(action, policy, underlying=underlying, close_legs=legs, open_legs=(), created_at=created_at, limit_price=float(plans["liquidation"]))
    if lifecycle.lane is LaneId.SHORT_STRANGLE and action.action_type is ActionType.ADJUST:
        plan = plans.get("roll")
        if not plan:
            raise ValueError("selected strangle adjustment has no executable roll plan")
        return build_strangle_adjustment_ticket(lifecycle, action, policy, underlying=underlying, close_legs=(plan["old"],), open_legs=(plan["new"],), created_at=created_at, limit_price=float(plan["credit"]))
    raise ValueError(f"unsupported selected management action: {action.action_type.value}")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _cooldown_elapsed(metadata: dict[str, Any], policy: CsaPolicy, observed_at: str) -> bool:
    last_adjustment = metadata.get("last_adjustment_at")
    if not last_adjustment:
        return True
    cooldown = (((policy.management.get("lifecycle") or {}).get("cooldown") or {}).get("minutes"))
    elapsed = (_parse_timestamp(observed_at) - _parse_timestamp(str(last_adjustment))).total_seconds() / 60.0
    return elapsed >= float(cooldown)


def _ticket_quote_map(ticket: Any, snapshot: Any) -> dict[str, dict[str, Any]]:
    quotes = {occ_symbol(snapshot.underlying, OptionLeg.from_quote(q, role="quote", side="buy")): q for q in snapshot.quotes}
    return {leg.instrument_id: {"bid": quotes[leg.instrument_id].bid, "ask": quotes[leg.instrument_id].ask, "fresh": True} for leg in ticket.legs if leg.instrument_id in quotes}
