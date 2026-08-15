"""One invocation that produces isolated live and shadow planning books.

The function is intentionally broker-inert.  It uses the existing portfolio
optimizer for both books and only separates inputs, portfolio mode, audit
receipts, and result ownership.  Later phases replace the old CSA scan caller
with this source-level entry point.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import Idea, Playbook, PortfolioState, UniverseEntry
from kamandal_v2.planner.engine import PlanRunResult, PlanningSourceGroup, run_plan
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.sheets import write_daily_plan
from kamandal_v2.stores.audit import AuditWriter
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.policy import ExecutionMode, PlaybookPolicy, PolicyCompilation, compile_playbook_policies


@dataclass(frozen=True, slots=True)
class PlanningBook:
    mode: ExecutionMode
    policy_ids: tuple[str, ...]
    result: PlanRunResult | None
    errors: tuple[str, ...]

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
) -> UnifiedPlanningResult:
    """Build independent books from one normalized Sheet snapshot.

    A policy error is attached to both books rather than allowing either book to
    masquerade as complete.  Once policy compilation succeeds, failures stay
    per-book: a shadow failure cannot erase a valid live result and vice versa.
    """
    compilation = compile_playbook_policies(playbook_rows)
    active_store = store or LocalStore()
    universe = [UniverseEntry.from_row(row) for row in universe_rows if row.get("symbol")]
    if not compilation.ok:
        errors = compilation.errors
        return UnifiedPlanningResult(
            compilation=compilation,
            live=PlanningBook(ExecutionMode.LIVE, (), None, errors),
            shadow=PlanningBook(ExecutionMode.SHADOW, (), None, errors),
        )
    live = _run_book(ExecutionMode.LIVE, compilation.policies, universe, config, idea_paths, provider, active_store, audit_root, write_sheet)
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
) -> PlanningBook:
    selected = tuple(policy for policy in policies if policy.mode is mode)
    if not selected:
        return PlanningBook(mode, (), None, ())
    mode_config = deepcopy(config)
    mode_config.setdefault("runtime", {})["mode"] = mode.value
    live_renderer = None
    if mode is ExecutionMode.SHADOW:
        mode_config.setdefault("execution", {})["approval_mode"] = "shadow_auto_top_plan"
    else:
        from kamandal_v2.live.advisory import _live_candidate_policy, live_config, render_live_plan_rows

        mode_config = live_config(mode_config)
        live_renderer = (render_live_plan_rows, _live_candidate_policy)
    playbooks = [Playbook.from_row(policy.fields) for policy in selected]
    try:
        result = run_plan(
            mode_config,
            idea_paths=idea_paths,
            config_source="seed",
            provider=provider,
            write_sheet=write_sheet if mode is ExecutionMode.SHADOW else False,
            store=store,
            audit=AuditWriter(Path(audit_root) / mode.value),
            candidate_postprocessor=live_renderer[1] if live_renderer is not None else None,
            universe_override=universe,
            playbooks_override=playbooks,
            source_groups_factory=lambda ideas, entries, selected_playbooks, portfolio: _source_groups(
                ideas,
                entries,
                selected_playbooks,
                policies=selected,
                portfolio=portfolio,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - report failure-isolated book receipt.
        return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), None, (f"{type(exc).__name__}: {exc}",))
    if mode is ExecutionMode.LIVE:
        rows = live_renderer[0](result, mode_config, store=store, mode="live")
        result.daily_plan_rows[:] = rows
        if write_sheet:
            write_daily_plan(mode_config, rows, DAILY_PLAN_HEADER, replace_lanes={"live"})
    elif result.plans and result.plans[0].operator_action == "approve":
        store.save_shadow_fills(result.plan_run_id, result.plans[0])
        store.event("unified_shadow_plan_auto_approved", {"plan_run_id": result.plan_run_id, "plan_id": result.plans[0].plan_id})
    return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), result, ())


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
