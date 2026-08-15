from __future__ import annotations

from kamandal_v2.stores.sqlite import LocalStore
import sqlite3
import pytest

from kamandal_v2.strategy_engine.cutover import (
    apply_cutover_fixture,
    build_cutover_manifest,
    build_sheet_mapping_manifest,
    materialize_sheet_mapping,
    rehearse_cutover_on_copy,
    restore_cutover_fixture,
    unified_schedule_manifest,
)
from kamandal_v2.strategy_engine.policy import compile_playbook_policies
from kamandal_v2.strategy_engine.runtime_cutover import (
    _apply_sheet_manifest,
    _restore_sheet_manifest,
    default_earnings_calendar_row,
)
from kamandal_v2.strategy_lanes.store import CsaStore


def _legacy_strangle_policy() -> dict[str, object]:
    return {
        "playbook_id": "legacy_strangle",
        "lane": "short_strangle",
        "stage": "live",
        "source_mode": "market_scan",
        "management": {"lifecycle": {"tested_side_confirmation": 2, "roll": {"min_credit": 0.1, "duration_trigger_dte": 21}, "adjustment_limit": 2, "inversion": {"allowed": False, "max_width": 5}, "cooldown": {"minutes": 30}, "loss_stages": {"watch_multiple": 2, "close_multiple": 3}, "fill": {"max_attempts": 2, "price_increment": 0.05}}},
        "resolved_fields": {"profit_target_pct": 40, "exit_dte_min": 21, "max_loss_multiple": 3},
        "policy_hash": "legacy-policy",
    }


def test_cutover_manifest_is_read_only_and_blocks_ambiguous_shapes(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.save_live_position_group(
        "strangle-1",
        {"compiled_management_policy": _legacy_strangle_policy(), "candidate": {"structure": "short_strangle", "legs": [
            {"side": "sell", "role": "short_put", "option_type": "put", "expiration": "2026-09-25", "strike": 90, "quantity": 1},
            {"side": "sell", "role": "short_call", "option_type": "call", "expiration": "2026-09-25", "strike": 110, "quantity": 1},
        ]}},
    )
    store.save_live_position_group("unknown-1", {"candidate": {"structure": "long_call", "legs": []}})

    manifest = build_cutover_manifest(store)

    assert [item.decision for item in manifest.decisions] == ["create", "block"]
    assert manifest.ready is False
    assert "generic close-only position requires active legs" in manifest.decisions[1].reason


def test_target_schedule_has_one_planning_and_management_owner() -> None:
    schedule = unified_schedule_manifest()

    assert "universe-proposer" in schedule["retire"]
    assert "csa-live-management" in schedule["retire"]
    assert "live-management" in schedule["retire"]
    assert schedule["add"] == ("unified-planning", "unified-lifecycle-management")


class _SheetFixture:
    def __init__(self, values: list[list[object]], *, rows: int = 25, cols: int = 12) -> None:
        self.values = [list(row) for row in values]
        self.rows = rows
        self.cols = cols

    def tab_dimensions(self, _title: str) -> tuple[int, int]:
        return self.rows, self.cols

    def resize_tab(self, _title: str, *, rows: int | None = None, cols: int | None = None) -> None:
        self.rows = rows or self.rows
        self.cols = cols or self.cols

    def batch_update_tab(self, _title: str, updates: list[dict[str, object]]) -> None:
        for update in updates:
            start, _end = str(update["range"]).split(":") if ":" in str(update["range"]) else (str(update["range"]), str(update["range"]))
            column, row = _a1(start)
            for row_offset, values in enumerate(update["values"]):
                target_row = row + row_offset
                while len(self.values) < target_row:
                    self.values.append([])
                for column_offset, value in enumerate(values):
                    target_column = column + column_offset
                    while len(self.values[target_row - 1]) < target_column:
                        self.values[target_row - 1].append("")
                    self.values[target_row - 1][target_column - 1] = value

    def batch_clear_tab(self, _title: str, ranges: list[str]) -> None:
        for raw_range in ranges:
            start, end = raw_range.split(":")
            start_column, start_row = _a1(start)
            end_column, end_row = _a1(end)
            for row in range(start_row, end_row + 1):
                if row > len(self.values):
                    continue
                for column in range(start_column, end_column + 1):
                    if column <= len(self.values[row - 1]):
                        self.values[row - 1][column - 1] = ""

    def read_tab_values(self, _title: str) -> list[list[str]]:
        trimmed = [list(row) for row in self.values]
        while trimmed and not any(str(value) for value in trimmed[-1]):
            trimmed.pop()
        for row in trimmed:
            while row and not str(row[-1]):
                row.pop()
        return [[str(value) for value in row] for row in trimmed]


def _a1(value: str) -> tuple[int, int]:
    letters = "".join(character for character in value if character.isalpha())
    digits = "".join(character for character in value if character.isdigit())
    column = 0
    for character in letters:
        column = column * 26 + ord(character.upper()) - 64
    return column, int(digits)


def test_runtime_sheet_writer_and_rollback_are_bounded() -> None:
    header = [
        "playbook_id", "enabled", "strategy_family", "structure", "csa_stage", "source_mode",
        "management_policy_json", "applicable_direction", "dte_min", "dte_max", "long_dte_min", "long_dte_max",
    ]
    source_values = [
        header,
        ["call_calendar_low_iv", "TRUE", "call_calendar", "call_calendar", "baseline", "", '{"event_expiration":{"near_before_days":2}}', "bullish", "5", "7", "45", "60"],
    ]
    rows = [dict(zip(header, source_values[1], strict=True))]
    earnings = default_earnings_calendar_row(rows)
    manifest = build_sheet_mapping_manifest(header, rows, earnings_calendar_row=earnings)
    target_header, target_rows = materialize_sheet_mapping(manifest, rows)
    client = _SheetFixture(source_values, rows=100, cols=len(header))

    _apply_sheet_manifest(
        client,
        title="playbooks",
        source_header=tuple(header),
        source_rows=rows,
        target_header=target_header,
        target_rows=target_rows,
        manifest=manifest,
    )
    _restore_sheet_manifest(
        client,
        title="playbooks",
        source_header=tuple(header),
        source_rows=rows,
        manifest=manifest,
        original_row_count=100,
        original_col_count=len(header),
    )

    assert client.read_tab_values("playbooks") == [[str(value) for value in row] for row in source_values]
    assert client.tab_dimensions("playbooks") == (100, len(header))


def test_sheet_mapping_is_bounded_non_applying_and_preserves_generic_calendar_rows() -> None:
    header = [
        "playbook_id", "enabled", "strategy_family", "structure", "csa_stage", "management_policy_json",
        "profit_target_pct", "short_delta_min", "short_delta_max", "source_mode",
        "applicable_direction", "dte_min", "dte_max", "long_dte_min", "long_dte_max",
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
        {
            "playbook_id": "directional", "enabled": "TRUE", "strategy_family": "call_diagonal",
            "structure": "call_diagonal", "csa_stage": "baseline", "source_mode": "idea",
            "management_policy_json": '{"lifecycle":{"fill":{"max_attempts":4,"price_increment":0.05},"long_only":{"requires_approval":true},"short_leg":{"roll":true}}}',
        },
    ]
    original = [dict(row) for row in rows]

    manifest = build_sheet_mapping_manifest(
        header,
        rows,
        earnings_calendar_row={
            "playbook_id": "earnings_calendar_directional", "strategy_family": "earnings_calendar",
            "structure": "call_calendar", "applicable_direction": "bullish,bearish", "dte_min": 5, "dte_max": 7,
            "long_dte_min": 45, "long_dte_max": 60, "event_timing": "confirmed_bmo_or_amc_final_pre_event_session",
            "event_near_expiry_after_days": 1, "paired_order_required": "TRUE", "post_event_exit": "first_eligible_post_event_session",
            "mode": "live", "enabled": "TRUE",
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
    assert changes[("directional", "management_policy_json")] == '{"lifecycle":{"fill":{"max_attempts":4,"price_increment":0.05}}}'
    assert manifest.row_additions[0].start_row == 5
    assert manifest.row_additions[0].values["enabled"] == "TRUE"
    assert manifest.row_additions[0].values["mode"] == "live"
    assert "rendered locally only" in manifest.row_additions[0].reason

    _target_header, materialized = materialize_sheet_mapping(manifest, rows)
    compiled = compile_playbook_policies(materialized)
    assert compiled.ok
    earnings = next(policy for policy in compiled.policies if policy.capability.key == "earnings_calendar")
    assert earnings.mode.value == "live"


def test_sheet_mapping_blocks_unreviewed_earnings_row_and_invalid_stage() -> None:
    manifest = build_sheet_mapping_manifest(
        ["playbook_id", "enabled", "strategy_family", "structure", "csa_stage"],
        [{"playbook_id": "bad", "enabled": "TRUE", "strategy_family": "short_put", "structure": "short_put", "csa_stage": "mystery"}],
    )

    assert manifest.ready is False
    assert any("unsupported csa_stage" in item for item in manifest.blockers)
    assert any("reviewed direction" in item for item in manifest.blockers)


def test_earnings_manifest_refuses_a_shadow_or_disabled_substitute() -> None:
    row = {
        "playbook_id": "earnings_calendar_directional", "strategy_family": "earnings_calendar",
        "structure": "put_calendar", "applicable_direction": "bullish,bearish", "dte_min": 5, "dte_max": 7,
        "long_dte_min": 45, "long_dte_max": 60, "event_timing": "confirmed_bmo_or_amc_final_pre_event_session",
        "event_near_expiry_after_days": 1, "paired_order_required": "TRUE", "post_event_exit": "first_eligible_post_event_session",
        "mode": "shadow", "enabled": "FALSE",
    }

    with pytest.raises(ValueError, match="mode=live"):
        build_sheet_mapping_manifest(
            ["playbook_id", "enabled", "strategy_family", "structure"],
            [],
            earnings_calendar_row=row,
        )


def test_fixture_cutover_apply_is_idempotent_and_restore_rehearses(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    store = LocalStore(database)
    store.save_live_position_group(
        "strangle-1",
        {"compiled_management_policy": _legacy_strangle_policy(), "candidate": {"structure": "short_strangle", "legs": [
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


def test_legacy_open_group_adopts_complete_sheet_policy_and_entry_economics(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    store = LocalStore(database)
    store.save_live_position_group(
        "spread-1",
        {
            "underlying": "XYZ",
            "playbook_id": "put_spread_default",
            "candidate_id": "candidate-1",
            "idea_id": "idea-1",
            "plan_id": "plan-1",
            "candidate": {
                "underlying": "XYZ",
                "playbook_id": "put_spread_default",
                "structure": "put_spread",
                "net_credit": 1.55,
                "legs": [
                    {"side": "buy", "role": "long_put", "option_type": "put", "expiration": "2026-09-18", "strike": 95, "quantity": 1},
                    {"side": "sell", "role": "short_put", "option_type": "put", "expiration": "2026-09-18", "strike": 100, "quantity": 1},
                ],
            },
        },
    )
    playbook_rows = [
        {
            "playbook_id": "put_spread_default",
            "enabled": "TRUE",
            "strategy_family": "put_spread",
            "structure": "put_spread",
            "mode": "live",
            "source_mode": "idea",
            "profit_target_pct": "50",
            "max_loss_multiple": "1.5",
            "exit_dte_min": "21",
            "management_policy_json": "{}",
        }
    ]

    manifest = build_cutover_manifest(store, playbook_rows=playbook_rows)
    receipt = apply_cutover_fixture(
        database,
        backup_dir=tmp_path / "backups",
        allow_fixture_apply=True,
        playbook_rows=playbook_rows,
    )
    lifecycle = CsaStore(database).lifecycle("adopt:spread-1")

    assert manifest.ready
    assert receipt.created_lifecycle_ids == ("adopt:spread-1",)
    assert lifecycle is not None
    assert lifecycle.metadata["execution_mode"] == "live"
    assert lifecycle.metadata["underlying"] == "XYZ"
    assert lifecycle.metadata["playbook_id"] == "put_spread_default"
    assert lifecycle.metadata["policy_at_adoption"] is True
    assert lifecycle.metadata["compiled_management_policy"]["policy_hash"] == lifecycle.policy_hash
    assert lifecycle.metadata["cumulative_cashflow"] == 1.55
    assert lifecycle.cashflow_ledger[0]["amount"] == 1.55


def test_production_shaped_runner_can_only_apply_to_a_fresh_copy(tmp_path) -> None:
    source = tmp_path / "source.db"
    store = LocalStore(source)
    store.save_live_position_group(
        "strangle-1",
        {"compiled_management_policy": _legacy_strangle_policy(), "candidate": {"structure": "short_strangle", "legs": [
            {"side": "sell", "role": "short_put", "option_type": "put", "expiration": "2026-09-25", "strike": 90, "quantity": 1},
            {"side": "sell", "role": "short_call", "option_type": "call", "expiration": "2026-09-25", "strike": 110, "quantity": 1},
        ]}},
    )

    receipt = rehearse_cutover_on_copy(
        source,
        work_database=tmp_path / "work.db",
        backup_dir=tmp_path / "backups",
        apply=True,
    )

    assert receipt.apply_receipt is not None
    assert receipt.verify_integrity_check == "ok"
    assert receipt.rollback_verified is True
    assert source.read_bytes() == (tmp_path / "work.db").read_bytes()
