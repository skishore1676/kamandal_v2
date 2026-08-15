"""End-to-end broker-inert CSA shadow scan orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from kamandal_v2.domain.models import Candidate, ChainSnapshot, Idea, PortfolioState, PreflightResult, utc_now
from kamandal_v2.live.orders import build_csa_live_ticket
from kamandal_v2.market.fixture import FixturePreflightClient
from kamandal_v2.market.public import occ_symbol
from kamandal_v2.market.tastytrade import TastytradeAdapter
from kamandal_v2.planner.candidate_builder import _apply_preflight_bpr
from kamandal_v2.planner.engine import (
    _candidate_contract_overlap,
    _market_provider,
    _open_live_contract_keys,
    _preflight_client,
    _shadow_portfolio_override,
)
from kamandal_v2.planner.shape_validators import validate_structure
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.action_arbiter import arbitrate_actions
from kamandal_v2.strategy_lanes.admission import AdmissionContext, evaluate_admission
from kamandal_v2.strategy_lanes.builders import build_lane_candidates
from kamandal_v2.strategy_lanes.daily_policy import load_daily_policy_snapshot, policy_tables_hash
from kamandal_v2.strategy_lanes.earnings_read import latest_earnings_snapshot
from kamandal_v2.strategy_lanes.lane_common import propose_action
from kamandal_v2.strategy_lanes.models import ActionType, CsaStage, LaneId, LifecycleState, SourceMode, StrategyOpportunity, stable_csa_id
from kamandal_v2.strategy_lanes.operator_policy import OperatorPolicyBundle, load_csa_operator_policy
from kamandal_v2.strategy_lanes.policy import CsaPolicy
from kamandal_v2.strategy_lanes.scoring import ScoreResult, score_opportunity
from kamandal_v2.strategy_lanes.shadow_execution import ShadowExecutionAdapter
from kamandal_v2.strategy_lanes.sources import idea_opportunity, market_scan_opportunities, portfolio_hedge_opportunities
from kamandal_v2.strategy_lanes.store import CsaStore
from kamandal_v2.strategy_lanes.tickets import open_ticket_from_candidate
from kamandal_v2.strategy_engine.lifecycle import freeze_lifecycle_policy


@dataclass(frozen=True, slots=True)
class ShadowScanResult:
    run_id: str
    started_at: str
    completed_at: str
    policy_count: int
    opportunity_count: int
    candidate_count: int
    admitted_count: int
    filled_count: int
    errors: tuple[str, ...]
    policy_hashes: tuple[str, ...]
    playbook_ids: tuple[str, ...]
    playbook_stages: dict[str, str]
    execution_mode: str
    live_intent_count: int
    policy_snapshot_date: str
    policy_snapshot_hash: str

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


def run_csa_shadow_scan(
    config: dict[str, Any],
    *,
    sqlite_path: str = "data/kamandal_v2.db",
    provider: str = "public",
    tables: dict[str, list[dict[str, Any]]] | None = None,
    ideas: Iterable[Idea] = (),
    market: Any | None = None,
    preflight: Any | None = None,
    observed_at: str | None = None,
) -> ShadowScanResult:
    return _run_csa_scan(
        config,
        sqlite_path=sqlite_path,
        provider=provider,
        tables=tables,
        ideas=ideas,
        market=market,
        preflight=preflight,
        observed_at=observed_at,
        stages=(CsaStage.SHADOW,),
        execution_mode="shadow",
    )


def run_csa_live_scan(
    config: dict[str, Any],
    *,
    sqlite_path: str = "data/kamandal_v2.db",
    provider: str = "public",
    tables: dict[str, list[dict[str, Any]]] | None = None,
    ideas: Iterable[Idea] = (),
    market: Any | None = None,
    preflight: Any | None = None,
    observed_at: str | None = None,
) -> ShadowScanResult:
    """Route Sheet-authorized pilot/live policies into the guarded live ledger."""
    return _run_csa_scan(
        config,
        sqlite_path=sqlite_path,
        provider=provider,
        tables=tables,
        ideas=ideas,
        market=market,
        preflight=preflight,
        observed_at=observed_at,
        stages=(CsaStage.PILOT_LIVE, CsaStage.LIVE),
        execution_mode="live",
    )


def _run_csa_scan(
    config: dict[str, Any],
    *,
    sqlite_path: str,
    provider: str,
    tables: dict[str, list[dict[str, Any]]] | None,
    ideas: Iterable[Idea],
    market: Any | None,
    preflight: Any | None,
    observed_at: str | None,
    stages: tuple[CsaStage, ...],
    execution_mode: str,
) -> ShadowScanResult:
    ideas = tuple(ideas)
    started_at = observed_at or utc_now()
    run_id = stable_csa_id("scan-run", [execution_mode, started_at, provider])
    csa_store = CsaStore(sqlite_path)
    local_store = LocalStore(sqlite_path, read_only=True)
    if tables is None:
        daily_policy = load_daily_policy_snapshot(config)
        bundle = daily_policy.policy
        policy_snapshot_date = daily_policy.trading_date
        policy_snapshot_hash = daily_policy.snapshot_hash
    else:
        bundle = load_csa_operator_policy(config, tables=tables, read_at=started_at)
        policy_snapshot_date = started_at[:10]
        policy_snapshot_hash = policy_tables_hash(
            {
                "universe": [dict(row) for row in (tables.get("universe") or [])],
                "playbooks": [dict(row) for row in (tables.get("playbooks") or [])],
            }
        )
    errors = list(bundle.errors)
    # The Sheet may contain baseline, shadow, pilot, and live rows at the same
    # time. This command owns only the shadow route; other stages are handled by
    # their own adapters and must not poison or duplicate a shadow run.
    selected_policies = tuple(policy for policy in bundle.policies if policy.stage in stages)
    working_orders = csa_store.working_shadow_orders() if execution_mode == "shadow" else []
    if not selected_policies:
        if working_orders:
            errors.append(
                f"{len(working_orders)} working shadow order(s) must resolve before the last shadow stage changes"
            )
        return _finish_scan(
            csa_store, run_id, started_at, selected_policies, (), 0, 0, 0, errors,
            execution_mode=execution_mode, live_intent_count=0,
            policy_snapshot_date=policy_snapshot_date,
            policy_snapshot_hash=policy_snapshot_hash,
        )

    if market is not None:
        resolved_market = market
    else:
        snapshotting_market = _market_provider(config, provider=provider, store=local_store)
        resolved_market = getattr(snapshotting_market, "inner", snapshotting_market)
    resolved_preflight = preflight or (_preflight_client(resolved_market) if provider == "public" else FixturePreflightClient())
    shadow_bpr_preflight = _shadow_bpr_preflight(config, provider=provider, execution_mode=execution_mode)
    snapshots: dict[str, ChainSnapshot] = {}
    observations: dict[str, dict[str, Any]] = {}

    symbols = _required_symbols(bundle, selected_policies, ideas)
    symbols.update(ticket.underlying for ticket, _attempt in working_orders)
    for symbol in sorted(symbols):
        try:
            snapshot = resolved_market.chain_snapshot(symbol)
            snapshots[symbol] = snapshot
            observations[symbol] = {
                "source_fresh": True,
                "underlying_price": snapshot.underlying_price,
                "iv_rank": resolved_market.iv_rank(symbol),
                "iv_percentile": resolved_market.iv_percentile(symbol),
                "iv_abs": resolved_market.iv_abs(symbol),
                "event_status": resolved_market.event_status(symbol),
                "chain_snapshot_id": snapshot.chain_snapshot_id,
            }
        except Exception as exc:  # noqa: BLE001
            observations[symbol] = {"source_fresh": False, "error": _safe_error(exc)}
            errors.append(f"{symbol}: market context unavailable: {_safe_error(exc)}")

    raw_portfolio = resolved_market.account_state()
    live_portfolio = local_store.live_portfolio_state(raw_portfolio)
    if execution_mode == "shadow":
        paper_portfolio = _shadow_portfolio_override(raw_portfolio, config, force=True)
        paper_portfolio = local_store.shadow_portfolio_state(paper_portfolio)
        portfolio = _reserve_open_csa_shadow_bpr(paper_portfolio, csa_store.open_lifecycles())
    else:
        portfolio = live_portfolio

    opportunities: list[StrategyOpportunity] = list(
        market_scan_opportunities(bundle.universe, selected_policies, observations, observed_at=started_at)
    )
    opportunities.extend(
        portfolio_hedge_opportunities(
            {**live_portfolio.to_dict(), "delta": live_portfolio.greeks.delta, "source_fresh": True},
            selected_policies,
            observations,
            observed_at=started_at,
        )
    )
    policy_by_id = {policy.playbook_id: policy for policy in selected_policies}
    for idea in ideas:
        for policy in selected_policies:
            if policy.source_mode is not SourceMode.IDEA:
                continue
            event_context = _event_context(sqlite_path, idea.underlying) if policy.lane is LaneId.EARNINGS_CALENDAR else {}
            opportunities.append(idea_opportunity(idea, policy, observed_at=started_at, event_context=event_context))

    candidate_count = 0
    admitted_count = 0
    filled_count = 0
    if execution_mode == "shadow":
        filled_count = _advance_working_orders(
            csa_store,
            working_orders,
            snapshots,
            active_policy_hashes={policy.policy_hash for policy in selected_policies},
            observed_at=started_at,
            errors=errors,
        )
    live_intent_count = 0
    open_lifecycles = csa_store.open_lifecycles()
    open_csa_keys = {
        (str(item.metadata.get("underlying") or ""), str(item.metadata.get("playbook_id") or ""))
        for item in open_lifecycles
        if execution_mode == "live"
        or str(item.metadata.get("execution_mode") or "shadow") == "shadow"
    }
    active_stage_intents = local_store.live_order_intents_by_status(
        {
            "stage_approved_pending_submit",
            "submitted",
            "repriced",
            "partially_filled",
            "replace_cancel_pending",
            "replace_waiting_cancel",
        }
    )
    if execution_mode == "live":
        open_csa_keys.update(
            (str(ticket.get("underlying") or ""), str(ticket.get("csa_playbook_id") or ""))
            for ticket in active_stage_intents
            if ticket.get("csa_policy_hash") and ticket.get("csa_playbook_id")
        )
    pilot_playbooks_used = {
        str(ticket.get("csa_playbook_id") or "")
        for ticket in local_store.live_order_intents_by_type("open")
        if ticket.get("csa_policy_hash")
        and str(ticket.get("csa_policy_snapshot_date") or "") == policy_snapshot_date
    }
    pilot_playbooks_used.update(
        str(item.metadata.get("playbook_id") or "")
        for item in csa_store.open_lifecycles()
        if str(item.metadata.get("execution_mode") or "") == "live"
    )
    open_live_contracts = _open_live_contract_keys(local_store) if execution_mode == "live" else set()
    for opportunity in opportunities:
        csa_store.save_opportunity(opportunity, scan_run_id=run_id)
        policy = policy_by_id[opportunity.playbook_id]
        snapshot = snapshots.get(opportunity.underlying)
        if snapshot is None:
            continue
        try:
            candidates = build_lane_candidates(opportunity, policy, snapshot)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{opportunity.opportunity_id}: candidate build failed: {_safe_error(exc)}")
            continue
        candidate_count += len(candidates)
        admitted: list[tuple[Candidate, ScoreResult, PreflightResult]] = []
        for candidate in candidates:
            preflight_result = _resolve_preflight(
                candidate,
                policy,
                resolved_preflight.preflight(candidate),
                shadow_bpr_preflight=shadow_bpr_preflight,
                execution_mode=execution_mode,
            )
            _apply_preflight_bpr(candidate, preflight_result)
            score = score_opportunity(policy, _score_components(candidate))
            context = _admission_context(
                candidate,
                policy,
                opportunity,
                portfolio,
                preflight_result,
                snapshot,
                execution_mode=execution_mode,
                ownership_clear=(opportunity.underlying, policy.playbook_id) not in open_csa_keys
                and not bool(_candidate_contract_overlap(candidate, open_live_contracts)),
            )
            decision = evaluate_admission(opportunity, policy, context, decided_at=started_at, score=score)
            csa_store.save_admission_decision(decision)
            if decision.admitted:
                admitted.append((candidate, score, preflight_result))
        if not admitted:
            continue
        admitted_count += 1
        candidate, score, preflight_result = sorted(admitted, key=lambda item: (-item[1].score, item[0].candidate_id))[0]
        if (
            execution_mode == "live"
            and policy.stage is CsaStage.PILOT_LIVE
            and policy.playbook_id in pilot_playbooks_used
        ):
            continue
        lifecycle = freeze_lifecycle_policy(LifecycleState(
            lifecycle_id=stable_csa_id("lifecycle", [opportunity.opportunity_id, candidate.candidate_id]),
            opportunity_id=opportunity.opportunity_id,
            lane=policy.lane,
            version=1,
            status="proposed" if execution_mode == "shadow" else "pending_live_submission",
            active_legs=(),
            cashflow_ledger=(),
            opened_at=started_at,
            updated_at=started_at,
            policy_hash=policy.policy_hash,
            metadata={
                "playbook_id": policy.playbook_id,
                "underlying": opportunity.underlying,
                "candidate_id": candidate.candidate_id,
                "score": score.score,
                "bpr": candidate.estimated_bpr,
                "bpr_source": (preflight_result.raw or {}).get("bpr_source") or "local_fallback",
                "policy": policy.to_dict(),
                "execution_mode": execution_mode,
                "policy_snapshot_date": policy_snapshot_date,
                "policy_snapshot_hash": policy_snapshot_hash,
                "event_context": dict(opportunity.event_context),
            },
        ), compiled_policy=policy.to_dict())
        csa_store.save_lifecycle(lifecycle)
        proposal = propose_action(lifecycle, ActionType.OPEN, "admitted", arbiter_class="routine_management", proposed_at=started_at)
        action = arbitrate_actions((proposal,)).selected
        csa_store.save_action(action)
        ticket = open_ticket_from_candidate(
            candidate,
            action,
            policy,
            created_at=started_at,
            limit_price=candidate.net_credit,
        )
        if execution_mode == "shadow":
            csa_store.save_shadow_order_intent(ticket)
            quotes = _ticket_quotes(ticket, snapshot)
            fill_policy = dict((policy.management.get("lifecycle") or {}).get("fill") or {})
            adapter = ShadowExecutionAdapter()
            # One market observation gets one fill attempt. A working order is
            # persisted and reconsidered on the next natural scan with fresh quotes.
            final_fill = adapter.simulate_fill(ticket, quotes, fill_policy, observed_at=started_at, attempt=0)
            csa_store.save_shadow_fill(final_fill)
            if final_fill.status == "filled":
                csa_store.save_lifecycle(adapter.adopt_fill(lifecycle, ticket, final_fill))
                open_csa_keys.add((opportunity.underlying, policy.playbook_id))
                portfolio = _reserve_candidate_bpr(portfolio, candidate)
                filled_count += 1
            elif final_fill.status == "missed":
                from dataclasses import replace

                csa_store.save_lifecycle(replace(lifecycle, status="entry_missed", updated_at=started_at))
            else:
                portfolio = _reserve_candidate_bpr(portfolio, candidate)
        else:
            candidate.preflight = preflight_result
            live_ticket = build_csa_live_ticket(ticket)
            live_ticket.update(
                {
                    "created_at": started_at,
                    "csa_policy_hash": policy.policy_hash,
                    "csa_playbook_id": policy.playbook_id,
                    "csa_stage": policy.stage.value,
                    "csa_lifecycle_id": lifecycle.lifecycle_id,
                    "csa_strategy_ticket": ticket.to_dict(),
                    "csa_policy_snapshot_date": policy_snapshot_date,
                    "csa_policy_snapshot_hash": policy_snapshot_hash,
                    "stage_authorized": True,
                    "pilot_contract_cap": 1 if policy.stage is CsaStage.PILOT_LIVE else None,
                }
            )
            # The live-approved-orders job owns submission and re-runs health,
            # freshness, preflight, concentration, and serialized order gates.
            LocalStore(sqlite_path).save_live_order_intent(
                live_ticket,
                status="stage_approved_pending_submit",
            )
            live_intent_count += 1
            if policy.stage is CsaStage.PILOT_LIVE:
                pilot_playbooks_used.add(policy.playbook_id)
            open_csa_keys.add((opportunity.underlying, policy.playbook_id))
    return _finish_scan(
        csa_store,
        run_id,
        started_at,
        selected_policies,
        opportunities,
        candidate_count,
        admitted_count,
        filled_count,
        errors,
        execution_mode=execution_mode,
        live_intent_count=live_intent_count,
        policy_snapshot_date=policy_snapshot_date,
        policy_snapshot_hash=policy_snapshot_hash,
    )


def _shadow_bpr_preflight(config: dict[str, Any], *, provider: str, execution_mode: str) -> Any | None:
    if execution_mode != "shadow" or provider != "public":
        return None
    adapter = TastytradeAdapter(config)
    return adapter if adapter.available() else None


def _resolve_preflight(
    candidate: Candidate,
    policy: CsaPolicy,
    primary: PreflightResult,
    *,
    shadow_bpr_preflight: Any | None,
    execution_mode: str,
) -> PreflightResult:
    if primary.ok or execution_mode != "shadow" or policy.lane is not LaneId.SHORT_STRANGLE:
        return primary
    raw = primary.raw if isinstance(primary.raw, dict) else {}
    public_error = raw.get("public_api_error") if isinstance(raw.get("public_api_error"), dict) else {}
    if int(public_error.get("code") or 0) != 159:
        return primary

    secondary = shadow_bpr_preflight.preflight(candidate) if shadow_bpr_preflight is not None else None
    bpr = float((secondary.bpr if secondary is not None else None) or candidate.estimated_bpr or 0.0)
    if bpr <= 0:
        return primary
    secondary_raw = secondary.raw if secondary is not None and isinstance(secondary.raw, dict) else {}
    return PreflightResult(
        ok=True,
        bpr=round(bpr, 2),
        message="shadow BPR estimated after Public Level 4 entitlement rejection",
        raw={
            "bpr_source": "broker_preflight" if secondary_raw.get("response") else "local_fallback",
            "bpr_broker": "tastytrade" if secondary_raw.get("response") else "local",
            "public_live_eligibility": "level_4_required",
            "public_error_code": 159,
            "secondary_preflight_ok": secondary.ok if secondary is not None else None,
            "shadow_only_warning": "public_short_strangle_level_4_required",
        },
    )

def _advance_working_orders(
    store: CsaStore,
    working_orders: list[tuple[Any, int]],
    snapshots: dict[str, ChainSnapshot],
    *,
    active_policy_hashes: set[str],
    observed_at: str,
    errors: list[str],
) -> int:
    filled_count = 0
    adapter = ShadowExecutionAdapter()
    for ticket, last_attempt in working_orders:
        if ticket.policy_hash not in active_policy_hashes:
            errors.append(
                f"{ticket.ticket_id}: working shadow order policy is no longer routed to shadow"
            )
            continue
        snapshot = snapshots.get(ticket.underlying)
        lifecycle = store.lifecycle(ticket.lifecycle_id)
        if snapshot is None or lifecycle is None:
            errors.append(f"{ticket.ticket_id}: working shadow order lacks fresh market or lifecycle state")
            continue
        if lifecycle.version != ticket.lifecycle_version or lifecycle.status != "proposed":
            errors.append(f"{ticket.ticket_id}: working shadow order no longer targets the proposed lifecycle")
            continue
        fill_policy = ticket.metadata.get("fill_policy") or {}
        try:
            fill = adapter.simulate_fill(
                ticket,
                _ticket_quotes(ticket, snapshot),
                fill_policy,
                observed_at=observed_at,
                attempt=last_attempt + 1,
            )
        except (TypeError, ValueError) as exc:
            errors.append(f"{ticket.ticket_id}: working shadow attempt failed: {_safe_error(exc)}")
            continue
        store.save_shadow_fill(fill)
        if fill.status == "filled":
            store.save_lifecycle(adapter.adopt_fill(lifecycle, ticket, fill))
            filled_count += 1
        elif fill.status == "missed":
            from dataclasses import replace

            store.save_lifecycle(replace(lifecycle, status="entry_missed", updated_at=observed_at))
    return filled_count


def _required_symbols(bundle: OperatorPolicyBundle, policies: tuple[CsaPolicy, ...], ideas: Iterable[Idea]) -> set[str]:
    symbols = {
        entry.symbol
        for entry in bundle.universe
        if entry.enabled and any(policy.source_mode is SourceMode.MARKET_SCAN for policy in policies)
    }
    symbols.update(idea.underlying for idea in ideas if idea.underlying)
    for policy in policies:
        if policy.source_mode is SourceMode.PORTFOLIO_HEDGE:
            lifecycle = policy.management.get("lifecycle") or {}
            symbols.update(str(item).upper() for item in (lifecycle.get("hedge_underlyings") or []) if str(item).strip())
    return symbols


def _admission_context(
    candidate: Candidate,
    policy: CsaPolicy,
    opportunity: StrategyOpportunity,
    portfolio: Any,
    preflight: PreflightResult,
    snapshot: ChainSnapshot,
    *,
    execution_mode: str,
    ownership_clear: bool,
) -> AdmissionContext:
    max_spread = max((abs(leg.ask - leg.bid) / max(leg.mid, 0.01) for leg in candidate.legs), default=1.0)
    min_oi = min((int(leg.open_interest or 0) for leg in candidate.legs), default=0)
    shape = validate_structure(candidate.structure, candidate.legs, snapshot.underlying_price)
    raw = preflight.raw if isinstance(preflight.raw, dict) else {}
    bpr_source = str(raw.get("bpr_source") or "local_fallback")
    bpr = float(candidate.estimated_bpr or 0.0)
    return AdmissionContext(
        market_data_fresh=bool(opportunity.market_context.get("source_fresh", True)),
        quote_valid=all(leg.bid >= 0 and leg.ask >= leg.bid for leg in candidate.legs),
        structure_valid=shape.valid,
        liquidity_valid=(
            max_spread <= float(policy.resolved_fields["max_bid_ask_pct"])
            and min_oi >= int(float(policy.resolved_fields["min_option_oi"]))
        ),
        bpr=bpr,
        bpr_source=bpr_source,
        broker_state_clear=bool(preflight.ok),
        portfolio_allowed=(
            True
            if execution_mode == "shadow"
            else bpr <= float(policy.resolved_fields["live_max_bpr_per_order"])
        ),
        buying_power_available=bpr <= float(portfolio.buying_power),
        ownership_clear=ownership_clear,
        working_order_conflict=False,
        event_state=str(opportunity.event_context.get("state") or "not_applicable"),
        evidence={"candidate_id": candidate.candidate_id, "shape_reason": shape.reason},
    )


def _reserve_open_csa_shadow_bpr(
    portfolio: PortfolioState,
    lifecycles: Iterable[LifecycleState],
) -> PortfolioState:
    reserved = [
        lifecycle
        for lifecycle in lifecycles
        if str(lifecycle.metadata.get("execution_mode") or "shadow") == "shadow"
    ]
    total = sum(max(float(item.metadata.get("bpr") or 0.0), 0.0) for item in reserved)
    per_underlying = dict(portfolio.per_underlying_bpr)
    for lifecycle in reserved:
        underlying = str(lifecycle.metadata.get("underlying") or "")
        if underlying:
            per_underlying[underlying] = round(
                per_underlying.get(underlying, 0.0) + max(float(lifecycle.metadata.get("bpr") or 0.0), 0.0),
                2,
            )
    return PortfolioState(
        account_size=portfolio.account_size,
        buying_power=round(max(portfolio.buying_power - total, 0.0), 2),
        bpr_used=round(portfolio.bpr_used + total, 2),
        positions_count=portfolio.positions_count + len(reserved),
        greeks=portfolio.greeks,
        per_underlying_bpr=per_underlying,
    )


def _reserve_candidate_bpr(portfolio: PortfolioState, candidate: Candidate) -> PortfolioState:
    bpr = max(float(candidate.estimated_bpr or 0.0), 0.0)
    per_underlying = dict(portfolio.per_underlying_bpr)
    per_underlying[candidate.underlying] = round(per_underlying.get(candidate.underlying, 0.0) + bpr, 2)
    return PortfolioState(
        account_size=portfolio.account_size,
        buying_power=round(max(portfolio.buying_power - bpr, 0.0), 2),
        bpr_used=round(portfolio.bpr_used + bpr, 2),
        positions_count=portfolio.positions_count + 1,
        greeks=portfolio.greeks,
        per_underlying_bpr=per_underlying,
    )


def _score_components(candidate: Candidate) -> dict[str, float]:
    bpr = max(float(candidate.estimated_bpr or 0.0), 0.01)
    credit = max(float(candidate.net_credit), 0.0) * 100.0
    short_deltas = [abs(float(leg.delta)) for leg in candidate.legs if leg.side == "sell"]
    pop = 100.0 - (max(short_deltas) * 100.0 if short_deltas else 100.0)
    max_spread = max((abs(leg.ask - leg.bid) / max(leg.mid, 0.01) for leg in candidate.legs), default=1.0)
    return {
        "credit": _bounded((credit / bpr) * 100.0),
        "pop": _bounded(pop),
        "liquidity": _bounded(candidate.liquidity_score * 100.0),
        "spread": _bounded(100.0 - max_spread * 100.0),
    }


def _ticket_quotes(ticket: Any, snapshot: ChainSnapshot) -> dict[str, dict[str, Any]]:
    by_instrument = {occ_symbol(snapshot.underlying, _quote_as_leg(quote)): quote for quote in snapshot.quotes}
    return {
        leg.instrument_id: {
            "bid": by_instrument[leg.instrument_id].bid,
            "ask": by_instrument[leg.instrument_id].ask,
            "fresh": True,
        }
        for leg in ticket.legs
        if leg.instrument_id in by_instrument
    }


def _quote_as_leg(quote: Any) -> Any:
    from kamandal_v2.domain.models import OptionLeg

    return OptionLeg.from_quote(quote, role="quote", side="buy")


def _event_context(sqlite_path: str, symbol: str) -> dict[str, Any]:
    snapshot = latest_earnings_snapshot(sqlite_path, symbol)
    if snapshot is None or not snapshot.next_earnings_date:
        return {"state": "unknown"}
    return {
        "state": "confirmed" if snapshot.confirmed else "known",
        "event_date": snapshot.next_earnings_date,
        "time_of_day": snapshot.time_of_day,
        "source": snapshot.source,
    }


def _finish_scan(
    store: CsaStore,
    run_id: str,
    started_at: str,
    policies: tuple[CsaPolicy, ...],
    opportunities: Iterable[StrategyOpportunity],
    candidate_count: int,
    admitted_count: int,
    filled_count: int,
    errors: list[str],
    *,
    execution_mode: str,
    live_intent_count: int,
    policy_snapshot_date: str,
    policy_snapshot_hash: str,
) -> ShadowScanResult:
    completed_at = utc_now()
    opportunity_count = len(tuple(opportunities))
    result = ShadowScanResult(
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        policy_count=len(policies),
        opportunity_count=opportunity_count,
        candidate_count=candidate_count,
        admitted_count=admitted_count,
        filled_count=filled_count,
        errors=tuple(errors),
        policy_hashes=tuple(sorted(policy.policy_hash for policy in policies)),
        playbook_ids=tuple(sorted(policy.playbook_id for policy in policies)),
        playbook_stages={policy.playbook_id: policy.stage.value for policy in sorted(policies, key=lambda item: item.playbook_id)},
        execution_mode=execution_mode,
        live_intent_count=live_intent_count,
        policy_snapshot_date=policy_snapshot_date,
        policy_snapshot_hash=policy_snapshot_hash,
    )
    payload = {"id": run_id, "lane": "all", "status": "completed" if result.ok else "completed_with_errors", "policy_hash": "multiple", **result.to_dict()}
    store.save_scan_run(payload)
    command = "csa-shadow-scan" if execution_mode == "shadow" else "csa-live-scan"
    store.save_run_receipt({"id": run_id, "command": command, "status": payload["status"], "started_at": started_at, "completed_at": completed_at, "result": result.to_dict()})
    return result


def _bounded(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]
