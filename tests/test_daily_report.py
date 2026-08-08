from __future__ import annotations

import sqlite3

from kamandal_v2.ops import daily_report


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
