"""End-to-end broker-inert CSA shadow scan orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from kamandal_v2.domain.models import Candidate, ChainSnapshot, Idea, PreflightResult, utc_now
from kamandal_v2.market.fixture import FixturePreflightClient
from kamandal_v2.market.public import occ_symbol
from kamandal_v2.planner.candidate_builder import _apply_preflight_bpr
from kamandal_v2.planner.engine import _candidate_contract_overlap, _market_provider, _open_live_contract_keys, _preflight_client
from kamandal_v2.planner.shape_validators import validate_structure
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.action_arbiter import arbitrate_actions
from kamandal_v2.strategy_lanes.admission import AdmissionContext, evaluate_admission
from kamandal_v2.strategy_lanes.builders import build_lane_candidates
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
    ideas = tuple(ideas)
    started_at = observed_at or utc_now()
    run_id = stable_csa_id("scan-run", [started_at, provider])
    csa_store = CsaStore(sqlite_path)
    local_store = LocalStore(sqlite_path, read_only=True)
    bundle = load_csa_operator_policy(config, tables=tables, read_at=started_at)
    errors = list(bundle.errors)
    shadow_policies = tuple(policy for policy in bundle.policies if policy.stage is CsaStage.SHADOW)
    for policy in bundle.policies:
        if policy.stage is not CsaStage.SHADOW:
            errors.append(f"{policy.playbook_id}: CSA-1 runtime accepts shadow stage only")
    if not shadow_policies:
        return _finish_scan(csa_store, run_id, started_at, shadow_policies, (), 0, 0, 0, errors)

    if market is not None:
        resolved_market = market
    else:
        snapshotting_market = _market_provider(config, provider=provider, store=local_store)
        resolved_market = getattr(snapshotting_market, "inner", snapshotting_market)
    resolved_preflight = preflight or (_preflight_client(resolved_market) if provider == "public" else FixturePreflightClient())
    snapshots: dict[str, ChainSnapshot] = {}
    observations: dict[str, dict[str, Any]] = {}

    symbols = _required_symbols(bundle, shadow_policies, ideas)
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

    opportunities: list[StrategyOpportunity] = list(
        market_scan_opportunities(bundle.universe, shadow_policies, observations, observed_at=started_at)
    )
    portfolio = local_store.live_portfolio_state(resolved_market.account_state())
    opportunities.extend(
        portfolio_hedge_opportunities(
            {**portfolio.to_dict(), "delta": portfolio.greeks.delta, "source_fresh": True},
            shadow_policies,
            observations,
            observed_at=started_at,
        )
    )
    policy_by_id = {policy.playbook_id: policy for policy in shadow_policies}
    for idea in ideas:
        for policy in shadow_policies:
            if policy.source_mode is not SourceMode.IDEA:
                continue
            event_context = _event_context(sqlite_path, idea.underlying) if policy.lane is LaneId.EARNINGS_CALENDAR else {}
            opportunities.append(idea_opportunity(idea, policy, observed_at=started_at, event_context=event_context))

    candidate_count = 0
    admitted_count = 0
    filled_count = 0
    open_csa_keys = {
        (str(item.metadata.get("underlying") or ""), str(item.metadata.get("playbook_id") or ""))
        for item in csa_store.open_lifecycles()
    }
    open_live_contracts = _open_live_contract_keys(local_store)
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
            preflight_result = resolved_preflight.preflight(candidate)
            _apply_preflight_bpr(candidate, preflight_result)
            score = score_opportunity(policy, _score_components(candidate))
            context = _admission_context(
                candidate,
                policy,
                opportunity,
                portfolio,
                preflight_result,
                snapshot,
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
        lifecycle = LifecycleState(
            lifecycle_id=stable_csa_id("lifecycle", [opportunity.opportunity_id, candidate.candidate_id]),
            opportunity_id=opportunity.opportunity_id,
            lane=policy.lane,
            version=1,
            status="proposed",
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
            },
        )
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
        csa_store.save_shadow_order_intent(ticket)
        quotes = _ticket_quotes(ticket, snapshot)
        fill_policy = dict((policy.management.get("lifecycle") or {}).get("fill") or {})
        adapter = ShadowExecutionAdapter()
        max_attempts = int(float(fill_policy["max_attempts"]))
        final_fill = None
        for attempt in range(max_attempts + 1):
            final_fill = adapter.simulate_fill(ticket, quotes, fill_policy, observed_at=started_at, attempt=attempt)
            if final_fill.status in {"filled", "missed"}:
                break
        if final_fill is None:
            continue
        csa_store.save_shadow_fill(final_fill)
        if final_fill.status == "filled":
            csa_store.save_lifecycle(adapter.adopt_fill(lifecycle, ticket, final_fill))
            open_csa_keys.add((opportunity.underlying, policy.playbook_id))
            filled_count += 1
        elif final_fill.status == "missed":
            from dataclasses import replace

            csa_store.save_lifecycle(replace(lifecycle, status="entry_missed", updated_at=started_at))
    return _finish_scan(
        csa_store,
        run_id,
        started_at,
        shadow_policies,
        opportunities,
        candidate_count,
        admitted_count,
        filled_count,
        errors,
    )


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
        portfolio_allowed=bpr <= float(policy.resolved_fields["live_max_bpr_per_order"]),
        buying_power_available=bpr <= float(portfolio.buying_power),
        ownership_clear=ownership_clear,
        working_order_conflict=False,
        event_state=str(opportunity.event_context.get("state") or "not_applicable"),
        evidence={"candidate_id": candidate.candidate_id, "shape_reason": shape.reason},
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
    return {"state": "confirmed" if snapshot.confirmed else "known", "event_date": snapshot.next_earnings_date, "source": snapshot.source}


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
    )
    payload = {"id": run_id, "lane": "all", "status": "completed" if result.ok else "completed_with_errors", "policy_hash": "multiple", **result.to_dict()}
    store.save_scan_run(payload)
    store.save_run_receipt({"id": run_id, "command": "csa-shadow-scan", "status": payload["status"], "started_at": started_at, "completed_at": completed_at, "result": result.to_dict()})
    return result


def _bounded(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]
