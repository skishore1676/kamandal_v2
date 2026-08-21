"""One invocation that produces isolated live and shadow planning books.

The function is intentionally broker-inert.  It uses the existing portfolio
optimizer for both books and only separates inputs, portfolio mode, audit
receipts, and result ownership.  Later phases replace the old CSA scan caller
with this source-level entry point.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import ChainSnapshot, Idea, OptionLeg, Playbook, PortfolioState, UniverseEntry
from kamandal_v2.planner.engine import PlanRunResult, PlanningSourceGroup, _market_provider, run_plan
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.sheets import write_daily_plan
from kamandal_v2.stores.audit import AuditWriter
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.lifecycle import freeze_lifecycle_policy
from kamandal_v2.strategy_engine.ownership import retire_orphaned_pending_live_lifecycles
from kamandal_v2.strategy_engine.policy import ExecutionMode, PlaybookPolicy, PolicyCompilation, compile_playbook_policies
from kamandal_v2.strategy_lanes.action_arbiter import arbitrate_actions
from kamandal_v2.strategy_lanes.daily_policy import DailyPolicySnapshot, policy_tables_hash
from kamandal_v2.strategy_lanes.models import ActionType, CsaStage, LaneId, LifecycleState, ShadowFill, SourceMode, stable_csa_id
from kamandal_v2.strategy_lanes.policy import CsaPolicy
from kamandal_v2.strategy_lanes.shadow_execution import ShadowExecutionAdapter
from kamandal_v2.strategy_lanes.store import CsaStore
from kamandal_v2.strategy_lanes.tickets import open_ticket_from_candidate
from kamandal_v2.strategy_lanes.lane_common import propose_action
from kamandal_v2.market.public import occ_symbol
from kamandal_v2.strategy_engine.event_timing import entry_session_due
from kamandal_v2.strategy_lanes.earnings_read import latest_earnings_snapshot


@dataclass(frozen=True, slots=True)
class PlanningBook:
    mode: ExecutionMode
    policy_ids: tuple[str, ...]
    result: PlanRunResult | None
    errors: tuple[str, ...]
    handoffs: tuple[dict[str, Any], ...] = ()

    @property
    def healthy_zero(self) -> bool:
        return self.result is not None and not self.result.plans and not self.errors


@dataclass(frozen=True, slots=True)
class UnifiedPlanningResult:
    compilation: PolicyCompilation
    live: PlanningBook
    shadow: PlanningBook


def run_unified_books(
    config: dict[str, Any],
    *,
    universe_rows: list[dict[str, Any]],
    playbook_rows: list[dict[str, Any]],
    idea_paths: list[str | Path],
    provider: str = "fixture",
    store: LocalStore | None = None,
    audit_root: str | Path = "data/audit/unified",
    write_sheet: bool = False,
    daily_policy_snapshot: DailyPolicySnapshot | None = None,
    exclude_candidate_ids: set[str] | None = None,
    exclude_contract_keys: set[str] | None = None,
    register_plan_attempt: bool = True,
) -> UnifiedPlanningResult:
    """Build independent books from one normalized Sheet snapshot.

    A policy error is attached to both books rather than allowing either book to
    masquerade as complete.  Once policy compilation succeeds, failures stay
    per-book: a shadow failure cannot erase a valid live result and vice versa.
    """
    compilation = compile_playbook_policies(playbook_rows)
    supplied_policy_hash = policy_tables_hash(
        {
            "universe": [dict(row) for row in universe_rows],
            "playbooks": [dict(row) for row in playbook_rows],
        }
    )
    active_store = store or LocalStore()
    retire_orphaned_pending_live_lifecycles(active_store)
    universe = [UniverseEntry.from_row(row) for row in universe_rows if row.get("symbol")]
    if not compilation.ok:
        errors = compilation.errors
        return UnifiedPlanningResult(
            compilation=compilation,
            live=PlanningBook(ExecutionMode.LIVE, (), None, errors),
            shadow=PlanningBook(ExecutionMode.SHADOW, (), None, errors),
        )
    live = _run_book(
        ExecutionMode.LIVE,
        compilation.policies,
        universe,
        config,
        idea_paths,
        provider,
        active_store,
        audit_root,
        write_sheet,
        daily_policy_snapshot=daily_policy_snapshot,
        supplied_policy_hash=supplied_policy_hash,
        exclude_candidate_ids=exclude_candidate_ids,
        exclude_contract_keys=exclude_contract_keys,
        register_plan_attempt=register_plan_attempt,
    )
    shadow = _run_book(ExecutionMode.SHADOW, compilation.policies, universe, config, idea_paths, provider, active_store, audit_root, write_sheet)
    return UnifiedPlanningResult(compilation=compilation, live=live, shadow=shadow)


def _run_book(
    mode: ExecutionMode,
    policies: tuple[PlaybookPolicy, ...],
    universe: list[UniverseEntry],
    config: dict[str, Any],
    idea_paths: list[str | Path],
    provider: str,
    store: LocalStore,
    audit_root: str | Path,
    write_sheet: bool,
    *,
    daily_policy_snapshot: DailyPolicySnapshot | None = None,
    supplied_policy_hash: str = "",
    exclude_candidate_ids: set[str] | None = None,
    exclude_contract_keys: set[str] | None = None,
    register_plan_attempt: bool = True,
) -> PlanningBook:
    selected = tuple(policy for policy in policies if policy.mode is mode)
    mode_config = deepcopy(config)
    mode_config.setdefault("runtime", {})["mode"] = mode.value
    live_renderer = None
    market_override = None
    if mode is ExecutionMode.SHADOW:
        mode_config.setdefault("execution", {})["approval_mode"] = "shadow_auto_top_plan"
        # Shadow is an execution adapter, not a looser strategy.  Promotion
        # evidence is only comparable when admission uses the live gate policy.
        live_gates = config.get("live") or {}
        shadow_gates = mode_config.setdefault("shadow", {})
        shadow_gates["match_gate_mode"] = str(live_gates.get("match_gate_mode") or "strict")
        shadow_gates["candidate_filter_mode"] = str(live_gates.get("candidate_filter_mode") or "strict")
        working_store = CsaStore(store.sqlite_path)
        working_orders = working_store.working_shadow_orders()
        if working_orders:
            active_playbook_ids = {policy.playbook_id for policy in selected}
            active_symbols = {
                ticket.underlying
                for ticket, _attempt in working_orders
                if _working_shadow_playbook_id(working_store, ticket) in active_playbook_ids
            }
            snapshots: dict[str, ChainSnapshot] = {}
            errors: list[str] = []
            if active_symbols:
                market_override = _market_provider(mode_config, provider=provider, store=store)
                for symbol in sorted(active_symbols):
                    try:
                        snapshots[symbol] = market_override.chain_snapshot(symbol)
                    except Exception as exc:  # noqa: BLE001 - the book receipt owns the failure.
                        errors.append(f"{symbol}: working shadow market unavailable: {type(exc).__name__}: {exc}")
            _advance_working_shadow_orders(
                working_store,
                working_orders,
                snapshots,
                active_playbook_ids=active_playbook_ids,
                observed_at=_planning_observed_at(mode_config),
                errors=errors,
            )
            if errors:
                return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), None, tuple(errors))
    if not selected:
        return PlanningBook(mode, (), None, ())
    if mode is ExecutionMode.LIVE:
        snapshot_error = _validate_live_policy_snapshot(
            daily_policy_snapshot,
            policies=selected,
            supplied_policy_hash=supplied_policy_hash,
        )
        if snapshot_error:
            return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), None, (snapshot_error,))
    if mode is ExecutionMode.LIVE:
        from kamandal_v2.live.advisory import _live_candidate_policy, live_config, render_live_plan_rows
        from kamandal_v2.live.plan_fallback import fallback_enabled

        mode_config = live_config(mode_config)
        live_renderer = (render_live_plan_rows, _live_candidate_policy)
    playbooks = [Playbook.from_row(policy.fields) for policy in selected]
    def candidate_postprocessor(candidates: list[Any], current_store: LocalStore, current_config: dict[str, Any], portfolio: PortfolioState) -> None:
        _gate_earnings_calendar_entries(candidates, selected, sqlite_path=current_store.sqlite_path, observed_at=_planning_observed_at(current_config))
        if live_renderer is not None:
            live_renderer[1](
                candidates,
                current_store,
                current_config,
                portfolio,
                exclude_candidate_ids=exclude_candidate_ids,
                exclude_contract_keys=exclude_contract_keys,
            )

    try:
        result = run_plan(
            mode_config,
            idea_paths=idea_paths,
            config_source="seed",
            provider=provider,
            write_sheet=write_sheet if mode is ExecutionMode.SHADOW else False,
            store=store,
            audit=AuditWriter(Path(audit_root) / mode.value),
            candidate_postprocessor=candidate_postprocessor,
            universe_override=universe,
            playbooks_override=playbooks,
            source_groups_factory=lambda ideas, entries, selected_playbooks, portfolio: _source_groups(
                ideas,
                entries,
                selected_playbooks,
                policies=selected,
                portfolio=portfolio,
            ),
            market_override=market_override,
        )
    except Exception as exc:  # noqa: BLE001 - report failure-isolated book receipt.
        return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), None, (f"{type(exc).__name__}: {exc}",))
    if mode is ExecutionMode.LIVE:
        rows = live_renderer[0](result, mode_config, store=store, mode="live_advisory")
        try:
            handoffs = _bind_selected_live_lifecycle(
                result,
                selected,
                store=store,
                daily_policy_snapshot=daily_policy_snapshot,
            )
        except Exception as exc:  # noqa: BLE001 - no live intent may lack an adoptable lifecycle.
            return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), result, (f"live handoff failed: {type(exc).__name__}: {exc}",))
        result.daily_plan_rows[:] = rows
        if write_sheet:
            write_daily_plan(mode_config, rows, DAILY_PLAN_HEADER, replace_lanes={"live_advisory"})
        if register_plan_attempt and fallback_enabled(mode_config) and result.plans:
            _register_unified_rank_one_attempt(
                result,
                store=store,
                handoffs=handoffs,
                daily_policy_snapshot=daily_policy_snapshot,
                idea_paths=idea_paths,
                provider=provider,
            )
        return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), result, (), tuple(handoffs))
    elif result.plans and result.plans[0].operator_action == "approve":
        try:
            handoffs = _materialize_shadow_handoff(result, selected, sqlite_path=store.sqlite_path)
        except Exception as exc:  # noqa: BLE001 - a missing typed handoff is an unhealthy book.
            return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), result, (f"shadow handoff failed: {type(exc).__name__}: {exc}",))
        store.event("unified_shadow_plan_auto_approved", {"plan_run_id": result.plan_run_id, "plan_id": result.plans[0].plan_id, "handoffs": handoffs})
        return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), result, (), tuple(handoffs))
    return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), result, ())


def _advance_working_shadow_orders(
    store: CsaStore,
    working_orders: list[tuple[Any, int]],
    snapshots: dict[str, ChainSnapshot],
    *,
    active_playbook_ids: set[str],
    observed_at: str,
    errors: list[str],
) -> int:
    """Continue frozen shadow entry tickets without consulting current policy details.

    The current Sheet controls whether a playbook remains routed to shadow.  A
    working ticket's price path and fill policy were frozen when it was created;
    recompiling the same playbook must not rewrite or strand that in-flight
    decision.  Ineligible or structurally orphaned shadow tickets are retired as
    missed evidence instead of failing the entire planning invocation.
    """

    filled_count = 0
    adapter = ShadowExecutionAdapter()
    for ticket, last_attempt in working_orders:
        lifecycle = store.lifecycle(ticket.lifecycle_id)
        playbook_id = _working_shadow_playbook_id(store, ticket, lifecycle=lifecycle)
        if playbook_id not in active_playbook_ids:
            _retire_working_shadow_order(
                store,
                ticket,
                lifecycle,
                last_attempt=last_attempt,
                observed_at=observed_at,
                reason="playbook_no_longer_routed_to_shadow",
                playbook_id=playbook_id,
            )
            continue
        if lifecycle is None:
            _retire_working_shadow_order(
                store,
                ticket,
                lifecycle,
                last_attempt=last_attempt,
                observed_at=observed_at,
                reason="lifecycle_missing",
                playbook_id=playbook_id,
            )
            continue
        if lifecycle.version != ticket.lifecycle_version or lifecycle.status != "proposed":
            _retire_working_shadow_order(
                store,
                ticket,
                lifecycle,
                last_attempt=last_attempt,
                observed_at=observed_at,
                reason="lifecycle_no_longer_proposed",
                playbook_id=playbook_id,
            )
            continue
        snapshot = snapshots.get(ticket.underlying)
        if snapshot is None:
            # A provider failure is retryable and remains an honest shadow-book
            # failure.  Do not silently consume the ticket without market data.
            if not any(error.startswith(f"{ticket.underlying}:") for error in errors):
                errors.append(f"{ticket.ticket_id}: working shadow order lacks fresh market state")
            continue
        fill_policy = ticket.metadata.get("fill_policy") or {}
        try:
            fill = adapter.simulate_fill(
                ticket,
                _working_ticket_quotes(ticket, snapshot),
                fill_policy,
                observed_at=observed_at,
                attempt=last_attempt + 1,
            )
        except (TypeError, ValueError) as exc:
            errors.append(f"{ticket.ticket_id}: working shadow attempt failed: {type(exc).__name__}: {exc}")
            continue
        store.save_shadow_fill(fill)
        if fill.status == "filled":
            store.save_lifecycle(adapter.adopt_fill(lifecycle, ticket, fill))
            filled_count += 1
        elif fill.status == "missed":
            store.save_lifecycle(replace(lifecycle, status="entry_missed", updated_at=observed_at))
    return filled_count


def _working_shadow_playbook_id(
    store: CsaStore,
    ticket: Any,
    *,
    lifecycle: LifecycleState | None = None,
) -> str:
    lifecycle = lifecycle if lifecycle is not None else store.lifecycle(ticket.lifecycle_id)
    return str(
        ticket.metadata.get("playbook_id")
        or ((lifecycle.metadata if lifecycle is not None else {}).get("playbook_id"))
        or ""
    )


def _retire_working_shadow_order(
    store: CsaStore,
    ticket: Any,
    lifecycle: LifecycleState | None,
    *,
    last_attempt: int,
    observed_at: str,
    reason: str,
    playbook_id: str,
) -> None:
    attempt = max(last_attempt + 1, 0)
    fill = ShadowFill(
        fill_id=stable_csa_id("shadow-fill", [ticket.ticket_id, attempt, observed_at, "missed", reason]),
        ticket_id=ticket.ticket_id,
        lifecycle_id=ticket.lifecycle_id,
        status="missed",
        attempt=attempt,
        natural_price=0.0,
        working_price=float(ticket.limit_price),
        filled_price=None,
        filled_at=observed_at,
        quote_evidence={
            "blocking": {
                "reason": reason,
                "playbook_id": playbook_id,
                "resolution": "retired_without_effect",
            }
        },
    )
    store.save_shadow_fill(fill)
    if (
        lifecycle is not None
        and lifecycle.version == ticket.lifecycle_version
        and lifecycle.status == "proposed"
    ):
        store.save_lifecycle(replace(lifecycle, status="entry_missed", updated_at=observed_at))


def _working_ticket_quotes(ticket: Any, snapshot: ChainSnapshot) -> dict[str, dict[str, Any]]:
    by_instrument = {
        occ_symbol(snapshot.underlying, OptionLeg.from_quote(quote, role="quote", side="buy")): quote
        for quote in snapshot.quotes
    }
    return {
        leg.instrument_id: {
            "bid": by_instrument[leg.instrument_id].bid,
            "ask": by_instrument[leg.instrument_id].ask,
            "fresh": True,
        }
        for leg in ticket.legs
        if leg.instrument_id in by_instrument
    }


def _bind_selected_live_lifecycle(
    result: PlanRunResult,
    policies: tuple[PlaybookPolicy, ...],
    *,
    store: LocalStore,
    daily_policy_snapshot: DailyPolicySnapshot | None,
) -> list[dict[str, Any]]:
    """Attach the frozen selected-entry lifecycle to its existing guarded ticket."""
    if not result.plans:
        return []
    selected_plan = result.plans[0]
    policy_by_id = {policy.playbook_id: policy for policy in policies}
    csa_store = CsaStore(store.sqlite_path)
    handoffs: list[dict[str, Any]] = []
    for candidate in selected_plan.candidates:
        policy = policy_by_id.get(candidate.playbook_id)
        if policy is None:
            raise ValueError(f"selected candidate {candidate.candidate_id} has no unified policy")
        if daily_policy_snapshot is None:  # Defended above, retained for direct callers.
            raise ValueError("live selected-entry handoff requires a daily policy snapshot")
        compiled = _shadow_csa_policy(policy, stage=CsaStage.LIVE)
        lifecycle_id = stable_csa_id("unified-live-lifecycle", [selected_plan.plan_id, candidate.candidate_id])
        lifecycle = freeze_lifecycle_policy(
            LifecycleState(
                lifecycle_id=lifecycle_id,
                opportunity_id=stable_csa_id("unified-opportunity", [policy.playbook_id, candidate.idea_id, candidate.underlying]),
                lane=compiled.lane,
                version=1,
                status="pending_live_submission",
                active_legs=(),
                cashflow_ledger=(),
                opened_at=_plan_time(result),
                updated_at=_plan_time(result),
                policy_hash=compiled.policy_hash,
                metadata={
                    "playbook_id": compiled.playbook_id,
                    "underlying": candidate.underlying,
                    "candidate_id": candidate.candidate_id,
                    "bpr": candidate.estimated_bpr,
                    "greeks": candidate.greeks.to_dict(),
                    "unified_plan_id": selected_plan.plan_id,
                    "execution_mode": "live",
                    # This is the daily Sheet snapshot identity, not the
                    # per-playbook compiled-policy identity below.
                    "policy_snapshot_hash": daily_policy_snapshot.snapshot_hash,
                    "policy_snapshot_date": daily_policy_snapshot.trading_date,
                    "entry_policy_hash": compiled.policy_hash,
                    "source_identity": {"idea_id": candidate.idea_id, "plan_run_id": result.plan_run_id},
                },
            ),
            compiled_policy=compiled.to_dict(),
        )
        existing = csa_store.lifecycle(lifecycle_id)
        if existing is None:
            csa_store.save_lifecycle(lifecycle)
        elif existing.policy_hash != lifecycle.policy_hash:
            raise ValueError(f"replayed live candidate policy differs for {candidate.candidate_id}")
        else:
            lifecycle = existing
        tickets = [
            ticket
            for ticket in store.live_order_intents_by_type("open")
            if str(ticket.get("plan_id") or "") == selected_plan.plan_id
            and str(ticket.get("candidate_id") or "") == candidate.candidate_id
        ]
        if len(tickets) != 1:
            raise ValueError(f"selected live candidate {candidate.candidate_id} has {len(tickets)} guarded intents")
        ticket = dict(tickets[0])
        action = arbitrate_actions((propose_action(lifecycle, ActionType.OPEN, "unified_plan_selected", arbiter_class="routine_management", proposed_at=_plan_time(result)),)).selected
        strategy_ticket = open_ticket_from_candidate(candidate, action, compiled, created_at=_plan_time(result), limit_price=candidate.net_credit)
        ticket.update(
            {
                "csa_policy_hash": compiled.policy_hash,
                "csa_playbook_id": compiled.playbook_id,
                "csa_stage": compiled.stage.value,
                "csa_lifecycle_id": lifecycle_id,
                "csa_strategy_ticket": strategy_ticket.to_dict(),
                "csa_policy_snapshot_date": str(lifecycle.metadata["policy_snapshot_date"]),
                "csa_policy_snapshot_hash": str(lifecycle.metadata["policy_snapshot_hash"]),
                "csa_authorization_policy": "unified_strategy_engine",
                "csa_compiled_policy_hash": compiled.policy_hash,
                "stage_authorized": True,
            }
        )
        store.save_live_order_intent(ticket, status=str(ticket["_ledger_status"]))
        handoffs.append(
            {"source_id": candidate.idea_id, "plan_id": selected_plan.plan_id, "candidate_id": candidate.candidate_id, "playbook_id": compiled.playbook_id, "capability": policy.capability.key, "mode": "live", "lifecycle_id": lifecycle_id, "ticket_id": strategy_ticket.ticket_id, "adapter_state": str(ticket["_ledger_status"])}
        )
    return handoffs


def _register_unified_rank_one_attempt(
    result: PlanRunResult,
    *,
    store: LocalStore,
    handoffs: list[dict[str, Any]],
    daily_policy_snapshot: DailyPolicySnapshot | None,
    idea_paths: list[str | Path],
    provider: str,
) -> dict[str, Any] | None:
    """Register fallback only after the unified typed live handoff succeeds."""

    if not result.plans or daily_policy_snapshot is None:
        return None
    from kamandal_v2.live.plan_fallback import register_rank_one_attempt

    selected_plan = result.plans[0]
    candidate_ids = {candidate.candidate_id for candidate in selected_plan.candidates}
    tickets = [
        dict(ticket)
        for ticket in store.live_order_intents_by_type("open")
        if str(ticket.get("plan_id") or "") == selected_plan.plan_id
        and str(ticket.get("candidate_id") or "") in candidate_ids
    ]
    if not tickets:
        return None
    handoff_by_candidate = {str(item.get("candidate_id") or ""): item for item in handoffs}
    if {str(ticket.get("candidate_id") or "") for ticket in tickets} != candidate_ids:
        raise ValueError("unified rank-one fallback registration lacks one guarded ticket per selected candidate")
    for ticket in tickets:
        candidate_id = str(ticket.get("candidate_id") or "")
        handoff = handoff_by_candidate.get(candidate_id) or {}
        if (
            ticket.get("stage_authorized") is not True
            or not ticket.get("csa_lifecycle_id")
            or not ticket.get("csa_compiled_policy_hash")
            or str(ticket.get("csa_policy_snapshot_hash") or "") != daily_policy_snapshot.snapshot_hash
            or str(ticket.get("csa_policy_snapshot_date") or "") != daily_policy_snapshot.trading_date
            or str(handoff.get("lifecycle_id") or "") != str(ticket.get("csa_lifecycle_id") or "")
        ):
            raise ValueError(f"unified rank-one ticket {candidate_id} failed typed lifecycle identity")
    return register_rank_one_attempt(
        store,
        campaign_id=f"unified:{daily_policy_snapshot.trading_date}:{selected_plan.plan_id}",
        plan=selected_plan,
        tickets=tickets,
        plan_run_id=result.plan_run_id,
        idea_paths=[str(path) for path in idea_paths],
        config_source="unified-plan",
        provider=provider,
        daily_policy_snapshot=daily_policy_snapshot,
        lifecycle_handoffs=handoffs,
    )


def run_unified_fallback_plan(
    config: dict[str, Any],
    *,
    store: LocalStore,
    idea_paths: list[str | Path],
    provider: str,
    exclude_candidate_ids: set[str] | None = None,
    exclude_contract_keys: set[str] | None = None,
    daily_policy_snapshot: DailyPolicySnapshot | None = None,
    expected_policy_snapshot: dict[str, str] | None = None,
    audit_root: str | Path = "data/audit/unified",
) -> UnifiedPlanningResult:
    """Replan through the production unified path using the frozen day snapshot."""

    if daily_policy_snapshot is None:
        from kamandal_v2.strategy_lanes.daily_policy import load_daily_policy_snapshot

        daily_policy_snapshot = load_daily_policy_snapshot(config)
    expected = expected_policy_snapshot or {}
    expected_date = str(expected.get("date") or "")
    expected_hash = str(expected.get("hash") or "")
    if expected_date and daily_policy_snapshot.trading_date != expected_date:
        raise ValueError("fallback policy snapshot date no longer matches rank-one campaign")
    if expected_hash and daily_policy_snapshot.snapshot_hash != expected_hash:
        raise ValueError("fallback policy snapshot hash no longer matches rank-one campaign")
    tables = daily_policy_snapshot.tables
    return run_unified_books(
        config,
        universe_rows=[dict(row) for row in tables.get("universe") or []],
        playbook_rows=[dict(row) for row in tables.get("playbooks") or []],
        idea_paths=idea_paths,
        provider=provider,
        store=store,
        audit_root=audit_root,
        write_sheet=False,
        daily_policy_snapshot=daily_policy_snapshot,
        exclude_candidate_ids=set(exclude_candidate_ids or set()),
        exclude_contract_keys=set(exclude_contract_keys or set()),
        register_plan_attempt=False,
    )


def _validate_live_policy_snapshot(
    snapshot: DailyPolicySnapshot | None,
    *,
    policies: tuple[PlaybookPolicy, ...],
    supplied_policy_hash: str,
) -> str | None:
    """Require live planning to use the exact persisted daily policy input.

    The daily snapshot is captured once by the unified command and becomes the
    shared identity between selection and the guarded executor.  Comparing the
    supplied raw tables to the snapshot prevents a caller from planning against
    a newer Sheet read while attaching an older snapshot hash to its ticket.
    """
    if snapshot is None:
        return "live planning requires the persisted daily policy snapshot"
    snapshot_hash = policy_tables_hash(snapshot.tables)
    if snapshot.snapshot_hash != snapshot_hash:
        return "live planning daily policy snapshot hash is invalid"
    if supplied_policy_hash != snapshot.snapshot_hash:
        return "live planning inputs differ from the persisted daily policy snapshot"
    # Every selected live playbook must still be present in the frozen daily
    # table.  Full raw-row equality is intentionally avoided here because the
    # planner has already normalized optional/blank Sheet fields.
    snapshot_ids = {
        str(row.get("playbook_id") or "")
        for row in snapshot.tables.get("playbooks") or []
    }
    missing = sorted(policy.playbook_id for policy in policies if policy.mode is ExecutionMode.LIVE and policy.playbook_id not in snapshot_ids)
    if missing:
        return "live planning snapshot missing playbook(s): " + ", ".join(missing)
    return None


def _materialize_shadow_handoff(
    result: PlanRunResult,
    policies: tuple[PlaybookPolicy, ...],
    *,
    sqlite_path: str | Path,
) -> list[dict[str, Any]]:
    """Persist the rank-one selected shadow package as typed lifecycle state.

    This is the only post-optimization shadow entry handoff.  It deliberately
    requires an explicitly migrated local schema instead of inventing a second
    untyped shadow ledger, and all identifiers derive from the selected plan
    and candidate so an interrupted or replayed run cannot duplicate effects.
    """
    selected_plan = next((plan for plan in result.plans if plan.operator_action == "approve"), None)
    if selected_plan is None:
        return []
    policy_by_id = {policy.playbook_id: policy for policy in policies}
    csa_store = CsaStore(sqlite_path)
    adapter = ShadowExecutionAdapter()
    handoffs: list[dict[str, Any]] = []
    for candidate in selected_plan.candidates:
        policy = policy_by_id.get(candidate.playbook_id)
        if policy is None:
            raise ValueError(f"selected candidate {candidate.candidate_id} has no unified policy")
        compiled = _shadow_csa_policy(policy)
        lifecycle_id = stable_csa_id("unified-lifecycle", [selected_plan.plan_id, candidate.candidate_id])
        lifecycle = freeze_lifecycle_policy(
            LifecycleState(
                lifecycle_id=lifecycle_id,
                opportunity_id=stable_csa_id("unified-opportunity", [policy.playbook_id, candidate.idea_id, candidate.underlying]),
                lane=compiled.lane,
                version=1,
                status="proposed",
                active_legs=(),
                cashflow_ledger=(),
                opened_at=_plan_time(result),
                updated_at=_plan_time(result),
                policy_hash=compiled.policy_hash,
                metadata={
                    "playbook_id": compiled.playbook_id,
                    "underlying": candidate.underlying,
                    "candidate_id": candidate.candidate_id,
                    "bpr": candidate.estimated_bpr,
                    "greeks": candidate.greeks.to_dict(),
                    "unified_plan_id": selected_plan.plan_id,
                    "execution_mode": "shadow",
                    "policy_snapshot_hash": compiled.policy_hash,
                    "policy_snapshot_date": _plan_time(result)[:10],
                    "source_identity": {"idea_id": candidate.idea_id, "plan_run_id": result.plan_run_id},
                },
            ),
            compiled_policy=compiled.to_dict(),
        )
        existing = csa_store.lifecycle(lifecycle_id)
        if existing is None:
            csa_store.save_lifecycle(lifecycle)
        elif existing.policy_hash != lifecycle.policy_hash:
            raise ValueError(f"replayed candidate policy differs for {candidate.candidate_id}")
        else:
            lifecycle = existing
            if lifecycle.status != "proposed":
                handoffs.append(
                    {
                        "source_id": candidate.idea_id,
                        "plan_id": selected_plan.plan_id,
                        "candidate_id": candidate.candidate_id,
                        "playbook_id": compiled.playbook_id,
                        "capability": policy.capability.key,
                        "mode": "shadow",
                        "lifecycle_id": lifecycle_id,
                        "ticket_id": "",
                        "adapter_state": "filled" if lifecycle.status == "open" else lifecycle.status,
                    }
                )
                continue
        proposal = propose_action(lifecycle, ActionType.OPEN, "unified_plan_selected", arbiter_class="routine_management", proposed_at=_plan_time(result))
        action = arbitrate_actions((proposal,)).selected
        csa_store.save_action(action)
        ticket = open_ticket_from_candidate(candidate, action, compiled, created_at=_plan_time(result), limit_price=candidate.net_credit)
        csa_store.save_shadow_order_intent(ticket)
        fill = adapter.simulate_fill(ticket, _candidate_quotes(candidate), _shadow_fill_policy(compiled), observed_at=_plan_time(result), attempt=0)
        csa_store.save_shadow_fill(fill)
        if fill.status == "filled" and lifecycle.status == "proposed":
            csa_store.save_lifecycle(adapter.adopt_fill(lifecycle, ticket, fill))
        handoffs.append(
            {
                "source_id": candidate.idea_id,
                "plan_id": selected_plan.plan_id,
                "candidate_id": candidate.candidate_id,
                "playbook_id": compiled.playbook_id,
                "capability": policy.capability.key,
                "mode": "shadow",
                "lifecycle_id": lifecycle_id,
                "ticket_id": ticket.ticket_id,
                "adapter_state": fill.status,
            }
        )
    return handoffs


def _shadow_csa_policy(policy: PlaybookPolicy, *, stage: CsaStage = CsaStage.SHADOW) -> CsaPolicy:
    capability = policy.capability.key
    if capability == "short_strangle":
        lane = LaneId.SHORT_STRANGLE
    elif capability == "earnings_calendar":
        lane = LaneId.EARNINGS_CALENDAR
    elif capability in {"call_diagonal", "put_diagonal", "narrative_ignition"}:
        lane = LaneId.DIRECTIONAL_DIAGONAL
    elif capability in {"call_spread", "call_vertical"}:
        lane = LaneId.CALL_VERTICAL
    else:
        lane = LaneId.GENERIC_CLOSE_ONLY
    try:
        source_mode = SourceMode(policy.source_mode)
    except ValueError as exc:
        raise ValueError(f"{policy.playbook_id}: unsupported source mode {policy.source_mode!r}") from exc
    return CsaPolicy(
        playbook_id=policy.playbook_id,
        lane=lane,
        stage=stage,
        source_mode=source_mode,
        management=dict(policy.management),
        resolved_fields=dict(policy.fields),
        policy_hash=policy.policy_hash,
        source="unified_policy_compiler",
        read_at=_plan_time_from_fields(policy.fields),
    )


def _shadow_fill_policy(policy: CsaPolicy) -> dict[str, Any]:
    lifecycle = policy.management.get("lifecycle") or {}
    fill = lifecycle.get("fill") if isinstance(lifecycle, dict) else None
    if not isinstance(fill, dict) or fill.get("max_attempts") in (None, "") or fill.get("price_increment") in (None, ""):
        raise ValueError(f"{policy.playbook_id}: shadow lifecycle requires management_policy_json.lifecycle.fill")
    return dict(fill)


def _candidate_quotes(candidate: Any) -> dict[str, dict[str, Any]]:
    return {
        occ_symbol(candidate.underlying, leg): {"bid": leg.bid, "ask": leg.ask, "fresh": True}
        for leg in candidate.legs
    }


def _gate_earnings_calendar_entries(
    candidates: list[Any],
    policies: tuple[PlaybookPolicy, ...],
    *,
    sqlite_path: str | Path,
    observed_at: str,
) -> None:
    """Make event-relative earnings entry a real optimizer eligibility gate."""
    by_playbook = {policy.playbook_id: policy for policy in policies}
    for candidate in candidates:
        policy = by_playbook.get(candidate.playbook_id)
        if policy is None or policy.capability.key != "earnings_calendar":
            continue
        snapshot = latest_earnings_snapshot(sqlite_path, candidate.underlying)
        if snapshot is None or not snapshot.confirmed or not snapshot.next_earnings_date:
            candidate.rejection_reason = "earnings_event_unconfirmed"
            continue
        try:
            event_date = date.fromisoformat(snapshot.next_earnings_date)
            entry_due = entry_session_due(event_date=event_date, time_of_day=snapshot.time_of_day, observed_at=observed_at)
            near_expiry = min(date.fromisoformat(leg.expiration) for leg in candidate.legs)
        except (TypeError, ValueError):
            candidate.rejection_reason = "earnings_event_timing_invalid"
            continue
        if not entry_due:
            candidate.rejection_reason = "earnings_entry_not_final_pre_event_session"
            continue
        if near_expiry <= event_date:
            candidate.rejection_reason = "earnings_near_expiry_not_after_event"
            continue
        candidate.reasons.extend(
            [
                f"earnings_event_date={event_date.isoformat()}",
                f"earnings_time_of_day={snapshot.time_of_day}",
                "earnings_entry_session=final_pre_event",
            ]
        )


def _planning_observed_at(config: dict[str, Any]) -> str:
    runtime = config.get("runtime") or {}
    value = runtime.get("observed_at")
    if value:
        return str(value)
    from kamandal_v2.domain.models import utc_now

    return utc_now()


def _plan_time(result: PlanRunResult) -> str:
    explicit = result.metrics.get("as_of") or result.metrics.get("observed_at")
    if explicit:
        return str(explicit)
    raw = result.plan_run_id.removeprefix("run_")
    return raw if "T" in raw else "1970-01-01T00:00:00Z"


def _plan_time_from_fields(fields: dict[str, Any]) -> str:
    return str(fields.get("policy_read_at") or "unified_entry")


def _source_groups(
    idea_inputs: list[Idea],
    universe: list[UniverseEntry],
    playbooks: list[Playbook],
    *,
    policies: tuple[PlaybookPolicy, ...],
    portfolio: PortfolioState,
) -> list[PlanningSourceGroup]:
    """Normalize every supported source mode before one candidate/plan pass."""
    policy_by_id = {policy.playbook_id: policy for policy in policies}
    groups: list[PlanningSourceGroup] = []
    for source_mode in ("idea", "market_scan", "portfolio_hedge"):
        selected = [playbook for playbook in playbooks if policy_by_id[playbook.playbook_id].source_mode == source_mode]
        if not selected:
            continue
        if source_mode == "idea":
            inputs = list(idea_inputs)
        elif source_mode == "market_scan":
            inputs = _market_scan_ideas(universe)
        else:
            inputs = _portfolio_hedge_ideas(selected, policy_by_id, portfolio)
        groups.append(PlanningSourceGroup(source_mode, inputs, selected))
    return groups


def _market_scan_ideas(universe: list[UniverseEntry]) -> list[Idea]:
    return [
        Idea.from_dict(
            {
                "idea_id": f"market_scan:{entry.symbol}",
                "source": "market_scan",
                "underlying": entry.symbol,
                "direction": "neutral",
                "horizon_days": 45,
                "operator_status": "approved",
            }
        )
        for entry in sorted(universe, key=lambda item: item.symbol)
        if entry.enabled
    ]


def _portfolio_hedge_ideas(
    playbooks: list[Playbook],
    policy_by_id: dict[str, PlaybookPolicy],
    portfolio: PortfolioState,
) -> list[Idea]:
    ideas: list[Idea] = []
    for playbook in playbooks:
        lifecycle = (policy_by_id[playbook.playbook_id].management.get("lifecycle") or {})
        trigger = lifecycle.get("portfolio_delta_trigger")
        underlyings = lifecycle.get("hedge_underlyings")
        if isinstance(trigger, bool) or trigger in (None, ""):
            continue
        if portfolio.greeks.delta <= float(trigger):
            continue
        if not isinstance(underlyings, list):
            continue
        direction = "bearish" if playbook.structure in {"put_spread", "put_diagonal", "long_put"} else "bullish"
        for raw_symbol in sorted(str(value).strip().upper() for value in underlyings if str(value).strip()):
            ideas.append(
                Idea.from_dict(
                    {
                        "idea_id": f"portfolio_hedge:{playbook.playbook_id}:{raw_symbol}",
                        "source": "portfolio_hedge",
                        "underlying": raw_symbol,
                        "direction": direction,
                        "strategy_hint": playbook.structure,
                        "horizon_days": 45,
                        "operator_status": "approved",
                    }
                )
            )
    return ideas
