from __future__ import annotations

from pathlib import Path

from kamandal_v2.strategy_engine.management import run_unified_lifecycle_management


def test_scheduled_manager_imports_only_generic_lifecycle_owners() -> None:
    source = Path("src/kamandal_v2/strategy_engine/management.py").read_text(encoding="utf-8")

    assert "run_csa_live_management" not in source
    assert "run_csa_shadow_management" not in source
    assert "run_live_lifecycle_management" in source
    assert "run_shadow_lifecycle_management" in source


def test_unified_management_runs_live_before_shadow_and_isolates_failure() -> None:
    calls: list[str] = []

    def typed_live():
        calls.append("live_lifecycle")
        raise RuntimeError("fixture live lifecycle failure")

    def typed_shadow():
        calls.append("shadow_lifecycle")
        return {"ok": True, "managed": 2}

    receipt = run_unified_lifecycle_management(
        {},
        sqlite_path="fixture.db",
        provider="fixture",
        live_lifecycle_manager=typed_live,
        shadow_lifecycle_manager=typed_shadow,
    )

    assert calls == ["live_lifecycle", "shadow_lifecycle"]
    assert receipt.ok is False
    assert receipt.branches[0].error == "RuntimeError: fixture live lifecycle failure"
    assert receipt.branches[1].result == {"ok": True, "managed": 2}
