from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kamandal_v2.domain.models import PortfolioState
from kamandal_v2.live.health import entry_health_gate, run_live_health
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.tools.launchd_job import health_attention


def _make_open_group_with_mark(
    store: LocalStore,
    group_id: str,
    *,
    loss_watch: bool = False,
    target_progress: float = 0.0,
    trigger_progress: float = 100.0,
    pnl_mid: float | None = None,
    pnl_natural: float = 0.0,
    execution_venue: str = "public_primary",
) -> None:
    store.save_live_position_group(
        group_id,
        {
            "group_id": group_id,
            "underlying": "AAPL",
            "playbook_id": "call_spread_test",
            "structure": "call_spread",
            "execution_venue": execution_venue,
            "candidate": {
                "candidate_id": "cand_1",
                "idea_id": "idea_1",
                "playbook_id": "call_spread_test",
                "underlying": "AAPL",
                "execution_venue": execution_venue,
            },
        },
    )
    store.record_live_position_mark(
        group_id,
        {
            "underlying": "AAPL",
            "entry_kind": "credit",
            "pnl_mid": target_progress if pnl_mid is None else pnl_mid,
            "pnl_natural": pnl_natural,
            "target_profit": 100.0,
            "target_progress_pct": target_progress,
            "trigger_progress_pct": trigger_progress,
            "target_reached": False,
            "loss_watch": bool(loss_watch),
            "loss_watch_observations": {"count": 2} if loss_watch else {"count": 0},
            "max_loss_watch": bool(loss_watch),
        },
    )


def test_live_health_green_for_clean_book(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_green", target_progress=20.0, trigger_progress=100.0)

    report = run_live_health(store)

    assert report["overall"] == "GREEN"
    assert report["counts"]["open_groups"] == 1
    assert report["counts"]["working_close_orders"] == 0
    assert report["counts"]["reconciliation_blockers"] == 0
    assert report["counts"]["loss_watch_groups"] == 0
    assert report["reasons"] == []


def test_live_health_prefers_fresh_canonical_lifecycle_mark(tmp_path: Path) -> None:
    database = tmp_path / "kamandal_v2.db"
    store = LocalStore(database)
    _make_open_group_with_mark(store, "group_canonical", target_progress=150.0, trigger_progress=100.0)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    lifecycle = {
        "lifecycle_id": "adopt:group_canonical",
        "opportunity_id": "fixture",
        "lane": "generic_close_only",
        "version": 1,
        "status": "open",
        "active_legs": [],
        "cashflow_ledger": [{"amount": -2.0}],
        "opened_at": "2026-08-18T14:00:00Z",
        "updated_at": "2026-08-18T20:15:00Z",
        "policy_hash": "fixture-policy",
        "metadata": {
            "execution_mode": "live",
            "legacy_source_id": "group_canonical",
            "underlying": "AAPL",
            "active_cost_basis": 2.0,
            "contract_multiplier": 100,
            "mark_pnl_price": 0.1,
            "mark_profit_pct": 5.0,
            "last_marked_at": "2026-08-18T20:15:00Z",
            "compiled_management_policy": {"resolved_fields": {"profit_target_pct": "50"}},
        },
    }
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO csa_lifecycles (id, opportunity_id, lane, version, status, opened_at, updated_at, policy_hash, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lifecycle["lifecycle_id"], lifecycle["opportunity_id"], lifecycle["lane"],
                lifecycle["version"], lifecycle["status"], lifecycle["opened_at"],
                lifecycle["updated_at"], lifecycle["policy_hash"], json.dumps(lifecycle),
            ),
        )

    report = run_live_health(store)

    assert report["counts"]["target_reached_groups"] == 0
    assert report["group_marks"][0]["mark_source"] == "canonical_lifecycle"
    assert report["group_marks"][0]["profit_pct"] == 5.0
    assert report["group_marks"][0]["target_progress_pct"] == 10.0


def test_mandatory_exit_invalid_quote_escalates_after_stall_deadline(tmp_path: Path) -> None:
    database = tmp_path / "kamandal_v2.db"
    store = LocalStore(database)
    _make_open_group_with_mark(store, "group_due", target_progress=0.0)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    lifecycle = {
        "lifecycle_id": "adopt:group_due",
        "opportunity_id": "fixture",
        "lane": "call_vertical",
        "version": 1,
        "status": "open",
        "active_legs": [],
        "cashflow_ledger": [{"amount": 1.0}],
        "opened_at": "2026-08-21T14:00:00Z",
        "updated_at": "2026-08-21T15:30:00Z",
        "policy_hash": "fixture-policy",
        "metadata": {
            "execution_mode": "live",
            "legacy_source_id": "group_due",
            "underlying": "AAPL",
            "cumulative_cashflow": 1.0,
            "contract_multiplier": 100,
            "mark_pnl_price": -0.2,
            "mark_natural_pnl_price": -0.4,
            "mark_profit_pct": -20,
            "last_marked_at": "2026-08-21T15:30:00Z",
            "mark_source": "validated_midpoint_package",
            "mark_quote_actionable": False,
            "mark_quote_blockers": ["spread_exceeds_frozen_policy"],
            "mark_selected_reason": "time_exit",
            "mark_selected_reason_class": "time_decision",
            "mark_execution_status": "waiting_valid_quote",
            "mark_waiting_valid_quote_since": "2026-08-21T15:00:00Z",
            "compiled_management_policy": {"resolved_fields": {"profit_target_pct": "50"}},
        },
    }
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO csa_lifecycles (id, opportunity_id, lane, version, status, opened_at, updated_at, policy_hash, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lifecycle["lifecycle_id"], lifecycle["opportunity_id"], lifecycle["lane"],
                lifecycle["version"], lifecycle["status"], lifecycle["opened_at"],
                lifecycle["updated_at"], lifecycle["policy_hash"], json.dumps(lifecycle),
            ),
        )

    report = run_live_health(
        store,
        {"live": {"health": {"exit_pipeline_stalled_minutes": 20}}},
        now=datetime(2026, 8, 21, 15, 31, tzinfo=UTC),
        allow_mutation=False,
    )

    assert report["overall"] == "RED"
    assert "mandatory_exit_quote_stalled" in report["reasons"]
    event = next(item for item in report["events"] if item["reason"] == "mandatory_exit_quote_stalled")
    assert event["age_minutes"] == 31.0
    assert event["operator_state"] == "operator_needed"
    assert health_attention(report)["notify"] is True


def test_live_health_does_not_page_for_midpoint_only_profit_target(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(
        store,
        "group_midpoint_only",
        target_progress=146.0,
        trigger_progress=95.0,
        pnl_natural=-21.0,
    )

    report = run_live_health(
        store,
        {
            "live": {
                "exit_approval_mode": "auto_rules",
                "exit_pricing": {"profit_target_trigger_pct": 95, "min_profit_to_trigger": 5},
            }
        },
    )

    assert report["overall"] == "GREEN"
    assert report["counts"]["target_reached_groups"] == 0
    assert "position_target_reached" not in report["reasons"]


def test_live_health_reports_executable_profit_target_as_self_healing(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(
        store,
        "group_executable_target",
        target_progress=100.0,
        trigger_progress=95.0,
        pnl_natural=6.0,
    )

    report = run_live_health(
        store,
        {
            "live": {
                "exit_approval_mode": "auto_rules",
                "exit_pricing": {"profit_target_trigger_pct": 95, "min_profit_to_trigger": 5},
            }
        },
    )

    assert report["overall"] == "YELLOW"
    assert report["counts"]["target_reached_groups"] == 1
    event = next(item for item in report["events"] if item["reason"] == "position_target_reached")
    assert event["operator_state"] == "self_healing"


def test_live_health_red_when_bpr_exceeds_hard_cap(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_risk", target_progress=20.0, trigger_progress=100.0)
    store.save_account_snapshot(
        "run_risk",
        PortfolioState(account_size=10_000, buying_power=3_500, bpr_used=6_500, positions_count=12),
    )

    report = run_live_health(
        store,
        {"portfolio": {"target_max_bpr_utilization_pct": 55, "hard_max_bpr_utilization_pct": 60}},
    )

    assert report["overall"] == "RED"
    assert report["scale"]["score"] == 25
    assert report["risk"]["bpr_used_pct"] == 65.0
    assert "portfolio_bpr_over_hard_cap" in report["reasons"]
    assert entry_health_gate(store, {"portfolio": {"hard_max_bpr_utilization_pct": 60}})["blocked"] is True


def test_live_health_yellow_for_pending_entry_approvals(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_pending", target_progress=20.0, trigger_progress=100.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "pending-open-ticket",
            "order_id": "order-pending-open",
            "plan_id": "plan-pending",
            "candidate_id": "cand-pending",
            "idea_id": "idea-pending",
            "intent_type": "open",
            "underlying": "AAPL",
        },
        status="pending_approval",
    )

    report = run_live_health(store)

    assert report["overall"] == "YELLOW"
    assert report["counts"]["pending_entry_approvals"] == 1
    assert report["scale"]["score"] == 70
    assert "pending_entry_approvals" in report["reasons"]
    assert report["events"][0]["operator_state"] == "operator_needed"


def test_live_health_ignores_lower_rank_auto_plan_alternatives(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_pending", target_progress=20.0, trigger_progress=100.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "pending-open-ticket",
            "order_id": "order-pending-open",
            "plan_id": "plan-pending",
            "candidate_id": "cand-pending",
            "idea_id": "idea-pending",
            "intent_type": "open",
            "underlying": "AAPL",
            "plan_rank": 2,
        },
        status="pending_approval",
    )

    report = run_live_health(store, {"live": {"entry_approval_mode": "auto_top_plan"}})

    assert report["overall"] == "GREEN"
    assert report["counts"]["pending_entry_approvals"] == 0
    assert "pending_entry_approvals" not in report["reasons"]


def test_live_health_keeps_rank_one_auto_plan_pending_visible(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_pending", target_progress=20.0, trigger_progress=100.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "pending-rank-one",
            "order_id": "order-rank-one",
            "plan_id": "plan-rank-one",
            "candidate_id": "candidate-rank-one",
            "idea_id": "idea-rank-one",
            "intent_type": "open",
            "underlying": "AAPL",
            "plan_rank": 1,
        },
        status="pending_approval",
    )

    report = run_live_health(store, {"live": {"entry_approval_mode": "auto_top_plan"}})

    assert report["overall"] == "YELLOW"
    pending = next(event for event in report["events"] if event["reason"] == "pending_entry_approvals")
    assert pending["operator_state"] == "self_handled"


def test_live_health_self_retires_prior_day_pending_entry_approvals(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_self_heal", target_progress=20.0, trigger_progress=100.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "old-pending-open-ticket",
            "order_id": "old-order-pending-open",
            "plan_id": "old-plan-pending",
            "candidate_id": "old-cand-pending",
            "idea_id": "old-idea-pending",
            "intent_type": "open",
            "underlying": "AAPL",
        },
        status="pending_approval",
    )
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute(
            "UPDATE live_order_intents SET updated_at = ?, created_at = ? WHERE ticket_hash = ?",
            ("2000-01-01 00:00:00", "2000-01-01 00:00:00", "old-pending-open-ticket"),
        )

    report = run_live_health(store, {"runtime": {"market_timezone": "America/Chicago"}})

    assert report["overall"] == "GREEN"
    assert report["counts"]["pending_entry_approvals"] == 0
    assert report["self_healing"]["entry_approvals_retired"] == 1
    assert report["self_healing"]["entry_approval_rows"][0]["reason"] == "stale_entry_approval_from_prior_market_day"
    assert store.live_order_intent("old-pending-open-ticket")["_ledger_status"] == "retired_stale_entry_approval"


def test_live_health_yellow_for_working_close_order(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_yellow", target_progress=20.0, trigger_progress=100.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "close-ticket-yellow",
            "order_id": "order-close-yellow",
            "plan_id": "plan-yellow",
            "candidate_id": "cand-yellow",
            "idea_id": "idea-yellow",
            "group_id": "group_yellow",
            "intent_type": "close",
            "underlying": "AAPL",
        },
        status="submitted",
    )

    report = run_live_health(store)

    assert report["overall"] == "YELLOW"
    assert report["counts"]["working_close_orders"] == 1
    assert "working_close_order" in report["reasons"]
    assert any(event["reason"] == "working_close_order" for event in report["events"])


def test_live_health_keeps_old_working_resting_profit_target_quiet(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_resting", target_progress=25.0, trigger_progress=100.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "resting-target",
            "order_id": "order-resting-target",
            "plan_id": "plan-resting",
            "candidate_id": "cand-resting",
            "idea_id": "idea-resting",
            "group_id": "group_resting",
            "intent_type": "close",
            "underlying": "AAPL",
            "resting_profit_order": True,
            "exit_reason_class": "resting_profit",
        },
        status="submitted",
    )
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute(
            "UPDATE live_order_intents SET updated_at = ?, created_at = ? WHERE ticket_hash = ?",
            ("2000-01-01 00:00:00", "2000-01-01 00:00:00", "resting-target"),
        )

    report = run_live_health(store, stale_close_order_minutes=1)

    assert report["overall"] == "GREEN"
    assert report["counts"]["working_resting_profit_orders"] == 1
    assert report["counts"]["stale_close_orders"] == 0
    assert report["reasons"] == []
    assert report["close_orders"][0]["reason"] == "working_resting_profit_order"


@pytest.mark.parametrize(
    "terminal_status",
    ["canceled", "cancelled", "expired", "expired_broker_status_missing", "expired_eod"],
)
def test_live_health_treats_unfilled_resting_profit_day_order_as_normal(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_resting_done", target_progress=5.0, trigger_progress=100.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "resting-target-done",
            "order_id": "order-resting-target-done",
            "plan_id": "plan-resting-done",
            "candidate_id": "cand-resting-done",
            "idea_id": "idea-resting-done",
            "group_id": "group_resting_done",
            "intent_type": "close",
            "underlying": "AAPL",
            "resting_profit_order": True,
            "csa_action_reason_class": "resting_profit",
        },
        status=terminal_status,
    )

    report = run_live_health(store)

    assert report["overall"] == "GREEN"
    assert report["counts"]["failed_close_orders"] == 0
    assert report["counts"]["resting_profit_orders_unfilled"] == 1
    assert report["reasons"] == []
    assert report["close_orders"][0]["reason"] == "resting_profit_order_unfilled"


def test_live_health_still_pages_rejected_resting_profit_target(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_resting_rejected", target_progress=25.0, trigger_progress=100.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "resting-target-rejected",
            "order_id": "order-resting-target-rejected",
            "plan_id": "plan-resting-rejected",
            "candidate_id": "cand-resting-rejected",
            "idea_id": "idea-resting-rejected",
            "group_id": "group_resting_rejected",
            "intent_type": "close",
            "underlying": "AAPL",
            "resting_profit_order": True,
        },
        status="rejected",
    )

    report = run_live_health(store)

    assert report["overall"] == "RED"
    assert report["counts"]["failed_close_orders"] == 1
    assert "failed_close_order" in report["reasons"]


def test_live_health_follows_replacement_child_when_cancelled_parent_updated_later(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_replaced", target_progress=20.0, trigger_progress=100.0)
    parent = {
        "ticket_hash": "close-parent",
        "order_id": "order-close-parent",
        "plan_id": "plan-replaced",
        "candidate_id": "cand-replaced",
        "idea_id": "idea-replaced",
        "group_id": "group_replaced",
        "intent_type": "close",
        "underlying": "AAPL",
    }
    child = {
        **parent,
        "ticket_hash": "close-child",
        "order_id": "order-close-child",
        "parent_ticket_hash": "close-parent",
        "replace_method": "staged_cancel",
    }
    store.save_live_order_intent(parent, status="cancelled")
    store.save_live_order_intent(child, status="submitted")
    child_updated_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    parent_updated_at = child_updated_at + timedelta(seconds=4)
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute(
            "UPDATE live_order_intents SET updated_at = ? WHERE ticket_hash = ?",
            (parent_updated_at.isoformat(sep=" "), "close-parent"),
        )
        conn.execute(
            "UPDATE live_order_intents SET updated_at = ? WHERE ticket_hash = ?",
            (child_updated_at.isoformat(sep=" "), "close-child"),
        )

    report = run_live_health(store)

    assert report["overall"] == "YELLOW"
    assert report["counts"]["failed_close_orders"] == 0
    assert report["counts"]["working_close_orders"] == 1
    parent_finding = next(item for item in report["close_orders"] if item["ticket_hash"] == "close-parent")
    child_finding = next(item for item in report["close_orders"] if item["ticket_hash"] == "close-child")
    assert parent_finding["reason"] == "superseded_close_order"
    assert child_finding["reason"] == "working_close_order"


def test_live_health_red_for_stale_urgent_close_order(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_urgent", target_progress=20.0, trigger_progress=100.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "close-ticket-urgent",
            "order_id": "order-close-urgent",
            "plan_id": "plan-urgent",
            "candidate_id": "cand-urgent",
            "idea_id": "idea-urgent",
            "group_id": "group_urgent",
            "intent_type": "close",
            "underlying": "AAPL",
            "exit_reason": "max_loss",
        },
        status="submitted",
    )
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute(
            "UPDATE live_order_intents SET updated_at = ?, created_at = ? WHERE ticket_hash = ?",
            ("2000-01-01 00:00:00", "2000-01-01 00:00:00", "close-ticket-urgent"),
        )

    report = run_live_health(store, {"live": {"health": {"urgent_close_order_stale_minutes": 1}}})

    assert report["overall"] == "RED"
    assert report["counts"]["urgent_close_orders"] == 1
    assert "urgent_close_order_stale" in report["reasons"]
    assert any(event["reason"] == "urgent_close_order_stale" for event in report["events"])


def test_live_health_distinguishes_pending_close_pipeline_from_working_broker_order(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_pending_close", target_progress=100.0, trigger_progress=95.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "close-ticket-pipeline",
            "order_id": "order-close-pipeline",
            "plan_id": "plan-pipeline",
            "candidate_id": "cand-pipeline",
            "idea_id": "idea-pipeline",
            "group_id": "group_pending_close",
            "intent_type": "close",
            "underlying": "AAPL",
        },
        status="approved_close_pending_submit",
    )

    report = run_live_health(store)

    assert report["overall"] == "YELLOW"
    assert report["counts"]["working_close_orders"] == 0
    assert report["counts"]["exit_pipeline_pending"] == 1
    assert "exit_pipeline_pending" in report["reasons"]


def test_live_health_treats_legacy_reprice_failure_as_broker_working(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_legacy_reprice", target_progress=100.0, trigger_progress=95.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "close-ticket-legacy-reprice",
            "order_id": "order-close-legacy-reprice",
            "plan_id": "plan-legacy-reprice",
            "candidate_id": "cand-legacy-reprice",
            "idea_id": "idea-legacy-reprice",
            "group_id": "group_legacy_reprice",
            "intent_type": "close",
            "underlying": "XLF",
        },
        status="reprice_blocked_preflight_failed",
    )

    report = run_live_health(store)

    assert report["overall"] == "YELLOW"
    assert report["counts"]["working_close_orders"] == 1
    assert report["counts"]["failed_close_orders"] == 0
    assert "working_close_order" in report["reasons"]


def test_live_health_red_for_stalled_ledger_close_pipeline(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_stalled_close", target_progress=100.0, trigger_progress=95.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "close-ticket-stalled",
            "order_id": "order-close-stalled",
            "plan_id": "plan-stalled",
            "candidate_id": "cand-stalled",
            "idea_id": "idea-stalled",
            "group_id": "group_stalled_close",
            "intent_type": "close",
            "underlying": "AAPL",
        },
        status="approved_close_pending_submit",
    )
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute(
            "UPDATE live_order_intents SET updated_at = ?, created_at = ? WHERE ticket_hash = ?",
            ("2000-01-01 00:00:00", "2000-01-01 00:00:00", "close-ticket-stalled"),
        )

    report = run_live_health(store, {"live": {"health": {"exit_pipeline_stalled_minutes": 1}}})

    assert report["overall"] == "RED"
    assert report["counts"]["exit_pipeline_stalled"] == 1
    assert "exit_pipeline_stalled" in report["reasons"]


def test_live_health_red_for_reconciliation_or_loss_watch(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_red", loss_watch=True, target_progress=20.0, trigger_progress=100.0)
    store.save_live_reconciliation_issue(
        {
            "issue_id": "rec-1",
            "issue_type": "broker_qty_mismatch",
            "group_id": "group_red",
            "underlying": "AAPL",
            "status": "open",
        },
    )

    report = run_live_health(store)

    assert report["overall"] == "RED"
    assert report["counts"]["reconciliation_blockers"] == 1
    assert report["counts"]["loss_watch_groups"] == 1
    assert report["reasons"][0] == "reconciliation_blocker"
    assert any(event["reason"] == "loss_watch" for event in report["events"])


def test_live_health_ignores_pending_confirmation_reconciliation_issue(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_pending", target_progress=20.0, trigger_progress=100.0)
    store.save_live_reconciliation_issue(
        {
            "issue_id": "rec-pending",
            "issue_type": "ghost_local_position",
            "group_id": "group_pending",
            "underlying": "AAPL",
            "status": "open",
            "decision": {
                "tier": "pending_confirmation",
                "action": "retire_local",
                "reason": "close_filled_waiting_for_broker_flat_confirmation",
            },
        },
    )

    report = run_live_health(store)

    assert report["overall"] == "GREEN"
    assert report["counts"]["reconciliation_blockers"] == 0
    assert "reconciliation_blocker" not in report["reasons"]


def test_live_health_ignores_close_failures_for_non_open_groups(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_green", target_progress=20.0, trigger_progress=100.0)
    store.save_live_order_intent(
        {
            "ticket_hash": "old-close-ticket",
            "order_id": "old-close-order",
            "plan_id": "old-plan",
            "candidate_id": "old-candidate",
            "idea_id": "old-idea",
            "group_id": "closed_group",
            "intent_type": "close",
            "underlying": "AAPL",
        },
        status="blocked_preflight_failed",
    )

    report = run_live_health(store)

    assert report["overall"] == "GREEN"
    assert report["counts"]["failed_close_orders"] == 0
    assert report["close_orders"] == []


def test_cli_live_health_uses_json_output(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    import importlib

    if "requests" not in sys.modules:
        sys.modules["requests"] = types.ModuleType("requests")
    main_module = importlib.import_module("kamandal_v2.cli")
    main = main_module.main

    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_cli", target_progress=20.0, trigger_progress=100.0)
    monkeypatch.setattr("kamandal_v2.cli.LocalStore", lambda *args, **kwargs: store)
    monkeypatch.setattr("kamandal_v2.cli.load_control", lambda: {})
    monkeypatch.setattr("kamandal_v2.cli.build_seed_tables", lambda _config: {})
    monkeypatch.setattr("sys.argv", ["kamandal", "live-health", "--json"])

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["overall"] == "GREEN"
    assert payload["counts"]["open_groups"] == 1


def _make_red_book(store: LocalStore) -> None:
    _make_open_group_with_mark(store, "group_red", target_progress=20.0, trigger_progress=100.0)
    store.save_live_reconciliation_issue(
        {
            "issue_id": "rec-gate",
            "issue_type": "broker_qty_mismatch",
            "group_id": "group_red",
            "underlying": "AAPL",
            "status": "open",
        },
    )


def test_entry_health_gate_blocks_on_red(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_red_book(store)

    gate = entry_health_gate(store, {})

    assert gate["overall"] == "RED"
    assert gate["blocked"] is True
    assert "reconciliation_blocker" in gate["reasons"]


def test_entry_health_gate_respects_disable_flag(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_red_book(store)

    gate = entry_health_gate(store, {"live": {"health": {"block_entries_on_red": False}}})

    assert gate["overall"] == "RED"
    assert gate["blocked"] is False


def test_entry_health_gate_allows_green_book(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_green", target_progress=20.0, trigger_progress=100.0)

    gate = entry_health_gate(store, {})

    assert gate["overall"] == "GREEN"
    assert gate["blocked"] is False


def _failed_close_intent(
    store: LocalStore,
    group_id: str,
    ticket_hash: str,
    *,
    execution_venue: str = "public_primary",
) -> None:
    store.save_live_order_intent(
        {
            "ticket_hash": ticket_hash,
            "order_id": f"order-{ticket_hash}",
            "plan_id": "plan-stale",
            "candidate_id": "cand-stale",
            "idea_id": "idea-stale",
            "group_id": group_id,
            "intent_type": "close",
            "underlying": "AAPL",
            "execution_venue": execution_venue,
        },
        status="blocked_preflight_failed",
    )


def test_failed_close_demoted_to_yellow_when_newer_decision_is_no_exit(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_stale", target_progress=20.0, trigger_progress=100.0)
    _failed_close_intent(store, "group_stale", "stale-close-ticket")
    store.record_live_management_decision(
        "group_stale",
        "hold",
        "no_exit",
        {"group_id": "group_stale", "action": "hold", "reason": "no_exit"},
    )

    report = run_live_health(store)

    assert report["overall"] == "YELLOW"
    assert report["counts"]["failed_close_orders"] == 0
    assert report["counts"]["stale_failed_close_orders"] == 1
    assert any(event["reason"] == "stale_failed_close_order" for event in report["events"])
    assert entry_health_gate(store, {})["blocked"] is False


def test_failed_close_stays_red_without_newer_no_exit_decision(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_hot", target_progress=20.0, trigger_progress=100.0)
    store.record_live_management_decision(
        "group_hot",
        "close",
        "max_loss",
        {"group_id": "group_hot", "action": "close", "reason": "max_loss"},
    )
    _failed_close_intent(store, "group_hot", "hot-close-ticket")

    report = run_live_health(store)

    assert report["overall"] == "RED"
    assert report["counts"]["failed_close_orders"] == 1
    assert entry_health_gate(store, {})["blocked"] is True


def test_public_failed_close_does_not_block_tasty_entry_but_shared_reconciliation_does(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(
        store,
        "group_public",
        target_progress=20.0,
        trigger_progress=100.0,
        execution_venue="public_primary",
    )
    store.record_live_management_decision(
        "group_public",
        "close",
        "max_loss",
        {"group_id": "group_public", "action": "close", "reason": "max_loss"},
    )
    _failed_close_intent(store, "group_public", "public-failed-close")

    public_gate = entry_health_gate(store, {}, execution_venue="public_primary")
    tasty_gate = entry_health_gate(store, {}, execution_venue="tasty_primary")

    assert public_gate["blocked"] is True
    assert public_gate["entry_overall"] == "RED"
    assert tasty_gate["blocked"] is False
    assert tasty_gate["entry_overall"] == "GREEN"
    assert tasty_gate["overall"] == "RED"
    assert tasty_gate["excluded_other_venue_reasons"] == ["failed_preflight_close"]

    store.save_live_reconciliation_issue(
        {
            "issue_id": "shared-reconciliation-blocker",
            "issue_type": "broker_qty_mismatch",
            "group_id": "group_public",
            "underlying": "AAPL",
            "execution_venue": "public_primary",
            "status": "open",
        },
    )

    tasty_gate = entry_health_gate(store, {}, execution_venue="tasty_primary")
    assert tasty_gate["blocked"] is True
    assert tasty_gate["entry_overall"] == "RED"
    assert tasty_gate["reasons"] == ["reconciliation_blocker"]


def test_superseded_failed_close_ignored_when_newer_close_ticket_exists(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _make_open_group_with_mark(store, "group_retry", target_progress=20.0, trigger_progress=100.0)
    _failed_close_intent(store, "group_retry", "old-failed-ticket")
    store.save_live_order_intent(
        {
            "ticket_hash": "new-pending-ticket",
            "order_id": "order-new-pending",
            "plan_id": "plan-retry",
            "candidate_id": "cand-retry",
            "idea_id": "idea-retry",
            "group_id": "group_retry",
            "intent_type": "close",
            "underlying": "AAPL",
        },
        status="pending_close_approval",
    )

    report = run_live_health(store)

    assert report["counts"]["failed_close_orders"] == 0
    assert not any(event["reason"] in {"failed_preflight_close", "failed_close_order"} for event in report["events"])
    assert any(order["reason"] == "superseded_close_order" for order in report["close_orders"])
