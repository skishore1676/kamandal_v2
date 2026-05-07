import json
import sqlite3
from datetime import date, timedelta

from kamandal_v2.management.shadow import manage_shadow_positions, mark_shadow_portfolio
from kamandal_v2.stores.sqlite import LocalStore


def _insert_shadow_fill(store: LocalStore, *, expiration: str, net_credit: float = 2.0) -> None:
    payload = {
        "fill_id": "fill1",
        "plan_run_id": "run1",
        "plan_id": "plan1",
        "candidate_id": "cand1",
        "idea_id": "idea1",
        "underlying": "TSLA",
        "playbook_id": "put_spread_default",
        "structure": "put_spread",
        "net_credit": net_credit,
        "estimated_bpr": 300.0,
        "greeks": {"delta": 0.1, "gamma": -0.01, "theta": 0.02, "vega": -0.1},
        "legs": [
            {"side": "sell", "quantity": 1, "expiration": expiration, "option_type": "put", "strike": 100.0},
            {"side": "buy", "quantity": 1, "expiration": expiration, "option_type": "put", "strike": 95.0},
        ],
    }
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO shadow_fills
            (id, plan_run_id, plan_id, candidate_id, idea_id, underlying, playbook_id, structure,
             net_credit, estimated_bpr, delta, gamma, theta, vega, status, opened_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                "fill1",
                "run1",
                "plan1",
                "cand1",
                "idea1",
                "TSLA",
                "put_spread_default",
                "put_spread",
                net_credit,
                300.0,
                0.1,
                -0.01,
                0.02,
                -0.1,
                date.today().isoformat() + " 09:30:00",
                json.dumps(payload),
            ),
        )
        conn.execute(
            "INSERT INTO chain_snapshots VALUES (?, ?, ?)",
            (
                "chain1",
                "TSLA",
                json.dumps({
                    "captured_at": "2026-05-07T14:00:00Z",
                    "underlying": "TSLA",
                    "underlying_price": 102.0,
                    "quotes": [
                        {"expiration": expiration, "option_type": "put", "strike": 100.0, "bid": 0.9, "ask": 1.1},
                        {"expiration": expiration, "option_type": "put", "strike": 95.0, "bid": 0.4, "ask": 0.6},
                    ],
                }),
            ),
        )


def test_mark_shadow_portfolio_uses_midpoint_quotes(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    expiration = (date.today() + timedelta(days=30)).isoformat()
    _insert_shadow_fill(store, expiration=expiration)

    mark = mark_shadow_portfolio(store)

    assert mark["position_count"] == 1
    assert mark["total_entry_credit"] == 200.0
    assert mark["total_mid_pnl"] == 150.0
    assert mark["rows"][0]["pnl_pct_of_credit"] == 75.0


def test_manage_shadow_positions_closes_profit_target(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    expiration = (date.today() + timedelta(days=30)).isoformat()
    _insert_shadow_fill(store, expiration=expiration)

    result = manage_shadow_positions({}, config_source="seed", store=store)

    assert result.closed_count == 1
    assert result.decisions[0]["reason"] == "profit_target"
    with sqlite3.connect(store.sqlite_path) as conn:
        status, close_reason = conn.execute("SELECT status, close_reason FROM shadow_fills WHERE id = 'fill1'").fetchone()
    assert status == "closed"
    assert close_reason == "profit_target"
