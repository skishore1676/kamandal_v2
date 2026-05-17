import csv
import json
import sqlite3

from kamandal_v2.reports.go_live_audit import build_go_live_audit_report
from kamandal_v2.stores.sqlite import LocalStore


def test_go_live_audit_report_writes_reviewable_csvs(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO events (created_at, event_type, payload) VALUES (?, ?, ?)",
            ("2026-05-15 14:00:00", "plan_run_completed", json.dumps({"plan_run_id": "run_20260515T140000Z"})),
        )
        conn.execute(
            "INSERT INTO ideas VALUES (?, ?, ?)",
            (
                "2026-05-15_x_TSLA_01",
                "pending",
                json.dumps(
                    {
                        "idea_id": "2026-05-15_x_TSLA_01",
                        "source": "x",
                        "underlying": "TSLA",
                        "direction": "bearish",
                        "horizon_days": 30,
                        "confidence": "medium",
                        "extraction_confidence": "medium",
                        "thesis_tags": ["overextended"],
                        "quote_evidence": "TSLA looks overextended into resistance after a sharp run.",
                    }
                ),
            ),
        )
        candidate = {
            "candidate_id": "cand1",
            "idea_id": "2026-05-15_x_TSLA_01",
            "underlying": "TSLA",
            "playbook_id": "call_spread_default",
            "structure": "call_spread",
            "net_credit": 1.1,
            "estimated_bpr": 390,
            "greeks": {"delta": -0.12, "theta": 0.04, "gamma": -0.01},
            "liquidity_score": 0.9,
            "rejection_reason": "",
            "preflight": {"ok": True},
        }
        conn.execute("INSERT INTO candidates VALUES (?, ?, ?)", ("cand1", "run_20260515T140000Z", json.dumps(candidate)))
        plan = {
            "plan_id": "plan1",
            "rank": 1,
            "score": 10,
            "bpr_utilization_pct": 2.0,
            "buying_power_after": 1000,
            "candidates": [candidate],
        }
        conn.execute("INSERT INTO plans VALUES (?, ?, ?, ?)", ("plan1", "run_20260515T140000Z", 1, json.dumps(plan)))
        conn.execute(
            """
            INSERT INTO shadow_fills
            (id, plan_run_id, plan_id, candidate_id, idea_id, underlying, playbook_id, structure, net_credit,
             estimated_bpr, delta, gamma, theta, vega, status, opened_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fill1",
                "run_20260515T140000Z",
                "plan1",
                "cand1",
                "2026-05-15_x_TSLA_01",
                "TSLA",
                "call_spread_default",
                "call_spread",
                1.1,
                390,
                -0.12,
                -0.01,
                0.04,
                0.0,
                "open",
                "2026-05-15 14:01:00",
                json.dumps(candidate),
            ),
        )

    result = build_go_live_audit_report(
        sqlite_path=store.sqlite_path,
        output_dir=tmp_path / "reports",
        dates=["2026-05-15"],
    )

    assert result.selected_dates == ["2026-05-15"]
    assert result.files["verdict_md"].exists()
    assert "Calibration Questions" in result.files["verdict_md"].read_text(encoding="utf-8")

    with result.files["ideas_csv"].open(encoding="utf-8") as handle:
        ideas = list(csv.DictReader(handle))
    assert ideas[0]["machine_verdict"] == "reviewable_input"

    with result.files["candidates_csv"].open(encoding="utf-8") as handle:
        candidates = list(csv.DictReader(handle))
    assert candidates[0]["machine_verdict"] == "good_plan_candidate"
    assert candidates[0]["suman_review"] == ""
