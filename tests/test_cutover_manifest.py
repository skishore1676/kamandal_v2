from __future__ import annotations

from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.cutover import build_cutover_manifest, unified_schedule_manifest


def test_cutover_manifest_is_read_only_and_blocks_ambiguous_shapes(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.save_live_position_group(
        "strangle-1",
        {"candidate": {"structure": "short_strangle", "legs": [
            {"side": "sell", "role": "short_put", "option_type": "put", "expiration": "2026-09-25", "strike": 90, "quantity": 1},
            {"side": "sell", "role": "short_call", "option_type": "call", "expiration": "2026-09-25", "strike": 110, "quantity": 1},
        ]}},
    )
    store.save_live_position_group("unknown-1", {"candidate": {"structure": "long_call", "legs": []}})

    manifest = build_cutover_manifest(store)

    assert [item.decision for item in manifest.decisions] == ["create", "block"]
    assert manifest.ready is False
    assert "unsupported structure long_call" in manifest.decisions[1].reason


def test_target_schedule_has_one_planning_and_management_owner() -> None:
    schedule = unified_schedule_manifest()

    assert "universe-proposer" in schedule["retire"]
    assert "csa-live-management" in schedule["retire"]
    assert "live-management" in schedule["retire"]
    assert schedule["add"] == ("unified-planning", "unified-lifecycle-management", "unified-lifecycle-history")
