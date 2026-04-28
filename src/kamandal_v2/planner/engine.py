"""Planner orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import Candidate, ChainSnapshot, Idea, Plan, PortfolioState, utc_now
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_run_id": self.plan_run_id,
            "ideas": [idea.to_dict() for idea in self.ideas],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "plans": [plan.to_dict() for plan in self.plans],
            "metrics": dict(self.metrics),
            "idea_diagnostics": list(self.idea_diagnostics),
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
    preflight = getattr(market, "inner", market) if provider == "public" else FixturePreflightClient()
    portfolio = market.account_state()

    store.save_ideas(ideas)
    store.save_account_snapshot(plan_run_id, portfolio)
    candidates = build_candidates(ideas, universe, playbooks, market, preflight)
    idea_diagnostics = diagnose_idea_matches(ideas, universe, playbooks, market)
    plans = generate_plans(candidates, portfolio, config)
    metrics = _plan_metrics(ideas, candidates, plans, universe, idea_diagnostics)
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
        "daily_plan_rows": rows,
    })
    audit.event("plan_run_completed", {"plan_run_id": plan_run_id, **metrics})

    if write_sheet:
        write_daily_plan(config, rows, DAILY_PLAN_HEADER)
    return PlanRunResult(
        plan_run_id=plan_run_id,
        ideas=ideas,
        candidates=candidates,
        plans=plans,
        daily_plan_rows=rows,
        metrics=metrics,
        idea_diagnostics=idea_diagnostics,
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
        payload = {
            "plan_id": top_plan.plan_id,
            "plan_run_id": result.plan_run_id,
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
        return _SnapshottingFixtureMarket(iv_market, store)
    return _SnapshottingFixtureMarket(FixtureMarketDataProvider(), store)


def _plan_metrics(
    ideas: list[Idea],
    candidates: list[Candidate],
    plans: list[Plan],
    universe: list[Any],
    idea_diagnostics: list[dict[str, object]],
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
    }
