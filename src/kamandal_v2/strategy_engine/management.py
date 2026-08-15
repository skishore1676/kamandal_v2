"""Single management invocation for the unified strategy-engine topology.

This is an orchestration boundary, not a second decision engine.  It invokes
the established close-only manager and the typed lifecycle manager in a fixed
live-before-shadow order, while making each receipt independently auditable.
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
    established_live_manager: Manager | None = None,
    typed_live_manager: Manager | None = None,
    typed_shadow_manager: Manager | None = None,
) -> UnifiedManagementReceipt:
    """Evaluate all lifecycle ownership in deterministic live-first order.

    The injectable callbacks make ordering and failure isolation explicit in
    source tests.  Defaults preserve the existing decision functions; only the
    schedule owner changes at cutover.
    """
    if established_live_manager is None or typed_live_manager is None or typed_shadow_manager is None:
        from kamandal_v2.live.management import run_live_management_plan
        from kamandal_v2.strategy_lanes.management_runtime import run_csa_live_management, run_csa_shadow_management

        established_live_manager = established_live_manager or (
            lambda: run_live_management_plan(config, config_source="sheet", write_sheet=True)
        )
        typed_live_manager = typed_live_manager or (
            lambda: run_csa_live_management(config, sqlite_path=sqlite_path, provider=provider)
        )
        typed_shadow_manager = typed_shadow_manager or (
            lambda: run_csa_shadow_management(config, sqlite_path=sqlite_path, provider=provider)
        )
    branches = (
        _run_branch("established_live", established_live_manager),
        _run_branch("typed_live", typed_live_manager),
        _run_branch("typed_shadow", typed_shadow_manager),
    )
    return UnifiedManagementReceipt(branches)


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
