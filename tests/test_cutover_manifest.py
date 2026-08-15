from __future__ import annotations

from kamandal_v2.stores.sqlite import LocalStore
import sqlite3

from kamandal_v2.strategy_engine.cutover import (
    apply_cutover_fixture,
    build_cutover_manifest,
    build_sheet_mapping_manifest,
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
    assert schedule["add"] == ("unified-planning", "unified-lifecycle-management")


def test_sheet_mapping_is_bounded_non_applying_and_preserves_generic_calendar_rows() -> None:
    header = [
        "playbook_id", "enabled", "strategy_family", "structure", "csa_stage", "management_policy_json",
        "profit_target_pct", "short_delta_min", "short_delta_max",
    ]
    rows = [
        {
            "playbook_id": "short_strangle_high_iv", "enabled": "TRUE", "strategy_family": "short_strangle",
            "structure": "short_strangle", "csa_stage": "shadow", "profit_target_pct": "40",
            "short_delta_min": "0.16", "short_delta_max": "0.25",
        },
        {
            "playbook_id": "generic_call_calendar", "enabled": "TRUE", "strategy_family": "call_calendar",
            "structure": "call_calendar", "csa_stage": "baseline",
            "management_policy_json": '{"event_expiration":{"near_before_days":2},"lifecycle":{"hold":true}}',
        },
    ]
    original = [dict(row) for row in rows]

    manifest = build_sheet_mapping_manifest(
        header,
        rows,
        earnings_calendar_row={
            "playbook_id": "earnings_calendar_bullish", "strategy_family": "earnings_calendar",
            "structure": "call_calendar", "applicable_direction": "bullish", "dte_min": 5, "dte_max": 7,
            "long_dte_min": 45, "long_dte_max": 60, "event_timing": "confirmed_bmo_or_amc_final_pre_event_session",
            "event_near_expiry_after_days": 1, "paired_order_required": "TRUE", "post_event_exit": "first_eligible_post_event_session",
        },
    )

    assert manifest.ready is True
    assert rows == original
    assert manifest.source_header == tuple(header)
    assert manifest.target_header[: len(header)] == tuple(header)
    assert "mode" in manifest.header_additions
    changes = {(item.playbook_id, item.column): item.new_value for item in manifest.cell_mappings}
    assert changes[("short_strangle_high_iv", "mode")] == "shadow"
    assert changes[("short_strangle_high_iv", "management_delta_target")] == 0.30
    assert changes[("short_strangle_high_iv", "management_delta_max")] == 0.40
    assert changes[("short_strangle_high_iv", "dte_action")] == "close"
    assert changes[("short_strangle_high_iv", "inversion_enabled")] == "FALSE"
    assert changes[("generic_call_calendar", "management_policy_json")] == '{"lifecycle":{"hold":true}}'
    assert manifest.row_additions[0].start_row == 4
    assert manifest.row_additions[0].values["enabled"] == "FALSE"
    assert manifest.row_additions[0].values["mode"] == "shadow"


def test_sheet_mapping_blocks_unreviewed_earnings_row_and_invalid_stage() -> None:
    manifest = build_sheet_mapping_manifest(
        ["playbook_id", "enabled", "strategy_family", "structure", "csa_stage"],
        [{"playbook_id": "bad", "enabled": "TRUE", "strategy_family": "short_put", "structure": "short_put", "csa_stage": "mystery"}],
    )

    assert manifest.ready is False
    assert any("unsupported csa_stage" in item for item in manifest.blockers)
    assert any("reviewed direction" in item for item in manifest.blockers)


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
