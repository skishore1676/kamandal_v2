"""Planner orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import Candidate, ChainSnapshot, Idea, Plan, PortfolioState, utc_now
from kamandal_v2.events.earnings import EarningsOverlayMarket, EarningsStore
from kamandal_v2.market.fixture import FixtureMarketDataProvider, FixturePreflightClient
from kamandal_v2.market.interfaces import MarketDataProvider
from kamandal_v2.market.public import PublicAdapter
from kamandal_v2.planner.candidate_builder import build_candidates, diagnose_idea_matches
from kamandal_v2.planner.config_loader import load_planner_config
from kamandal_v2.planner.daily_plan import render_daily_plan_rows
from kamandal_v2.planner.idea_loader import load_ideas
from kamandal_v2.planner.plan_generator import generate_plans
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.sheets import write_daily_plan
from kamandal_v2.stores.audit import AuditWriter
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.volatility.iv import IvOverlayMarket
from kamandal_v2.volatility.iv_store import IvStore


@dataclass(slots=True)
class PlanRunResult:
    plan_run_id: str
    ideas: list[Idea]
    candidates: list[Candidate]
    plans: list[Plan]
    daily_plan_rows: list[list[Any]]
    metrics: dict[str, Any]
    idea_diagnostics: list[dict[str, object]]
    rejection_summary: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_run_id": self.plan_run_id,
            "ideas": [idea.to_dict() for idea in self.ideas],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "plans": [plan.to_dict() for plan in self.plans],
            "metrics": dict(self.metrics),
            "idea_diagnostics": list(self.idea_diagnostics),
            "rejection_summary": list(self.rejection_summary),
        }


def run_plan(
    config: dict[str, Any],
    *,
    idea_paths: list[str | Path],
    config_source: str = "sheet",
    provider: str = "fixture",
    write_sheet: bool = False,
    store: LocalStore | None = None,
    audit: AuditWriter | None = None,
) -> PlanRunResult:
    plan_run_id = "run_" + utc_now().replace(":", "").replace("-", "")
    store = store or LocalStore()
    audit = audit or AuditWriter()
    universe, playbooks = load_planner_config(config, source=config_source)
    ideas = load_ideas(idea_paths)
    market = _market_provider(config, provider=provider, store=store)
    preflight = _preflight_client(market) if provider == "public" else FixturePreflightClient()
    portfolio_raw = market.account_state()
    portfolio = _shadow_portfolio_override(portfolio_raw, config)
    portfolio = _shadow_portfolio_with_open_fills(portfolio, store, config)
    match_gate_mode = _match_gate_mode(config)
    candidate_filter_mode = _candidate_filter_mode(config)

    store.save_ideas(ideas)
    store.save_account_snapshot(plan_run_id, portfolio)
    candidates = build_candidates(
        ideas,
        universe,
        playbooks,
        market,
        preflight,
        match_gate_mode=match_gate_mode,
        candidate_filter_mode=candidate_filter_mode,
    )
    _reject_open_shadow_candidates(candidates, store, config)
    idea_diagnostics = diagnose_idea_matches(ideas, universe, playbooks, market, match_gate_mode=match_gate_mode)
    plans = generate_plans(candidates, portfolio, config)
    rejection_summary = _rejection_summary(ideas, candidates, idea_diagnostics)
    metrics = _plan_metrics(
        ideas,
        candidates,
        plans,
        universe,
        idea_diagnostics,
        portfolio_raw,
        portfolio,
        match_gate_mode,
        candidate_filter_mode,
    )
    mode = str((config.get("runtime") or {}).get("mode") or "shadow")
    rows = render_daily_plan_rows(plans, mode=mode)

    store.save_candidates(plan_run_id, candidates)
    store.save_plans(plan_run_id, plans)
    store.event("plan_run_completed", {"plan_run_id": plan_run_id, "ideas": len(ideas), "candidates": len(candidates), "plans": len(plans)})
    audit.write_json("latest_plan_run", {
        "plan_run_id": plan_run_id,
        "ideas": [idea.to_dict() for idea in ideas],
        "candidates": [candidate.to_dict() for candidate in candidates],
        "plans": [plan.to_dict() for plan in plans],
        "metrics": metrics,
        "idea_diagnostics": idea_diagnostics,
        "rejection_summary": rejection_summary,
        "daily_plan_rows": rows,
    })
    audit.event("plan_run_completed", {"plan_run_id": plan_run_id, **metrics})

    if write_sheet and rows:
        write_daily_plan(config, rows, DAILY_PLAN_HEADER)
    elif write_sheet:
        store.event("daily_plan_write_skipped", {"plan_run_id": plan_run_id, "reason": "no_eligible_plans"})
        audit.event("daily_plan_write_skipped", {"plan_run_id": plan_run_id, "reason": "no_eligible_plans"})
    return PlanRunResult(
        plan_run_id=plan_run_id,
        ideas=ideas,
        candidates=candidates,
        plans=plans,
        daily_plan_rows=rows,
        metrics=metrics,
        idea_diagnostics=idea_diagnostics,
        rejection_summary=rejection_summary,
    )


def run_shadow_cycle(
    config: dict[str, Any],
    *,
    idea_paths: list[str | Path],
    config_source: str = "sheet",
    provider: str = "fixture",
    write_sheet: bool = True,
    store: LocalStore | None = None,
    audit: AuditWriter | None = None,
) -> PlanRunResult:
    store = store or LocalStore()
    audit = audit or AuditWriter()
    result = run_plan(
        config,
        idea_paths=idea_paths,
        config_source=config_source,
        provider=provider,
        write_sheet=write_sheet,
        store=store,
        audit=audit,
    )
    top_plan = result.plans[0] if result.plans else None
    if top_plan is not None and top_plan.operator_action == "approve":
        store.save_shadow_fills(result.plan_run_id, top_plan)
        payload = {
            "plan_id": top_plan.plan_id,
            "plan_run_id": result.plan_run_id,
            "portfolio_before": top_plan.portfolio_before.to_dict(),
            "portfolio_after": top_plan.portfolio_after.to_dict(),
            "shadow_fills": [
                {
                    "candidate_id": candidate.candidate_id,
                    "underlying": candidate.underlying,
                    "playbook_id": candidate.playbook_id,
                    "structure": candidate.structure,
                    "net_credit": candidate.net_credit,
                    "estimated_bpr": candidate.estimated_bpr,
                    "legs": [leg.to_dict() for leg in candidate.legs],
                }
                for candidate in top_plan.candidates
            ],
        }
        store.event("shadow_plan_auto_approved", payload)
        audit.write_json("latest_shadow_cycle", payload)
        audit.event("shadow_plan_auto_approved", {"plan_id": top_plan.plan_id})
    return result


class _SnapshottingFixtureMarket:
    def __init__(self, inner: MarketDataProvider, store: LocalStore) -> None:
        self.inner = inner
        self.store = store

    def account_state(self) -> PortfolioState:
        return self.inner.account_state()

    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        snapshot = self.inner.chain_snapshot(underlying)
        self.store.save_chain_snapshot(snapshot)
        return snapshot

    def iv_percentile(self, underlying: str) -> float | None:
        return self.inner.iv_percentile(underlying)

    def iv_rank(self, underlying: str) -> float | None:
        return self.inner.iv_rank(underlying)

    def iv_abs(self, underlying: str) -> float | None:
        return self.inner.iv_abs(underlying)

    def event_status(self, underlying: str) -> str:
        return self.inner.event_status(underlying)


def _market_provider(config: dict[str, Any], *, provider: str, store: LocalStore) -> MarketDataProvider:
    if provider == "public":
        volatility_config = config.get("volatility") or {}
        metric = str(volatility_config.get("metric") or "atm_30_45_mean_iv")
        lookback = int(volatility_config.get("lookback_days") or 252)
        min_observations = int(volatility_config.get("min_observations_for_percentile") or 1)
        missing_policy = str(volatility_config.get("missing_iv_policy") or "strict").lower()
        provisional_percentile = float(volatility_config.get("provisional_percentile") or 50.0)
        public_market = PublicAdapter(config)
        iv_market = IvOverlayMarket(
            public_market,
            IvStore(),
            metric=metric,
            lookback=lookback,
            min_observations=min_observations,
            missing_policy=missing_policy,
            provisional_percentile=provisional_percentile,
        )
        event_market = EarningsOverlayMarket(iv_market, EarningsStore())
        return _SnapshottingFixtureMarket(event_market, store)
    return _SnapshottingFixtureMarket(EarningsOverlayMarket(FixtureMarketDataProvider(), EarningsStore()), store)


def _preflight_client(market: Any) -> Any:
    current = market
    while current is not None:
        if hasattr(current, "preflight"):
            return current
        current = getattr(current, "inner", None)
    raise RuntimeError("public provider did not expose a preflight client")


def _shadow_portfolio_override(portfolio: PortfolioState, config: dict[str, Any]) -> PortfolioState:
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").lower()
    if mode != "shadow":
        return portfolio
    shadow = config.get("shadow") or {}
    account_size = shadow.get("account_size_override")
    buying_power = shadow.get("buying_power_override")
    bpr_used = shadow.get("bpr_used_override")
    if account_size in (None, "") and buying_power in (None, "") and bpr_used in (None, ""):
        return portfolio
    next_account_size = float(account_size if account_size not in (None, "") else portfolio.account_size)
    next_buying_power = float(buying_power if buying_power not in (None, "") else portfolio.buying_power)
    next_bpr_used = float(bpr_used if bpr_used not in (None, "") else portfolio.bpr_used)
    return PortfolioState(
        account_size=next_account_size,
        buying_power=next_buying_power,
        bpr_used=next_bpr_used,
        positions_count=portfolio.positions_count,
        greeks=portfolio.greeks,
        per_underlying_bpr=dict(portfolio.per_underlying_bpr),
    )


def _shadow_portfolio_with_open_fills(portfolio: PortfolioState, store: LocalStore, config: dict[str, Any]) -> PortfolioState:
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").lower()
    if mode != "shadow":
        return portfolio
    return store.shadow_portfolio_state(portfolio)


def _reject_open_shadow_candidates(candidates: list[Candidate], store: LocalStore, config: dict[str, Any]) -> None:
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").lower()
    if mode != "shadow":
        return
    open_candidate_ids = store.open_shadow_candidate_ids()
    open_idea_ids = store.open_shadow_idea_ids()
    if not open_candidate_ids and not open_idea_ids:
        return
    for candidate in candidates:
        if candidate.candidate_id in open_candidate_ids and candidate.eligible:
            candidate.rejection_reason = "shadow_candidate_already_open"
        if candidate.idea_id in open_idea_ids and candidate.eligible:
            candidate.rejection_reason = "shadow_idea_already_open"


def _candidate_filter_mode(config: dict[str, Any]) -> str:
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").lower()
    if mode != "shadow":
        return "strict"
    requested = str((config.get("shadow") or {}).get("candidate_filter_mode") or "strict").lower()
    return "warn" if requested == "warn" else "strict"


def _match_gate_mode(config: dict[str, Any]) -> str:
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").lower()
    if mode != "shadow":
        return "strict"
    requested = str((config.get("shadow") or {}).get("match_gate_mode") or "strict").lower()
    return "permissive" if requested == "permissive" else "strict"


def _plan_metrics(
    ideas: list[Idea],
    candidates: list[Candidate],
    plans: list[Plan],
    universe: list[Any],
    idea_diagnostics: list[dict[str, object]],
    portfolio_raw: PortfolioState,
    portfolio_effective: PortfolioState,
    match_gate_mode: str,
    candidate_filter_mode: str,
) -> dict[str, Any]:
    universe_symbols = {entry.symbol for entry in universe if entry.enabled}
    eligible_candidates = [candidate for candidate in candidates if candidate.eligible]
    rejected_candidates = [candidate for candidate in candidates if not candidate.eligible]
    preflight_failures = [
        candidate for candidate in rejected_candidates
        if candidate.preflight is not None and not candidate.preflight.ok
    ]
    return {
        "ideas_loaded": len(ideas),
        "ideas_in_universe": sum(1 for idea in ideas if idea.underlying in universe_symbols),
        "ideas_out_of_universe": sum(1 for idea in ideas if idea.underlying not in universe_symbols),
        "candidates_built": len(candidates),
        "candidates_eligible": len(eligible_candidates),
        "candidates_rejected": len(rejected_candidates),
        "preflight_failures": len(preflight_failures),
        "ideas_with_playbook_match": sum(1 for item in idea_diagnostics if item.get("status") == "matched_playbooks"),
        "ideas_without_playbook_match": sum(1 for item in idea_diagnostics if item.get("status") == "no_playbook_match"),
        "plans": len(plans),
        "match_gate_mode": match_gate_mode,
        "candidate_filter_mode": candidate_filter_mode,
        "account_size_raw": portfolio_raw.account_size,
        "account_size_effective": portfolio_effective.account_size,
        "buying_power_raw": portfolio_raw.buying_power,
        "buying_power_effective": portfolio_effective.buying_power,
    }


def _rejection_summary(
    ideas: list[Idea],
    candidates: list[Candidate],
    idea_diagnostics: list[dict[str, object]],
) -> list[str]:
    diagnostics_by_id = {str(item.get("idea_id")): item for item in idea_diagnostics}
    candidates_by_idea: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        candidates_by_idea.setdefault(candidate.idea_id, []).append(candidate)

    grouped: dict[str, int] = {}
    for idea in ideas:
        idea_candidates = candidates_by_idea.get(idea.idea_id, [])
        eligible = [candidate for candidate in idea_candidates if candidate.eligible]
        if eligible:
            continue
        diagnostic = diagnostics_by_id.get(idea.idea_id, {})
        matched = diagnostic.get("matched_playbooks") or []
        if not matched:
            summary = str(diagnostic.get("summary") or "No matching playbook.")
            _count_summary(grouped, f"{idea.underlying}: no playbook match - {summary}")
            continue
        if not idea_candidates:
            _count_summary(
                grouped,
                f"{idea.underlying}: matched {', '.join(str(item) for item in matched[:4])} but built no option structures",
            )
            continue
        reason_counts: dict[str, int] = {}
        for candidate in idea_candidates:
            reason = candidate.rejection_reason or "not_eligible"
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        top = sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
        reason_text = ", ".join(f"{reason} ({count})" for reason, count in top)
        _count_summary(
            grouped,
            f"{idea.underlying}: matched {', '.join(str(item) for item in matched[:4])} but rejected candidates - {reason_text}",
        )
    return [f"{count} {line}" for line, count in sorted(grouped.items(), key=lambda item: (-item[1], item[0]))]


def _count_summary(grouped: dict[str, int], line: str) -> None:
    grouped[line] = grouped.get(line, 0) + 1
