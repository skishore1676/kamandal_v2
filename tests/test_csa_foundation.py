from __future__ import annotations

import json
import sqlite3

import pytest

from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.migrations import CSA_TABLES, csa_schema_ready, migrate_csa_database
from kamandal_v2.strategy_lanes.models import (
    ActionDisposition,
    ActionType,
    AdmissionDecision,
    AdmissionStageResult,
    CsaAction,
    LaneId,
    LegEffect,
    LegSide,
    LifecycleState,
    SourceMode,
    StrategyOpportunity,
    StrategyTicket,
    TicketLeg,
)
from kamandal_v2.strategy_lanes.policy import PolicyError, compile_csa_policies, compile_csa_policy
from kamandal_v2.strategy_lanes.registry import LaneRegistry, UnknownLaneError
from kamandal_v2.strategy_lanes.store import CsaStore


def _strangle_policy_row(**overrides):  # noqa: ANN003, ANN202
    row = {
        "playbook_id": "short_strangle_test",
        "enabled": "TRUE",
        "strategy_family": "short_strangle",
        "structure": "short_strangle",
        "csa_stage": "shadow",
        "source_mode": "market_scan",
        "management_policy_json": json.dumps(
            {
                "lifecycle": {
                    "tested_side_confirmation": 2,
                    "roll": {"same_expiry": True, "min_credit": 0.1, "duration_trigger_dte": 21},
                    "adjustment_limit": 1,
                    "inversion": {"allowed": False, "max_width": 5},
                    "cooldown": {"minutes": 15},
                    "loss_stages": {"watch_multiple": 2, "close_multiple": 3},
                    "fill": {"max_attempts": 2, "price_increment": 0.05},
                },
            }
        ),
        "sizing_method": "fixed_contracts",
        "sizing_value": 1,
        "max_contracts": 1,
        "score_weight_credit": 1,
        "score_weight_pop": 1,
        "score_weight_liquidity": 1,
        "score_weight_spread": 1,
        "max_bid_ask_pct": 1,
        "min_option_oi": 1,
        "dte_min": 30,
        "dte_max": 60,
        "short_delta_min": 0.1,
        "short_delta_max": 0.2,
        "iv_rank_min": 35,
        "iv_rank_max": 100,
        "profit_target_pct": 50,
        "exit_dte_min": 21,
        "live_max_bpr_per_order": 2500,
        "universe_expansion_enabled": "TRUE",
        "underlying_price_min": 50,
        "underlying_price_max": 250,
    }
    row.update(overrides)
    return row


def test_csa_policy_compiles_sheet_values_with_stable_provenance() -> None:
    row = _strangle_policy_row()
    first = compile_csa_policy(row, source="google_sheet", read_at="2026-08-08T12:00:00Z")
    second = compile_csa_policy(dict(row), source="google_sheet", read_at="2026-08-08T12:01:00Z")

    assert first is not None and second is not None
    assert first.lane is LaneId.SHORT_STRANGLE
    assert first.source_mode is SourceMode.MARKET_SCAN
    assert first.policy_hash == second.policy_hash
    assert first.resolved_fields["underlying_price_min"] == 50
    assert first.read_at != second.read_at


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"csa_stage": ""}, "none"),
        ({"enabled": "FALSE"}, "enabled=TRUE"),
        ({"source_mode": "idea"}, "incompatible"),
        ({"dte_min": ""}, "missing required Sheet fields"),
        ({"management_policy_json": "not-json"}, "not valid JSON"),
        ({"management_policy_json": "{}"}, "lifecycle must be a non-empty object"),
        ({"underlying_price_max": ""}, "underlying_price_max"),
    ],
)
def test_csa_policy_fails_closed(changes, message) -> None:  # noqa: ANN001
    row = _strangle_policy_row(**changes)
    if message == "none":
        assert compile_csa_policy(row, source="google_sheet", read_at="now") is None
        return
    with pytest.raises(PolicyError, match=message):
        compile_csa_policy(row, source="google_sheet", read_at="now")


def test_csa_policy_compilation_collects_row_errors_without_admitting_them() -> None:
    result = compile_csa_policies(
        [_strangle_policy_row(), _strangle_policy_row(playbook_id="bad", dte_max="")],
        source="google_sheet",
        read_at="now",
    )
    assert len(result.policies) == 1
    assert len(result.errors) == 1
    assert not result.ok


def test_csa_policy_rejects_noncanonical_or_stale_operator_evidence() -> None:
    with pytest.raises(PolicyError, match="must come from google_sheet"):
        compile_csa_policy(_strangle_policy_row(), source="seed", read_at="now")
    with pytest.raises(PolicyError, match="is stale"):
        compile_csa_policy(
            _strangle_policy_row(),
            source="google_sheet",
            read_at="then",
            source_fresh=False,
        )


def test_csa_policy_rejects_duplicate_json_score_weights() -> None:
    row = _strangle_policy_row(
        management_policy_json=json.dumps({"score_weights": {"credit": 1}, "lifecycle": {"hold": True}})
    )
    with pytest.raises(PolicyError, match="existing Sheet columns"):
        compile_csa_policy(row, source="google_sheet", read_at="now")


def test_nonshadow_policy_requires_supported_live_management_mode() -> None:
    row = _strangle_policy_row(csa_stage="pilot_live")
    with pytest.raises(PolicyError, match="live_management_mode=close_only"):
        compile_csa_policy(row, source="google_sheet", read_at="now")

    management = json.loads(row["management_policy_json"])
    management["lifecycle"]["live_management_mode"] = "close_only"
    row["management_policy_json"] = json.dumps(management)

    policy = compile_csa_policy(row, source="google_sheet", read_at="now")
    assert policy is not None
    assert policy.stage.value == "pilot_live"


@pytest.mark.parametrize(
    "changes",
    [
        {"dte_min": "not-a-number"},
        {"iv_rank_min": 101, "iv_rank_max": 20},
        {"max_contracts": 1.5},
        {"score_weight_credit": 0, "score_weight_pop": 0, "score_weight_liquidity": 0, "score_weight_spread": 0},
    ],
)
def test_csa_policy_rejects_invalid_numeric_operator_values(changes) -> None:  # noqa: ANN001
    with pytest.raises(PolicyError):
        compile_csa_policy(_strangle_policy_row(**changes), source="google_sheet", read_at="now")


def test_lane_registry_is_explicit_and_fail_closed() -> None:
    registry = LaneRegistry()
    handler = lambda: "ok"
    registry.register(LaneId.SHORT_STRANGLE, handler)

    assert registry.resolve("short_strangle") is handler
    with pytest.raises(UnknownLaneError):
        registry.resolve("earnings_calendar")
    with pytest.raises(ValueError):
        registry.register(LaneId.SHORT_STRANGLE, handler)


def test_migration_dry_run_preserves_original_database(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    before_bytes = database.read_bytes()

    receipt = migrate_csa_database(database, dry_run=True)

    assert database.read_bytes() == before_bytes
    assert not csa_schema_ready(database)
    assert set(CSA_TABLES).issubset(receipt.after_tables)
    assert receipt.integrity_check == "ok"
    assert receipt.backup_path == ""


def test_migration_is_additive_idempotent_and_backed_up(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO events(event_type, payload) VALUES ('baseline', '{}')")

    first = migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    second = migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")

    assert csa_schema_ready(database)
    assert set(first.before_tables).issubset(first.after_tables)
    assert second.added_tables == ()
    assert first.backup_path and first.backup_sha256
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='baseline'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM csa_schema_migrations").fetchone()[0] == 1


def test_csa_store_persists_typed_records_and_prevents_version_regression(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    store = CsaStore(database)
    opportunity = StrategyOpportunity(
        opportunity_id="opp-1",
        lane=LaneId.SHORT_STRANGLE,
        source_mode=SourceMode.MARKET_SCAN,
        playbook_id="short_strangle_test",
        underlying="XYZ",
        observed_at="2026-08-08T12:00:00Z",
        source_id="scan-1",
        policy_hash="policy-hash",
        evidence={"quote_source": "fixture"},
    )
    decision = AdmissionDecision(
        decision_id="decision-1",
        opportunity_id=opportunity.opportunity_id,
        admitted=True,
        primary_blocker="",
        stages=(AdmissionStageResult("source", True),),
        policy_hash="policy-hash",
        decided_at="2026-08-08T12:01:00Z",
    )
    lifecycle = LifecycleState(
        lifecycle_id="life-1",
        opportunity_id=opportunity.opportunity_id,
        lane=LaneId.SHORT_STRANGLE,
        version=1,
        status="proposed",
        active_legs=(),
        cashflow_ledger=(),
        opened_at="2026-08-08T12:02:00Z",
        updated_at="2026-08-08T12:02:00Z",
        policy_hash="policy-hash",
    )
    action = CsaAction(
        action_id="action-1",
        lifecycle_id=lifecycle.lifecycle_id,
        lifecycle_version=1,
        action_type=ActionType.OPEN,
        disposition=ActionDisposition.SELECTED,
        reason_codes=("admitted",),
        proposed_at="2026-08-08T12:03:00Z",
        priority=1,
    )
    ticket = StrategyTicket(
        ticket_id="ticket-1",
        action_id=action.action_id,
        lifecycle_id=lifecycle.lifecycle_id,
        lifecycle_version=1,
        lane=LaneId.SHORT_STRANGLE,
        underlying="XYZ",
        order_kind="credit",
        limit_price=1.25,
        legs=(TicketLeg("XYZ-option", LegSide.SELL, LegEffect.OPEN, 1, "put", "2026-09-18", 90, "short_put"),),
        policy_hash="policy-hash",
        created_at="2026-08-08T12:04:00Z",
    )

    store.save_opportunity(opportunity, scan_run_id="scan-1")
    store.save_admission_decision(decision)
    store.save_lifecycle(lifecycle)
    store.save_action(action)
    store.save_action(action)
    store.save_shadow_order_intent(ticket)

    assert len(store.rows("csa_opportunities")) == 1
    assert len(store.rows("csa_actions")) == 1
    assert len(store.rows("csa_shadow_order_intents")) == 1
    with pytest.raises(ValueError, match="newer"):
        store.save_lifecycle(
            LifecycleState(
                lifecycle_id="life-1",
                opportunity_id="opp-1",
                lane=LaneId.SHORT_STRANGLE,
                version=0,
                status="stale",
                active_legs=(),
                cashflow_ledger=(),
                opened_at="2026-08-08T12:02:00Z",
                updated_at="2026-08-08T12:05:00Z",
                policy_hash="policy-hash",
            )
        )


def test_csa_store_requires_explicit_migration(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    with pytest.raises(RuntimeError, match="explicit CSA migration"):
        CsaStore(database)
