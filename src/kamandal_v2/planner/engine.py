"""Planner orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import os
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from kamandal_v2.domain.models import Candidate, ChainSnapshot, Idea, Plan, PortfolioState, PreflightResult, utc_now
from kamandal_v2.events.earnings import EarningsOverlayMarket, EarningsStore
from kamandal_v2.market.fixture import FixtureMarketDataProvider, FixturePreflightClient
from kamandal_v2.market.interfaces import MarketDataProvider
from kamandal_v2.market.public import PublicAdapter
from kamandal_v2.market.tastytrade import TastytradeAdapter
from kamandal_v2.planner.candidate_builder import build_candidates, diagnose_idea_matches
from kamandal_v2.planner.config_loader import load_planner_config
from kamandal_v2.planner.daily_plan import render_daily_plan_rows
from kamandal_v2.planner.idea_loader import load_ideas
from kamandal_v2.planner.plan_generator import generate_plans
from kamandal_v2.planner.shadow_preflight import shadow_preflight_client
from kamandal_v2.planner.structural_break_gates import annotate_structural_breaks
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.sheets import write_daily_plan
from kamandal_v2.stores.audit import AuditWriter
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.volatility.iv import IvOverlayMarket, PrimaryIvOverlayMarket
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


@dataclass(slots=True)
class PlanningSourceGroup:
    """A normalized opportunity input routed to a bounded playbook subset."""

    source_name: str
    ideas: list[Idea]
    playbooks: list[Any]


def run_plan(
    config: dict[str, Any],
    *,
    idea_paths: list[str | Path],
    config_source: str = "sheet",
    provider: str = "fixture",
    write_sheet: bool = False,
    store: LocalStore | None = None,
    audit: AuditWriter | None = None,
    candidate_postprocessor: Callable[[list[Candidate], LocalStore, dict[str, Any], PortfolioState], None] | None = None,
    plan_top_n: int = 5,
    plan_max_new_positions: int | None = None,
    universe_override: list[Any] | None = None,
    playbooks_override: list[Any] | None = None,
    source_groups_factory: Callable[[list[Idea], list[Any], list[Any], PortfolioState], list[PlanningSourceGroup]] | None = None,
    supplemental_candidate_factory: Callable[[MarketDataProvider, list[Any], PortfolioState, LocalStore], list[Candidate]] | None = None,
    market_override: MarketDataProvider | None = None,
    portfolio_override: PortfolioState | None = None,
) -> PlanRunResult:
    plan_run_id = "run_" + utc_now().replace(":", "").replace("-", "")
    store = store or LocalStore()
    audit = audit or AuditWriter()
    if universe_override is None or playbooks_override is None:
        loaded_universe, loaded_playbooks = load_planner_config(config, source=config_source)
        universe = loaded_universe if universe_override is None else universe_override
        playbooks = loaded_playbooks if playbooks_override is None else playbooks_override
    else:
        universe, playbooks = universe_override, playbooks_override
    loaded_ideas = annotate_structural_breaks(load_ideas(idea_paths), config)
    market = market_override or _market_provider(config, provider=provider, store=store)
    preflight = _preflight_client(market) if provider == "public" else FixturePreflightClient()
    portfolio_raw = portfolio_override if portfolio_override is not None else market.account_state()
    portfolio = _shadow_portfolio_override(portfolio_raw, config)
    portfolio = _shadow_portfolio_with_open_fills(portfolio, store, config)
    portfolio = _live_portfolio_with_open_groups(portfolio, store, config)
    preflight = _live_overlap_preflight_guard(preflight, store, config)
    match_gate_mode = _match_gate_mode(config)
    candidate_filter_mode = _candidate_filter_mode(config)
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").strip().lower()
    if mode not in {"live", "shadow"}:
        raise ValueError(f"runtime.mode must be live or shadow, got {mode!r}")
    preflight = shadow_preflight_client(config, preflight, provider=provider, mode=mode)

    source_groups = (
        source_groups_factory(loaded_ideas, universe, playbooks, portfolio)
        if source_groups_factory is not None
        else [PlanningSourceGroup("idea", loaded_ideas, playbooks)]
    )
    ideas = [idea for group in source_groups for idea in group.ideas]
    store.save_ideas(ideas)
    store.save_account_snapshot(plan_run_id, portfolio, mode=mode)
    candidates = [
        candidate
        for group in source_groups
        for candidate in build_candidates(
            group.ideas,
            universe,
            group.playbooks,
            market,
            preflight,
            match_gate_mode=match_gate_mode,
            candidate_filter_mode=candidate_filter_mode,
            config=config,
        )
    ]
    if supplemental_candidate_factory is not None:
        candidates.extend(supplemental_candidate_factory(market, playbooks, portfolio, store))
    if candidate_postprocessor is not None:
        candidate_postprocessor(candidates, store, config, portfolio)
    _reject_open_shadow_candidates(candidates, store, config)
    idea_diagnostics = [
        diagnostic
        for group in source_groups
        for diagnostic in diagnose_idea_matches(
            group.ideas,
            universe,
            group.playbooks,
            market,
            match_gate_mode=match_gate_mode,
            config=config,
        )
    ]
    plans = generate_plans(candidates, portfolio, config, top_n=plan_top_n, max_new_positions=plan_max_new_positions)
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

    if write_sheet:
        write_daily_plan(config, rows, DAILY_PLAN_HEADER, replace_lanes={mode})
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


def _market_provider(
    config: dict[str, Any],
    *,
    provider: str,
    store: LocalStore,
    required_expiration_dates: list[str] | tuple[str, ...] = (),
) -> MarketDataProvider:
    volatility_config = config.get("volatility") or {}
    metric = str(volatility_config.get("metric") or "atm_30_45_mean_iv")
    lookback = int(volatility_config.get("lookback_days") or 252)
    min_observations = int(volatility_config.get("min_observations_for_percentile") or 1)
    missing_policy = str(volatility_config.get("missing_iv_policy") or "strict").lower()
    provisional_percentile = float(volatility_config.get("provisional_percentile") or 50.0)
    iv_store = IvStore()

    if provider == "public":
        public_market = PublicAdapter(config)
        if required_expiration_dates:
            public_market.expiration_dates = sorted(
                {
                    *public_market.expiration_dates,
                    *(str(value) for value in required_expiration_dates if str(value)),
                }
            )
        iv_market = _iv_market(
            config,
            public_market,
            iv_store,
            metric=metric,
            lookback=lookback,
            min_observations=min_observations,
            missing_policy=missing_policy,
            provisional_percentile=provisional_percentile,
        )
        event_market = EarningsOverlayMarket(iv_market, EarningsStore())
        return _SnapshottingFixtureMarket(event_market, store)
    fixture_market = FixtureMarketDataProvider()
    iv_market = _iv_market(
        config,
        fixture_market,
        iv_store,
        metric=metric,
        lookback=lookback,
        min_observations=min_observations,
        missing_policy=missing_policy,
        provisional_percentile=provisional_percentile,
    )
    return _SnapshottingFixtureMarket(EarningsOverlayMarket(iv_market, EarningsStore()), store)


def _iv_market(
    config: dict[str, Any],
    inner: MarketDataProvider,
    iv_store: IvStore,
    *,
    metric: str,
    lookback: int,
    min_observations: int,
    missing_policy: str,
    provisional_percentile: float,
) -> MarketDataProvider:
    if _use_tastytrade_live_iv(config):
        tastytrade = TastytradeAdapter(config)
        if tastytrade.available():
            return PrimaryIvOverlayMarket(
                inner,
                iv_store,
                primary=tastytrade,
                metric=metric,
                lookback=lookback,
                min_observations=min_observations,
                missing_policy=missing_policy,
                provisional_percentile=provisional_percentile,
            )
    return IvOverlayMarket(
        inner,
        iv_store,
        metric=metric,
        lookback=lookback,
        min_observations=min_observations,
        missing_policy=missing_policy,
        provisional_percentile=provisional_percentile,
    )


def _use_tastytrade_live_iv(config: dict[str, Any]) -> bool:
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").lower()
    metrics_provider = str((config.get("broker") or {}).get("market_metrics_provider") or "").lower()
    active_broker = str((config.get("broker") or {}).get("active") or "").lower()
    return mode == "live" and (metrics_provider or active_broker) in {"tastytrade", "tasty"}


def _preflight_client(market: Any) -> Any:
    current = market
    while current is not None:
        if hasattr(current, "preflight"):
            return current
        current = getattr(current, "inner", None)
    raise RuntimeError("public provider did not expose a preflight client")


class _LiveOverlapPreflightGuard:
    def __init__(self, inner: Any, open_contracts: set[str]) -> None:
        self.inner = inner
        self.open_contracts = open_contracts

    def preflight(self, candidate: Candidate) -> PreflightResult:
        overlap = _candidate_contract_overlap(candidate, self.open_contracts)
        if overlap:
            return PreflightResult(
                ok=False,
                bpr=candidate.estimated_bpr,
                message=f"live_contract_already_open:{overlap}",
                raw={"source": "live_overlap_preflight_guard", "live_contract_already_open": overlap},
            )
        return self.inner.preflight(candidate)

    def preflight_ticket(self, ticket: dict[str, Any]) -> Any:
        return self.inner.preflight_ticket(ticket)


def _live_overlap_preflight_guard(preflight: Any, store: LocalStore, config: dict[str, Any]) -> Any:
    if str((config.get("runtime") or {}).get("mode") or "").lower() != "live":
        return preflight
    open_contracts = _open_live_contract_keys(store)
    if not open_contracts:
        return preflight
    return _LiveOverlapPreflightGuard(preflight, open_contracts)


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


def _candidate_contract_overlap(candidate: Candidate, open_contracts: set[str]) -> str:
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


def _shadow_portfolio_override(
    portfolio: PortfolioState,
    config: dict[str, Any],
    *,
    force: bool = False,
) -> PortfolioState:
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").lower()
    if mode != "shadow" and not force:
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


def _live_portfolio_with_open_groups(portfolio: PortfolioState, store: LocalStore, config: dict[str, Any]) -> PortfolioState:
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").lower()
    if mode != "live":
        return portfolio
    return store.live_portfolio_state(portfolio)


def _reject_open_shadow_candidates(candidates: list[Candidate], store: LocalStore, config: dict[str, Any]) -> None:
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").lower()
    if mode != "shadow":
        return
    open_candidate_ids = store.open_shadow_candidate_ids()
    open_idea_ids = store.open_shadow_idea_ids()
    traded_idea_ids = _shadow_traded_idea_ids(store, config)
    if not open_candidate_ids and not open_idea_ids and not traded_idea_ids:
        return
    for candidate in candidates:
        if candidate.candidate_id in open_candidate_ids and candidate.eligible:
            candidate.rejection_reason = "shadow_candidate_already_open"
        elif candidate.idea_id in open_idea_ids and candidate.eligible:
            candidate.rejection_reason = "shadow_idea_already_open"
        # The cooldown is an idea-level lifecycle decision, not a candidate-level
        # eligibility check.  Apply it consistently even when one shape already
        # failed a construction guard so every sibling records the same reason
        # the idea cannot be traded again during the cooldown window.
        elif candidate.idea_id in traded_idea_ids:
            candidate.rejection_reason = "shadow_idea_already_traded_today"


def _shadow_traded_idea_ids(store: LocalStore, config: dict[str, Any]) -> set[str]:
    cooldown_days = int((config.get("shadow") or {}).get("idea_cooldown_days") or 0)
    if cooldown_days <= 0:
        return set()
    market_tz = os.environ.get("KAMANDAL_MARKET_TZ") or str((config.get("runtime") or {}).get("market_timezone") or "America/Chicago")
    today = datetime.now(ZoneInfo(market_tz)).date()
    start_date = today - timedelta(days=max(cooldown_days - 1, 0))
    local_start = datetime.combine(start_date, time.min, tzinfo=ZoneInfo(market_tz))
    utc_start = local_start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    return store.shadow_idea_ids_opened_since(utc_start)


def _candidate_filter_mode(config: dict[str, Any]) -> str:
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").lower()
    if mode != "shadow":
        requested = str((config.get("live") or {}).get("candidate_filter_mode") or "strict").lower()
        return "warn" if requested == "warn" else "strict"
    requested = str((config.get("shadow") or {}).get("candidate_filter_mode") or "strict").lower()
    return "warn" if requested == "warn" else "strict"


def _match_gate_mode(config: dict[str, Any]) -> str:
    mode = str((config.get("runtime") or {}).get("mode") or "shadow").lower()
    if mode != "shadow":
        requested = str((config.get("live") or {}).get("match_gate_mode") or "strict").lower()
        return "permissive" if requested == "permissive" else "strict"
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
        "matched_playbooks_zero_raw_candidates": sum(len(item.get("zero_candidate_diagnostics") or []) for item in idea_diagnostics),
        "plans": len(plans),
        "match_gate_mode": match_gate_mode,
        "candidate_filter_mode": candidate_filter_mode,
        "account_size_raw": portfolio_raw.account_size,
        "account_size_effective": portfolio_effective.account_size,
        "buying_power_raw": portfolio_raw.buying_power,
        "buying_power_effective": portfolio_effective.buying_power,
        "vertical_gate_unmet": _vertical_gate_unmet_count(candidates),
    }


def _vertical_gate_unmet_count(candidates: list[Candidate]) -> int:
    """Count idea x playbook vertical pairs where constructions were built but
    none cleared the credit-width-ratio gate at any width searched."""
    groups: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in candidates:
        if candidate.structure not in {"put_spread", "call_spread"}:
            continue
        groups.setdefault((candidate.idea_id, candidate.playbook_id), []).append(candidate)
    return sum(
        1
        for group_candidates in groups.values()
        if group_candidates and all(
            candidate.rejection_reason.startswith("credit_width_ratio_below_min")
            for candidate in group_candidates
        )
    )


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
            zero_diagnostics = diagnostic.get("zero_candidate_diagnostics") or []
            if zero_diagnostics:
                details = ", ".join(
                    f"{item.get('playbook_id')}:{item.get('reason')}"
                    for item in zero_diagnostics[:3]
                    if isinstance(item, dict)
                )
                _count_summary(
                    grouped,
                    f"{idea.underlying}: matched {', '.join(str(item) for item in matched[:4])} but built no option structures - {details}",
                )
                continue
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
