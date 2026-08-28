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
    stage_live_management_replacement,
)
from kamandal_v2.live.orders import build_csa_live_ticket
from kamandal_v2.live.option_sessions import submission_window
from kamandal_v2.live.position_management import live_exit_policy
from kamandal_v2.market.public import occ_symbol
from kamandal_v2.planner.engine import _market_provider
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.action_arbiter import arbitrate_actions
from kamandal_v2.strategy_lanes.earnings_read import latest_earnings_snapshot
from kamandal_v2.strategy_lanes.lane_common import lifecycle_number, policy_bool, propose_action
from kamandal_v2.strategy_lanes.models import ActionType, CsaStage, LaneId, LifecycleState, SourceMode
from kamandal_v2.strategy_lanes.observations import PackageObservation, observe_package
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


@dataclass(frozen=True, slots=True)
class RestingProfitPolicy:
    """Platform policy for exact, non-conceding package profit orders."""

    enabled: bool = False
    arm_progress_pct: float = 25.0


def resting_profit_policy(config: dict[str, Any] | None, execution_mode: str) -> RestingProfitPolicy:
    raw = ((((config or {}).get("live") or {}).get("resting_profit") or {}))
    if not isinstance(raw, dict):
        raw = {}
    if execution_mode not in {"live", "shadow"}:
        raise ValueError(f"unsupported resting-profit execution mode: {execution_mode}")
    enabled = _config_bool(raw.get(f"{execution_mode}_enabled"), False)
    try:
        arm_progress_pct = float(raw.get("arm_progress_pct", 25.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("live.resting_profit.arm_progress_pct must be numeric") from exc
    if not 0.0 <= arm_progress_pct <= 100.0:
        raise ValueError("live.resting_profit.arm_progress_pct must be between 0 and 100")
    return RestingProfitPolicy(enabled=enabled, arm_progress_pct=arm_progress_pct)


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
    live_working_tickets = (
        baseline_store.live_order_intents_by_status(active_statuses)
        if execution_mode == "live"
        else []
    )
    shadow_working_tickets = (
        [ticket for ticket, _attempt in store.working_shadow_orders()]
        if execution_mode == "shadow"
        else []
    )
    resting_policy = resting_profit_policy(config, execution_mode)
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
            owned_live_tickets = [
                ticket for ticket in live_working_tickets if _ticket_owns_lifecycle(ticket, lifecycle)
            ]
            owned_shadow_tickets = [
                ticket for ticket in shadow_working_tickets if _ticket_owns_lifecycle(ticket, lifecycle)
            ]
            if execution_mode == "shadow":
                order_day = _parse_timestamp(started_at).date().isoformat()
                for ticket in owned_shadow_tickets:
                    if (
                        _is_resting_profit_ticket(ticket)
                        and str(ticket.metadata.get("resting_order_day") or "") != order_day
                    ):
                        store.update_shadow_order_intent_status(ticket.ticket_id, "expired_eod")
                owned_shadow_tickets = [
                    ticket
                    for ticket in owned_shadow_tickets
                    if not _is_resting_profit_ticket(ticket)
                    or str(ticket.metadata.get("resting_order_day") or "") == order_day
                ]
            existing_resting = next(
                (
                    ticket
                    for ticket in (
                        owned_live_tickets if execution_mode == "live" else owned_shadow_tickets
                    )
                    if _ticket_owns_lifecycle(ticket, lifecycle)
                    and _is_resting_profit_ticket(ticket)
                ),
                None,
            )
            ordinary_working_conflict = any(
                not _is_resting_profit_ticket(ticket) for ticket in owned_live_tickets
            )
            snapshot = market.chain_snapshot(underlying)
            active_legs = _active_option_legs(lifecycle, snapshot)
            context, plans, observed_lifecycle, observation = _management_context(
                lifecycle,
                policy,
                active_legs,
                snapshot,
                market,
                sqlite_path,
                config=config,
                observed_at=started_at,
                ownership_clear=True,
                working_order_conflict=ordinary_working_conflict,
            )
            if (
                execution_mode == "shadow"
                and existing_resting is not None
                and observation.quote_actionable
            ):
                adapter = ShadowExecutionAdapter()
                fill = adapter.simulate_fill(
                    existing_resting,
                    _ticket_quote_map(existing_resting, snapshot),
                    dict((policy.management.get("lifecycle") or {}).get("fill") or {}),
                    observed_at=started_at,
                    attempt=0,
                )
                store.save_shadow_fill(fill)
                if fill.status == "filled":
                    store.save_lifecycle(adapter.adopt_fill(observed_lifecycle, existing_resting, fill))
                    filled_actions += 1
                    counts["resting_profit_fill"] = counts.get("resting_profit_fill", 0) + 1
                    continue
            proposals = list(
                registry.resolve(lifecycle.lane)(observed_lifecycle, policy, context, proposed_at=started_at)
            )
            if existing_resting is None:
                resting_proposal = _resting_profit_proposal(
                    observed_lifecycle,
                    policy,
                    observation,
                    resting_policy,
                    proposed_at=started_at,
                )
                if resting_proposal is not None:
                    proposals.append(resting_proposal)
            raw_selected = arbitrate_actions(proposals).selected
            selected, observation = _apply_management_safety(
                raw_selected,
                observed_lifecycle,
                observation,
                config=config,
                store=writable_live_store,
                proposed_at=started_at,
            )
            execution_status = "held" if selected.action_type in {ActionType.HOLD, ActionType.BLOCK} else "ready"
            if selected.action_type not in {ActionType.HOLD, ActionType.BLOCK} and not observation.quote_actionable:
                execution_status = "waiting_valid_quote"
            observation = replace(
                observation,
                selected_action_type=selected.action_type.value,
                selected_reason=str(selected.reason_codes[0] if selected.reason_codes else ""),
                selected_reason_class=str(selected.payload.get("arbiter_class") or ""),
                execution_status=execution_status,
            )
            observed_lifecycle = _with_observation_mark(observed_lifecycle, observation)
            store.save_lifecycle(observed_lifecycle)
            observation_payload = {
                **observation.to_dict(),
                "observation_kind": "canonical_package",
                "group_id": observed_lifecycle.position_projection_id,
                "entry_kind": "credit" if float(observed_lifecycle.metadata.get("cumulative_cashflow") or 0.0) > 0 else "debit",
                "pnl_mid": observation.midpoint_pnl * 100.0,
                "pnl_natural": observation.natural_pnl * 100.0,
                "target_profit": _target_profit_dollars(observed_lifecycle, policy),
                "target_progress_pct": _target_progress(observed_lifecycle, policy, observation),
                "max_loss_watch": observation.adverse_loss_watch,
                "raw_selected_reason": str(raw_selected.reason_codes[0] if raw_selected.reason_codes else ""),
                "raw_selected_reason_class": str(raw_selected.payload.get("arbiter_class") or ""),
            }
            writable_live_store.record_live_position_mark(lifecycle.lifecycle_id, observation_payload)
            store.save_action(selected)
            counts[selected.action_type.value] = counts.get(selected.action_type.value, 0) + 1
            if selected.action_type in {ActionType.HOLD, ActionType.BLOCK}:
                continue
            if not observation.quote_actionable:
                continue
            ticket = _management_ticket(selected, observed_lifecycle, policy, active_legs, plans, underlying, started_at, observation=observation, config=config)
            if execution_mode == "shadow":
                if existing_resting is not None:
                    store.update_shadow_order_intent_status(existing_resting.ticket_id, "cancelled_superseded")
                    ticket = replace(
                        ticket,
                        metadata={
                            **ticket.metadata,
                            "parent_shadow_ticket_id": existing_resting.ticket_id,
                            "replace_reason": "superseded_management_action",
                        },
                    )
                is_resting = _is_resting_profit_ticket(ticket)
                store.save_shadow_order_intent(ticket, status="working" if is_resting else "proposed")
                quotes = _ticket_quote_map(ticket, snapshot)
                fill_policy = dict((policy.management.get("lifecycle") or {}).get("fill") or {})
                adapter = ShadowExecutionAdapter()
                final_fill = None
                attempts = (0,) if is_resting else range(int(float(fill_policy["max_attempts"])) + 1)
                for attempt in attempts:
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
                    if existing_resting is not None:
                        parent_status = str(existing_resting.get("_ledger_status") or "")
                        if parent_status in PENDING_TICKET_STATUSES:
                            writable_live_store.update_live_order_intent_status(
                                str(existing_resting["ticket_hash"]),
                                "cancelled_superseded_pre_submit",
                            )
                            writable_live_store.save_live_order_intent(
                                live_ticket,
                                status="stage_approved_pending_submit",
                            )
                        else:
                            stage_live_management_replacement(
                                writable_live_store,
                                existing_resting,
                                live_ticket,
                            )
                    else:
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
            result.append(
                OptionLeg(
                    role=str(item["role"]),
                    side=str(item["side"]),
                    option_type=str(item["option_type"]),
                    strike=float(item["strike"]),
                    expiration=str(item["expiration"]),
                    quantity=int(item["quantity"]),
                    mid=0.0,
                    bid=0.0,
                    ask=0.0,
                    delta=0.0,
                    gamma=0.0,
                    theta=0.0,
                    vega=0.0,
                    open_interest=0,
                )
            )
        else:
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
    config: dict[str, Any],
    observed_at: str,
    ownership_clear: bool,
    working_order_conflict: bool,
):
    quote_age = int((((config.get("live") or {}).get("option_submission") or {}).get("quote_max_age_minutes") or 10))
    observation = observe_package(
        lifecycle,
        policy,
        legs,
        snapshot,
        observed_at=observed_at,
        quote_max_age_minutes=quote_age,
    )
    profit_pct = observation.profit_pct if observation.quote_actionable else -1.0e12
    loss_multiple = observation.loss_multiple if observation.quote_actionable else 0.0
    observed_date = _parse_timestamp(observed_at).date()
    dtes = [max((date.fromisoformat(leg.expiration) - observed_date).days, 0) for leg in legs]
    half_time = _half_time_state(lifecycle, policy, legs, remaining_dtes=dtes)
    pre_event = _pre_event_exit_state(
        policy,
        sqlite_path=sqlite_path,
        underlying=snapshot.underlying,
        observed_date=observed_date,
    )
    common = {
        "working_order_conflict": working_order_conflict,
        "ownership_clear": ownership_clear,
        "hard_emergency": False,
        "event_exit_due": pre_event["due"],
        "half_time_exit_due": half_time["due"],
        "profit_pct": profit_pct,
        "loss_multiple": loss_multiple,
    }
    plans: dict[str, Any] = {
        "midpoint_liquidation": observation.midpoint_liquidation,
        "natural_liquidation": observation.natural_liquidation,
        "profit_target_close": _target_close_price(lifecycle, policy),
    }
    metadata = dict(lifecycle.metadata)
    metadata.update(
        {
            "last_marked_at": observed_at,
            "mark_liquidation_price": observation.midpoint_liquidation,
            "mark_natural_liquidation_price": observation.natural_liquidation,
            "mark_pnl_price": observation.midpoint_pnl,
            "mark_natural_pnl_price": observation.natural_pnl,
            "mark_profit_pct": round(profit_pct, 6),
            "mark_source": "validated_midpoint_package",
            "mark_observation_id": observation.observation_id,
            "mark_quote_actionable": observation.quote_actionable,
            "mark_quote_blockers": list(observation.quote_blockers),
            "mark_max_leg_bid_ask_pct": observation.max_leg_bid_ask_pct,
            "contract_multiplier": 100,
            "mark_entry_dte": half_time["entry_dte"],
            "mark_remaining_dte": half_time["remaining_dte"],
            "mark_half_time_threshold": half_time["threshold"],
            "mark_half_time_exit_due": half_time["due"],
            "mark_days_to_earnings": pre_event["days_to_event"],
            "mark_earnings_date": pre_event["event_date"],
            "mark_pre_event_exit_due": pre_event["due"],
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
            required_confirmations=int(_resolved_or_lifecycle(policy, "tested_side_confirmations", "tested_side_confirmation")),
            rearm_inside_confirmations=int(_resolved_or_default(policy, "rearm_inside_confirmations", 2)),
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
        context = {
            **common,
            "near_dte": (
                max((date.fromisoformat(short.expiration) - observed_date).days, 0)
                if short is not None
                else 0
            ),
            "far_dte": max((date.fromisoformat(long.expiration) - observed_date).days, 0),
            "short_leg_present": short is not None,
            "paired_position_complete": short is not None and len(legs) == 2,
        }
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
    return context, plans, replace(lifecycle, updated_at=observed_at, metadata=metadata), observation


def _half_time_state(
    lifecycle: LifecycleState,
    policy: CsaPolicy,
    legs: tuple[OptionLeg, ...],
    *,
    remaining_dtes: list[int],
) -> dict[str, Any]:
    """Reproduce the established half-time exit from immutable lifecycle facts."""

    enabled = policy_bool(
        policy.resolved_fields.get("half_time_exit", True),
        label=f"{policy.playbook_id}.half_time_exit",
    )
    opened_at = lifecycle.opened_at
    if lifecycle.cashflow_ledger:
        opened_at = str(lifecycle.cashflow_ledger[0].get("filled_at") or opened_at)
    opened_date = _parse_timestamp(opened_at).date()
    entry_dtes = [max((date.fromisoformat(leg.expiration) - opened_date).days, 0) for leg in legs]
    entry_dte = min(entry_dtes) if entry_dtes else None
    remaining_dte = min(remaining_dtes) if remaining_dtes else None
    threshold = entry_dte // 2 if entry_dte is not None else None
    due = bool(
        enabled
        and remaining_dte is not None
        and threshold is not None
        and remaining_dte <= threshold
    )
    return {
        "entry_dte": entry_dte,
        "remaining_dte": remaining_dte,
        "threshold": threshold,
        "due": due,
    }


def _pre_event_exit_state(
    policy: CsaPolicy,
    *,
    sqlite_path: str,
    underlying: str,
    observed_date: date,
) -> dict[str, Any]:
    """Evaluate the optional Sheet-owned pre-earnings risk exit.

    This preserves the established live rule: use the latest captured earnings
    date and calendar-day distance.  The specialised earnings-calendar lane
    ignores this state and retains its confirmed post-announcement exit.
    """

    raw_days = policy.resolved_fields.get("exit_pre_event_days")
    if isinstance(raw_days, bool) or raw_days in (None, ""):
        return {"due": False, "days_to_event": None, "event_date": ""}
    try:
        numeric_threshold = float(raw_days)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{policy.playbook_id}.exit_pre_event_days must be an integer") from exc
    if not numeric_threshold.is_integer():
        raise ValueError(f"{policy.playbook_id}.exit_pre_event_days must be an integer")
    threshold = int(numeric_threshold)
    if threshold < 0:
        raise ValueError(f"{policy.playbook_id}.exit_pre_event_days must be nonnegative")
    snapshot = latest_earnings_snapshot(sqlite_path, underlying)
    if snapshot is None or not snapshot.next_earnings_date:
        return {"due": False, "days_to_event": None, "event_date": ""}
    try:
        event_date = date.fromisoformat(snapshot.next_earnings_date)
    except ValueError as exc:
        raise ValueError(f"{underlying}: invalid captured earnings date") from exc
    days_to_event = (event_date - observed_date).days
    return {
        "due": days_to_event <= threshold,
        "days_to_event": days_to_event,
        "event_date": event_date.isoformat(),
    }


def _strangle_roll_plans(tested: str, put: OptionLeg, call: OptionLeg, snapshot: Any, policy: CsaPolicy):
    if not tested:
        return None, None
    old = call if tested == "put" else put
    eligible = [
        q
        for q in snapshot.quotes
        if q.option_type == old.option_type
        and q.expiration == old.expiration
        and abs(q.delta) <= _resolved_or_default(policy, "management_delta_max", 0.40)
        and q.mid > 0
        and q.ask >= q.bid >= 0
        and q.spread_pct <= float(policy.resolved_fields["max_bid_ask_pct"])
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
        target_delta = _resolved_or_default(policy, "management_delta_target", 0.30)
        new = min(candidates, key=lambda q: (abs(abs(q.delta) - target_delta), -(q.mid - old.mid), q.strike))
        return {
            "old": old,
            "new": OptionLeg.from_quote(new, role=old.role, side="sell"),
            "credit": new.mid - old.mid,
            "natural_credit": new.bid - old.ask,
        }

    return plan(ordinary), None


def _resolved_or_default(policy: CsaPolicy, field: str, default: float) -> float:
    raw = policy.resolved_fields.get(field)
    return float(default if raw in (None, "") else raw)


def _resolved_or_lifecycle(policy: CsaPolicy, field: str, lifecycle_field: str) -> float:
    raw = policy.resolved_fields.get(field)
    if raw not in (None, ""):
        return float(raw)
    return lifecycle_number(policy, lifecycle_field)


def _management_ticket(
    action: Any,
    lifecycle: LifecycleState,
    policy: CsaPolicy,
    legs: tuple[OptionLeg, ...],
    plans: dict[str, Any],
    underlying: str,
    created_at: str,
    *,
    observation: PackageObservation,
    config: dict[str, Any],
):
    if action.action_type is ActionType.CLOSE:
        reason_class = str(action.payload.get("arbiter_class") or "")
        resting_profit = reason_class == "resting_profit"
        limit_price = float(
            plans.get("profit_target_close", _target_close_price(lifecycle, policy))
            if resting_profit
            else plans["midpoint_liquidation"]
        )
        ticket = mixed_ticket(
            action,
            policy,
            underlying=underlying,
            close_legs=legs,
            open_legs=(),
            created_at=created_at,
            limit_price=limit_price,
        )
        exit_policy = live_exit_policy(config)
        cumulative_dollars = float(lifecycle.metadata.get("cumulative_cashflow") or 0.0) * 100.0
        floor_pnl = max(
            exit_policy.min_profit_to_trigger,
            _target_profit_dollars(lifecycle, policy) * exit_policy.profit_floor_pct / 100.0,
        )
        floor_net = floor_pnl - cumulative_dollars
        ticket = replace(
            ticket,
            metadata={
                **ticket.metadata,
                "decision_observation_id": observation.observation_id,
                "exit_reason": str(action.reason_codes[0] if action.reason_codes else ""),
                "exit_reason_class": reason_class,
                "exit_midpoint_net": observation.midpoint_liquidation * 100.0,
                "exit_natural_net": observation.natural_liquidation * 100.0,
                **(
                    {
                        "resting_profit_order": True,
                        "resting_profit_arm_progress_pct": float(action.payload.get("arm_progress_pct") or 25.0),
                        "resting_order_day": str(action.payload.get("resting_order_day") or ""),
                        "exit_target_net": limit_price * 100.0,
                        "target_profit_dollars": _target_profit_dollars(lifecycle, policy),
                    }
                    if resting_profit
                    else {}
                ),
                **(
                    {"exit_profit_floor_net": floor_net}
                    if str(action.payload.get("arbiter_class") or "") == "executable_profit"
                    else {}
                ),
                "execution_envelope": (
                    {
                        "initial": "strategy_target",
                        "boundary": "strategy_target",
                        "target_net": limit_price * 100.0,
                        "reprice_allowed": False,
                        "quote_max_bid_ask_pct": observation.max_bid_ask_pct,
                    }
                    if resting_profit
                    else {
                        "initial": "midpoint",
                        "boundary": "natural",
                        "midpoint_net": observation.midpoint_liquidation * 100.0,
                        "natural_net": observation.natural_liquidation * 100.0,
                        "quote_max_bid_ask_pct": observation.max_bid_ask_pct,
                    }
                ),
            },
        )
        return _with_position_projection(ticket, lifecycle)
    if lifecycle.lane is LaneId.SHORT_STRANGLE and action.action_type is ActionType.ADJUST:
        plan = plans.get("roll")
        if not plan:
            raise ValueError("selected strangle adjustment has no executable roll plan")
        ticket = build_strangle_adjustment_ticket(lifecycle, action, policy, underlying=underlying, close_legs=(plan["old"],), open_legs=(plan["new"],), created_at=created_at, limit_price=float(plan["credit"]))
        ticket = replace(
            ticket,
            metadata={
                **ticket.metadata,
                "decision_observation_id": observation.observation_id,
                "execution_envelope": {
                    "initial": "midpoint",
                    "boundary": "natural",
                    "midpoint_net": float(plan["credit"]) * 100.0,
                    "natural_net": float(plan["natural_credit"]) * 100.0,
                    "quote_max_bid_ask_pct": observation.max_bid_ask_pct,
                },
            },
        )
        return _with_position_projection(ticket, lifecycle)
    raise ValueError(f"unsupported selected management action: {action.action_type.value}")


def _apply_management_safety(
    selected: Any,
    lifecycle: LifecycleState,
    observation: PackageObservation,
    *,
    config: dict[str, Any],
    store: LocalStore,
    proposed_at: str,
) -> tuple[Any, PackageObservation]:
    reason_class = str(selected.payload.get("arbiter_class") or "")
    if reason_class != "adverse_price_loss":
        return selected, observation
    window = submission_window(
        config,
        {
            "underlying": observation.underlying,
            "intent_type": "close",
            "csa_action_type": "close",
            "csa_action_reason_class": reason_class,
        },
        close=True,
        now=_parse_timestamp(proposed_at),
    )
    window_allowed = bool(window["allowed"])
    policy = live_exit_policy(config)
    prior = store.canonical_loss_confirmation_count(
        lifecycle.lifecycle_id,
        observed_at=proposed_at,
        window_minutes=policy.loss_watch_window_minutes,
    )
    confirmations = prior + 1 if observation.quote_actionable and window_allowed else prior
    observation = replace(
        observation,
        adverse_loss_watch=True,
        loss_window_allowed=window_allowed,
        loss_confirmation_count=confirmations,
    )
    if not window_allowed or confirmations < policy.loss_watch_confirmations_required:
        reason = "adverse_loss_session_buffer" if not window_allowed else "loss_watch_debouncing"
        hold = propose_action(
            lifecycle,
            ActionType.HOLD,
            reason,
            arbiter_class="hold",
            proposed_at=proposed_at,
            payload={
                "deferred_action_reason": str(selected.reason_codes[0] if selected.reason_codes else ""),
                "loss_confirmation_count": confirmations,
                "loss_confirmations_required": policy.loss_watch_confirmations_required,
                "session_reason": str(window.get("reason") or ""),
            },
        )
        return arbitrate_actions((hold,)).selected, observation
    return selected, observation


def _with_observation_mark(lifecycle: LifecycleState, observation: PackageObservation) -> LifecycleState:
    waiting_since = ""
    if observation.execution_status == "waiting_valid_quote":
        waiting_since = str(lifecycle.metadata.get("mark_waiting_valid_quote_since") or observation.observed_at)
    metadata = {
        **lifecycle.metadata,
        "mark_observation_id": observation.observation_id,
        "mark_liquidation_price": observation.midpoint_liquidation,
        "mark_natural_liquidation_price": observation.natural_liquidation,
        "mark_pnl_price": observation.midpoint_pnl,
        "mark_natural_pnl_price": observation.natural_pnl,
        "mark_profit_pct": observation.profit_pct,
        "mark_source": "validated_midpoint_package",
        "mark_quote_actionable": observation.quote_actionable,
        "mark_quote_blockers": list(observation.quote_blockers),
        "mark_max_leg_bid_ask_pct": observation.max_leg_bid_ask_pct,
        "mark_selected_reason": observation.selected_reason,
        "mark_selected_reason_class": observation.selected_reason_class,
        "mark_execution_status": observation.execution_status,
        "mark_waiting_valid_quote_since": waiting_since,
    }
    return replace(lifecycle, metadata=metadata)


def _target_profit_dollars(lifecycle: LifecycleState, policy: CsaPolicy) -> float:
    cumulative = float(lifecycle.metadata.get("cumulative_cashflow") or 0.0)
    entry = float(lifecycle.cashflow_ledger[0]["amount"]) if lifecycle.cashflow_ledger else cumulative
    return abs(entry) * 100.0 * float(policy.resolved_fields.get("profit_target_pct") or 0.0) / 100.0


def _target_close_price(lifecycle: LifecycleState, policy: CsaPolicy) -> float:
    """Return the signed package cashflow that realizes original target dollars."""

    cumulative_dollars = float(lifecycle.metadata.get("cumulative_cashflow") or 0.0) * 100.0
    return round((_target_profit_dollars(lifecycle, policy) - cumulative_dollars) / 100.0, 6)


def _resting_profit_proposal(
    lifecycle: LifecycleState,
    policy: CsaPolicy,
    observation: PackageObservation,
    resting_policy: RestingProfitPolicy,
    *,
    proposed_at: str,
):
    if not resting_policy.enabled or not observation.quote_actionable:
        return None
    progress = _target_progress(lifecycle, policy, observation)
    if progress < resting_policy.arm_progress_pct:
        return None
    order_day = _parse_timestamp(proposed_at).date().isoformat()
    return propose_action(
        lifecycle,
        ActionType.CLOSE,
        "profit_target_resting",
        arbiter_class="resting_profit",
        proposed_at=proposed_at,
        payload={
            "resting_order_day": order_day,
            "arm_progress_pct": resting_policy.arm_progress_pct,
            "target_progress_pct": round(progress, 6),
        },
    )


def _target_progress(lifecycle: LifecycleState, policy: CsaPolicy, observation: PackageObservation) -> float:
    target = _target_profit_dollars(lifecycle, policy)
    return observation.midpoint_pnl * 100.0 / target * 100.0 if target > 0 else 0.0


def _with_position_projection(ticket: Any, lifecycle: LifecycleState) -> Any:
    if not lifecycle.position_projection_id:
        return ticket
    return replace(
        ticket,
        metadata={
            **ticket.metadata,
            "position_projection_id": lifecycle.position_projection_id,
        },
    )


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


def _is_resting_profit_ticket(ticket: Any) -> bool:
    if isinstance(ticket, dict):
        if bool(ticket.get("resting_profit_order")):
            return True
        nested = ticket.get("csa_strategy_ticket") or {}
        return bool((nested.get("metadata") or {}).get("resting_profit_order"))
    return bool(getattr(ticket, "metadata", {}).get("resting_profit_order"))


def _ticket_owns_lifecycle(ticket: Any, lifecycle: LifecycleState) -> bool:
    if isinstance(ticket, dict):
        owners = {
            str(ticket.get("csa_lifecycle_id") or ""),
            str(ticket.get("position_projection_id") or ticket.get("group_id") or ""),
        }
        return bool({lifecycle.lifecycle_id, lifecycle.position_projection_id} & (owners - {""}))
    return str(getattr(ticket, "lifecycle_id", "")) == lifecycle.lifecycle_id


def _config_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("resting-profit enable flags must be boolean")
