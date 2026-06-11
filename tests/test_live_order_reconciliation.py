from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from kamandal_v2.live.health import run_live_health
from kamandal_v2.live.order_reconciliation import reconcile_live_orders
from kamandal_v2.live.reconciliation import reconcile_live_positions
from kamandal_v2.stores.sqlite import LocalStore


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
    store.save_live_order_intent(ticket, status="submitted")

    class Broker:
        def get_order(self, _order_id: str) -> dict[str, Any]:
            return {"status": "REJECTED"}

    result = reconcile_live_orders(_config(), store=store, adapter=Broker())

    assert result["results"][0]["broker_status"] == "REJECTED"
    assert result["results"][0]["reconciled_status"] == "rejected"
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "rejected"


def test_position_reconciliation_runs_order_reconciliation(tmp_path: Path, monkeypatch: Any) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = _close_ticket()
    store.save_live_order_intent(ticket, status="pending_close_approval")
    _set_order_updated_at(store, ticket["ticket_hash"], "2000-01-01 00:00:00")

    class Broker:
        def broker_positions(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config: Broker())

    result = reconcile_live_positions(_config(), store=store)

    assert result["order_reconciliation"]["expired_stale_close_approvals"] == 1
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "expired_stale_close_approval"


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
