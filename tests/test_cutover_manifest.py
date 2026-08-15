from __future__ import annotations

from kamandal_v2.stores.sqlite import LocalStore
import sqlite3

from kamandal_v2.strategy_engine.cutover import (
    apply_cutover_fixture,
    build_cutover_manifest,
    restore_cutover_fixture,
    unified_schedule_manifest,
)
from kamandal_v2.strategy_lanes.store import CsaStore


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


def test_fixture_cutover_apply_is_idempotent_and_restore_rehearses(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    store = LocalStore(database)
    store.save_live_position_group(
        "strangle-1",
        {"candidate": {"structure": "short_strangle", "legs": [
            {"side": "sell", "role": "short_put", "option_type": "put", "expiration": "2026-09-25", "strike": 90, "quantity": 1},
            {"side": "sell", "role": "short_call", "option_type": "call", "expiration": "2026-09-25", "strike": 110, "quantity": 1},
        ]}},
    )
    before = database.read_bytes()

    receipt = apply_cutover_fixture(database, backup_dir=tmp_path / "backups", allow_fixture_apply=True)
    repeat = apply_cutover_fixture(database, backup_dir=tmp_path / "backups", allow_fixture_apply=True)

    assert receipt.integrity_check == "ok"
    assert repeat.created_lifecycle_ids == receipt.created_lifecycle_ids
    assert CsaStore(database).lifecycle("adopt:strangle-1") is not None
    restore_cutover_fixture(receipt)
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
