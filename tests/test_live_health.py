from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

from kamandal_v2.live.health import entry_health_gate, run_live_health
from kamandal_v2.stores.sqlite import LocalStore


def _make_open_group_with_mark(store: LocalStore, group_id: str, *, loss_watch: bool = False, target_progress: float = 0.0, trigger_progress: float = 100.0) -> None:
    store.save_live_position_group(
        group_id,
        {
            "group_id": group_id,
            "underlying": "AAPL",
            "playbook_id": "call_spread_test",
            "structure": "call_spread",
            "candidate": {
                "candidate_id": "cand_1",
                "idea_id": "idea_1",
                "playbook_id": "call_spread_test",
                "underlying": "AAPL",
            },
        },
    )
    store.record_live_position_mark(
        group_id,
        {
            "underlying": "AAPL",
            "entry_kind": "credit",
            "pnl_mid": 12.5,
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
