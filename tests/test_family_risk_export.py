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
        candidate = {"underlying": "SPY", "structure": "call_spread", "net_credit": 1.5, "legs": [
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
    # Source freshness is the most recent broker observation across the account
    # snapshot readback (19:59:00) and the FILLED order confirmation (18:00:05).
    assert payload["source_observed_at"] == "2026-07-10T19:59:00+00:00"
    assert payload["account_snapshot"]["observed_at"] == "2026-07-10T19:59:00+00:00"
    assert payload["account_snapshot"]["bpr_used_pct"] == 50.0
    assert payload["assigned_capital"] == 10000
    assert payload["assigned_capital_provenance"] == "broker_account_net_liquidation_snapshot"
    assert payload["correlation_clusters"] == {"broad_index": ["SPY"]}
    assert "must-not-export" not in encoded and "raw-order-id" not in encoded
    row = payload["live_positions"][0]
    assert row["position_as_of"] == "2026-07-10T18:00:00+00:00"
    assert row["mark_as_of"] == "2026-07-10T19:59:30+00:00"
    assert row["broker_as_of"] == "2026-07-10T18:00:05+00:00"
    assert row["contract_multiplier"] == 100.0
    assert row["multiplier_provenance"] == "app_standard_option_contract"
    assert row["multiplier_provenance_detail"] == "app standard equity-option multiplier constant"
    # Credit call spread: (width 5 - credit 1.5) * 100 * qty 1 = 350 defined-risk max loss.
    assert row["worst_case_loss_usd"] == 350.0
    assert row["estimated_bpr"] == 350.0
    assert row["bpr_basis"] == "defined_risk_max_loss"
    assert row["bpr_basis_detail"].startswith("app_defined_risk_credit_vertical")
    assert row["structure"] == "call_spread"
    assert row["planned_stop_loss_usd"] is None
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


def test_missing_entry_economics_leaves_risk_explicitly_null(tmp_path: Path) -> None:
    db, config = _fixture(tmp_path)
    # Remove the persisted net_credit so entry economics are unknown; the exporter
    # must refuse to guess BPR/worst-case loss and say so explicitly.
    with sqlite3.connect(db) as conn:
        for table in ("live_position_groups", "live_positions"):
            (payload_row,) = conn.execute(f"SELECT payload FROM {table}").fetchone()
            payload = json.loads(payload_row)
            payload["candidate"].pop("net_credit", None)
            conn.execute(f"UPDATE {table} SET payload = ?", (json.dumps(payload),))
    row = build_export(db_path=db, config_path=config, account_alias="k",
                       now=datetime(2026, 7, 10, 20, tzinfo=UTC))["live_positions"][0]
    assert row["worst_case_loss_usd"] is None
    assert row["estimated_bpr"] is None
    assert row["bpr_basis"] == "unknown"
    assert row["bpr_basis_detail"] == "unknown_or_unsupported_structure"
    assert row["contract_multiplier"] == 100.0  # multiplier is still a known app fact


def test_atomic_failure_preserves_previous_snapshot(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "snapshot.json"; out.write_text('{"old": true}\n')
    monkeypatch.setattr("kamandal_v2.tools.family_risk_export.os.replace",
                        lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError): atomic_write_json(out, {"new": True})
    assert json.loads(out.read_text()) == {"old": True}
