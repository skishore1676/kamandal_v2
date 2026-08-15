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

from kamandal_v2.domain.models import Playbook, UniverseEntry
from kamandal_v2.planner.engine import PlanRunResult, run_plan
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
    live = _run_book(ExecutionMode.LIVE, compilation.policies, universe, config, idea_paths, provider, active_store, audit_root)
    shadow = _run_book(ExecutionMode.SHADOW, compilation.policies, universe, config, idea_paths, provider, active_store, audit_root)
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
) -> PlanningBook:
    selected = tuple(policy for policy in policies if policy.mode is mode)
    if not selected:
        return PlanningBook(mode, (), None, ())
    mode_config = deepcopy(config)
    mode_config.setdefault("runtime", {})["mode"] = mode.value
    if mode is ExecutionMode.SHADOW:
        mode_config.setdefault("execution", {})["approval_mode"] = "shadow_auto_top_plan"
    playbooks = [Playbook.from_row(policy.fields) for policy in selected]
    try:
        result = run_plan(
            mode_config,
            idea_paths=idea_paths,
            config_source="seed",
            provider=provider,
            write_sheet=False,
            store=store,
            audit=AuditWriter(Path(audit_root) / mode.value),
            universe_override=universe,
            playbooks_override=playbooks,
        )
    except Exception as exc:  # noqa: BLE001 - report failure-isolated book receipt.
        return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), None, (f"{type(exc).__name__}: {exc}",))
    return PlanningBook(mode, tuple(policy.playbook_id for policy in selected), result, ())
