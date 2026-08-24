from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from kamandal_v2.live.health import run_live_health
from kamandal_v2.live.orders import build_csa_live_ticket
from kamandal_v2.live.order_reconciliation import reconcile_live_orders
from kamandal_v2.live.reconciliation import reconcile_live_positions
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.strategy_lanes.models import (
    LaneId,
    LegEffect,
    LegSide,
    LifecycleState,
    StrategyTicket,
    TicketLeg,
)
from kamandal_v2.strategy_lanes.store import CsaStore


def _config() -> dict[str, Any]:
    return {
        "live": {
            "reconciliation": {
                "enabled": True,
                "order_reconciliation_enabled": True,
                "expire_stale_close_approvals": True,
                "stale_close_approval_minutes": 120,
            }
        },
        "broker": {"active": "public"},
    }


def _close_ticket(ticket_hash: str = "ticket-close-stale", *, group_id: str = "group_order_recon") -> dict[str, Any]:
    return {
        "ticket_hash": ticket_hash,
        "order_id": f"order-{ticket_hash}",
        "plan_id": "plan-order-recon",
        "candidate_id": "candidate-order-recon",
        "idea_id": "idea-order-recon",
        "group_id": group_id,
        "intent_type": "close",
        "underlying": "PLTR",
        "structure": "put_spread",
        "limit_price": "3.45",
    }


def _open_group(store: LocalStore, group_id: str = "group_order_recon") -> None:
    store.save_live_position_group(
        group_id,
        {
            "group_id": group_id,
            "underlying": "PLTR",
            "playbook_id": "put_spread_default",
            "structure": "put_spread",
            "candidate": {"candidate_id": "candidate-order-recon", "underlying": "PLTR"},
        },
    )


def _set_order_updated_at(store: LocalStore, ticket_hash: str, value: str) -> None:
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute("UPDATE live_order_intents SET updated_at = ?, created_at = ? WHERE ticket_hash = ?", (value, value, ticket_hash))


def test_order_reconciler_expires_stale_pending_close_approval(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = _close_ticket()
    store.save_live_order_intent(ticket, status="pending_close_approval")
    _set_order_updated_at(store, ticket["ticket_hash"], "2000-01-01 00:00:00")

    result = reconcile_live_orders(_config(), store=store)

    assert result["expired_stale_close_approvals"] == 1
    assert result["results"][0]["reconciled_status"] == "expired_stale_close_approval"
    stored = store.live_order_intent(ticket["ticket_hash"])
    assert stored is not None
    assert stored["_ledger_status"] == "expired_stale_close_approval"
    assert stored["order_reconciliation"]["reason"] == "local_close_approval_stale"


def test_order_reconciler_expires_stale_approved_close_pending_submit(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = _close_ticket("ticket-close-approved-stale")
    store.save_live_order_intent(ticket, status="approved_close_pending_submit")
    _set_order_updated_at(store, ticket["ticket_hash"], "2000-01-01 00:00:00")

    result = reconcile_live_orders(_config(), store=store)

    assert result["expired_stale_close_approvals"] == 1
    assert result["results"][0]["ledger_status"] == "approved_close_pending_submit"
    assert result["results"][0]["reconciled_status"] == "expired_stale_close_approval"
    stored = store.live_order_intent(ticket["ticket_hash"])
    assert stored is not None
    assert stored["_ledger_status"] == "expired_stale_close_approval"


def test_order_reconciler_reports_stale_approval_without_apply_config(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = _close_ticket()
    store.save_live_order_intent(ticket, status="pending_close_approval")
    _set_order_updated_at(store, ticket["ticket_hash"], "2000-01-01 00:00:00")
    config = _config()
    config["live"]["reconciliation"]["expire_stale_close_approvals"] = False

    result = reconcile_live_orders(config, store=store)

    assert result["stale_close_approvals"] == 1
    assert result["expired_stale_close_approvals"] == 0
    assert result["results"][0]["reconciled_status"] == "stale_close_approval"
    assert result["results"][0]["action_required"] == "expire_stale_close_approval"
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "pending_close_approval"


def test_order_reconciler_dry_run_reports_without_mutating(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = _close_ticket()
    store.save_live_order_intent(ticket, status="pending_close_approval")
    _set_order_updated_at(store, ticket["ticket_hash"], "2000-01-01 00:00:00")

    result = reconcile_live_orders(_config(), store=store, dry_run=True)

    assert result["expired_stale_close_approvals"] == 1
    assert result["dry_run"] is True
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "pending_close_approval"


def test_order_reconciler_updates_terminal_broker_close_status(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = _close_ticket("ticket-close-submitted")
    ticket["client_order_id"] = ticket["order_id"]
    ticket["broker_order_id"] = "987654"
    store.save_live_order_intent(ticket, status="submitted")
    observed = []

    class Broker:
        def get_order(self, order_id: str) -> dict[str, Any]:
            observed.append(order_id)
            return {"status": "REJECTED"}

    result = reconcile_live_orders(_config(), store=store, adapter=Broker())

    assert result["results"][0]["broker_status"] == "REJECTED"
    assert result["results"][0]["reconciled_status"] == "rejected"
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "rejected"
    assert observed == ["987654"]


def test_order_reconciler_observes_staged_cancel_without_consuming_parent(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = _close_ticket("ticket-staged-parent")
    store.save_live_order_intent(ticket, status="replace_cancel_pending")

    class Broker:
        def get_order(self, _order_id: str) -> dict[str, Any]:
            return {"status": "CANCELLED"}

    result = reconcile_live_orders(_config(), store=store, adapter=Broker())

    assert result["results"][0]["reconciled_status"] == "staged_replace_parent_observed"
    assert result["results"][0]["staged_replace_parent_status"] == "CANCELLED"
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "replace_cancel_pending"


def test_position_reconciliation_runs_order_reconciliation(tmp_path: Path, monkeypatch: Any) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = _close_ticket()
    store.save_live_order_intent(ticket, status="pending_close_approval")
    _set_order_updated_at(store, ticket["ticket_hash"], "2000-01-01 00:00:00")

    class Broker:
        def broker_positions(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config, **_kwargs: Broker())

    result = reconcile_live_positions(_config(), store=store)

    assert result["order_reconciliation"]["expired_stale_close_approvals"] == 1
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "expired_stale_close_approval"


def test_position_reconciliation_never_queries_tasty_order_through_public_on_tasty_outage(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = _close_ticket("ticket-tasty-submitted")
    ticket["execution_venue"] = "tasty_primary"
    ticket["broker_order_id"] = "tasty-order-42"
    store.save_live_order_intent(ticket, status="submitted")
    public_order_queries: list[str] = []

    class PublicBroker:
        def broker_positions(self) -> list[dict[str, Any]]:
            return []

        def get_order(self, order_id: str) -> dict[str, Any]:
            public_order_queries.append(order_id)
            return {"status": "WORKING"}

    def adapter_for_venue(_config: dict[str, Any], *, execution_venue: str | None = None) -> Any:
        if execution_venue == "tasty_primary":
            raise RuntimeError("tastytrade unavailable")
        return PublicBroker()

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", adapter_for_venue)
    monkeypatch.setattr("kamandal_v2.live.order_reconciliation.broker_adapter", adapter_for_venue)

    result = reconcile_live_positions(_config(), store=store, dry_run=True)

    assert result["broker_venue_errors"] == {"tasty_primary": "tastytrade unavailable"}
    order_result = result["order_reconciliation"]["results"][0]
    assert order_result["execution_venue"] == "tasty_primary"
    assert order_result["reconciled_status"] == "broker_venue_unavailable"
    assert public_order_queries == []


def test_position_reconciliation_isolates_same_occ_symbol_by_execution_venue(tmp_path: Path, monkeypatch: Any) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    leg = {
        "role": "short_call",
        "side": "sell",
        "option_type": "call",
        "strike": 465.0,
        "expiration": "2026-10-16",
        "quantity": 1,
    }
    for venue in ("public_primary", "tasty_primary"):
        group_id = f"group-{venue}"
        group = {
            "group_id": group_id,
            "underlying": "QQQ",
            "execution_venue": venue,
            "candidate": {
                "underlying": "QQQ",
                "execution_venue": venue,
                "legs": [leg],
            },
        }
        store.save_live_position_group(group_id, group)

    occ = "QQQ261016C00465000"

    class PublicBroker:
        def broker_positions(self) -> list[dict[str, Any]]:
            return [{
                "asset_type": "option",
                "occ_symbol": occ,
                "underlying": "QQQ",
                "quantity": -2,
            }]

    class TastyBroker:
        def broker_positions(self) -> list[dict[str, Any]]:
            return []

    def adapter_for_venue(_config: dict[str, Any], *, execution_venue: str | None = None) -> Any:
        return TastyBroker() if execution_venue == "tasty_primary" else PublicBroker()

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", adapter_for_venue)

    config = _config()
    config["live"]["reconciliation"]["auto_retire_ghost_after_confirmations"] = False
    result = reconcile_live_positions(config, store=store, dry_run=True)

    assert result["broker_venues_checked"] == ["public_primary", "tasty_primary"]
    assert result["broker_venue_errors"] == {}
    issue_types = {issue["issue_type"] for issue in result["issues"]}
    assert issue_types == {"quantity_mismatch", "ghost_local_position"}
    subjects = {issue["subject_id"] for issue in result["issues"]}
    assert f"public_primary::{occ}" in subjects
    assert "group-tasty_primary" in subjects


def test_position_reconciliation_repairs_pre_fix_filled_canonical_close(tmp_path: Path, monkeypatch: Any) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    migrate_csa_database(store.sqlite_path, dry_run=False, backup_dir=tmp_path / "backups")
    group_id = "live_group_amzn_calendar"
    lifecycle = LifecycleState(
        lifecycle_id="adopt:" + group_id,
        opportunity_id="legacy:" + group_id,
        lane=LaneId.EARNINGS_CALENDAR,
        version=2,
        status="open",
        active_legs=(
            {
                "side": "buy",
                "effect": "open",
                "quantity": 1,
                "option_type": "call",
                "expiration": "2026-10-16",
                "strike": 290.0,
                "role": "long_call",
            },
        ),
        cashflow_ledger=(),
        opened_at="2026-08-03T14:30:00Z",
        updated_at="2026-08-21T13:30:05Z",
        policy_hash="policy",
        metadata={
            "execution_mode": "live",
            "legacy_source_id": group_id,
            "underlying": "AMZN",
        },
    )
    CsaStore(store.sqlite_path).save_lifecycle(lifecycle)
    group = {
        "group_id": group_id,
        "underlying": "AMZN",
        "structure": "call_calendar",
        "candidate": {
            "underlying": "AMZN",
            "structure": "call_calendar",
            "legs": [
                {
                    "expiration": "2026-10-16",
                    "option_type": "call",
                    "strike": 290.0,
                    "quantity": 1,
                    "side": "buy",
                }
            ],
        },
    }
    store.save_live_position_group(group_id, group)
    store.save_live_position(group_id, group_id, group)
    close_ticket = StrategyTicket(
        ticket_id="close-amzn-calendar",
        action_id="close-amzn",
        lifecycle_id=lifecycle.lifecycle_id,
        lifecycle_version=1,
        lane=lifecycle.lane,
        underlying="AMZN",
        order_kind="credit",
        limit_price=3.30,
        legs=(
            TicketLeg(
                instrument_id="AMZN  261016C00290000",
                side=LegSide.SELL,
                effect=LegEffect.CLOSE,
                quantity=1,
                option_type="call",
                expiration="2026-10-16",
                strike=290.0,
                role="long_call",
            ),
        ),
        policy_hash="policy",
        created_at="2026-08-21T13:30:00Z",
        metadata={"action_type": "close"},
    )
    live_ticket = build_csa_live_ticket(close_ticket)
    assert "group_id" not in live_ticket  # Exact shape emitted before the fix.
    store.save_live_order_intent(live_ticket, status="close_filled")
    store.record_live_order_status(
        live_ticket["order_id"],
        "FILLED",
        {"status": "FILLED", "averagePrice": "3.3103", "filledAt": "2026-08-21T13:30:05Z"},
        ticket_hash=live_ticket["ticket_hash"],
    )

    class Broker:
        def broker_positions(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config, **_kwargs: Broker())

    result = reconcile_live_positions(_config(), store=store)

    assert result["canonical_lifecycle_repairs"][0]["status"] == "applied"
    assert result["canonical_lifecycle_repairs"][0]["result"]["projection_retired"] is True
    assert CsaStore(store.sqlite_path).lifecycle(lifecycle.lifecycle_id).status == "closed"
    assert store.open_live_position_groups() == []


def test_position_reconciliation_terminalizes_broker_flat_retired_projection(tmp_path: Path, monkeypatch: Any) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    migrate_csa_database(store.sqlite_path, dry_run=False, backup_dir=tmp_path / "backups")
    group_id = "live_group_gld_retired"
    active_legs = (
        {
            "instrument_id": "GLD260918P00300000",
            "side": "buy",
            "effect": "open",
            "quantity": 1,
            "option_type": "put",
            "expiration": "2026-09-18",
            "strike": 300.0,
            "role": "long_put",
        },
    )
    lifecycle = LifecycleState(
        lifecycle_id="adopt:" + group_id,
        opportunity_id="legacy:" + group_id,
        lane=LaneId.GENERIC_CLOSE_ONLY,
        version=2,
        status="open",
        active_legs=active_legs,
        cashflow_ledger=(),
        opened_at="2026-08-10T14:30:00Z",
        updated_at="2026-08-19T19:20:11Z",
        policy_hash="policy",
        metadata={
            "execution_mode": "live",
            "position_projection_id": group_id,
            "underlying": "GLD",
        },
    )
    CsaStore(store.sqlite_path).save_lifecycle(lifecycle)
    group = {
        "group_id": group_id,
        "underlying": "GLD",
        "structure": "put_spread",
        "candidate": {
            "underlying": "GLD",
            "structure": "put_spread",
            "legs": [
                {
                    "side": "buy",
                    "quantity": 1,
                    "option_type": "put",
                    "expiration": "2026-09-18",
                    "strike": 300.0,
                }
            ],
        },
    }
    store.save_live_position_group(group_id, group, status="reconciled_retired")
    store.save_live_position(group_id, group_id, group, status="reconciled_retired")

    class Broker:
        def broker_positions(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config, **_kwargs: Broker())

    result = reconcile_live_positions(_config(), store=store)

    repair = result["retired_projection_lifecycle_repairs"][0]
    assert repair["status"] == "applied"
    assert repair["position_projection_id"] == group_id
    closed = CsaStore(store.sqlite_path).lifecycle(lifecycle.lifecycle_id)
    assert closed.status == "closed"
    assert closed.version == 3
    assert closed.active_legs == ()
    assert closed.metadata["terminal_active_legs_snapshot"] == list(active_legs)
    assert closed.metadata["terminal_economics_status"] == "reconciled_without_fill"


def test_position_reconciliation_terminalizes_exhausted_pending_entry(tmp_path: Path, monkeypatch: Any) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    migrate_csa_database(store.sqlite_path, dry_run=False, backup_dir=tmp_path / "backups")
    lifecycle = LifecycleState(
        lifecycle_id="pending-adbe",
        opportunity_id="adbe-opportunity",
        lane=LaneId.GENERIC_CLOSE_ONLY,
        version=1,
        status="pending_live_submission",
        active_legs=(),
        cashflow_ledger=(),
        opened_at="2026-08-21T14:25:08Z",
        updated_at="2026-08-21T14:25:08Z",
        policy_hash="policy",
        metadata={
            "execution_mode": "live",
            "unified_plan_id": "adbe-plan",
            "candidate_id": "adbe-candidate",
            "underlying": "ADBE",
        },
    )
    CsaStore(store.sqlite_path).save_lifecycle(lifecycle)
    ticket = {
        "ticket_hash": "adbe-terminal-ticket",
        "order_id": "adbe-terminal-order",
        "plan_id": "adbe-plan",
        "candidate_id": "adbe-candidate",
        "idea_id": "adbe-idea",
        "intent_type": "open",
        "underlying": "ADBE",
        "structure": "put_spread",
        "csa_lifecycle_id": lifecycle.lifecycle_id,
    }
    store.save_live_order_intent(ticket, status="expired")

    class Broker:
        def broker_positions(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config, **_kwargs: Broker())

    result = reconcile_live_positions(_config(), store=store)

    repair = result["pending_lifecycle_repairs"][0]
    assert repair["lifecycle_id"] == lifecycle.lifecycle_id
    assert repair["terminal_ticket_statuses"] == ["expired"]
    retired = CsaStore(store.sqlite_path).lifecycle(lifecycle.lifecycle_id)
    assert retired.status == "entry_missed"
    assert retired.metadata["entry_retirement_reason"] == "guarded_open_intent_lineage_terminal"


def test_live_health_ignores_expired_stale_close_approval_as_actionable_failure(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    _open_group(store)
    ticket = _close_ticket()
    store.save_live_order_intent(ticket, status="expired_stale_close_approval")

    report = run_live_health(store)

    assert report["counts"]["working_close_orders"] == 0
    assert report["counts"]["failed_close_orders"] == 0
    assert report["events"] == []



def test_retires_failed_close_when_exit_decision_lapsed(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _open_group(store, "group_lapsed")
    store.save_live_order_intent(_close_ticket("ticket-lapsed", group_id="group_lapsed"), status="blocked_preflight_failed")
    store.record_live_management_decision(
        "group_lapsed",
        "hold",
        "no_exit",
        {"group_id": "group_lapsed", "action": "hold", "reason": "no_exit"},
    )

    result = reconcile_live_orders(_config(), store=store, adapter=object())

    assert result["retired_stale_close_failures"] == 1
    retired = [item for item in result["results"] if item["reconciled_status"] == "retired_stale_close_failure"]
    assert retired[0]["ticket_hash"] == "ticket-lapsed"
    intent = store.live_order_intent("ticket-lapsed")
    assert intent["_ledger_status"] == "retired_stale_close_failure"
    health = run_live_health(store, _config())
    assert health["overall"] == "GREEN"
    assert health["counts"]["failed_close_orders"] == 0
    assert health["counts"]["stale_failed_close_orders"] == 0


def test_keeps_failed_close_when_exit_still_wanted(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _open_group(store, "group_wanted")
    store.record_live_management_decision(
        "group_wanted",
        "close",
        "max_loss",
        {"group_id": "group_wanted", "action": "close", "reason": "max_loss"},
    )
    store.save_live_order_intent(_close_ticket("ticket-wanted", group_id="group_wanted"), status="blocked_preflight_failed")

    result = reconcile_live_orders(_config(), store=store, adapter=object())

    assert result["retired_stale_close_failures"] == 0
    assert store.live_order_intent("ticket-wanted")["_ledger_status"] == "blocked_preflight_failed"
    assert run_live_health(store, _config())["overall"] == "RED"


def test_retire_stale_close_failures_respects_dry_run(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _open_group(store, "group_dry")
    store.save_live_order_intent(_close_ticket("ticket-dry", group_id="group_dry"), status="blocked_preflight_failed")
    store.record_live_management_decision(
        "group_dry",
        "hold",
        "no_exit",
        {"group_id": "group_dry", "action": "hold", "reason": "no_exit"},
    )

    result = reconcile_live_orders(_config(), store=store, adapter=object(), dry_run=True)

    assert result["retired_stale_close_failures"] == 1
    assert store.live_order_intent("ticket-dry")["_ledger_status"] == "blocked_preflight_failed"
