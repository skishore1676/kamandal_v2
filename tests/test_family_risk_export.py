from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from kamandal_v2.tools.family_risk_export import atomic_write_json, build_export


def _fixture(tmp_path: Path, *, duplicate_cluster: bool = False) -> tuple[Path, Path]:
    db = tmp_path / "k.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        CREATE TABLE live_position_groups(group_id TEXT,status TEXT,opened_at TEXT,closed_at TEXT,payload TEXT);
        CREATE TABLE live_positions(id TEXT,group_id TEXT,order_id TEXT,status TEXT,payload TEXT);
        CREATE TABLE account_snapshots(id TEXT,payload TEXT);
        CREATE TABLE live_position_marks(id INTEGER,created_at TEXT,group_id TEXT,quote_fresh INTEGER,payload TEXT);
        CREATE TABLE live_order_status(id INTEGER,created_at TEXT,order_id TEXT,status TEXT,payload TEXT);
        """)
        candidate = {"underlying": "SPY", "structure": "call_spread", "legs": [
            {"side": "sell", "quantity": 1, "option_type": "call", "strike": 750, "expiration": "2026-08-21"},
            {"side": "buy", "quantity": 1, "option_type": "call", "strike": 755, "expiration": "2026-08-21"},
        ]}
        group = {"group_id": "g1", "underlying": "SPY", "structure": "call_spread", "candidate": candidate,
                 "secret": "must-not-export"}
        conn.execute("INSERT INTO live_position_groups VALUES (?,?,?,?,?)",
                     ("g1", "open", "2026-07-10 18:00:00", None, json.dumps(group)))
        conn.execute("INSERT INTO live_positions VALUES (?,?,?,?,?)",
                     ("p1", "g1", "raw-order-id", "open", json.dumps(group)))
        conn.execute("INSERT INTO account_snapshots VALUES (?,?)", ("account_20260710T195900Z", json.dumps({
            "account_size": 10000, "buying_power": 5000, "bpr_used": 5000, "positions_count": 1,
            "greeks": {"delta": 2}, "accountId": "must-not-export"})))
        mark = {"legs": [
            {"side": "sell", "quantity": 1, "delta": .3, "gamma": .1, "theta": -.1, "vega": .2},
            {"side": "buy", "quantity": 1, "delta": .2, "gamma": .05, "theta": -.05, "vega": .1},
        ], "secret": "must-not-export"}
        conn.execute("INSERT INTO live_position_marks VALUES (?,?,?,?,?)",
                     (1, "2026-07-10 19:59:30", "g1", 1, json.dumps(mark)))
        order = {"orderId": "raw-order-id", "filledQuantity": 1, "secret": "must-not-export", "legs": [
            {"instrument": {"symbol": "SPY260821C00750000", "type": "OPTION"}},
            {"instrument": {"symbol": "SPY260821C00755000", "type": "OPTION"}},
        ]}
        conn.execute("INSERT INTO live_order_status VALUES (?,?,?,?,?)",
                     (1, "2026-07-10 18:00:05", "raw-order-id", "FILLED", json.dumps(order)))
    config = tmp_path / "control.yaml"
    clusters = {"broad_index": ["SPY"]}
    if duplicate_cluster: clusters["also_index"] = ["SPY"]
    config.write_text(yaml.safe_dump({"broker": {"active": "public", "public": {}},
                                      "risk_manager": {"correlation_clusters": clusters}}))
    return db, config


def test_export_uses_allowlisted_atomic_persisted_facts(tmp_path: Path) -> None:
    db, config = _fixture(tmp_path)
    payload = build_export(db_path=db, config_path=config, account_alias="kamandal",
                           now=datetime(2026, 7, 10, 20, tzinfo=UTC))
    encoded = json.dumps(payload)
    assert payload["source_observed_at"] is None  # fresh mark is not broker-position proof
    assert payload["account_snapshot"]["observed_at"] == "2026-07-10T19:59:00+00:00"
    assert "must-not-export" not in encoded and "raw-order-id" not in encoded
    row = payload["live_positions"][0]
    assert row["position_as_of"] == "2026-07-10T18:00:00+00:00"
    assert row["mark_as_of"] == "2026-07-10T19:59:30+00:00"
    assert row["broker_as_of"] is None
    assert row["contract_multiplier"] is None
    assert row["cluster"] == "broad_index"
    assert row["broker_position_ids"][0].startswith("broker-option:sha256:")


def test_ambiguous_cluster_or_missing_filled_order_refuses_snapshot(tmp_path: Path) -> None:
    db, config = _fixture(tmp_path, duplicate_cluster=True)
    with pytest.raises(ValueError, match="multiple configured"):
        build_export(db_path=db, config_path=config, account_alias="x")
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM live_order_status")
    config.write_text(yaml.safe_dump({"broker": {"active": "public"}}))
    with pytest.raises(ValueError, match="FILLED broker order"):
        build_export(db_path=db, config_path=config, account_alias="x")


def test_atomic_failure_preserves_previous_snapshot(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "snapshot.json"; out.write_text('{"old": true}\n')
    monkeypatch.setattr("kamandal_v2.tools.family_risk_export.os.replace",
                        lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError): atomic_write_json(out, {"new": True})
    assert json.loads(out.read_text()) == {"old": True}
