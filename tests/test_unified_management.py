from __future__ import annotations

from kamandal_v2.strategy_engine.management import run_unified_lifecycle_management


def test_unified_management_runs_live_before_shadow_and_isolates_failure() -> None:
    calls: list[str] = []

    def established():
        calls.append("established_live")
        return {"ok": True, "managed": 1}

    def typed_live():
        calls.append("typed_live")
        raise RuntimeError("fixture live lifecycle failure")

    def typed_shadow():
        calls.append("typed_shadow")
        return {"ok": True, "managed": 2}

    receipt = run_unified_lifecycle_management(
        {},
        sqlite_path="fixture.db",
        provider="fixture",
        established_live_manager=established,
        typed_live_manager=typed_live,
        typed_shadow_manager=typed_shadow,
    )

    assert calls == ["established_live", "typed_live", "typed_shadow"]
    assert receipt.ok is False
    assert receipt.branches[1].error == "RuntimeError: fixture live lifecycle failure"
    assert receipt.branches[2].result == {"ok": True, "managed": 2}
