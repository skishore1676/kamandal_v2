import json
import sqlite3
from datetime import date, timedelta

from kamandal_v2.config import load_control
from kamandal_v2.cli import _live_submit_requested
from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg, Playbook, PortfolioState, PreflightResult, UniverseEntry
from kamandal_v2.live.approval import approve_live_request, expire_live_approval_requests, send_pending_live_approval_requests
from kamandal_v2.live.advisory import _live_candidate_policy, live_config, run_live_advisory_plan
from kamandal_v2.live.execution import cleanup_live_approvals, execute_live_approved, record_manual_live_fill
from kamandal_v2.live.management import run_live_management_plan
from kamandal_v2.live.orders import APPROVE_LIVE, APPROVE_LIVE_CLOSE, _limit_price, build_close_ticket, build_open_ticket
from kamandal_v2.planner.engine import run_plan
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


def test_live_approval_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("KAMANDAL_LIVE_ENTRY_APPROVAL_MODE", "auto_top_plan")
    monkeypatch.setenv("KAMANDAL_LIVE_EXIT_APPROVAL_MODE", "auto_rules")
    monkeypatch.setenv("KAMANDAL_LIVE_AUTO_SUBMIT_ENTRIES", "true")
    monkeypatch.setenv("KAMANDAL_LIVE_AUTO_SUBMIT_EXITS", "false")
    monkeypatch.setenv("KAMANDAL_TELEGRAM_APPROVAL_TARGET", "123")
    monkeypatch.setenv("KAMANDAL_TELEGRAM_APPROVAL_EXPIRY_MINUTES", "7")

    control = load_control()

    assert control["live"]["entry_approval_mode"] == "auto_top_plan"
    assert control["live"]["exit_approval_mode"] == "auto_rules"
    assert control["live"]["auto_submit_entries"] is True
    assert control["live"]["auto_submit_exits"] is False
    assert control["live"]["telegram_approval"]["target"] == "123"
    assert control["live"]["telegram_approval"]["expiry_minutes"] == 7


def test_live_submit_auto_respects_global_and_lane_flags(monkeypatch) -> None:
    args = type("Args", (), {"submit": False, "submit_auto": True})()
    config = {"live": {"auto_submit_entries": True, "auto_submit_exits": False}}

    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT", "1")

    assert _live_submit_requested(config, args, close=False) is True
    assert _live_submit_requested(config, args, close=True) is False

    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT", "0")
    assert _live_submit_requested(config, args, close=False) is False


def test_live_policy_blocks_single_leg_entries_when_min_entry_legs_is_two(tmp_path) -> None:
    candidate = Candidate(
        candidate_id="cand",
        idea_id="idea",
        underlying="AMZN",
        playbook_id="long_call_directional",
        structure="long_call",
        legs=[
            OptionLeg(
                role="long_call",
                side="buy",
                option_type="call",
                strike=265,
                expiration="2026-08-21",
                quantity=1,
                mid=18.75,
                bid=18.55,
                ask=18.95,
                delta=0.5,
                gamma=0.0,
                theta=0.0,
                vega=0.0,
                open_interest=1000,
            )
        ],
        net_credit=-18.75,
        estimated_bpr=1875,
        greeks=Greeks(),
        liquidity_score=1.0,
        score=1.0,
        preflight=PreflightResult(ok=True, bpr=1875, message="ok", raw={"request": {}, "response": {"buyingPowerRequirement": "1875"}}),
    )

    _live_candidate_policy(
        [candidate],
        LocalStore(tmp_path / "kamandal.db"),
        {"live": {"min_entry_legs": 2, "max_bpr_per_order": 2500}, "execution": {"max_contracts_per_order": 1}},
        PortfolioState(account_size=10_000, buying_power=10_000, bpr_used=0, positions_count=0),
    )

    assert candidate.rejection_reason == "live_leg_count_below_min:1<2"


def test_live_can_warn_on_quality_filters_without_permissive_matching(tmp_path, monkeypatch) -> None:
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
            applicable_thesis_tags=["overextended"],
            applicable_horizon_min=14,
            applicable_horizon_max=60,
            dte_min=30,
            dte_max=45,
            spread_width=5,
            short_delta_min=0.15,
            short_delta_max=0.30,
            min_credit_to_width_ratio=0.95,
            max_bid_ask_pct=0.01,
            min_option_oi=10_000,
            profit_target_pct=50,
            exit_dte_min=21,
        ),
        Playbook(
            playbook_id="call_spread_permissive_only",
            enabled=True,
            strategy_family="call_spread",
            structure="call_spread",
            variant="bad_horizon",
            leg_count=2,
            profiles=["large_cap"],
            applicable_direction=["bearish"],
            applicable_thesis_tags=["overextended"],
            applicable_horizon_min=90,
            applicable_horizon_max=120,
            dte_min=30,
            dte_max=45,
            spread_width=5,
            short_delta_min=0.15,
            short_delta_max=0.30,
            profit_target_pct=50,
            exit_dte_min=21,
        ),
    ]
    monkeypatch.setattr("kamandal_v2.planner.engine.load_planner_config", lambda _config, source="sheet": (universe, playbooks))
    control = _live_control()
    control["live"]["candidate_filter_mode"] = "warn"
    control["live"]["match_gate_mode"] = "strict"
    result = run_plan(
        live_config(control),
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert result.metrics["candidate_filter_mode"] == "warn"
    assert result.metrics["match_gate_mode"] == "strict"
    assert any(candidate.eligible and candidate.playbook_id == "call_spread" for candidate in result.candidates)
    assert not any(candidate.playbook_id == "call_spread_permissive_only" for candidate in result.candidates)


def test_live_advisory_uses_sheet_playbooks_not_legacy_structure_allowlist(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)
    control = load_control()
    control["live"]["allowed_structures"] = ["long_call", "long_put"]

    result = run_live_advisory_plan(
        control,
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert result.plans
    assert result.plans[0].candidates[0].structure == "call_spread"
    assert "live_structure_not_allowed" not in result.rejection_summary


def test_live_advisory_prefers_tastytrade_iv_metrics(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)

    class FakeTastytradeAdapter:
        def __init__(self, _config):
            pass

        def available(self):
            return True

        def iv_percentile(self, _underlying):
            return 71.2

        def iv_rank(self, _underlying):
            return 23.5

        def iv_abs(self, _underlying):
            return 0.42

    monkeypatch.setattr("kamandal_v2.planner.engine.TastytradeAdapter", FakeTastytradeAdapter)
    control = _live_control()
    control["broker"]["active"] = "public"
    control["broker"]["market_metrics_provider"] = "tastytrade"

    result = run_live_advisory_plan(
        control,
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    reasons = result.plans[0].candidates[0].reasons
    assert "iv_pct=71.2" in reasons
    assert "iv_rank=23.5" in reasons
    assert "iv_abs=42.0" in reasons


def test_live_bpr_cap_uses_structure_absolute_and_account_percent(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)
    control = _live_control()
    control["live"]["max_bpr_per_order"] = 2500
    control["live"]["max_bpr_per_order_pct"] = 25
    control["live"]["max_bpr_per_order_by_structure"] = {"default": 500, "strangle": 2500}
    result = run_live_advisory_plan(
        control,
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert result.plans
    assert result.plans[0].candidates[0].estimated_bpr <= 500


def test_live_bpr_cap_rejects_default_structure_above_default_absolute(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)
    control = _live_control()
    control["live"]["max_bpr_per_order"] = 2500
    control["live"]["max_bpr_per_order_pct"] = 25
    control["live"]["max_bpr_per_order_by_structure"] = {"default": 10, "strangle": 2500}
    result = run_live_advisory_plan(
        control,
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert not result.plans
    assert any("live_bpr_above_max" in item for item in result.rejection_summary)


def test_live_bpr_cap_treats_short_strangle_as_strangle() -> None:
    from kamandal_v2.live.advisory import _candidate_bpr_cap

    candidate = type("Candidate", (), {"structure": "short_strangle"})()
    portfolio = type("Portfolio", (), {"account_size": 10_000})()
    live_cfg = {
        "max_bpr_per_order": 2500,
        "max_bpr_per_order_pct": 25,
        "max_bpr_per_order_by_structure": {"default": 500, "strangle": 2500},
    }

    assert _candidate_bpr_cap(candidate, portfolio, live_cfg) == 2500


def test_live_ticket_limit_prices_use_public_nickel_ticks() -> None:
    assert _limit_price(-12.425) == "12.45"
    assert _limit_price(1.127) == "-1.10"


def test_live_open_ticket_uses_accepted_public_preflight_limit_price() -> None:
    candidate = Candidate(
        candidate_id="cand",
        idea_id="idea",
        underlying="AMZN",
        playbook_id="long_call_directional",
        structure="long_call",
        legs=[
            OptionLeg(
                role="long_call",
                side="buy",
                option_type="call",
                strike=265,
                expiration="2026-08-21",
                quantity=1,
                mid=12.425,
                bid=12.4,
                ask=12.45,
                delta=0.5,
                gamma=0.0,
                theta=0.0,
                vega=0.0,
                open_interest=1000,
            )
        ],
        net_credit=-12.425,
        estimated_bpr=1245,
        greeks=Greeks(),
        liquidity_score=1.0,
        score=1.0,
        preflight=PreflightResult(ok=True, bpr=1245, message="ok", raw={"request": {"limitPrice": "12.45"}}),
    )
    plan = type("Plan", (), {"plan_id": "plan", "plan_rank": 1})()

    ticket = build_open_ticket(plan, candidate)

    assert ticket["limit_price"] == "12.45"
    assert ticket["submit_payload"]["limitPrice"] == "12.45"


def test_live_advisory_uses_real_account_and_writes_live_approval(tmp_path, monkeypatch) -> None:
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
    assert row["operator_action"] == APPROVE_LIVE
    assert detail["lane"] == "live_advisory"
    assert detail["order_ticket_json"]["intent_type"] == "open"
    assert detail["order_ticket_json"]["submit_payload"]["type"] == "LIMIT"
    assert detail["order_ticket_json"]["submit_payload"]["quantity"] == "1"


def test_live_advisory_auto_top_plan_sets_sheet_approval(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)
    control = _live_control()
    control["live"]["entry_approval_mode"] = "auto_top_plan"
    result = run_live_advisory_plan(
        control,
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    row = dict(zip(DAILY_PLAN_HEADER, result.daily_plan_rows[0], strict=False))

    assert row["operator_action"] == APPROVE_LIVE


def test_live_advisory_telegram_mode_creates_pending_request_without_sheet_approval(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)
    control = _live_control()
    control["live"]["entry_approval_mode"] = "telegram_approval"
    control["live"]["telegram_approval"]["enabled"] = False
    store = LocalStore(tmp_path / "kamandal.db")

    result = run_live_advisory_plan(
        control,
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )

    row = dict(zip(DAILY_PLAN_HEADER, result.daily_plan_rows[0], strict=False))
    detail = json.loads(row["plan_detail_json"])
    requests = store.live_approval_requests_by_status({"pending"})

    assert row["operator_action"] == ""
    assert detail["live_gate_status"] == "telegram_pending"
    assert detail["live_approval_request_id"]
    assert len(requests) == 1
    assert requests[0]["request_id"] == detail["live_approval_request_id"]
    assert requests[0]["ticket_hash"] == detail["order_ticket_json"]["ticket_hash"]
    assert "Approve: approve" in requests[0]["message"]


def test_approve_live_request_updates_matching_sheet_row(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)
    control = _live_control()
    control["live"]["entry_approval_mode"] = "telegram_approval"
    control["live"]["telegram_approval"]["enabled"] = False
    store = LocalStore(tmp_path / "kamandal.db")
    result = run_live_advisory_plan(
        control,
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )
    row = dict(zip(DAILY_PLAN_HEADER, result.daily_plan_rows[0], strict=False))
    request = store.live_approval_requests_by_status({"pending"})[0]
    written = {}

    class FakeSheetClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        def read_tab(self, _title):
            return [dict(row)]

        def replace_tab(self, title, *, header, rows):
            written["title"] = title
            written["header"] = header
            written["rows"] = rows
            return len(rows)

    monkeypatch.setattr("kamandal_v2.live.approval.GoogleSheetClient", FakeSheetClient)

    approved = approve_live_request(control, request["request_id"], source="telegram", store=store)

    updated_row = dict(zip(DAILY_PLAN_HEADER, written["rows"][0], strict=False))
    assert approved["status"] == "approved"
    assert updated_row["operator_action"] == APPROVE_LIVE
    assert "approved via telegram" in updated_row["operator_notes"]
    assert updated_row["plan_status"] == "telegram_approved"
    assert store.live_approval_request(request["request_id"])["_ledger_status"] == "approved"


def test_expire_live_approval_requests_marks_old_pending_request(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    request = {
        "request_id": "KAM-OLD",
        "ticket_hash": "hash",
        "plan_id": "plan",
        "candidate_id": "cand",
        "idea_id": "idea",
        "underlying": "TSLA",
        "structure": "call_spread",
        "status": "pending",
        "expires_at": "2020-01-01T00:00:00Z",
    }
    store.save_live_approval_request(request)

    result = expire_live_approval_requests(store=store)

    assert result["expired"] == 1
    assert store.live_approval_request("KAM-OLD")["_ledger_status"] == "expired"


def test_send_pending_live_approval_requests_sends_unsent_pending_request(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    request = {
        "request_id": "KAM-SEND",
        "ticket_hash": "hash",
        "plan_id": "plan",
        "candidate_id": "cand",
        "idea_id": "idea",
        "underlying": "TSLA",
        "structure": "call_spread",
        "status": "pending",
        "expires_at": (date.today() + timedelta(days=1)).isoformat() + "T00:00:00Z",
        "message": "Kamandal live approval\n\nKAM-SEND",
    }
    sent = []
    store.save_live_approval_request(request)
    monkeypatch.setattr("kamandal_v2.live.approval.send_live_approval_message", lambda _config, message: sent.append(message))

    result = send_pending_live_approval_requests(_live_control(), store=store)

    updated = store.live_approval_request("KAM-SEND")
    assert result["sent"] == 1
    assert sent == ["Kamandal live approval\n\nKAM-SEND"]
    assert updated["_ledger_status"] == "pending"
    assert updated["sent_at"]


def test_live_advisory_records_telegram_send_failure_without_blocking_sheet_row(tmp_path, monkeypatch) -> None:
    _patch_live_config(monkeypatch)
    control = _live_control()
    control["live"]["entry_approval_mode"] = "telegram_approval"
    control["live"]["telegram_approval"]["enabled"] = True
    store = LocalStore(tmp_path / "kamandal.db")

    def fail_send(_config, _message):
        raise RuntimeError("telegram unavailable")

    monkeypatch.setattr("kamandal_v2.live.approval.send_live_approval_message", fail_send)

    result = run_live_advisory_plan(
        control,
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )

    request = store.live_approval_requests_by_status({"pending"})[0]
    row = dict(zip(DAILY_PLAN_HEADER, result.daily_plan_rows[0], strict=False))
    assert row["operator_action"] == ""
    assert request["send_error"] == "telegram unavailable"


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


def test_live_execute_records_submit_failure_without_crashing(tmp_path, monkeypatch) -> None:
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

    class FailingBrokerAdapter:
        def __init__(self, _config):
            pass

        def preflight_ticket(self, _ticket):
            return PreflightResult(ok=True, bpr=50.0, message="ok")

        def place_order_ticket(self, _ticket):
            raise RuntimeError("broker rejected payload")

    live_control = _live_control()
    live_control["runtime"]["mode"] = "live"
    live_control["runtime"]["trading_enabled"] = True
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", FailingBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": [row]})

    executed = execute_live_approved(live_control, submit=True, store=store)

    assert executed["processed"] == 1
    assert executed["results"][0]["status"] == "submit_failed"
    retried = execute_live_approved(live_control, submit=True, store=store)
    assert retried["processed"] == 1
    assert retried["results"][0]["reason"] == "ticket_already_submit_failed"
    with sqlite3.connect(store.sqlite_path) as conn:
        assert conn.execute("SELECT count(*) FROM live_order_attempts WHERE ok = 0").fetchone()[0] == 1


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


def test_single_leg_close_ticket_uses_positive_public_limit_price() -> None:
    group = {
        "group_id": "live_group_1",
        "plan_id": "plan",
        "candidate_id": "cand",
        "idea_id": "idea",
        "underlying": "AMZN",
        "playbook_id": "long_call_directional",
        "structure": "long_call",
        "candidate": {
            "candidate_id": "cand",
            "idea_id": "idea",
            "underlying": "AMZN",
            "playbook_id": "long_call_directional",
            "structure": "long_call",
            "net_credit": -18.75,
            "legs": [
                {
                    "role": "long_call",
                    "side": "buy",
                    "option_type": "call",
                    "strike": 265,
                    "expiration": "2026-08-21",
                    "quantity": 1,
                    "mid": 18.75,
                    "bid": 18.55,
                    "ask": 18.95,
                    "delta": 0.5,
                    "gamma": 0.0,
                    "theta": 0.0,
                    "vega": 0.0,
                    "open_interest": 1000,
                }
            ],
        },
    }

    close_ticket = build_close_ticket(group)

    assert close_ticket["submit_payload"]["orderSide"] == "SELL"
    assert close_ticket["submit_payload"]["openCloseIndicator"] == "CLOSE"
    assert close_ticket["submit_payload"]["limitPrice"] == "18.75"


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

    control = load_control()
    control["live"]["allow_same_day_exits"] = True
    control["live"]["exit_approval_mode"] = "auto_rules"
    control["live"]["exit_pricing"]["require_fresh_quotes"] = False
    managed = run_live_management_plan(control, config_source="seed", write_sheet=False, store=store)

    assert managed["close_recommendations"] == 1
    row = dict(zip(DAILY_PLAN_HEADER, managed["daily_plan_rows"][0], strict=False))
    detail = json.loads(row["plan_detail_json"])
    assert detail["lane"] == "live_close_advisory"
    assert detail["order_ticket_json"]["intent_type"] == "close"
    assert row["operator_action"] == APPROVE_LIVE_CLOSE


def test_live_management_blocks_same_day_close_by_default(tmp_path, monkeypatch) -> None:
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

    control = load_control()
    control["live"]["exit_pricing"]["require_fresh_quotes"] = False
    managed = run_live_management_plan(control, config_source="seed", write_sheet=False, store=store)

    assert managed["close_recommendations"] == 0
    assert managed["decisions"][0]["action"] == "hold"
    assert managed["decisions"][0]["reason"] == "same_day_live_exit_blocked"


def test_execute_live_close_blocks_same_day_approved_ticket(tmp_path, monkeypatch) -> None:
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
    open_ticket = json.loads(row["plan_detail_json"])["order_ticket_json"]
    record_manual_live_fill(open_ticket["ticket_hash"], store=store)
    group = store.open_live_position_groups()[0]
    close_ticket = build_close_ticket(group)
    store.save_live_order_intent(close_ticket, status="pending_close_approval")
    close_row = dict(zip(DAILY_PLAN_HEADER, ["" for _ in DAILY_PLAN_HEADER], strict=False))
    close_row["operator_action"] = APPROVE_LIVE_CLOSE
    close_row["mode"] = "live_close_advisory"
    close_row["plan_detail_json"] = json.dumps({"lane": "live_close_advisory", "order_ticket_json": close_ticket})
    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": [close_row]})

    live_control = _live_control()
    live_control["runtime"]["mode"] = "live"
    live_control["runtime"]["trading_enabled"] = True
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    executed = execute_live_approved(live_control, submit=True, close=True, store=store)

    assert executed["processed"] == 1
    assert executed["results"][0]["reason"] == "same_day_live_exit_blocked"


def test_execute_live_close_allows_same_day_after_configured_date(tmp_path, monkeypatch) -> None:
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
    open_ticket = json.loads(row["plan_detail_json"])["order_ticket_json"]
    record_manual_live_fill(open_ticket["ticket_hash"], store=store)
    group = store.open_live_position_groups()[0]
    close_ticket = build_close_ticket(group)
    store.save_live_order_intent(close_ticket, status="pending_close_approval")
    close_row = dict(zip(DAILY_PLAN_HEADER, ["" for _ in DAILY_PLAN_HEADER], strict=False))
    close_row["operator_action"] = APPROVE_LIVE_CLOSE
    close_row["mode"] = "live_close_advisory"
    close_row["plan_detail_json"] = json.dumps({"lane": "live_close_advisory", "order_ticket_json": close_ticket})

    class PassingBrokerAdapter:
        def __init__(self, _config):
            pass

        def preflight_ticket(self, _ticket):
            return PreflightResult(ok=True, bpr=0.0, message="ok")

        def place_order_ticket(self, ticket):
            return {"orderId": ticket["order_id"]}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", PassingBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": [close_row]})
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    live_control = _live_control()
    live_control["runtime"]["mode"] = "live"
    live_control["runtime"]["trading_enabled"] = True
    live_control["live"]["allow_same_day_exits_after"] = "2000-01-01"

    executed = execute_live_approved(live_control, submit=True, close=True, store=store)

    assert executed["processed"] == 1
    assert executed["results"][0]["status"] == "submitted"


def test_cleanup_live_approvals_clears_terminal_ticket_status(tmp_path, monkeypatch) -> None:
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
    ticket = json.loads(row["plan_detail_json"])["order_ticket_json"]
    store.update_live_order_intent_status(ticket["ticket_hash"], "filled")
    written = {}

    class FakeSheetClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        def read_tab(self, _title):
            return [row]

        def replace_tab(self, _title, *, header, rows):
            written["rows"] = [dict(zip(header, item, strict=False)) for item in rows]
            return len(rows)

    monkeypatch.setattr("kamandal_v2.live.execution.GoogleSheetClient", FakeSheetClient)

    cleaned = cleanup_live_approvals(load_control(), store=store)

    assert cleaned["cleared"] == 1
    assert written["rows"][0]["operator_action"] == ""
    assert written["rows"][0]["plan_status"] == "filled"
    assert "auto-cleared stale APPROVE_LIVE" in written["rows"][0]["operator_notes"]
