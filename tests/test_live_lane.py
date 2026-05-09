import json
import sqlite3
from datetime import date, timedelta

from kamandal_v2.config import load_control
from kamandal_v2.domain.models import Playbook, UniverseEntry
from kamandal_v2.live.advisory import live_config, run_live_advisory_plan
from kamandal_v2.live.execution import execute_live_approved, record_manual_live_fill
from kamandal_v2.live.management import run_live_management_plan
from kamandal_v2.live.orders import APPROVE_LIVE, build_close_ticket
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.stores.audit import AuditWriter
from kamandal_v2.stores.sqlite import LocalStore


def _live_control() -> dict:
    control = load_control()
    control["live"]["max_bpr_per_order"] = 1000
    return control


def _ideas_file(tmp_path) -> str:
    path = tmp_path / "ideas.yaml"
    path.write_text(
        """
ideas:
  - idea_id: tsla_bear_call_spread
    source: test
    underlying: TSLA
    direction: bearish
    strategy_hint: call_spread
    thesis_tags: [overextended, defined_risk]
    horizon_days: 45
    confidence: test
    operator_status: approved
""",
        encoding="utf-8",
    )
    return str(path)


def _patch_live_config(monkeypatch) -> None:
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_cap", allowed_playbooks=["call_spread"])]
    playbooks = [
        Playbook(
            playbook_id="call_spread",
            enabled=True,
            strategy_family="call_spread",
            structure="call_spread",
            variant="default",
            leg_count=2,
            profiles=["large_cap"],
            applicable_direction=["bearish"],
            applicable_thesis_tags=["overextended", "defined_risk"],
            applicable_horizon_min=14,
            applicable_horizon_max=60,
            dte_min=30,
            dte_max=45,
            spread_width=5,
            short_delta_min=0.15,
            short_delta_max=0.30,
            min_credit_to_width_ratio=0.05,
            max_bid_ask_pct=0.50,
            min_option_oi=0,
            profit_target_pct=50,
            exit_dte_min=21,
        )
    ]
    monkeypatch.setattr("kamandal_v2.planner.engine.load_planner_config", lambda _config, source="sheet": (universe, playbooks))
    monkeypatch.setattr("kamandal_v2.live.management.load_planner_config", lambda _config, source="sheet": (universe, playbooks))


def test_live_config_ignores_shadow_overrides() -> None:
    control = load_control()
    config = live_config(control)

    assert config["runtime"]["mode"] == "live"
    assert config["execution"]["approval_mode"] == "live_plan_only"
    assert config["shadow"]["account_size_override"] == 20_000


def test_live_advisory_uses_real_account_and_writes_blank_approval(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)
    store = LocalStore(tmp_path / "kamandal.db")
    result = run_live_advisory_plan(
        _live_control(),
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert result.plans
    assert result.metrics["account_size_effective"] == 5000.0
    assert result.metrics["account_size_raw"] == 5000.0
    assert len(result.plans[0].candidates) == 1
    row = dict(zip(DAILY_PLAN_HEADER, result.daily_plan_rows[0], strict=False))
    detail = json.loads(row["plan_detail_json"])
    assert row["operator_action"] == ""
    assert detail["lane"] == "live_advisory"
    assert detail["order_ticket_json"]["intent_type"] == "open"


def test_live_execute_approved_dry_run_uses_sheet_gate(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)
    store = LocalStore(tmp_path / "kamandal.db")
    result = run_live_advisory_plan(
        _live_control(),
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )
    row = dict(zip(DAILY_PLAN_HEADER, result.daily_plan_rows[0], strict=False))
    row["operator_action"] = APPROVE_LIVE

    monkeypatch.setattr(
        "kamandal_v2.live.execution.pull_sheet_tables",
        lambda _config: {"daily_plan": [row]},
    )
    executed = execute_live_approved(load_control(), submit=False, store=store)

    assert executed["processed"] == 1
    assert executed["results"][0]["status"] == "dry_run"
    with sqlite3.connect(store.sqlite_path) as conn:
        assert conn.execute("SELECT count(*) FROM live_order_attempts").fetchone()[0] == 1


def test_close_ticket_reverses_sides_and_uses_close_indicator(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)
    store = LocalStore(tmp_path / "kamandal.db")
    result = run_live_advisory_plan(
        _live_control(),
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )
    row = dict(zip(DAILY_PLAN_HEADER, result.daily_plan_rows[0], strict=False))
    ticket = json.loads(row["plan_detail_json"])["order_ticket_json"]
    record_manual_live_fill(ticket["ticket_hash"], store=store)
    group = store.open_live_position_groups()[0]

    close_ticket = build_close_ticket(group)

    assert close_ticket["intent_type"] == "close"
    assert {leg["openCloseIndicator"] for leg in close_ticket["submit_payload"]["legs"]} == {"CLOSE"}
    open_sides = [leg["side"] for leg in ticket["submit_payload"]["legs"]]
    close_sides = [leg["side"] for leg in close_ticket["submit_payload"]["legs"]]
    assert close_sides == ["BUY" if side == "SELL" else "SELL" for side in open_sides]


def test_live_management_writes_full_group_close_advisory(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)
    store = LocalStore(tmp_path / "kamandal.db")
    result = run_live_advisory_plan(
        _live_control(),
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )
    row = dict(zip(DAILY_PLAN_HEADER, result.daily_plan_rows[0], strict=False))
    ticket = json.loads(row["plan_detail_json"])["order_ticket_json"]
    record_manual_live_fill(ticket["ticket_hash"], store=store)
    candidate = result.plans[0].candidates[0]
    expiration = candidate.legs[0].expiration
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chain_snapshots VALUES (?, ?, ?)",
            (
                "cheap_chain",
                candidate.underlying,
                json.dumps({
                    "captured_at": "2099-01-01T14:00:00Z",
                    "underlying": candidate.underlying,
                    "underlying_price": 100.0,
                    "quotes": [
                        {"expiration": expiration, "option_type": leg.option_type, "strike": leg.strike, "bid": 0.01, "ask": 0.02}
                        for leg in candidate.legs
                    ],
                }),
            ),
        )

    managed = run_live_management_plan(load_control(), config_source="seed", write_sheet=False, store=store)

    assert managed["close_recommendations"] == 1
    detail = json.loads(dict(zip(DAILY_PLAN_HEADER, managed["daily_plan_rows"][0], strict=False))["plan_detail_json"])
    assert detail["lane"] == "live_close_advisory"
    assert detail["order_ticket_json"]["intent_type"] == "close"
