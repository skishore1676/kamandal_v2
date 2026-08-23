from __future__ import annotations

from kamandal_v2.strategy_engine.history import HISTORY_SCHEMA_VERSION, history_record, lifecycle_history
from kamandal_v2.strategy_lanes.models import LaneId, LifecycleState
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.strategy_lanes.store import CsaStore
from kamandal_v2.stores.sqlite import LocalStore


def _lifecycle(*, status: str, metadata: dict) -> LifecycleState:  # noqa: ANN001
    return LifecycleState(
        lifecycle_id="life-1",
        opportunity_id="opp-1",
        lane=LaneId.SHORT_STRANGLE,
        version=2,
        status=status,
        active_legs=(),
        cashflow_ledger=({"amount": 1.25, "filled_at": "2026-08-14T15:00:00Z"},),
        opened_at="2026-08-14T15:00:00Z",
        updated_at="2026-08-14T15:05:00Z",
        policy_hash="hash-1",
        metadata=metadata,
    )


def test_history_distinguishes_open_marks_from_realized_economics() -> None:
    open_record = history_record(_lifecycle(status="open", metadata={"policy": {"hash": "hash-1"}, "mark_pnl_price": 0.5, "mark_source": "natural_close_quote"}))
    closed_record = history_record(_lifecycle(status="closed", metadata={"policy": {"hash": "hash-1"}, "realized_pnl_price": 1.25}))

    assert open_record["schema_version"] == HISTORY_SCHEMA_VERSION
    assert open_record["economics"]["state"] == "open_mark"
    assert open_record["economics"]["realized_pnl_price"] is None
    assert closed_record["economics"]["state"] == "realized"
    assert closed_record["economics"]["mark_pnl_price"] is None


def test_history_calls_missing_provenance_an_evidence_gap_not_zero_pnl() -> None:
    record = history_record(_lifecycle(status="open", metadata={}))

    assert record["economics"]["cashflow_total"] == 1.25
    assert record["evidence_quality"] == "incomplete"
    assert "compiled_policy" in record["evidence_limitations"]
    assert "open_mark" in record["evidence_limitations"]


def test_adopted_lifecycle_history_joins_reconciliation_without_typed_ticket(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    runtime_store = LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    lifecycle = _lifecycle(
        status="open",
        metadata={
            "execution_mode": "live",
            "position_projection_id": "legacy-group-1",
            "policy": {"hash": "hash-1"},
            "mark_pnl_price": 0.5,
        },
    )
    CsaStore(database).save_lifecycle(lifecycle)
    runtime_store.save_live_reconciliation_issue(
        {
            "issue_id": "legacy-reconciliation",
            "issue_type": "adopted_position_observed",
            "group_id": "legacy-group-1",
            "underlying": "AAPL",
            "status": "resolved",
        }
    )

    record = lifecycle_history(CsaStore(database, read_only=True), lifecycle_id=lifecycle.lifecycle_id)[0]

    assert record["tickets"] == []
    assert record["reconciliation"][0]["issue_id"] == "legacy-reconciliation"
