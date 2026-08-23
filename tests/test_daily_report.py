from __future__ import annotations

import sqlite3
from pathlib import Path

from kamandal_v2.domain.models import PortfolioState
from kamandal_v2.ops import daily_report
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.reports import _runtime_error_summary


def test_report_status_aggregates_live_reconciliation_and_idea_truth() -> None:
    red = daily_report._report_status(
        {"overall": "GREEN", "reasons": []},
        [{"status": "open"}],
        {"active_files": 3},
    )
    assert red["level"] == "RED"
    assert red["components"]["reconciliation"] == "RED"

    yellow = daily_report._report_status(
        {"overall": "GREEN", "reasons": []},
        [],
        {"active_files": 0},
    )
    assert yellow["level"] == "YELLOW"
    assert yellow["reason"] == "no_active_idea_files"

    separated = daily_report._report_status(
        {"overall": "GREEN", "reasons": []},
        [],
        {"active_files": 2},
        csa_shadow={"runtime_status": "GREEN", "evidence_status": "RED"},
    )
    assert separated["level"] == "GREEN"
    assert separated["domains"] == {
        "current_live_operations": "GREEN",
        "current_shadow_runtime": "GREEN",
        "accumulated_shadow_evidence": "RED",
    }


def test_runtime_error_summary_distinguishes_recovered_from_active_failures() -> None:
    receipts = [
        {"started_at": "2026-08-18T14:00:00Z", "command": "manage", "result": {"execution_mode": "live", "errors": ["quote 429"]}},
        {"started_at": "2026-08-18T14:05:00Z", "command": "manage", "result": {"execution_mode": "live", "errors": []}},
        {"started_at": "2026-08-18T14:05:01Z", "command": "manage", "result": {"execution_mode": "shadow", "errors": []}},
    ]

    recovered = _runtime_error_summary(receipts)

    assert recovered == {
        "runtime_status": "GREEN",
        "active_run_errors": [],
        "recovered_run_error_count": 1,
    }
    receipts.append(
        {"started_at": "2026-08-18T14:10:01Z", "command": "manage", "result": {"execution_mode": "shadow", "errors": ["missing quote"]}}
    )
    active = _runtime_error_summary(receipts)
    assert active["runtime_status"] == "RED"
    assert active["active_run_errors"] == ["missing quote"]
    assert active["recovered_run_error_count"] == 1


def test_live_position_report_excludes_historical_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE live_positions (group_id TEXT, underlying TEXT, status TEXT, payload TEXT)")
    conn.executemany(
        "INSERT INTO live_positions VALUES (?,?,?,?)",
        [
            ("open-1", "AMZN", "open", "{}"),
            ("old-1", "SPY", "reconciled_retired", "{}"),
            ("old-2", "META", "closed", "{}"),
        ],
    )

    rows = daily_report._load_live_positions(conn)

    assert [row["group_id"] for row in rows] == ["open-1"]


def test_daily_report_health_probe_is_read_only(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    observed = {}

    class Store:
        def __init__(self, sqlite_path, *, read_only=False):  # noqa: ANN001
            observed["path"] = sqlite_path
            observed["read_only"] = read_only

    def fake_health(_store, _config, **kwargs):  # noqa: ANN001
        observed.update(kwargs)
        return {"overall": "GREEN"}

    monkeypatch.setattr("kamandal_v2.stores.sqlite.LocalStore", Store)
    monkeypatch.setattr(daily_report, "run_live_health", fake_health)

    result = daily_report._safe_live_health(tmp_path / "db.sqlite", {}, now=None)

    assert result["overall"] == "GREEN"
    assert observed["read_only"] is True
    assert observed["allow_mutation"] is False


def test_local_store_read_only_mode_cannot_write(tmp_path) -> None:
    from kamandal_v2.stores.sqlite import LocalStore

    db_path = tmp_path / "store.sqlite"
    writable = LocalStore(db_path)
    with writable._connect() as conn:
        conn.execute("INSERT INTO events (event_type, payload) VALUES (?, ?)", ("fixture", "{}"))

    read_only = LocalStore(db_path, read_only=True)
    with read_only._connect() as conn:
        assert conn.execute("SELECT event_type FROM events").fetchone()["event_type"] == "fixture"
    try:
        with read_only._connect() as conn:
            conn.execute("INSERT INTO events (event_type, payload) VALUES (?, ?)", ("should_fail", "{}"))
    except sqlite3.OperationalError as exc:
        assert "readonly" in str(exc).lower()
    else:
        raise AssertionError("read-only LocalStore accepted a write")


def test_daily_report_keeps_live_and_shadow_bpr_separate(tmp_path) -> None:
    database = tmp_path / "store.sqlite"
    store = LocalStore(database)
    store.save_account_snapshot(
        "run_20260818T193000Z",
        PortfolioState(account_size=11_500, buying_power=9_250, bpr_used=2_250, positions_count=5),
        mode="live",
    )
    store.save_account_snapshot(
        "run_20260818T193500Z",
        PortfolioState(account_size=20_000, buying_power=9_300, bpr_used=10_700, positions_count=3),
        mode="shadow",
    )

    books = daily_report._portfolio_books(database)

    assert books["live"]["bpr_used_pct"] == 19.57
    assert books["shadow"]["bpr_used_pct"] == 53.5
    assert books["live"]["snapshot_id"].startswith("live:")
    assert books["shadow"]["snapshot_id"].startswith("shadow:")


def test_daily_report_emits_exact_strategy_evidence_packet(tmp_path) -> None:
    database = tmp_path / "missing.sqlite"
    output_dir = tmp_path / "reports"

    written = daily_report.write_daily_report(
        database,
        output_dir=output_dir,
        trading_date="2026-08-21",
        config={},
    )

    artifacts = written.report["strategy_evidence_artifacts"]
    assert Path(artifacts["scorecard"]).name == "csa1_scorecard_2026-08-21.json"
    assert Path(artifacts["weekly_economics"]).name == "csa1_weekly_economics_2026-08-21.json"
    assert Path(artifacts["experiment_status"]).name == "csa1_experiment_status_2026-08-21.json"
    assert all(Path(path).exists() for path in artifacts.values())
