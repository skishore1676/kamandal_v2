from __future__ import annotations

from kamandal_v2.strategy_engine.management import run_unified_lifecycle_management


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
