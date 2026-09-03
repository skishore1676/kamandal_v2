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
import json
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import ChainSnapshot, Idea, OptionLeg, Playbook, PortfolioState, UniverseEntry
from kamandal_v2.intelligence.observed_packages import ObservedPackageBatch, ObservedPackageEvidence
from kamandal_v2.intelligence.trade_sources import (
    TradeSourceMode,
    TradeSourceOutputKind,
    TradeSourcePolicy,
    compile_trade_source_policies,
    source_id_from_idea_source,
)
from kamandal_v2.planner.engine import PlanRunResult, PlanningSourceGroup, _market_provider, _preflight_client, run_plan
from kamandal_v2.planner.observed_package_candidates import (
    build_observed_package_candidates,
    persist_observed_package_batches,
    record_observed_packages_not_authorized,
)
from kamandal_v2.market.venue_router import VenueAwareMarket
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
from kamandal_v2.live.pricing import shadow_entry_limit_price
from kamandal_v2.strategy_engine.event_timing import entry_session_due
from kamandal_v2.strategy_lanes.earnings_read import latest_earnings_snapshot


LIVE_ENTRY_BINDABLE_STATUSES = {
    "pending_approval",
    "stage_approved_pending_submit",
    "waiting_entry_window",
}


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
    include_shadow: bool = True,
    observed_package_batches: tuple[ObservedPackageBatch, ...] = (),
    trade_source_rows: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
) -> UnifiedPlanningResult:
    """Build independent books from one normalized Sheet snapshot.

    A policy error is attached to both books rather than allowing either book to
    masquerade as complete.  Once policy compilation succeeds, failures stay
    per-book: a shadow failure cannot erase a valid live result and vice versa.
    """
    effective_trade_source_rows = trade_source_rows
    if (
        effective_trade_source_rows is None
        and daily_policy_snapshot is not None
        and "trade_sources" in daily_policy_snapshot.tables
    ):
        effective_trade_source_rows = daily_policy_snapshot.tables["trade_sources"]
    compilation = compile_playbook_policies(playbook_rows)
    required_sources = (
        tuple(
            str(profile.get("profile_id") or "")
            for profile in (((config.get("source_intelligence") or {}).get("correspondents") or {}).get("profiles") or [])
            if isinstance(profile, dict) and profile.get("enabled") is True
        )
        if effective_trade_source_rows is not None
        else ()
    )
    source_compilation = compile_trade_source_policies(
        effective_trade_source_rows or (),
        required_source_ids=required_sources,
    )
    source_policies = source_compilation.by_key() if effective_trade_source_rows is not None else None
    if source_compilation.errors:
        compilation = PolicyCompilation(
            policies=compilation.policies,
            errors=tuple((*compilation.errors, *source_compilation.errors)),
        )
    supplied_tables = {
        "universe": [dict(row) for row in universe_rows],
        "playbooks": [dict(row) for row in playbook_rows],
    }
    # Preserve read compatibility with a snapshot frozen before the atomic
    # trade-source migration.  New snapshots always carry trade_sources.
    if effective_trade_source_rows is not None:
        supplied_tables["trade_sources"] = [dict(row) for row in effective_trade_source_rows]
    supplied_policy_hash = policy_tables_hash(supplied_tables)
    active_store = store or LocalStore()
    observed_packages: tuple[ObservedPackageEvidence, ...] = ()
    if observed_package_batches:
        observed_packages = persist_observed_package_batches(observed_package_batches, store=active_store)
    retire_orphaned_pending_live_lifecycles(active_store)
    universe = [UniverseEntry.from_row(row) for row in universe_rows if row.get("symbol")]
    if not compilation.ok:
        if observed_packages:
            record_observed_packages_not_authorized(
                observed_packages,
                store=active_store,
                blocker="policy_compilation_failed",
            )
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
        trade_source_policies=source_policies,
    )
    if include_shadow:
        if observed_packages and not any(
            "exact_package" in policy.accepted_inputs for policy in compilation.policies
        ):
            record_observed_packages_not_authorized(
                observed_packages,
                store=active_store,
                blocker="unsupported",
            )
        shadow = _run_book(
            ExecutionMode.SHADOW,
            compilation.policies,
            universe,
            config,
            idea_paths,
            provider,
            active_store,
            audit_root,
            write_sheet,
            observed_packages=observed_packages,
            trade_source_policies=source_policies,
        )
    else:
        shadow = PlanningBook(
            ExecutionMode.SHADOW,
            tuple(policy.playbook_id for policy in compilation.policies if policy.mode is ExecutionMode.SHADOW),
            None,
            (),
        )
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
    observed_packages: tuple[ObservedPackageEvidence, ...] = (),
    trade_source_policies: dict[tuple[str, TradeSourceOutputKind], TradeSourcePolicy] | None = None,
) -> PlanningBook:
    shadow_input_kinds = {
        output_kind.value
        for (_source_id, output_kind), source_policy in (trade_source_policies or {}).items()
        if source_policy.mode is TradeSourceMode.SHADOW
    }
    selected = tuple(
        policy
        for policy in policies
        if policy.mode is mode
        or (
            mode is ExecutionMode.SHADOW
            and policy.mode is ExecutionMode.LIVE
            and bool(shadow_input_kinds.intersection(policy.accepted_inputs))
        )
    )
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
    if provider == "public":
        base_market = market_override or _market_provider(mode_config, provider=provider, store=store)
        venues = {str(policy.fields.get("execution_venue") or "public_primary") for policy in selected}
        market_override = VenueAwareMarket(
            base_market,
            _preflight_client(base_market),
            mode_config,
            mode=mode.value,
            venues=venues,
            provider=provider,
        )
    elif mode is ExecutionMode.SHADOW and observed_packages and market_override is None:
        # Exact packages need current quotes even when there are no thesis
        # ideas.  This remains the configured read-only market provider; no
        # broker preflight or account authorization is introduced here.
        market_override = _market_provider(mode_config, provider=provider, store=store)
    playbooks = [Playbook.from_row(policy.fields) for policy in selected]
    def candidate_postprocessor(candidates: list[Any], current_store: LocalStore, current_config: dict[str, Any], portfolio: PortfolioState) -> None:
        _gate_earnings_calendar_entries(candidates, selected, sqlite_path=current_store.sqlite_path, observed_at=_planning_observed_at(current_config))
        if live_renderer is not None:
            _gate_reserved_pilot_live_candidates(candidates, selected, CsaStore(current_store.sqlite_path, read_only=True))
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
                mode=mode,
                trade_source_policies=trade_source_policies,
            ),
            supplemental_candidate_factory=(
                lambda market, selected_playbooks, _portfolio, current_store: build_observed_package_candidates(
                    observed_packages,
                    policies=selected,
                    playbooks=selected_playbooks,
                    market=market,
                    store=current_store,
                    config=mode_config,
                    trade_source_policies=trade_source_policies,
                )
            )
            if mode is ExecutionMode.SHADOW and observed_packages
            else None,
            market_override=market_override,
            portfolio_override=_configured_shadow_portfolio(mode_config) if mode is ExecutionMode.SHADOW else None,
        )
    except Exception as exc:  # noqa: BLE001 - report failure-isolated book receipt.
        return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), None, (f"{type(exc).__name__}: {exc}",))
    _record_trade_source_plan_dispositions(result, store=store, mode=mode)
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
            handoffs = _materialize_shadow_handoff(result, selected, config=mode_config, sqlite_path=store.sqlite_path)
        except Exception as exc:  # noqa: BLE001 - a missing typed handoff is an unhealthy book.
            return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), result, (f"shadow handoff failed: {type(exc).__name__}: {exc}",))
        store.event("unified_shadow_plan_auto_approved", {"plan_run_id": result.plan_run_id, "plan_id": result.plans[0].plan_id, "handoffs": handoffs})
        return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), result, (), tuple(handoffs))
    return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), result, ())


def _record_trade_source_plan_dispositions(
    result: PlanRunResult,
    *,
    store: LocalStore,
    mode: ExecutionMode,
) -> None:
    rank_one_ids = {
        candidate.candidate_id
        for candidate in (result.plans[0].candidates if result.plans else [])
    }
    for idea in result.ideas:
        source_id = source_id_from_idea_source(idea.source)
        if source_id is None:
            continue
        candidates = [candidate for candidate in result.candidates if candidate.idea_id == idea.idea_id]
        selected = [candidate for candidate in candidates if candidate.candidate_id in rank_one_ids]
        eligible = [candidate for candidate in candidates if candidate.eligible]
        if selected:
            status = "selected_rank_1"
            reason = ""
        elif eligible:
            status = "eligible_not_selected"
            reason = "portfolio_optimizer"
        elif candidates:
            status = "candidate_rejected"
            reason = candidates[0].rejection_reason
        else:
            status = "no_candidate"
            diagnostic = next(
                (item for item in result.idea_diagnostics if str(item.get("idea_id") or "") == idea.idea_id),
                {},
            )
            reason = str(diagnostic.get("status") or "no_compatible_candidate")
        store.event(
            "trade_source_planner_disposition",
            {
                "source_id": source_id,
                "idea_id": idea.idea_id,
                "plan_run_id": result.plan_run_id,
                "status": status,
                "reason": reason,
                "mode": mode.value,
                "broker_effects": False,
            },
        )


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
    _assert_pilot_live_reservation_available(selected_plan, policy_by_id, csa_store)
    handoffs: list[dict[str, Any]] = []
    for candidate in selected_plan.candidates:
        policy = policy_by_id.get(candidate.playbook_id)
        if policy is None:
            raise ValueError(f"selected candidate {candidate.candidate_id} has no unified policy")
        if daily_policy_snapshot is None:  # Defended above, retained for direct callers.
            raise ValueError("live selected-entry handoff requires a daily policy snapshot")
        pilot_live = _is_pilot_live(policy)
        compiled = _shadow_csa_policy(
            policy,
            stage=CsaStage.PILOT_LIVE if pilot_live else CsaStage.LIVE,
        )
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
                    "execution_venue": candidate.execution_venue,
                    # This is the daily Sheet snapshot identity, not the
                    # per-playbook compiled-policy identity below.
                    "policy_snapshot_hash": daily_policy_snapshot.snapshot_hash,
                    "policy_snapshot_date": daily_policy_snapshot.trading_date,
                    "entry_policy_hash": compiled.policy_hash,
                    "pilot_live": pilot_live,
                    "pilot_policy_hash": policy.policy_hash if pilot_live else None,
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
            for ticket in store.live_order_intents_by_type("open", statuses=LIVE_ENTRY_BINDABLE_STATUSES)
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
                "execution_venue": candidate.execution_venue,
                "csa_compiled_policy_hash": compiled.policy_hash,
                "stage_authorized": True,
                "pilot_contract_cap": 1 if pilot_live else None,
            }
        )
        store.save_live_order_intent(ticket, status=str(ticket["_ledger_status"]))
        handoffs.append(
            {"source_id": candidate.idea_id, "plan_id": selected_plan.plan_id, "candidate_id": candidate.candidate_id, "playbook_id": compiled.playbook_id, "capability": policy.capability.key, "mode": "pilot_live" if pilot_live else "live", "lifecycle_id": lifecycle_id, "ticket_id": strategy_ticket.ticket_id, "adapter_state": str(ticket["_ledger_status"])}
        )
    return handoffs


def _is_pilot_live(policy: PlaybookPolicy) -> bool:
    """Preserve the bounded canary envelope while the unified engine has two modes."""

    return str(policy.fields.get("csa_stage") or "").strip().lower() == CsaStage.PILOT_LIVE.value


def _assert_pilot_live_reservation_available(
    selected_plan: Any,
    policy_by_id: dict[str, PlaybookPolicy],
    csa_store: CsaStore,
) -> None:
    """Permit one lifecycle for a pilot policy version, including idempotent replay.

    The reservation is deliberately stronger than a per-day limit.  Once a
    pilot policy creates its canary lifecycle, another lifecycle requires the
    row to leave ``pilot_live`` (or an explicitly revised policy version).
    """

    intended: dict[tuple[str, str], set[str]] = {}
    for candidate in selected_plan.candidates:
        policy = policy_by_id.get(candidate.playbook_id)
        if policy is None or not _is_pilot_live(policy):
            continue
        key = (policy.playbook_id, policy.policy_hash)
        intended.setdefault(key, set()).add(
            stable_csa_id("unified-live-lifecycle", [selected_plan.plan_id, candidate.candidate_id])
        )
    for (playbook_id, policy_hash), lifecycle_ids in intended.items():
        if len(lifecycle_ids) != 1:
            raise ValueError(f"pilot_live {playbook_id} selected more than one canary lifecycle")

        reservations = _pilot_live_reservations(csa_store, playbook_id=playbook_id, policy_hash=policy_hash)
        if reservations and reservations != lifecycle_ids:
            raise ValueError(f"pilot_live {playbook_id} already reserved its one canary lifecycle")


def _gate_reserved_pilot_live_candidates(
    candidates: list[Any],
    policies: tuple[PlaybookPolicy, ...],
    csa_store: CsaStore,
) -> None:
    by_playbook = {policy.playbook_id: policy for policy in policies if _is_pilot_live(policy)}
    for candidate in candidates:
        policy = by_playbook.get(candidate.playbook_id)
        if policy is None or candidate.rejection_reason:
            continue
        if _pilot_live_reservations(csa_store, playbook_id=policy.playbook_id, policy_hash=policy.policy_hash):
            candidate.rejection_reason = "pilot_live_canary_already_reserved"


def _pilot_live_reservations(
    csa_store: CsaStore,
    *,
    playbook_id: str,
    policy_hash: str,
) -> set[str]:
    reservations: set[str] = set()
    for row in csa_store.rows("csa_lifecycles"):
        payload = json.loads(str(row.get("payload") or "{}"))
        metadata = payload.get("metadata") or {}
        if (
            str(metadata.get("playbook_id") or "") == playbook_id
            and metadata.get("pilot_live") is True
            and str(metadata.get("pilot_policy_hash") or "") == policy_hash
        ):
            reservations.add(str(payload.get("lifecycle_id") or row.get("id") or ""))
    return reservations


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
        for ticket in store.live_order_intents_by_type("open", statuses=LIVE_ENTRY_BINDABLE_STATUSES)
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
        include_shadow=False,
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
    config: dict[str, Any],
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
                    "execution_venue": candidate.execution_venue,
                    "policy_snapshot_hash": compiled.policy_hash,
                    "policy_snapshot_date": _plan_time(result)[:10],
                    "source_identity": {
                        "idea_id": candidate.idea_id,
                        "plan_run_id": result.plan_run_id,
                        **{
                            key: candidate.metadata[key]
                            for key in (
                                "source_mode",
                                "source_profile",
                                "source_event_id",
                                "canonical_post_id",
                                "package_signature",
                                "evidence_revision_id",
                            )
                            if key in candidate.metadata
                        },
                    },
                    **{
                        key: candidate.metadata[key]
                        for key in (
                            "observational_entry_mark",
                            "observational_mark_kind",
                            "chain_snapshot_id",
                            "chain_captured_at",
                            "displayed_price",
                            "displayed_trade_time",
                            "broker_effects",
                        )
                        if key in candidate.metadata
                    },
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
        fill_policy = _shadow_fill_policy(compiled)
        max_concession = float(fill_policy["max_attempts"]) * float(fill_policy["price_increment"])
        ticket = open_ticket_from_candidate(
            candidate,
            action,
            compiled,
            created_at=_plan_time(result),
            limit_price=shadow_entry_limit_price(candidate, config, max_concession=max_concession),
        )
        csa_store.save_shadow_order_intent(ticket)
        fill = adapter.simulate_fill(ticket, _candidate_quotes(candidate), fill_policy, observed_at=_plan_time(result), attempt=0)
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
    resolved_fields = dict(policy.fields)
    management = dict(policy.management)
    if policy.strangle_management is not None:
        resolved_fields.setdefault("loss_close_multiple", policy.strangle_management.loss_close_multiple)
        resolved_fields.setdefault("management_delta_target", policy.strangle_management.target_delta)
        resolved_fields.setdefault("management_delta_max", policy.strangle_management.max_delta)
        resolved_fields.setdefault("tested_side_confirmations", policy.strangle_management.tested_side_confirmations)
        resolved_fields.setdefault("rearm_inside_confirmations", policy.strangle_management.rearm_inside_confirmations)
        resolved_fields.setdefault("filled_side_adjustment_limit", policy.strangle_management.filled_side_adjustment_limit)
    return CsaPolicy(
        playbook_id=policy.playbook_id,
        lane=lane,
        stage=stage,
        source_mode=source_mode,
        management=management,
        resolved_fields=resolved_fields,
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


def _configured_shadow_portfolio(config: dict[str, Any]) -> PortfolioState:
    """Return the Sheet-independent synthetic book without reading a broker account."""

    shadow = config.get("shadow") or {}
    required = ("account_size_override", "buying_power_override", "bpr_used_override")
    missing = [name for name in required if shadow.get(name) in (None, "")]
    if missing:
        raise ValueError("shadow portfolio requires configured override(s): " + ", ".join(missing))
    return PortfolioState(
        account_size=float(shadow["account_size_override"]),
        buying_power=float(shadow["buying_power_override"]),
        bpr_used=float(shadow["bpr_used_override"]),
        positions_count=0,
    )


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
    mode: ExecutionMode,
    trade_source_policies: dict[tuple[str, TradeSourceOutputKind], TradeSourcePolicy] | None,
) -> list[PlanningSourceGroup]:
    """Normalize every supported source mode before one candidate/plan pass."""
    policy_by_id = {policy.playbook_id: policy for policy in policies}
    groups: list[PlanningSourceGroup] = []
    idea_groups: dict[tuple[str, ...], list[Idea]] = {}
    for idea in idea_inputs:
        source_id = source_id_from_idea_source(idea.source)
        source_policy = (
            (trade_source_policies or {}).get((source_id, TradeSourceOutputKind.IDEA))
            if source_id is not None
            else None
        )
        if source_id is not None and trade_source_policies is not None and (
            source_policy is None or not source_policy.planner_enabled
        ):
            continue
        eligible_ids: list[str] = []
        for playbook in playbooks:
            policy = policy_by_id[playbook.playbook_id]
            if "idea" not in policy.accepted_inputs:
                continue
            effective_mode = policy.mode
            if source_policy is not None and source_policy.mode is TradeSourceMode.SHADOW:
                effective_mode = ExecutionMode.SHADOW
            if effective_mode is mode:
                eligible_ids.append(playbook.playbook_id)
        if eligible_ids:
            idea_groups.setdefault(tuple(sorted(eligible_ids)), []).append(idea)
    playbook_by_id = {playbook.playbook_id: playbook for playbook in playbooks}
    for playbook_ids, inputs in idea_groups.items():
        groups.append(PlanningSourceGroup("idea", inputs, [playbook_by_id[item] for item in playbook_ids]))

    for source_mode in ("market_scan", "portfolio_hedge"):
        selected = [
            playbook
            for playbook in playbooks
            if source_mode in policy_by_id[playbook.playbook_id].accepted_inputs
            and policy_by_id[playbook.playbook_id].mode is mode
        ]
        if not selected:
            continue
        if source_mode == "market_scan":
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
