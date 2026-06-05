import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

from kamandal_v2.config import load_control
from kamandal_v2.cli import _live_submit_requested
from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg, Plan, Playbook, PortfolioState, PreflightResult, UniverseEntry
from kamandal_v2.live.approval import approve_live_request, expire_live_approval_requests, send_pending_live_approval_requests
from kamandal_v2.live.advisory import _live_candidate_policy, live_config, render_live_plan_rows, run_live_advisory_plan
from kamandal_v2.live.execution import cleanup_live_approvals, execute_live_approved, record_manual_live_fill, sync_live_orders
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


def _patch_live_config(monkeypatch, *, profit_target_pct: float = 50) -> None:
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
            profit_target_pct=profit_target_pct,
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
    monkeypatch.setenv("KAMANDAL_LIVE_MAX_ORDERS_PER_PLAN", "3")
    monkeypatch.setenv("KAMANDAL_LIVE_MAX_ENTRY_SUBMITS_PER_RUN", "1")
    monkeypatch.setenv("KAMANDAL_LIVE_MAX_BASKETS_PER_DAY", "3")
    monkeypatch.setenv("KAMANDAL_LIVE_BASKET_TARGET_NEW_BPR_PCT", "7")
    monkeypatch.setenv("KAMANDAL_LIVE_BASKET_HARD_NEW_BPR_PCT", "9")
    monkeypatch.setenv("KAMANDAL_LIVE_BASKET_MIN_MARGINAL_SCORE", "4")
    monkeypatch.setenv("KAMANDAL_ENTRY_REPRICE_EXPIRE_AFTER_MINUTES", "45")
    monkeypatch.setenv("KAMANDAL_TELEGRAM_APPROVAL_TARGET", "123")
    monkeypatch.setenv("KAMANDAL_TELEGRAM_APPROVAL_EXPIRY_MINUTES", "7")

    control = load_control()

    assert control["live"]["entry_approval_mode"] == "auto_top_plan"
    assert control["live"]["exit_approval_mode"] == "auto_rules"
    assert control["live"]["auto_submit_entries"] is True
    assert control["live"]["auto_submit_exits"] is False
    assert control["live"]["max_orders_per_plan"] == 3
    assert control["live"]["max_live_entry_submits_per_run"] == 1
    assert control["live"]["max_live_baskets_per_day"] == 3
    assert control["live"]["basket"]["target_new_bpr_pct"] == 7
    assert control["live"]["basket"]["hard_new_bpr_pct"] == 9
    assert control["live"]["basket"]["min_marginal_score"] == 4
    assert control["live"]["entry_reprice"]["expire_after_minutes"] == 45
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


def test_live_policy_allows_unaligned_mentioned_strategy_by_default(tmp_path) -> None:
    legs = [
        OptionLeg("long_put", "buy", "put", 920, "2026-07-10", 1, 1.0, 0.95, 1.05, -0.25, 0.0, -0.01, 0.1, 100),
        OptionLeg("short_put", "sell", "put", 925, "2026-07-10", 1, 2.0, 1.95, 2.05, -0.30, 0.0, -0.01, 0.1, 100),
    ]
    candidate = Candidate(
        candidate_id="cand",
        idea_id="idea",
        underlying="COST",
        playbook_id="put_spread_default",
        structure="put_spread",
        legs=legs,
        net_credit=1.0,
        estimated_bpr=400,
        greeks=Greeks(theta=0.01),
        liquidity_score=1.0,
        score=1.0,
        reasons=["mentioned_strategy=call_calendar"],
        preflight=PreflightResult(ok=True, bpr=400, message="ok", raw={"response": {"buyingPowerRequirement": "400"}}),
    )

    _live_candidate_policy(
        [candidate],
        LocalStore(tmp_path / "kamandal.db"),
        {"live": {"min_entry_legs": 2, "max_bpr_per_order": 2500}, "execution": {"max_contracts_per_order": 1}},
        PortfolioState(account_size=10_000, buying_power=10_000, bpr_used=0, positions_count=0),
    )

    assert candidate.rejection_reason == ""


def test_live_policy_can_strictly_block_unaligned_mentioned_strategy(tmp_path) -> None:
    legs = [
        OptionLeg("long_put", "buy", "put", 920, "2026-07-10", 1, 1.0, 0.95, 1.05, -0.25, 0.0, -0.01, 0.1, 100),
        OptionLeg("short_put", "sell", "put", 925, "2026-07-10", 1, 2.0, 1.95, 2.05, -0.30, 0.0, -0.01, 0.1, 100),
    ]
    candidate = Candidate(
        candidate_id="cand",
        idea_id="idea",
        underlying="COST",
        playbook_id="put_spread_default",
        structure="put_spread",
        legs=legs,
        net_credit=1.0,
        estimated_bpr=400,
        greeks=Greeks(theta=0.01),
        liquidity_score=1.0,
        score=1.0,
        reasons=["mentioned_strategy=call_calendar"],
        preflight=PreflightResult(ok=True, bpr=400, message="ok", raw={"response": {"buyingPowerRequirement": "400"}}),
    )

    _live_candidate_policy(
        [candidate],
        LocalStore(tmp_path / "kamandal.db"),
        {"live": {"mentioned_strategy_policy": "strict", "min_entry_legs": 2, "max_bpr_per_order": 2500}, "execution": {"max_contracts_per_order": 1}},
        PortfolioState(account_size=10_000, buying_power=10_000, bpr_used=0, positions_count=0),
    )

    assert candidate.rejection_reason == "live_mentioned_strategy_mismatch:call_calendar!=put_spread"


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
    store = LocalStore(tmp_path / "kamandal.db")

    result = run_live_advisory_plan(
        control,
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        persist_order_intents=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert result.plans
    assert result.plans[0].candidates[0].structure == "call_spread"
    assert "live_structure_not_allowed" not in result.rejection_summary
    assert store.live_order_intents_by_status({"pending_approval"}) == []


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


def test_live_advisory_row_carries_all_basket_tickets(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    before = PortfolioState(account_size=10_000, buying_power=10_000, bpr_used=0, positions_count=0, greeks=Greeks())
    after = PortfolioState(account_size=10_000, buying_power=9_200, bpr_used=800, positions_count=2, greeks=Greeks(delta=-0.2, theta=0.1))
    plan = Plan(
        plan_id="plan_basket",
        plan_rank=1,
        status="eligible",
        candidates=[
            _ticket_candidate("cand_msft", "idea_msft", "MSFT"),
            _ticket_candidate("cand_nvda", "idea_nvda", "NVDA"),
        ],
        score=42.0,
        total_bpr=800.0,
        bpr_utilization_pct=8.0,
        buying_power_after=9_200.0,
        portfolio_before=before,
        portfolio_after=after,
    )
    result = type("PlanRunResult", (), {"plans": [plan], "metrics": {}})()

    rows = render_live_plan_rows(result, {"live": {"entry_approval_mode": "auto_top_plan"}}, store=store)

    row = dict(zip(DAILY_PLAN_HEADER, rows[0], strict=False))
    detail = json.loads(row["plan_detail_json"])
    tickets = detail["order_tickets_json"]
    assert row["operator_action"] == APPROVE_LIVE
    assert detail["order_ticket_json"]["candidate_id"] == "cand_msft"
    assert [ticket["candidate_id"] for ticket in tickets] == ["cand_msft", "cand_nvda"]
    assert detail["basket_execution_json"]["mode"] == "concurrent"
    assert store.live_order_intent(tickets[0]["ticket_hash"])["_ledger_status"] == "pending_approval"
    assert store.live_order_intent(tickets[1]["ticket_hash"])["_ledger_status"] == "pending_approval"


def _ticket_candidate(candidate_id: str, idea_id: str, underlying: str) -> Candidate:
    legs = [
        OptionLeg("long_put", "buy", "put", 95, "2026-07-17", 1, 0.8, 0.75, 0.85, -0.2, 0.0, -0.01, 0.1, 500),
        OptionLeg("short_put", "sell", "put", 100, "2026-07-17", 1, 1.8, 1.75, 1.85, -0.3, 0.0, -0.02, 0.1, 500),
    ]
    return Candidate(
        candidate_id=candidate_id,
        idea_id=idea_id,
        underlying=underlying,
        playbook_id="put_spread_default",
        structure="put_spread",
        legs=legs,
        net_credit=1.0,
        estimated_bpr=400.0,
        greeks=Greeks(delta=-0.1, theta=0.05),
        liquidity_score=0.9,
        score=10.0,
        preflight=PreflightResult(ok=True, bpr=400.0, message="ok", raw={"response": {"buyingPowerRequirement": "400"}}),
    )


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


def test_live_execute_approved_dry_run_can_process_basket_tickets(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    before = PortfolioState(account_size=10_000, buying_power=10_000, bpr_used=0, positions_count=0, greeks=Greeks())
    after = PortfolioState(account_size=10_000, buying_power=9_200, bpr_used=800, positions_count=2, greeks=Greeks(delta=-0.2, theta=0.1))
    plan = Plan(
        plan_id="plan_basket",
        plan_rank=1,
        status="eligible",
        candidates=[
            _ticket_candidate("cand_msft", "idea_msft", "MSFT"),
            _ticket_candidate("cand_nvda", "idea_nvda", "NVDA"),
        ],
        score=42.0,
        total_bpr=800.0,
        bpr_utilization_pct=8.0,
        buying_power_after=9_200.0,
        portfolio_before=before,
        portfolio_after=after,
    )
    result = type("PlanRunResult", (), {"plans": [plan], "metrics": {}})()
    rows = render_live_plan_rows(
        result,
        {"live": {"entry_approval_mode": "auto_top_plan", "max_orders_per_plan": 2}},
        store=store,
    )
    row = dict(zip(DAILY_PLAN_HEADER, rows[0], strict=False))
    row["operator_action"] = APPROVE_LIVE

    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": [row]})
    config = load_control()
    config["live"]["max_orders_per_plan"] = 2

    executed = execute_live_approved(config, submit=False, store=store)

    assert executed["processed"] == 2
    assert [item["status"] for item in executed["results"]] == ["dry_run", "dry_run"]
    with sqlite3.connect(store.sqlite_path) as conn:
        assert conn.execute("SELECT count(*) FROM live_order_attempts").fetchone()[0] == 2


def test_live_submit_stages_next_basket_ticket_after_prior_fill(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    before = PortfolioState(account_size=10_000, buying_power=10_000, bpr_used=0, positions_count=0, greeks=Greeks())
    after = PortfolioState(account_size=10_000, buying_power=9_200, bpr_used=800, positions_count=2, greeks=Greeks(delta=-0.2, theta=0.1))
    plan = Plan(
        plan_id="plan_basket",
        plan_rank=1,
        status="eligible",
        candidates=[
            _ticket_candidate("cand_msft", "idea_msft", "MSFT"),
            _ticket_candidate("cand_nvda", "idea_nvda", "NVDA"),
        ],
        score=42.0,
        total_bpr=800.0,
        bpr_utilization_pct=8.0,
        buying_power_after=9_200.0,
        portfolio_before=before,
        portfolio_after=after,
    )
    rows = render_live_plan_rows(
        type("PlanRunResult", (), {"plans": [plan], "metrics": {}})(),
        {"live": {"entry_approval_mode": "auto_top_plan", "max_live_entry_submits_per_run": 1}},
        store=store,
    )
    row = dict(zip(DAILY_PLAN_HEADER, rows[0], strict=False))
    row["operator_action"] = APPROVE_LIVE
    tickets = json.loads(row["plan_detail_json"])["order_tickets_json"]
    store.update_live_order_intent_status(tickets[0]["ticket_hash"], "filled")

    class PassingBrokerAdapter:
        def __init__(self, _config):
            pass

        def preflight_ticket(self, _ticket):
            return PreflightResult(ok=True, bpr=400.0, message="ok")

        def place_order_ticket(self, ticket):
            return {"orderId": ticket["order_id"]}

    live_control = _live_control()
    live_control["runtime"]["mode"] = "live"
    live_control["runtime"]["trading_enabled"] = True
    live_control["live"]["max_live_entry_submits_per_run"] = 1
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", PassingBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": [row]})

    executed = execute_live_approved(live_control, submit=True, store=store)

    assert executed["processed"] == 1
    assert executed["results"][0]["ticket_hash"] == tickets[1]["ticket_hash"]
    assert executed["results"][0]["status"] == "submitted"


def test_live_submit_can_submit_multiple_pending_basket_tickets_when_configured(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    before = PortfolioState(account_size=10_000, buying_power=10_000, bpr_used=0, positions_count=0, greeks=Greeks())
    after = PortfolioState(account_size=10_000, buying_power=9_200, bpr_used=800, positions_count=2, greeks=Greeks(delta=-0.2, theta=0.1))
    plan = Plan(
        plan_id="plan_basket",
        plan_rank=1,
        status="eligible",
        candidates=[
            _ticket_candidate("cand_msft", "idea_msft", "MSFT"),
            _ticket_candidate("cand_nvda", "idea_nvda", "NVDA"),
        ],
        score=42.0,
        total_bpr=800.0,
        bpr_utilization_pct=8.0,
        buying_power_after=9_200.0,
        portfolio_before=before,
        portfolio_after=after,
    )
    rows = render_live_plan_rows(
        type("PlanRunResult", (), {"plans": [plan], "metrics": {}})(),
        {"live": {"entry_approval_mode": "auto_top_plan", "max_live_entry_submits_per_run": 2}},
        store=store,
    )
    row = dict(zip(DAILY_PLAN_HEADER, rows[0], strict=False))
    row["operator_action"] = APPROVE_LIVE

    class PassingBrokerAdapter:
        def __init__(self, _config):
            pass

        def preflight_ticket(self, _ticket):
            return PreflightResult(ok=True, bpr=400.0, message="ok")

        def place_order_ticket(self, ticket):
            return {"orderId": ticket["order_id"]}

    live_control = _live_control()
    live_control["runtime"]["mode"] = "live"
    live_control["runtime"]["trading_enabled"] = True
    live_control["live"]["max_live_entry_submits_per_run"] = 2
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", PassingBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": [row]})

    executed = execute_live_approved(live_control, submit=True, store=store)

    assert executed["processed"] == 2
    assert [item["status"] for item in executed["results"]] == ["submitted", "submitted"]


def test_cleanup_live_approvals_keeps_pending_basket_ticket(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    before = PortfolioState(account_size=10_000, buying_power=10_000, bpr_used=0, positions_count=0, greeks=Greeks())
    after = PortfolioState(account_size=10_000, buying_power=9_200, bpr_used=800, positions_count=2, greeks=Greeks(delta=-0.2, theta=0.1))
    plan = Plan(
        plan_id="plan_basket",
        plan_rank=1,
        status="eligible",
        candidates=[
            _ticket_candidate("cand_msft", "idea_msft", "MSFT"),
            _ticket_candidate("cand_nvda", "idea_nvda", "NVDA"),
        ],
        score=42.0,
        total_bpr=800.0,
        bpr_utilization_pct=8.0,
        buying_power_after=9_200.0,
        portfolio_before=before,
        portfolio_after=after,
    )
    rows = render_live_plan_rows(
        type("PlanRunResult", (), {"plans": [plan], "metrics": {}})(),
        {"live": {"entry_approval_mode": "auto_top_plan"}},
        store=store,
    )
    row = dict(zip(DAILY_PLAN_HEADER, rows[0], strict=False))
    row["operator_action"] = APPROVE_LIVE
    tickets = json.loads(row["plan_detail_json"])["order_tickets_json"]
    store.update_live_order_intent_status(tickets[0]["ticket_hash"], "filled")
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

    assert cleaned["cleared"] == 0
    assert written == {}


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
    assert retried["results"][0]["reason"] == "basket_ticket_failed:submit_failed"
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


def _store_filled_fixture_position_with_cheap_chain(tmp_path, monkeypatch, *, profit_target_pct: float) -> LocalStore:
    _patch_live_config(monkeypatch, profit_target_pct=profit_target_pct)
    store = LocalStore(tmp_path / f"kamandal_{str(profit_target_pct).replace('.', '_')}.db")
    result = run_live_advisory_plan(
        _live_control(),
        idea_paths=[_ideas_file(tmp_path)],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / f"audit_{str(profit_target_pct).replace('.', '_')}"),
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
    return store


def test_live_management_normalizes_sheet_fraction_profit_targets(tmp_path, monkeypatch) -> None:
    for raw_profit_target_pct, expected_profit_target_pct in ((0.5, 50.0), (0.25, 25.0)):
        store = _store_filled_fixture_position_with_cheap_chain(
            tmp_path,
            monkeypatch,
            profit_target_pct=raw_profit_target_pct,
        )
        control = load_control()
        control["live"]["allow_same_day_exits"] = True
        control["live"]["exit_approval_mode"] = "disabled"
        control["live"]["exit_pricing"]["require_fresh_quotes"] = False

        managed = run_live_management_plan(control, config_source="seed", write_sheet=False, store=store)

        assert managed["marks"][0]["profit_target_pct"] == expected_profit_target_pct
        assert managed["marks"][0]["target_profit"] == round(
            managed["marks"][0]["entry_value"] * expected_profit_target_pct / 100.0,
            2,
        )
        assert managed["decisions"][0]["action"] == "close"
        assert managed["decisions"][0]["reason"] == "profit_target"


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


def test_live_management_surfaces_loss_watch_without_close_ticket(tmp_path, monkeypatch) -> None:
    universe = [UniverseEntry(symbol="TSLA", enabled=True, profile="large_cap", allowed_playbooks=["put_spread"])]
    playbooks = [
        Playbook(
            playbook_id="put_spread",
            enabled=True,
            strategy_family="put_spread",
            structure="put_spread",
            variant="default",
            leg_count=2,
            profiles=["large_cap"],
            max_loss_multiple=2.0,
            profit_target_pct=50,
            exit_dte_min=21,
            half_time_exit=False,
        )
    ]
    monkeypatch.setattr("kamandal_v2.live.management.load_planner_config", lambda _config, source="sheet": (universe, playbooks))
    store = LocalStore(tmp_path / "kamandal.db")
    group = {
        "group_id": "loss_watch_group",
        "plan_id": "plan_loss_watch",
        "candidate_id": "candidate_loss_watch",
        "idea_id": "idea_loss_watch",
        "underlying": "TSLA",
        "playbook_id": "put_spread",
        "structure": "put_spread",
        "candidate": {
            "candidate_id": "candidate_loss_watch",
            "idea_id": "idea_loss_watch",
            "underlying": "TSLA",
            "playbook_id": "put_spread",
            "structure": "put_spread",
            "net_credit": 1.0,
            "estimated_bpr": 400.0,
            "legs": [
                {"side": "sell", "option_type": "put", "expiration": "2026-07-17", "strike": 100.0, "quantity": 1},
                {"side": "buy", "option_type": "put", "expiration": "2026-07-17", "strike": 95.0, "quantity": 1},
            ],
        },
    }
    store.save_live_position_group("loss_watch_group", group)
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chain_snapshots VALUES (?, ?, ?)",
            (
                "loss_watch_chain",
                "TSLA",
                json.dumps({
                    "captured_at": "2099-01-01T14:00:00Z",
                    "underlying": "TSLA",
                    "underlying_price": 102.0,
                    "quotes": [
                        {"expiration": "2026-07-17", "option_type": "put", "strike": 100.0, "bid": 4.0, "ask": 4.2, "delta": -0.55},
                        {"expiration": "2026-07-17", "option_type": "put", "strike": 95.0, "bid": 2.0, "ask": 2.2, "delta": -0.35},
                    ],
                }),
            ),
        )
    control = load_control()
    control["live"]["exit_approval_mode"] = "auto_rules"
    control["live"]["exit_pricing"]["require_fresh_quotes"] = False

    first = run_live_management_plan(control, config_source="seed", write_sheet=False, store=store)

    assert first["close_recommendations"] == 0
    assert first["review_recommendations"] == 0
    assert first["decisions"][0]["action"] == "hold"
    assert first["decisions"][0]["reason"] == "loss_watch_debouncing"
    assert first["marks"][0]["max_loss_watch"] is True
    assert first["marks"][0]["loss_watch_observations"]["count"] == 1
    assert first["daily_plan_rows"] == []

    managed = run_live_management_plan(control, config_source="seed", write_sheet=False, store=store)

    assert managed["close_recommendations"] == 0
    assert managed["review_recommendations"] == 1
    assert managed["decisions"][0]["action"] == "review"
    assert managed["decisions"][0]["reason"] == "loss_watch"
    assert managed["marks"][0]["loss_watch_observations"]["count"] == 2
    row = dict(zip(DAILY_PLAN_HEADER, managed["daily_plan_rows"][0], strict=False))
    detail = json.loads(row["plan_detail_json"])
    assert row["plan_status"] == "review"
    assert row["operator_action"] == ""
    assert "order_ticket_json" not in detail


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


def test_cleanup_live_approvals_clears_close_filled_ticket_status(tmp_path, monkeypatch) -> None:
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
    open_row = dict(zip(DAILY_PLAN_HEADER, result.daily_plan_rows[0], strict=False))
    open_ticket = json.loads(open_row["plan_detail_json"])["order_ticket_json"]
    record_manual_live_fill(open_ticket["ticket_hash"], store=store)
    group = store.open_live_position_groups()[0]
    close_ticket = build_close_ticket(group)
    store.save_live_order_intent(close_ticket, status="pending_close_approval")
    row = dict(zip(DAILY_PLAN_HEADER, ["" for _ in DAILY_PLAN_HEADER], strict=False))
    row["operator_action"] = APPROVE_LIVE_CLOSE
    row["mode"] = "live_close_advisory"
    row["plan_detail_json"] = json.dumps({"lane": "live_close_advisory", "order_ticket_json": close_ticket})
    store.update_live_order_intent_status(close_ticket["ticket_hash"], "close_filled")
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
    assert "auto-cleared stale APPROVE_LIVE_CLOSE" in written["rows"][0]["operator_notes"]


def test_sync_live_orders_marks_cancelled_intent_terminal(tmp_path, monkeypatch) -> None:
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
    store.update_live_order_intent_status(ticket["ticket_hash"], "submitted")

    class CancelledBrokerAdapter:
        def __init__(self, _config):
            pass

        def get_order(self, _order_id):
            return {"status": "CANCELLED"}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", CancelledBrokerAdapter)

    synced = sync_live_orders(_live_control(), store=store)

    assert synced["synced"] == 1
    assert synced["orders"][0]["status"] == "CANCELLED"
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "cancelled"


def test_sync_live_orders_polls_cancel_pending_repriced_intent(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "order_id": "order-old",
        "plan_id": "plan",
        "candidate_id": "cand",
        "idea_id": "idea",
        "intent_type": "open",
        "underlying": "GOOGL",
        "playbook_id": "put_spread_default",
        "structure": "put_spread",
        "quantity": 1,
        "limit_price": "-3.65",
        "time_in_force": "DAY",
        "created_at": "2026-06-05T14:00:00Z",
        "legs": [],
        "submit_payload": {"orderId": "order-old", "quantity": "1", "type": "LIMIT", "limitPrice": "-3.65", "legs": []},
    }
    from kamandal_v2.live.orders import ticket_hash

    ticket["ticket_hash"] = ticket_hash(ticket)
    store.save_live_order_intent(ticket, status="repriced")

    class CancelledBrokerAdapter:
        def __init__(self, _config):
            pass

        def get_order(self, _order_id):
            return {"status": "CANCELLED"}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", CancelledBrokerAdapter)

    synced = sync_live_orders(_live_control(), store=store)

    assert synced["synced"] == 1
    assert synced["orders"][0]["ledger_status"] == "repriced"
    assert synced["orders"][0]["status"] == "CANCELLED"
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "cancelled"


def test_sync_live_orders_flags_cancel_pending_working_order(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "order_id": "order-old",
        "plan_id": "plan",
        "candidate_id": "cand",
        "idea_id": "idea",
        "intent_type": "open",
        "underlying": "MRVL",
        "playbook_id": "put_spread_default",
        "structure": "put_spread",
        "quantity": 1,
        "limit_price": "-2.12",
        "time_in_force": "DAY",
        "created_at": "2026-06-05T14:00:00Z",
        "legs": [],
        "submit_payload": {"orderId": "order-old", "quantity": "1", "type": "LIMIT", "limitPrice": "-2.12", "legs": []},
    }
    from kamandal_v2.live.orders import ticket_hash

    ticket["ticket_hash"] = ticket_hash(ticket)
    store.save_live_order_intent(ticket, status="expired")

    class WorkingBrokerAdapter:
        def __init__(self, _config):
            pass

        def get_order(self, _order_id):
            return {"status": "NEW"}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", WorkingBrokerAdapter)

    synced = sync_live_orders(_live_control(), store=store)

    assert synced["orders"][0]["ledger_status"] == "expired"
    assert synced["orders"][0]["cancel_pending"] is True
    assert synced["orders"][0]["needs_broker_cancel_review"] is True
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "expired"


def test_sync_live_orders_read_only_skips_entry_management(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "order_id": "order-old",
        "plan_id": "plan",
        "candidate_id": "cand",
        "idea_id": "idea",
        "intent_type": "open",
        "underlying": "COST",
        "playbook_id": "put_spread_default",
        "structure": "put_spread",
        "quantity": 1,
        "limit_price": "-2.25",
        "time_in_force": "DAY",
        "created_at": "2026-05-29T14:00:00Z",
        "preflight": {"raw": {"entry_pricing": {"base_mid_limit": 2.15, "improved_limit": 2.25}}},
        "legs": [],
        "submit_payload": {"orderId": "order-old", "quantity": "1", "type": "LIMIT", "limitPrice": "-2.25", "legs": []},
    }
    from kamandal_v2.live.orders import ticket_hash

    ticket["ticket_hash"] = ticket_hash(ticket)
    store.save_live_order_intent(ticket, status="submitted")
    calls = []

    class WorkingBrokerAdapter:
        def __init__(self, _config):
            pass

        def get_order(self, _order_id):
            return {"status": "NEW", "createdAt": "2026-05-29T14:00:00Z"}

        def cancel_order(self, order_id):
            calls.append(("cancel", order_id))
            return {"orderId": order_id}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", WorkingBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.datetime", type("FrozenDateTime", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 5, 29, 14, 31, tzinfo=UTC))}))
    config = _live_control()
    config["live"]["entry_reprice"] = {"enabled": True, "after_minutes": 5, "max_reprices": 1, "expire_after_minutes": 30}

    synced = sync_live_orders(config, store=store, manage_entries=False)

    assert synced["manage_entries"] is False
    assert synced["orders"][0]["entry_management_skipped"] is True
    assert calls == []
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "submitted"


def test_sync_live_orders_continues_after_order_fetch_failure(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    from kamandal_v2.live.orders import ticket_hash

    missing = {
        "order_id": "missing-order",
        "plan_id": "plan",
        "candidate_id": "cand-missing",
        "idea_id": "idea",
        "intent_type": "open",
        "underlying": "AAPL",
        "playbook_id": "put_spread_default",
        "structure": "put_spread",
        "quantity": 1,
        "limit_price": "-1.38",
        "time_in_force": "DAY",
        "created_at": "2026-05-12T14:24:47Z",
        "legs": [],
        "submit_payload": {"orderId": "missing-order", "legs": []},
    }
    active = {
        **missing,
        "order_id": "active-order",
        "candidate_id": "cand-active",
        "underlying": "GOOGL",
        "created_at": "2026-06-05T14:24:47Z",
        "submit_payload": {"orderId": "active-order", "legs": []},
    }
    missing["ticket_hash"] = ticket_hash(missing)
    active["ticket_hash"] = ticket_hash(active)
    store.save_live_order_intent(missing, status="expired")
    store.save_live_order_intent(active, status="submitted")

    class PartiallyMissingBrokerAdapter:
        def __init__(self, _config):
            pass

        def get_order(self, order_id):
            if order_id == "missing-order":
                raise RuntimeError("Public API GET /userapigateway/trading/5OS69079/order/missing-order failed status=404: ")
            return {"status": "CANCELLED"}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", PartiallyMissingBrokerAdapter)

    synced = sync_live_orders(_live_control(), store=store, manage_entries=False)

    assert synced["synced"] == 2
    errors = [order for order in synced["orders"] if order["status"] == "BROKER_STATUS_FETCH_FAILED"]
    assert len(errors) == 1
    assert "/trading/<account>/" in errors[0]["error"]
    assert "5OS69079" not in errors[0]["error"]
    assert synced["orders"][0]["status"] == "CANCELLED"


def test_sync_live_orders_reprices_stale_new_entry_once(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "order_id": "order-old",
        "plan_id": "plan",
        "plan_rank": 1,
        "candidate_id": "cand",
        "idea_id": "idea",
        "intent_type": "open",
        "underlying": "COST",
        "playbook_id": "put_spread_default",
        "structure": "put_spread",
        "quantity": 1,
        "limit_price": "-2.25",
        "time_in_force": "DAY",
        "created_at": "2026-05-29T14:00:00Z",
        "preflight": {"raw": {"entry_pricing": {"base_mid_limit": 2.15, "improved_limit": 2.25}}},
        "legs": [
            {"role": "long_put", "side": "buy", "option_type": "put", "strike": 920, "expiration": "2026-07-10", "quantity": 1},
            {"role": "short_put", "side": "sell", "option_type": "put", "strike": 925, "expiration": "2026-07-10", "quantity": 1},
        ],
        "submit_payload": {"orderId": "order-old", "quantity": "1", "type": "LIMIT", "limitPrice": "-2.25", "legs": []},
    }
    from kamandal_v2.live.orders import ticket_hash

    ticket["ticket_hash"] = ticket_hash(ticket)
    store.save_live_order_intent(ticket, status="submitted")
    calls = []

    class RepriceBrokerAdapter:
        def __init__(self, _config):
            pass

        def get_order(self, _order_id):
            return {"status": "NEW", "createdAt": "2026-05-29T14:00:00Z"}

        def cancel_order(self, order_id):
            calls.append(("cancel", order_id))
            return {"orderId": order_id, "status": "CANCEL_REQUESTED"}

        def preflight_ticket(self, repriced_ticket):
            calls.append(("preflight", repriced_ticket["limit_price"]))
            return PreflightResult(ok=True, bpr=200, message="ok", raw={"request": repriced_ticket["submit_payload"], "response": {"buyingPowerRequirement": "200"}})

        def place_order_ticket(self, repriced_ticket):
            calls.append(("place", repriced_ticket["order_id"], repriced_ticket["limit_price"]))
            return {"orderId": repriced_ticket["order_id"]}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", RepriceBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.datetime", type("FrozenDateTime", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 5, 29, 14, 6, tzinfo=UTC))}))
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    config = _live_control()
    config["runtime"]["mode"] = "live"
    config["runtime"]["trading_enabled"] = True
    config["live"]["entry_reprice"] = {"enabled": True, "after_minutes": 5, "max_reprices": 1, "improvement_multiplier": 0.5}

    synced = sync_live_orders(config, store=store)

    assert synced["orders"][0]["reprice_status"] == "submitted"
    assert ("preflight", "-2.20") in calls
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "repriced"
    repriced = store.live_order_intent(synced["orders"][0]["reprice_ticket_hash"])
    assert repriced["_ledger_status"] == "submitted"
    assert repriced["limit_price"] == "-2.20"


def test_sync_live_orders_reprices_stale_entry_twice_then_expires(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "order_id": "order-old",
        "plan_id": "plan",
        "plan_rank": 1,
        "candidate_id": "cand",
        "idea_id": "idea",
        "intent_type": "open",
        "underlying": "COST",
        "playbook_id": "put_spread_default",
        "structure": "put_spread",
        "quantity": 1,
        "limit_price": "-2.25",
        "time_in_force": "DAY",
        "created_at": "2026-05-29T14:00:00Z",
        "preflight": {"raw": {"entry_pricing": {"base_mid_limit": 2.15, "improved_limit": 2.25}}},
        "legs": [
            {"role": "long_put", "side": "buy", "option_type": "put", "strike": 920, "expiration": "2026-07-10", "quantity": 1},
            {"role": "short_put", "side": "sell", "option_type": "put", "strike": 925, "expiration": "2026-07-10", "quantity": 1},
        ],
        "submit_payload": {"orderId": "order-old", "quantity": "1", "type": "LIMIT", "limitPrice": "-2.25", "legs": []},
    }
    from kamandal_v2.live.orders import ticket_hash

    ticket["ticket_hash"] = ticket_hash(ticket)
    store.save_live_order_intent(ticket, status="submitted")
    calls = []
    order_created_at = {"order-old": "2026-05-29T14:00:00Z"}
    current_now = {"value": datetime(2026, 5, 29, 14, 6, tzinfo=UTC)}

    class RepriceBrokerAdapter:
        def __init__(self, _config):
            pass

        def get_order(self, order_id):
            return {"status": "NEW", "createdAt": order_created_at[order_id]}

        def cancel_order(self, order_id):
            calls.append(("cancel", order_id))
            return {"orderId": order_id, "status": "CANCEL_REQUESTED"}

        def preflight_ticket(self, repriced_ticket):
            calls.append(("preflight", repriced_ticket["limit_price"]))
            return PreflightResult(ok=True, bpr=200, message="ok", raw={"request": repriced_ticket["submit_payload"], "response": {"buyingPowerRequirement": "200"}})

        def place_order_ticket(self, repriced_ticket):
            calls.append(("place", repriced_ticket["order_id"], repriced_ticket["limit_price"]))
            order_created_at[repriced_ticket["order_id"]] = repriced_ticket["created_at"]
            return {"orderId": repriced_ticket["order_id"]}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", RepriceBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.datetime", type("FrozenDateTime", (datetime,), {"now": classmethod(lambda cls, tz=None: current_now["value"])}))
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    config = _live_control()
    config["runtime"]["mode"] = "live"
    config["runtime"]["trading_enabled"] = True
    config["live"]["entry_reprice"] = {
        "enabled": True,
        "after_minutes": 5,
        "max_reprices": 2,
        "improvement_multipliers": [0.5, 0.0],
        "expire_after_minutes": 30,
    }

    first = sync_live_orders(config, store=store)
    first_ticket = store.live_order_intent(first["orders"][0]["reprice_ticket_hash"])
    assert first_ticket["limit_price"] == "-2.20"
    assert first_ticket["preflight"]["raw"]["entry_pricing"] == {"base_mid_limit": 2.15, "improved_limit": 2.25}

    current_now["value"] = datetime(2026, 5, 29, 14, 12, tzinfo=UTC)
    second = sync_live_orders(config, store=store)
    second_ticket = store.live_order_intent(second["orders"][0]["reprice_ticket_hash"])
    assert second_ticket["limit_price"] == "-2.15"
    assert second_ticket["reprice_attempt"] == 2

    current_now["value"] = datetime(2026, 5, 29, 14, 31, tzinfo=UTC)
    expired = sync_live_orders(config, store=store)
    assert expired["orders"][0]["expire_status"] == "cancel_requested"
    assert store.live_order_intent(second_ticket["ticket_hash"])["_ledger_status"] == "expired"
    assert calls.count(("cancel", "order-old")) == 1
    assert any(call[0] == "cancel" and call[1] == second_ticket["order_id"] for call in calls)
