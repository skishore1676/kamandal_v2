"""Single management invocation for the unified strategy-engine topology.

This is an orchestration boundary, not a second decision engine.  It invokes
the one typed lifecycle owner in fixed live-before-shadow order, while making
each receipt independently auditable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable


Manager = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class ManagementBranchReceipt:
    branch: str
    ok: bool
    result: dict[str, Any] | None
    error: str = ""


@dataclass(frozen=True, slots=True)
class UnifiedManagementReceipt:
    branches: tuple[ManagementBranchReceipt, ...]

    @property
    def ok(self) -> bool:
        return all(branch.ok for branch in self.branches)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "branches": [asdict(branch) for branch in self.branches]}


def run_unified_lifecycle_management(
    config: dict[str, Any],
    *,
    sqlite_path: str,
    provider: str,
    branch: str = "all",
    live_lifecycle_manager: Manager | None = None,
    shadow_lifecycle_manager: Manager | None = None,
) -> UnifiedManagementReceipt:
    """Evaluate canonical lifecycle ownership in deterministic live-first order.

    ``branch`` lets the scheduled runner finish the complete guarded-live effect
    cycle before starting broker-inert shadow work.  The default remains ``all``
    for read-only callers and compatibility, while scheduled operation invokes
    ``live`` and ``shadow`` separately around the live effect boundary.
    """
    if branch not in {"all", "live", "shadow"}:
        raise ValueError(f"unsupported lifecycle-management branch: {branch}")
    if live_lifecycle_manager is None or shadow_lifecycle_manager is None:
        from kamandal_v2.strategy_lanes.management_runtime import (
            run_live_lifecycle_management,
            run_shadow_lifecycle_management,
        )

        live_lifecycle_manager = live_lifecycle_manager or (
            lambda: run_live_lifecycle_management(config, sqlite_path=sqlite_path, provider=provider)
        )
        shadow_lifecycle_manager = shadow_lifecycle_manager or (
            lambda: run_shadow_lifecycle_management(config, sqlite_path=sqlite_path, provider=provider)
        )
    branches: list[ManagementBranchReceipt] = []
    if branch in {"all", "live"}:
        branches.append(_run_branch("live_lifecycle", live_lifecycle_manager))
    if branch in {"all", "shadow"}:
        branches.append(_run_branch("shadow_lifecycle", shadow_lifecycle_manager))
    return UnifiedManagementReceipt(tuple(branches))


def _run_branch(branch: str, manager: Manager) -> ManagementBranchReceipt:
    try:
        result = manager()
    except Exception as exc:  # noqa: BLE001 - one branch may never starve another.
        return ManagementBranchReceipt(branch, False, None, f"{type(exc).__name__}: {exc}")
    if hasattr(result, "to_dict"):
        result = result.to_dict()
    if not isinstance(result, dict):
        result = {"value": result}
    explicit_ok = bool(result.get("ok", True))
    return ManagementBranchReceipt(branch, explicit_ok, result, "" if explicit_ok else "manager reported not ok")
