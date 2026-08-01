from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

from kamandal_v2.live.book import live_book_sheet_rows, run_live_book
from kamandal_v2.live.management import run_live_management_plan
from kamandal_v2.live.reconciliation import reconcile_live_positions
from kamandal_v2.schemas import LIVE_BOOK_HEADER
from kamandal_v2.stores.sqlite import LocalStore


def _group(store: LocalStore, group_id: str = "group_book") -> dict[str, Any]:
    payload = {
        "group_id": group_id,
        "order_id": "open-order-book",
        "plan_id": "plan-book",
        "candidate_id": "candidate-book",
        "idea_id": "idea-book",
        "underlying": "AAPL",
        "playbook_id": "call_spread_default",
        "structure": "call_spread",
        "candidate": {
            "candidate_id": "candidate-book",
            "idea_id": "idea-book",
            "underlying": "AAPL",
            "playbook_id": "call_spread_default",
            "structure": "call_spread",
            "net_credit": 1.2,
            "legs": [{"expiration": "2099-01-17", "option_type": "call", "strike": 200.0, "side": "sell"}],
        },
    }
    store.save_live_position_group(group_id, payload)
    return payload


def _mark(store: LocalStore, group_id: str = "group_book", *, pnl: float = 30.0) -> None:
    store.record_live_position_mark(
        group_id,
        {
            "group_id": group_id,
            "underlying": "AAPL",
            "playbook_id": "call_spread_default",
            "structure": "call_spread",
            "opened_at": "2026-06-10 14:30:00",
            "entry_kind": "credit",
            "entry_net_cashflow": 120.0,
            "entry_value": 120.0,
            "close_mid_net": -90.0,
            "pnl_mid": pnl,
            "target_profit": 60.0,
            "max_loss_multiple": 2.0,
            "target_progress_pct": 50.0,
            "quote_fresh": True,
            "dte": {"remaining": 36, "entry": 45, "half_time_threshold": 22},
        },
    )


def test_live_book_builds_per_group_dashboard_row(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _group(store)
    _mark(store, pnl=30.0)
    _mark(store, pnl=-10.0)
    close_ticket = {
        "ticket_hash": "close-ticket-book",
        "order_id": "close-order-book",
        "plan_id": "plan-book",
        "candidate_id": "candidate-book",
        "idea_id": "idea-book",
        "group_id": "group_book",
        "intent_type": "close",
        "underlying": "AAPL",
        "structure": "call_spread",
    }
    store.save_live_order_intent(close_ticket, status="blocked_preflight_failed")
    store.record_live_order_attempt(
        close_ticket,
        action="preflight_close",
        submit=False,
        ok=False,
        request_payload={"ticket": "fixture"},
        response_payload={"ok": False, "message": "Public ticket preflight failed", "raw": {"response": {"error_code": "157"}}},
    )
    store.record_live_management_decision(
        "group_book",
        "hold",
        "working_close_order",
        {"group_id": "group_book", "action": "hold", "reason": "working_close_order", "blocked_reason": "profit_target", "urgency": "normal"},
    )

    report = run_live_book(store)
    health_row = report["rows"][0]
    row = report["rows"][1]

    assert report["open_groups"] == 1
    assert report["updated_at_cst"]
    assert health_row["symbol"] == "_HEALTH_"
    assert health_row["structure"] == "RED"
    assert health_row["recommended_action"] == "entries_blocked:health_red"
    assert "failed_preflight_close" in health_row["management_blocker"]
    assert row["updated_at_cst"] == report["updated_at_cst"]
    assert row["symbol"] == "AAPL"
    assert row["structure"] == "call_spread"
    assert row["dte"] == 36
    assert row["entry_credit_debit"] == 120.0
    assert row["unrealized_pnl"] == -10.0
    assert row["mfe"] == 30.0
    assert row["mae"] == -10.0
    assert row["last_close_attempt"].startswith("blocked_preflight_failed")
    assert "preflight_close ok=False" in row["last_preflight_result"]
    assert row["broker_error_code"] == "157"
    assert row["management_blocker"] == "working_close_order"
    assert row["recommended_action"] == "hold:working_close_order:normal"


def test_live_book_extracts_broker_code_from_preflight_message_string(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _group(store)
    _mark(store)
    close_ticket = {
        "ticket_hash": "close-ticket-message-code",
        "order_id": "close-order-message-code",
        "plan_id": "plan-book",
        "candidate_id": "candidate-book",
        "idea_id": "idea-book",
        "group_id": "group_book",
        "intent_type": "close",
        "underlying": "AAPL",
        "structure": "call_spread",
    }
    store.save_live_order_intent(close_ticket, status="blocked_preflight_failed")
    store.record_live_order_attempt(
        close_ticket,
        action="preflight_close",
        submit=False,
        ok=False,
        request_payload={"ticket": "fixture"},
        response_payload={
            "ok": False,
            "message": 'Public API failed status=400: {"code":157,"message":"active pending order"}',
        },
    )

    row = run_live_book(store)["rows"][1]

    assert row["broker_error_code"] == "157"


def test_live_book_sheet_rows_match_header(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _group(store)
    _mark(store)

    report = run_live_book(store)
    rows = live_book_sheet_rows(report, LIVE_BOOK_HEADER)

    assert len(rows) == 2
    assert all(len(row) == len(LIVE_BOOK_HEADER) for row in rows)
    assert rows[0][LIVE_BOOK_HEADER.index("symbol")] == "_HEALTH_"
    assert rows[1][LIVE_BOOK_HEADER.index("updated_at_cst")] == report["updated_at_cst"]
    assert rows[1][LIVE_BOOK_HEADER.index("symbol")] == "AAPL"
    report_json = json.loads(rows[1][LIVE_BOOK_HEADER.index("report_json")])
    assert report_json["symbol"] == "AAPL"
    assert report_json["updated_at_cst"] == report["updated_at_cst"]


def test_cli_live_book_can_write_sheet(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    import importlib

    if "requests" not in sys.modules:
        sys.modules["requests"] = types.ModuleType("requests")
    main_module = importlib.import_module("kamandal_v2.cli")
    main = main_module.main

    store = LocalStore(tmp_path / "kamandal_v2.db")
    _group(store)
    _mark(store)
    calls: list[dict[str, Any]] = []

    def _write_live_book(config: dict[str, Any], header: list[str], rows: list[list[Any]]) -> int:
        calls.append({"config": config, "header": header, "rows": rows})
        return len(rows)

    monkeypatch.setattr("kamandal_v2.cli.LocalStore", lambda *args, **kwargs: store)
    monkeypatch.setattr("kamandal_v2.cli.load_control", lambda: {"google_sheets": {"spreadsheet_id": "fixture"}})
    monkeypatch.setattr("kamandal_v2.cli.build_seed_tables", lambda _config: {})
    monkeypatch.setattr("kamandal_v2.cli.write_live_book", _write_live_book)
    monkeypatch.setattr("sys.argv", ["kamandal", "live-book", "--write-sheet"])

    main()

    output = capsys.readouterr().out
    assert "LIVE BOOK groups=1" in output
    assert "sheet_rows_written=2" in output
    assert len(calls) == 1
    assert calls[0]["header"] == LIVE_BOOK_HEADER
    assert calls[0]["rows"][0][LIVE_BOOK_HEADER.index("symbol")] == "_HEALTH_"
    assert calls[0]["rows"][1][LIVE_BOOK_HEADER.index("updated_at_cst")]
    assert calls[0]["rows"][1][LIVE_BOOK_HEADER.index("symbol")] == "AAPL"


def test_live_management_write_sheet_refreshes_live_book(tmp_path: Path, monkeypatch: Any) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr("kamandal_v2.live.management.load_planner_config", lambda _config, source="sheet": ([], []))
    monkeypatch.setattr(
        "kamandal_v2.live.management.write_live_book",
        lambda config, header, rows: calls.append({"config": config, "header": header, "rows": rows}) or len(rows),
    )

    result = run_live_management_plan({}, write_sheet=True, store=store)

    assert result["live_book_rows_written"] == 1
    assert len(calls) == 1
    assert calls[0]["config"] == {}
    assert calls[0]["header"] == LIVE_BOOK_HEADER
    assert len(calls[0]["rows"]) == 1
    assert calls[0]["rows"][0][LIVE_BOOK_HEADER.index("symbol")] == "_HEALTH_"


def test_reconciliation_write_sheet_refreshes_live_book(tmp_path: Path, monkeypatch: Any) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    calls: list[dict[str, Any]] = []
    daily_plan_calls: list[dict[str, Any]] = []

    class Broker:
        def broker_positions(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config: Broker())
    monkeypatch.setattr(
        "kamandal_v2.live.reconciliation.write_daily_plan",
        lambda config, rows, header, *, replace_lanes: daily_plan_calls.append(
            {"config": config, "rows": rows, "header": header, "replace_lanes": replace_lanes}
        ),
    )
    monkeypatch.setattr(
        "kamandal_v2.live.reconciliation.write_live_book",
        lambda config, header, rows: calls.append({"config": config, "header": header, "rows": rows}) or len(rows),
    )

    result = reconcile_live_positions({"live": {"reconciliation": {"enabled": True}}, "broker": {"active": "public"}}, write_sheet=True, store=store)

    assert result["live_book_rows_written"] == 1
    assert len(calls) == 1
    assert calls[0]["config"] == {"live": {"reconciliation": {"enabled": True}}, "broker": {"active": "public"}}
    assert calls[0]["header"] == LIVE_BOOK_HEADER
    assert len(calls[0]["rows"]) == 1
    assert calls[0]["rows"][0][LIVE_BOOK_HEADER.index("symbol")] == "_HEALTH_"
    assert len(daily_plan_calls) == 1
    assert daily_plan_calls[0]["rows"] == []
    assert daily_plan_calls[0]["replace_lanes"] == {"live_reconciliation"}
