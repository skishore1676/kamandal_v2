import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

import kamandal_v2.live.execution as live_execution
from kamandal_v2.config import load_control
from kamandal_v2.cli import _live_submit_requested
from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg, Plan, Playbook, PortfolioState, PreflightResult, UniverseEntry
from kamandal_v2.live.approval import approve_live_request, expire_live_approval_requests, send_pending_live_approval_requests
from kamandal_v2.live.advisory import _live_candidate_policy, live_config, render_live_plan_rows, run_live_advisory_plan
from kamandal_v2.live.execution import (
    cleanup_live_approvals,
    execute_live_approved,
    record_manual_live_fill,
    render_terminal_entry_receipt,
    sync_live_orders,
)
from kamandal_v2.ops.alerts import AlertResult
from kamandal_v2.live.management import run_live_management_plan
from kamandal_v2.live.orders import APPROVE_LIVE, APPROVE_LIVE_CLOSE, REJECT_CLOSE, _limit_price, build_close_ticket, build_open_ticket
from kamandal_v2.planner.engine import run_plan
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.stores.audit import AuditWriter
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.daily_policy import DailyPolicySnapshot
from kamandal_v2.strategy_lanes.models import CsaStage, LaneId, SourceMode
from kamandal_v2.strategy_lanes.operator_policy import OperatorPolicyBundle
from kamandal_v2.strategy_lanes.policy import CsaPolicy


@pytest.fixture(autouse=True)
def _isolate_fixture_plans_from_runtime_earnings(monkeypatch) -> None:
    # Host-side verification must never inherit oldmac's live notification
    # posture. Tests that exercise receipts opt in explicitly and replace the
    # sender with a fake below.
    monkeypatch.setenv("KAMANDAL_ENTRY_TERMINAL_RECEIPT_ENABLED", "false")
    monkeypatch.setattr(
        "kamandal_v2.events.earnings.EarningsStore.latest",
        lambda _self, _symbol, *, source=None: None,
    )
    monkeypatch.setattr(
        "kamandal_v2.live.execution.submission_window",
        lambda _config, ticket, *, close: {
            "allowed": True,
            "reason": "test_submission_window",
            "intent_type": "close" if close else "open",
            "underlying": str(ticket.get("underlying") or ""),
            "retryable_next_session": False,
        },
    )


def _live_control() -> dict:
    control = load_control()
    control["live"]["max_bpr_per_order"] = 1000
    control["risk_manager"]["enabled"] = False
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
    monkeypatch.setenv("KAMANDAL_LIVE_RECONCILIATION_POST_FILL_GRACE_MINUTES", "15")
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
    assert control["live"]["reconciliation"]["post_fill_position_grace_minutes"] == 15
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


def test_live_policy_blocks_cluster_capped_candidate_before_staging(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.save_live_position_group("group_nvda", {"group_id": "group_nvda", "underlying": "NVDA"})
    store.save_live_position_group("group_amd", {"group_id": "group_amd", "underlying": "AMD"})
    legs = [
        OptionLeg("long_put", "buy", "put", 95, "2026-08-21", 1, 1.0, 0.95, 1.05, -0.10, 0.0, -0.01, 0.1, 100),
        OptionLeg("short_put", "sell", "put", 100, "2026-08-21", 1, 2.0, 1.95, 2.05, -0.20, 0.0, -0.01, 0.1, 100),
    ]
    candidate = Candidate(
        candidate_id="cand",
        idea_id="idea_mrvl",
        underlying="MRVL",
        playbook_id="put_spread_default",
        structure="put_spread",
        legs=legs,
        net_credit=1.0,
        estimated_bpr=400,
        greeks=Greeks(theta=0.01),
        liquidity_score=1.0,
        score=1.0,
        preflight=PreflightResult(ok=True, bpr=400, message="ok", raw={"response": {"buyingPowerRequirement": "400"}}),
    )

    _live_candidate_policy(
        [candidate],
        store,
        {
            "live": {"min_entry_legs": 2, "max_bpr_per_order": 2500},
            "execution": {"max_contracts_per_order": 1},
            "risk_manager": {
                "enabled": True,
                "max_positions_per_cluster": 2,
                "correlation_clusters": {"semis": ["NVDA", "AMD", "MRVL"]},
            },
        },
        PortfolioState(account_size=10_000, buying_power=10_000, bpr_used=0, positions_count=2),
    )

    assert candidate.rejection_reason == "live_risk_cluster_cap:semis"


def test_live_policy_blocks_same_underlying_at_cap_before_staging(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.save_live_position_group("group_baba_1", {"group_id": "group_baba_1", "underlying": "BABA"})
    store.save_live_position_group("group_baba_2", {"group_id": "group_baba_2", "underlying": "BABA"})
    legs = [
        OptionLeg("long_put", "buy", "put", 95, "2026-08-21", 1, 1.0, 0.95, 1.05, -0.10, 0.0, -0.01, 0.1, 100),
        OptionLeg("short_put", "sell", "put", 100, "2026-08-21", 1, 2.0, 1.95, 2.05, -0.20, 0.0, -0.01, 0.1, 100),
    ]
    candidate = Candidate(
        candidate_id="cand",
        idea_id="idea_baba",
        underlying="BABA",
        playbook_id="put_spread_default",
        structure="put_spread",
        legs=legs,
        net_credit=1.0,
        estimated_bpr=400,
        greeks=Greeks(theta=0.01),
        liquidity_score=1.0,
        score=1.0,
        preflight=PreflightResult(ok=True, bpr=400, message="ok"),
    )

    _live_candidate_policy(
        [candidate],
        store,
        {
            "live": {"min_entry_legs": 2, "max_bpr_per_order": 2500},
            "execution": {"max_contracts_per_order": 1},
            "risk_manager": {"enabled": True, "max_positions_per_underlying": 2},
        },
        PortfolioState(account_size=10_000, buying_power=10_000, bpr_used=0, positions_count=2),
    )

    assert candidate.rejection_reason == "live_risk_underlying_cap:BABA"


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
    control["planner"]["vertical_width_search"]["enabled"] = False
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


def test_live_bpr_cap_allows_explicit_diagonal_structure_cap() -> None:
    from kamandal_v2.live.advisory import _candidate_bpr_cap

    candidate = type("Candidate", (), {"structure": "call_diagonal"})()
    portfolio = type("Portfolio", (), {"account_size": 10_000})()
    live_cfg = {
        "max_bpr_per_order": 2500,
        "max_bpr_per_order_pct": 25,
        "max_bpr_per_order_by_structure": {"default": 500, "call_diagonal": 2500},
    }

    assert _candidate_bpr_cap(candidate, portfolio, live_cfg) == 2500


def test_live_bpr_cap_prefers_sheet_playbook_cap_from_candidate_reason() -> None:
    from kamandal_v2.live.advisory import _candidate_bpr_cap

    candidate = type(
        "Candidate",
        (),
        {"structure": "call_diagonal", "reasons": ["live_max_bpr_per_order=1500"]},
    )()
    portfolio = type("Portfolio", (), {"account_size": 10_000})()
    live_cfg = {
        "max_bpr_per_order": 2500,
        "max_bpr_per_order_pct": 25,
        "max_bpr_per_order_by_structure": {"default": 500, "call_diagonal": 2500},
    }

    assert _candidate_bpr_cap(candidate, portfolio, live_cfg) == 1500


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
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
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


def test_live_execute_approved_accepts_sheet_stage_authorized_ledger_ticket(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    candidate = _ticket_candidate("csa_candidate", "csa_idea", "MSFT")
    plan = type("StagePlan", (), {"plan_id": "csa_lifecycle", "plan_rank": 1})()
    ticket = build_open_ticket(plan, candidate)
    ticket.update({
        "csa_policy_hash": "policy-hash",
        "csa_playbook_id": "short_strangle_csa",
        "csa_stage": "pilot_live",
        "csa_policy_snapshot_date": "2026-08-15",
        "csa_policy_snapshot_hash": "snapshot-hash",
        "stage_authorized": True,
        "pilot_contract_cap": 1,
    })
    store.save_live_order_intent(ticket, status="stage_approved_pending_submit")
    snapshot = DailyPolicySnapshot(
        trading_date="2026-08-15",
        captured_at="2026-08-15T12:00:00Z",
        snapshot_hash="snapshot-hash",
        tables={"universe": [], "playbooks": []},
        path=tmp_path / "strategy_policy_fixture.json",
        policy=OperatorPolicyBundle(
            (),
            (
                CsaPolicy(
                    playbook_id="short_strangle_csa",
                    lane=LaneId.SHORT_STRANGLE,
                    stage=CsaStage.PILOT_LIVE,
                    source_mode=SourceMode.MARKET_SCAN,
                    management={},
                    resolved_fields={},
                    policy_hash="policy-hash",
                    source="fixture",
                    read_at="2026-08-15T12:00:00Z",
                ),
            ),
            (),
            "2026-08-15T12:00:00Z",
            source="fixture",
        ),
    )
    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": []})
    monkeypatch.setattr("kamandal_v2.live.execution.load_daily_policy_snapshot", lambda _config: snapshot)
    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", lambda _config: object())

    executed = execute_live_approved(_live_control(), submit=False, store=store)

    assert executed["source"] == "stage_authorized_ledger"
    assert executed["processed"] == 1
    assert executed["results"][0]["status"] == "dry_run"


def test_live_execute_blocks_stage_ticket_when_daily_snapshot_is_missing(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    candidate = _ticket_candidate("csa_candidate", "csa_idea", "MSFT")
    plan = type("StagePlan", (), {"plan_id": "csa_lifecycle", "plan_rank": 1})()
    ticket = build_open_ticket(plan, candidate)
    ticket.update({
        "csa_policy_hash": "policy-hash",
        "csa_playbook_id": "short_strangle_csa",
        "csa_stage": "pilot_live",
        "csa_lifecycle_id": "csa_lifecycle",
        "stage_authorized": True,
    })
    store.save_live_order_intent(ticket, status="stage_approved_pending_submit")
    monkeypatch.setenv("KAMANDAL_STRATEGY_POLICY_SNAPSHOT_DIR", str(tmp_path / "missing-policy-snapshots"))
    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": [], "playbooks": []})

    executed = execute_live_approved(_live_control(), submit=False, store=store)

    assert executed["source"] == "stage_authorized_ledger"
    assert executed["results"] == [
        {"status": "blocked", "reason": "blocked_daily_policy_snapshot:FileNotFoundError"}
    ]


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


def test_cleanup_live_approvals_retires_stale_unreferenced_entry_approvals(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    from kamandal_v2.live.orders import ticket_hash

    def make_ticket(order_id: str, underlying: str) -> dict:
        ticket = {
            "order_id": order_id,
            "plan_id": f"plan_{underlying.lower()}",
            "candidate_id": f"cand_{underlying.lower()}",
            "idea_id": f"idea_{underlying.lower()}",
            "intent_type": "open",
            "underlying": underlying,
            "playbook_id": "call_spread_default",
            "structure": "call_spread",
            "quantity": 1,
            "limit_price": "-1.25",
            "time_in_force": "DAY",
            "created_at": "2026-06-15T10:00:00Z",
            "legs": [],
            "submit_payload": {"orderId": order_id, "legs": []},
        }
        ticket["ticket_hash"] = ticket_hash(ticket)
        return ticket

    keep_ticket = make_ticket("keep-order", "SPY")
    retire_ticket = make_ticket("retire-order", "JPM")
    old_sheet_ticket = make_ticket("old-sheet-order", "MSFT")
    store.save_live_order_intent(keep_ticket, status="pending_approval")
    store.save_live_order_intent(retire_ticket, status="pending_approval")
    store.save_live_order_intent(old_sheet_ticket, status="pending_approval")
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute(
            "UPDATE live_order_intents SET created_at = ?, updated_at = ?",
            ("2000-01-01 00:00:00", "2000-01-01 00:00:00"),
        )
    row = dict(zip(DAILY_PLAN_HEADER, ["" for _ in DAILY_PLAN_HEADER], strict=False))
    row["plan_date"] = date.today().isoformat()
    row["mode"] = "live_advisory"
    row["plan_detail_json"] = json.dumps({"lane": "live_advisory", "order_ticket_json": keep_ticket})
    old_row = dict(row)
    old_row["plan_date"] = "2000-01-01"
    old_row["plan_detail_json"] = json.dumps({"lane": "live_advisory", "order_ticket_json": old_sheet_ticket})
    written = {}

    class FakeSheetClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        def read_tab(self, _title):
            return [row, old_row]

        def replace_tab(self, _title, *, header, rows):
            written["rows"] = rows
            return len(rows)

    monkeypatch.setattr("kamandal_v2.live.execution.GoogleSheetClient", FakeSheetClient)

    cleaned = cleanup_live_approvals(load_control(), store=store)

    assert cleaned["cleared"] == 0
    assert cleaned["retired_stale_entry_approvals"] == 2
    retired_hashes = {item["ticket_hash"] for item in cleaned["retired_rows"]}
    assert retired_hashes == {retire_ticket["ticket_hash"], old_sheet_ticket["ticket_hash"]}
    assert store.live_order_intent(keep_ticket["ticket_hash"])["_ledger_status"] == "pending_approval"
    retired = store.live_order_intent(retire_ticket["ticket_hash"])
    assert retired["_ledger_status"] == "retired_stale_entry_approval"
    assert retired["order_reconciliation"]["reason"] == "stale_entry_approval_not_in_current_daily_plan"
    assert store.live_order_intent(old_sheet_ticket["ticket_hash"])["_ledger_status"] == "retired_stale_entry_approval"
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
    control["live"]["exit_submit_source"] = "sheet"
    control["live"]["exit_pricing"]["require_fresh_quotes"] = False
    managed = run_live_management_plan(control, config_source="seed", write_sheet=False, store=store)

    assert managed["close_recommendations"] == 1
    row = dict(zip(DAILY_PLAN_HEADER, managed["daily_plan_rows"][0], strict=False))
    detail = json.loads(row["plan_detail_json"])
    assert detail["lane"] == "live_close_advisory"
    assert detail["order_ticket_json"]["intent_type"] == "close"
    assert detail["order_ticket_json"]["exit_natural_limit_price"] == f"{abs(detail['decision']['close_natural_net']) / 100.0:.2f}"
    assert detail["order_ticket_json"]["exit_min_profit_to_trigger"] == control["live"]["exit_pricing"]["min_profit_to_trigger"]
    assert detail["order_ticket_json"]["exit_profit_floor_pct"] == control["live"]["exit_pricing"]["profit_floor_pct"]
    assert detail["order_ticket_json"].get("exit_profit_floor_net") is not None
    assert detail["order_ticket_json"].get("exit_profit_floor_limit_price") is not None
    assert row["operator_action"] == APPROVE_LIVE_CLOSE


def test_live_management_suppresses_duplicate_close_when_group_has_working_close(tmp_path, monkeypatch) -> None:
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
    working_close = build_close_ticket(group, close_net_credit=-1.15)
    store.save_live_order_intent(working_close, status="submitted")
    newer_pending_close = build_close_ticket(group, close_net_credit=-1.25)
    store.save_live_order_intent(newer_pending_close, status="pending_close_approval")
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

    assert managed["close_recommendations"] == 0
    assert managed["working_close_orders"] == 1
    assert managed["daily_plan_rows"] == []
    assert managed["decisions"][0]["action"] == "hold"
    assert managed["decisions"][0]["reason"] == "working_close_order"
    assert managed["decisions"][0]["blocked_reason"] == "profit_target"
    assert managed["decisions"][0]["working_close_order"]["ticket_hash"] == working_close["ticket_hash"]
    assert managed["decisions"][0]["working_close_order"]["ledger_status"] == "submitted"
    with sqlite3.connect(store.sqlite_path) as conn:
        assert conn.execute("SELECT count(*) FROM live_order_intents WHERE intent_type = 'close'").fetchone()[0] == 2


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
    # Pin the legacy debounce/review policy; deployed config now hard-closes on max loss.
    control["live"]["exit_pricing"]["max_loss_action"] = "close_when_confirmed"
    control["live"]["exit_pricing"]["max_loss_requires_confirmation"] = True
    control["live"]["exit_pricing"]["loss_watch_confirmations_required"] = 2

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
    control["live"]["allow_same_day_exits_after"] = ""
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
    live_control["live"]["allow_same_day_exits_after"] = ""
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
    live_control["live"]["exit_submit_source"] = "sheet"
    live_control["live"]["allow_same_day_exits_after"] = "2000-01-01"
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")

    executed = execute_live_approved(live_control, submit=True, close=True, store=store)

    assert executed["processed"] == 1
    assert executed["results"][0]["status"] == "submitted"


def test_execute_live_close_ledger_source_drains_multiple_approved_tickets(tmp_path, monkeypatch) -> None:
    from kamandal_v2.live.orders import ticket_hash

    store = LocalStore(tmp_path / "kamandal.db")
    tickets = []
    for index, underlying in enumerate(["QQQ", "IWM"], start=1):
        ticket = {
            "order_id": f"close-order-{index}",
            "plan_id": f"close-plan-{index}",
            "candidate_id": f"close-candidate-{index}",
            "idea_id": f"close-idea-{index}",
            "group_id": f"close-group-{index}",
            "intent_type": "close",
            "underlying": underlying,
            "structure": "put_spread",
            "quantity": 1,
            "limit_price": "1.20",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "legs": [],
            "submit_payload": {"orderId": f"close-order-{index}", "quantity": "1", "type": "LIMIT", "limitPrice": "1.20", "legs": []},
        }
        ticket["ticket_hash"] = ticket_hash(ticket)
        store.save_live_position_group(
            ticket["group_id"],
            {
                "group_id": ticket["group_id"],
                "underlying": underlying,
                "playbook_id": "put_spread_default",
                "structure": "put_spread",
                "candidate": {"underlying": underlying, "legs": []},
            },
        )
        status = "pending_close_approval" if index == 1 else "approved_close_pending_submit"
        store.save_live_order_intent(ticket, status=status)
        tickets.append(ticket)

    calls = []

    class PassingBrokerAdapter:
        def __init__(self, _config):
            pass

        def preflight_ticket(self, ticket):
            calls.append(("preflight", ticket["order_id"]))
            return PreflightResult(ok=True, bpr=0.0, message="ok", raw={"request": ticket["submit_payload"]})

        def place_order_ticket(self, ticket):
            calls.append(("place", ticket["order_id"]))
            return {"orderId": ticket["order_id"]}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", PassingBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": []})
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    config = _live_control()
    config["runtime"]["mode"] = "live"
    config["runtime"]["trading_enabled"] = True
    config["live"]["exit_submit_source"] = "ledger"
    config["live"]["max_close_submits_per_run"] = 10
    config["live"]["allow_same_day_exits_after"] = "2000-01-01"

    executed = execute_live_approved(config, submit=True, close=True, store=store)

    assert executed["source"] == "ledger"
    assert executed["processed"] == 2
    assert [result["status"] for result in executed["results"]] == ["submitted", "submitted"]
    assert set(calls) == {
        ("preflight", "close-order-1"),
        ("place", "close-order-1"),
        ("preflight", "close-order-2"),
        ("place", "close-order-2"),
    }
    assert [store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] for ticket in tickets] == ["submitted", "submitted"]


def test_live_management_applies_reject_close_for_local_ledger_intent(tmp_path, monkeypatch) -> None:
    from kamandal_v2.live.orders import ticket_hash

    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "order_id": "reject-close-order",
        "plan_id": "reject-close-plan",
        "candidate_id": "reject-close-candidate",
        "idea_id": "reject-close-idea",
        "group_id": "reject-close-group",
        "intent_type": "close",
        "underlying": "QQQ",
        "structure": "put_spread",
        "quantity": 1,
        "limit_price": "1.20",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "legs": [],
        "submit_payload": {"orderId": "reject-close-order", "quantity": "1", "type": "LIMIT", "limitPrice": "1.20", "legs": []},
    }
    ticket["ticket_hash"] = ticket_hash(ticket)
    store.save_live_order_intent(ticket, status="approved_close_pending_submit")
    row = dict(zip(DAILY_PLAN_HEADER, ["" for _ in DAILY_PLAN_HEADER], strict=False))
    row["operator_action"] = REJECT_CLOSE
    row["mode"] = "live_close_advisory"
    row["operator_notes"] = "skip this close"
    row["plan_detail_json"] = json.dumps({"lane": "live_close_advisory", "order_ticket_json": ticket})

    monkeypatch.setattr("kamandal_v2.live.management.pull_sheet_tables", lambda _config: {"daily_plan": [row]})
    monkeypatch.setattr("kamandal_v2.live.management.load_planner_config", lambda _config, source="sheet": ([], []))
    config = _live_control()
    config["live"]["exit_submit_source"] = "ledger"
    config["live"]["exit_pricing"]["require_fresh_quotes"] = False

    result = run_live_management_plan(config, config_source="seed", write_sheet=False, store=store)

    assert result["operator_commands"]["retired"] == 1
    stored = store.live_order_intent(ticket["ticket_hash"])
    assert stored["_ledger_status"] == "rejected_by_operator"
    assert stored["operator_command"]["action"] == REJECT_CLOSE


def test_execute_live_close_blocks_already_active_close_ticket(tmp_path, monkeypatch) -> None:
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
    store.save_live_order_intent(close_ticket, status="submitted")
    close_row = dict(zip(DAILY_PLAN_HEADER, ["" for _ in DAILY_PLAN_HEADER], strict=False))
    close_row["operator_action"] = APPROVE_LIVE_CLOSE
    close_row["mode"] = "live_close_advisory"
    close_row["plan_detail_json"] = json.dumps({"lane": "live_close_advisory", "order_ticket_json": close_ticket})

    class UnexpectedBrokerAdapter:
        def __init__(self, _config):
            pass

        def preflight_ticket(self, _ticket):
            raise AssertionError("active close tickets should not be preflighted again")

        def place_order_ticket(self, _ticket):
            raise AssertionError("active close tickets should not be submitted again")

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", UnexpectedBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": [close_row]})
    live_control = _live_control()
    live_control["runtime"]["mode"] = "live"
    live_control["runtime"]["trading_enabled"] = True
    live_control["live"]["exit_submit_source"] = "sheet"
    live_control["live"]["allow_same_day_exits_after"] = "2000-01-01"
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")

    executed = execute_live_approved(live_control, submit=True, close=True, store=store)

    assert executed["processed"] == 1
    assert executed["results"][0]["status"] == "blocked"
    assert executed["results"][0]["reason"] == "close_ticket_active:submitted"


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


def test_sync_live_orders_serializes_cross_job_broker_mutation(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    lock_calls = []

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", lambda _config: object())
    monkeypatch.setattr(
        "kamandal_v2.live.execution.fcntl.flock",
        lambda file_descriptor, operation: lock_calls.append((file_descriptor, operation)),
    )

    result = sync_live_orders(_live_control(), store=store)

    assert result == {"synced": 0, "manage_entries": True, "orders": []}
    assert [operation for _, operation in lock_calls] == [live_execution.fcntl.LOCK_EX, live_execution.fcntl.LOCK_UN]
    assert (tmp_path / "runlocks" / "live_order_sync.lock").exists()


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


def _replacement_entry_ticket(
    ticket_hash: str,
    *,
    order_id: str,
    parent_ticket_hash: str = "",
    quantity: int = 1,
) -> dict:
    ticket = {
        "ticket_hash": ticket_hash,
        "order_id": order_id,
        "plan_id": "plan-lineage",
        "candidate_id": "candidate-lineage",
        "idea_id": "idea-lineage",
        "intent_type": "open",
        "underlying": "AMZN",
        "playbook_id": "put_calendar_low_iv",
        "structure": "put_calendar",
        "quantity": quantity,
        "limit_price": "5.45",
        "created_at": "2026-07-31T14:40:04Z",
        "legs": [
            {"role": "short_near", "side": "sell", "option_type": "put", "strike": 270, "expiration": "2026-08-28", "quantity": 1},
            {"role": "long_far", "side": "buy", "option_type": "put", "strike": 270, "expiration": "2026-10-16", "quantity": 1},
        ],
        "submit_payload": {"orderId": order_id, "quantity": str(quantity), "type": "LIMIT", "limitPrice": "5.45", "legs": []},
    }
    if parent_ticket_hash:
        ticket["parent_ticket_hash"] = parent_ticket_hash
        ticket["replaces_order_id"] = order_id
    return ticket


def test_sync_atomic_replace_projects_one_position_per_lineage(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    parent = _replacement_entry_ticket("ticket-parent", order_id="broker-order")
    child = _replacement_entry_ticket("ticket-child", order_id="broker-order", parent_ticket_hash="ticket-parent")
    store.save_live_order_intent(parent, status="repriced")
    store.save_live_order_intent(child, status="submitted")

    class FilledBroker:
        def get_order(self, _order_id):
            return {"status": "FILLED", "filledQuantity": "1", "averagePrice": "5.45"}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", lambda _config: FilledBroker())

    first = sync_live_orders(_live_control(), store=store)
    second = sync_live_orders(_live_control(), store=store)

    assert first["synced"] == 2
    assert second["synced"] == 0
    groups = store.open_live_position_groups()
    assert len(groups) == 1
    assert groups[0]["group_id"] == "live_group_ticket-parent"
    assert groups[0]["execution_lineage"]["canonical_ticket_hash"] == "ticket-child"
    assert groups[0]["entry_snapshot"]["source_ticket_hash"] == "ticket-child"
    assert store.live_order_intent("ticket-parent")["_ledger_status"] == "filled_via_replacement"
    assert store.live_order_intent("ticket-child")["_ledger_status"] == "filled"


def test_sync_staged_replace_keeps_root_identity_when_child_order_fills(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    parent = _replacement_entry_ticket("ticket-parent", order_id="broker-parent")
    child = _replacement_entry_ticket("ticket-child", order_id="broker-child", parent_ticket_hash="ticket-parent")
    child["replaces_order_id"] = "broker-parent"
    store.save_live_order_intent(parent, status="repriced")
    store.save_live_order_intent(child, status="submitted")

    class Broker:
        def get_order(self, order_id):
            if order_id == "broker-child":
                return {"status": "FILLED", "filledQuantity": "1", "averagePrice": "5.45"}
            return {"status": "CANCELLED", "filledQuantity": "0"}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", lambda _config: Broker())

    result = sync_live_orders(_live_control(), store=store)

    assert result["synced"] == 2
    assert {group["group_id"] for group in store.open_live_position_groups()} == {"live_group_ticket-parent"}
    assert store.live_order_intent("ticket-parent")["_ledger_status"] == "filled_via_replacement"
    assert store.live_order_intent("ticket-child")["_ledger_status"] == "filled"


def test_sync_sums_partial_parent_and_child_fills_across_staged_replacement(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    parent = _replacement_entry_ticket("ticket-parent", order_id="broker-parent", quantity=2)
    child = _replacement_entry_ticket(
        "ticket-child",
        order_id="broker-child",
        parent_ticket_hash="ticket-parent",
        quantity=1,
    )
    child["replaces_order_id"] = "broker-parent"
    store.save_live_order_intent(parent, status="submitted")
    store.save_live_order_intent(child, status="submitted")
    calls = {"broker-parent": 0, "broker-child": 0}

    class Broker:
        def get_order(self, order_id):
            calls[order_id] += 1
            if order_id == "broker-parent":
                if calls[order_id] == 1:
                    return {"status": "PARTIALLY_FILLED", "filledQuantity": "1", "averagePrice": "5.40"}
                return {"status": "CANCELLED", "filledQuantity": "1", "averagePrice": "5.40"}
            if calls[order_id] == 1:
                return {"status": "WORKING", "filledQuantity": "0"}
            return {"status": "FILLED", "filledQuantity": "1", "averagePrice": "5.50"}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", lambda _config: Broker())

    sync_live_orders(_live_control(), store=store, manage_entries=False)
    opened_at = store.open_live_position_groups()[0]["opened_at"]
    sync_live_orders(_live_control(), store=store, manage_entries=False)

    group = store.open_live_position_groups()[0]
    assert group["opened_at"] == opened_at
    assert group["group_id"] == "live_group_ticket-parent"
    assert group["entry_snapshot"]["fill_quantity"] == 2.0
    assert group["entry_snapshot"]["entry_net_credit"] == -5.45
    assert {fill["order_id"] for fill in group["entry_snapshot"]["execution_fills"]} == {
        "broker-parent",
        "broker-child",
    }
    assert {leg["quantity"] for leg in group["candidate"]["legs"]} == {2.0}


def test_sync_preserves_terminal_partial_fill_as_one_open_position(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = _replacement_entry_ticket("ticket-partial", order_id="broker-partial", quantity=2)
    store.save_live_order_intent(ticket, status="submitted")
    responses = iter(
        [
            {"status": "PARTIALLY_FILLED", "filledQuantity": "1", "averagePrice": "5.40"},
            {"status": "CANCELLED", "filledQuantity": "1", "averagePrice": "5.40"},
        ]
    )

    class Broker:
        def get_order(self, _order_id):
            return next(responses)

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", lambda _config: Broker())

    first = sync_live_orders(_live_control(), store=store)
    second = sync_live_orders(_live_control(), store=store)

    assert first["orders"][0]["position_projection"]["status"] == "partially_filled"
    assert second["orders"][0]["partial_fill_preserved"] is True
    group = store.open_live_position_groups()[0]
    assert group["entry_snapshot"]["fill_quantity"] == 1.0
    assert {leg["quantity"] for leg in group["candidate"]["legs"]} == {1.0}
    assert store.live_order_intent("ticket-partial")["_ledger_status"] == "partially_filled_terminal"


def test_sync_refuses_ambiguous_replacement_siblings(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    parent = _replacement_entry_ticket("ticket-parent", order_id="broker-order")
    first_child = _replacement_entry_ticket("ticket-child-a", order_id="broker-order", parent_ticket_hash="ticket-parent")
    second_child = _replacement_entry_ticket("ticket-child-b", order_id="broker-order", parent_ticket_hash="ticket-parent")
    store.save_live_order_intent(parent, status="repriced")
    store.save_live_order_intent(first_child, status="submitted")
    store.save_live_order_intent(second_child, status="submitted")

    class FilledBroker:
        def get_order(self, _order_id):
            return {"status": "FILLED", "filledQuantity": "1", "averagePrice": "5.45"}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", lambda _config: FilledBroker())

    result = sync_live_orders(_live_control(), store=store)

    assert result["synced"] == 3
    assert store.open_live_position_groups() == []
    assert {item["position_projection"]["reason"] for item in result["orders"]} == {"multiple_terminal_replacement_tickets"}


@pytest.mark.parametrize("lineage_kind", ["missing_parent", "cycle"])
def test_sync_refuses_broken_replacement_lineage(tmp_path, monkeypatch, lineage_kind) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    first = _replacement_entry_ticket("ticket-first", order_id="broker-first")
    if lineage_kind == "missing_parent":
        first["parent_ticket_hash"] = "ticket-missing"
        tickets = [first]
        expected_reason = "replacement_parent_missing"
    else:
        second = _replacement_entry_ticket(
            "ticket-second",
            order_id="broker-second",
            parent_ticket_hash="ticket-first",
        )
        first["parent_ticket_hash"] = "ticket-second"
        tickets = [first, second]
        expected_reason = "replacement_lineage_cycle"
    for ticket in tickets:
        store.save_live_order_intent(ticket, status="submitted")

    class FilledBroker:
        def get_order(self, _order_id):
            return {"status": "FILLED", "filledQuantity": "1", "averagePrice": "5.45"}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", lambda _config: FilledBroker())

    result = sync_live_orders(_live_control(), store=store)

    assert store.open_live_position_groups() == []
    assert {item["position_projection"]["reason"] for item in result["orders"]} == {expected_reason}


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
    missing_result = [order for order in synced["orders"] if order["order_id"] == "missing-order"][0]
    assert missing_result["status"] == "BROKER_ORDER_NOT_FOUND"
    assert missing_result["reconciled_status"] == "expired_broker_status_missing"
    assert "/trading/<account>/" in missing_result["error"]
    assert "5OS69079" not in missing_result["error"]
    assert store.live_order_intent(missing["ticket_hash"])["_ledger_status"] == "expired_broker_status_missing"
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


@pytest.mark.parametrize("ledger_status", ["submitted", "reprice_blocked_preflight_failed"])
def test_sync_live_orders_atomically_reprices_stale_close_order(tmp_path, monkeypatch, ledger_status) -> None:
    from kamandal_v2.live.orders import ticket_hash

    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "order_id": "close-order-old",
        "plan_id": "close-plan",
        "candidate_id": "close-candidate",
        "idea_id": "close-idea",
        "group_id": "close-group",
        "intent_type": "close",
        "underlying": "QQQ",
        "structure": "put_spread",
        "quantity": 1,
        "limit_price": "2.40",
        "time_in_force": "DAY",
        "created_at": "2026-05-29T14:00:00Z",
        "exit_reason": "profit_target",
        "exit_natural_limit_price": "2.20",
        "exit_profit_floor_limit_price": "2.30",
        "legs": [
            {"role": "long_put", "side": "buy", "option_type": "put", "strike": 920, "expiration": "2026-07-10", "quantity": 1},
            {"role": "short_put", "side": "sell", "option_type": "put", "strike": 925, "expiration": "2026-07-10", "quantity": 1},
        ],
        "submit_payload": {"orderId": "close-order-old", "quantity": "1", "type": "LIMIT", "limitPrice": "2.40", "legs": []},
    }
    ticket["ticket_hash"] = ticket_hash(ticket)
    store.save_live_order_intent(ticket, status=ledger_status)
    calls = []

    class RepriceCloseBrokerAdapter:
        def __init__(self, _config):
            pass

        def get_order(self, _order_id):
            return {"status": "NEW", "createdAt": "2026-05-29T14:00:00Z"}

        def replace_order(self, order_id, repriced_ticket):
            calls.append(("replace", order_id, repriced_ticket["order_id"], repriced_ticket["limit_price"]))
            return {"orderId": repriced_ticket["order_id"]}

        def cancel_order(self, order_id):
            calls.append(("cancel", order_id))
            return {"orderId": order_id, "status": "CANCEL_REQUESTED"}

        def preflight_ticket(self, repriced_ticket):
            calls.append(("preflight", repriced_ticket["limit_price"]))
            return PreflightResult(ok=True, bpr=0, message="ok", raw={"request": repriced_ticket["submit_payload"]})

        def place_order_ticket(self, repriced_ticket):
            calls.append(("place", repriced_ticket["order_id"], repriced_ticket["limit_price"]))
            return {"orderId": repriced_ticket["order_id"]}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", RepriceCloseBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.datetime", type("FrozenDateTime", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 5, 29, 14, 12, tzinfo=UTC))}))
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    config = _live_control()
    config["runtime"]["mode"] = "live"
    config["runtime"]["trading_enabled"] = True
    config["live"]["exit_reprice"] = {"enabled": True, "after_minutes": 10, "max_reprices": 1, "step_multiplier": 1.0, "expire_after_minutes": 390}

    synced = sync_live_orders(config, store=store)

    assert synced["orders"][0]["reprice_status"] == "submitted"
    assert synced["orders"][0]["reprice_method"] == "broker_atomic"
    assert ("replace", "close-order-old", synced["orders"][0]["reprice_order_id"], "2.20") in calls
    assert not any(call[0] in {"preflight", "cancel", "place"} for call in calls)
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "repriced"
    child = store.live_order_intent(synced["orders"][0]["reprice_ticket_hash"])
    assert child["_ledger_status"] == "submitted"
    assert child["parent_ticket_hash"] == ticket["ticket_hash"]
    assert child["limit_price"] == "2.20"
    assert child["replace_method"] == "broker_atomic"


def test_sync_live_orders_keeps_original_working_when_atomic_replace_is_indeterminate(tmp_path, monkeypatch) -> None:
    from kamandal_v2.live.orders import ticket_hash

    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "order_id": "close-order-old",
        "plan_id": "close-plan",
        "candidate_id": "close-candidate",
        "idea_id": "close-idea",
        "group_id": "close-group",
        "intent_type": "close",
        "underlying": "XLF",
        "structure": "put_calendar",
        "quantity": 1,
        "limit_price": "-0.45",
        "time_in_force": "DAY",
        "created_at": "2026-05-29T14:00:00Z",
        "exit_reason": "half_time",
        "exit_natural_limit_price": "-0.40",
        "legs": [],
        "submit_payload": {
            "orderId": "close-order-old",
            "quantity": "1",
            "type": "LIMIT",
            "limitPrice": "-0.45",
            "expiration": {"timeInForce": "DAY"},
            "legs": [],
        },
    }
    ticket["ticket_hash"] = ticket_hash(ticket)
    store.save_live_order_intent(ticket, status="submitted")

    class IndeterminateReplaceBroker:
        def __init__(self, _config):
            pass

        def get_order(self, _order_id):
            return {"status": "NEW", "createdAt": "2026-05-29T14:00:00Z"}

        def replace_order(self, _order_id, _repriced_ticket):
            return {}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", IndeterminateReplaceBroker)
    monkeypatch.setattr(
        "kamandal_v2.live.execution.datetime",
        type("FrozenDateTime", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 5, 29, 14, 12, tzinfo=UTC))}),
    )
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    config = _live_control()
    config["runtime"]["mode"] = "live"
    config["runtime"]["trading_enabled"] = True
    config["live"]["exit_reprice"] = {
        "enabled": True,
        "after_minutes": 10,
        "max_reprices": 1,
        "step_multiplier": 1.0,
        "expire_after_minutes": 390,
    }

    synced = sync_live_orders(config, store=store)

    assert synced["orders"][0]["reprice_status"] == "failed"
    assert "missing orderId" in synced["orders"][0]["reprice_message"]
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "submitted"
    assert store.live_order_child_intents(ticket["ticket_hash"]) == []


def test_sync_live_orders_stages_signed_multileg_cancel_then_uses_portfolio_and_preflight(tmp_path, monkeypatch) -> None:
    from kamandal_v2.live.orders import ticket_hash

    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "order_id": "xlf-close-old",
        "plan_id": "close-plan",
        "candidate_id": "close-candidate",
        "idea_id": "close-idea",
        "group_id": "xlf-group",
        "intent_type": "close",
        "underlying": "XLF",
        "structure": "put_calendar",
        "quantity": 1,
        "limit_price": "-0.45",
        "time_in_force": "DAY",
        "created_at": "2026-05-29T14:00:00Z",
        "exit_reason": "half_time",
        "exit_natural_limit_price": "-0.40",
        "legs": [
            {
                "role": "short_near",
                "side": "buy",
                "option_type": "put",
                "strike": 56,
                "expiration": "2026-08-14",
                "quantity": 1,
            },
            {
                "role": "long_far",
                "side": "sell",
                "option_type": "put",
                "strike": 56,
                "expiration": "2026-09-18",
                "quantity": 1,
            },
        ],
        "submit_payload": {
            "orderId": "xlf-close-old",
            "quantity": "1",
            "type": "LIMIT",
            "limitPrice": "-0.45",
            "expiration": {"timeInForce": "DAY"},
            "legs": [],
        },
    }
    ticket["ticket_hash"] = ticket_hash(ticket)
    store.save_live_order_intent(ticket, status="reprice_blocked_preflight_failed")
    calls = []
    broker_status = {"value": "NEW"}

    class StagedReplaceBroker:
        def __init__(self, _config):
            pass

        def get_order(self, order_id):
            calls.append(("get", order_id))
            return {"status": broker_status["value"], "createdAt": "2026-05-29T14:00:00Z"}

        def supports_atomic_replace(self, _replacement):
            return False

        def replace_order(self, _order_id, _replacement):
            raise AssertionError("signed multileg must not use atomic replace")

        def cancel_order(self, order_id):
            calls.append(("cancel", order_id))
            return {"orderId": order_id, "status": "CANCEL_REQUESTED"}

        def broker_positions(self):
            calls.append(("portfolio",))
            return [
                {
                    "underlying": "XLF",
                    "expiration": "2026-08-14",
                    "option_type": "put",
                    "strike": 56,
                    "quantity": -1,
                },
                {
                    "underlying": "XLF",
                    "expiration": "2026-09-18",
                    "option_type": "put",
                    "strike": 56,
                    "quantity": 1,
                },
            ]

        def preflight_ticket(self, replacement):
            calls.append(("preflight", replacement["limit_price"]))
            return PreflightResult(ok=True, bpr=0, message="ok", raw={"request": replacement["submit_payload"]})

        def place_order_ticket(self, replacement):
            calls.append(("place", replacement["order_id"], replacement["limit_price"]))
            return {"orderId": replacement["order_id"]}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", StagedReplaceBroker)
    monkeypatch.setattr(
        "kamandal_v2.live.execution.datetime",
        type("FrozenDateTime", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 5, 29, 14, 12, tzinfo=UTC))}),
    )
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    config = _live_control()
    config["runtime"]["mode"] = "live"
    config["runtime"]["trading_enabled"] = True
    config["live"]["exit_reprice"] = {
        "enabled": True,
        "after_minutes": 10,
        "max_reprices": 1,
        "step_multiplier": 1.0,
        "expire_after_minutes": 390,
    }

    staged = sync_live_orders(config, store=store)

    assert staged["orders"][0]["reprice_status"] == "cancel_requested"
    assert staged["orders"][0]["reprice_method"] == "staged_cancel"
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "replace_cancel_pending"
    child = store.live_order_child_intents(ticket["ticket_hash"])[0]
    assert child["_ledger_status"] == "replace_waiting_cancel"
    assert not any(call[0] in {"portfolio", "preflight", "place"} for call in calls)

    # Simulate the legacy race where reconciliation consumed the staged parent.
    store.update_live_order_intent_status(ticket["ticket_hash"], "cancelled")
    broker_status["value"] = "CANCELLED"
    replaced = sync_live_orders(config, store=store)

    assert replaced["orders"][0]["reprice_status"] == "submitted"
    assert replaced["orders"][0]["reprice_method"] == "staged_cancel"
    assert replaced["orders"][0]["position_evidence"]["intact"] is True
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "repriced"
    submitted_child = store.live_order_intent(replaced["orders"][0]["reprice_ticket_hash"])
    assert submitted_child["_ledger_status"] == "submitted"
    assert ("portfolio",) in calls
    assert ("preflight", "-0.40") in calls
    assert any(call[0] == "place" for call in calls)


def test_sync_live_orders_profit_target_reprice_respects_floor(tmp_path, monkeypatch) -> None:
    from kamandal_v2.live.orders import ticket_hash

    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "order_id": "close-order-floor",
        "plan_id": "close-plan",
        "candidate_id": "close-candidate",
        "idea_id": "close-idea",
        "group_id": "close-group",
        "intent_type": "close",
        "underlying": "QQQ",
        "structure": "put_spread",
        "quantity": 1,
        "limit_price": "2.40",
        "time_in_force": "DAY",
        "created_at": "2026-05-29T14:00:00Z",
        "exit_reason": "profit_target",
        "exit_natural_net": -280.0,
        "exit_profit_floor_net": -230.0,
        "legs": [
            {"role": "long_put", "side": "buy", "option_type": "put", "strike": 920, "expiration": "2026-07-10", "quantity": 1},
            {"role": "short_put", "side": "sell", "option_type": "put", "strike": 925, "expiration": "2026-07-10", "quantity": 1},
        ],
        "submit_payload": {"orderId": "close-order-floor", "quantity": "1", "type": "LIMIT", "limitPrice": "2.40", "legs": []},
    }
    ticket["ticket_hash"] = ticket_hash(ticket)
    store.save_live_order_intent(ticket, status="submitted")
    calls = []

    class RepriceCloseBrokerAdapter:
        def __init__(self, _config):
            pass

        def get_order(self, _order_id):
            return {"status": "NEW", "createdAt": "2026-05-29T14:00:00Z"}

        def cancel_order(self, order_id):
            return {"orderId": order_id, "status": "CANCEL_REQUESTED"}

        def preflight_ticket(self, repriced_ticket):
            calls.append(("preflight", repriced_ticket["limit_price"]))
            return PreflightResult(ok=True, bpr=0, message="ok", raw={"request": repriced_ticket["submit_payload"]})

        def place_order_ticket(self, repriced_ticket):
            return {"orderId": repriced_ticket["order_id"]}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", RepriceCloseBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.datetime", type("FrozenDateTime", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 5, 29, 14, 12, tzinfo=UTC))}))
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    config = _live_control()
    config["runtime"]["mode"] = "live"
    config["runtime"]["trading_enabled"] = True
    config["live"]["exit_reprice"] = {"enabled": True, "after_minutes": 10, "max_reprices": 1, "step_multiplier": 1.0, "expire_after_minutes": 390}

    synced = sync_live_orders(config, store=store)

    assert synced["orders"][0]["reprice_status"] == "submitted"
    assert ("preflight", "2.30") in calls


def test_sync_live_orders_profit_target_reprice_handles_credit_close_floor(tmp_path, monkeypatch) -> None:
    from kamandal_v2.live.orders import ticket_hash

    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "order_id": "close-order-credit-floor",
        "plan_id": "close-plan",
        "candidate_id": "close-candidate",
        "idea_id": "close-idea",
        "group_id": "close-group",
        "intent_type": "close",
        "underlying": "AAPL",
        "structure": "calendar",
        "quantity": 1,
        "limit_price": "-15.00",
        "time_in_force": "DAY",
        "created_at": "2026-05-29T14:00:00Z",
        "exit_reason": "profit_target",
        "exit_natural_net": 1200.0,
        "exit_profit_floor_net": 1250.0,
        "legs": [
            {"role": "near_short_call", "side": "buy", "option_type": "call", "strike": 100, "expiration": "2026-07-10", "quantity": 1},
            {"role": "far_long_call", "side": "sell", "option_type": "call", "strike": 100, "expiration": "2026-08-21", "quantity": 1},
        ],
        "submit_payload": {"orderId": "close-order-credit-floor", "quantity": "1", "type": "LIMIT", "limitPrice": "-15.00", "legs": []},
    }
    ticket["ticket_hash"] = ticket_hash(ticket)
    store.save_live_order_intent(ticket, status="submitted")
    calls = []

    class RepriceCloseBrokerAdapter:
        def __init__(self, _config):
            pass

        def get_order(self, _order_id):
            return {"status": "NEW", "createdAt": "2026-05-29T14:00:00Z"}

        def cancel_order(self, order_id):
            return {"orderId": order_id, "status": "CANCEL_REQUESTED"}

        def preflight_ticket(self, repriced_ticket):
            calls.append(("preflight", repriced_ticket["limit_price"]))
            return PreflightResult(ok=True, bpr=0, message="ok", raw={"request": repriced_ticket["submit_payload"]})

        def place_order_ticket(self, repriced_ticket):
            return {"orderId": repriced_ticket["order_id"]}

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", RepriceCloseBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.datetime", type("FrozenDateTime", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 5, 29, 14, 12, tzinfo=UTC))}))
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    config = _live_control()
    config["runtime"]["mode"] = "live"
    config["runtime"]["trading_enabled"] = True
    config["live"]["exit_reprice"] = {"enabled": True, "after_minutes": 10, "max_reprices": 1, "step_multiplier": 1.0, "expire_after_minutes": 390}

    synced = sync_live_orders(config, store=store)

    assert synced["orders"][0]["reprice_status"] == "submitted"
    assert ("preflight", "-12.50") in calls


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


def test_sync_live_orders_sends_one_terminal_unfilled_entry_receipt(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    root = {
        "ticket_hash": "ticket-root",
        "order_id": "order-root",
        "plan_id": "plan",
        "candidate_id": "cand",
        "idea_id": "idea",
        "intent_type": "open",
        "underlying": "TSM",
        "structure": "put_spread",
        "limit_price": "-1.80",
        "created_at": "2026-07-17T14:30:00Z",
        "legs": [{"expiration": "2026-08-28"}],
    }
    repriced = {
        **root,
        "ticket_hash": "ticket-final",
        "order_id": "order-final",
        "parent_ticket_hash": "ticket-root",
        "reprice_attempt": 1,
        "limit_price": "-1.45",
        "created_at": "2026-07-17T14:45:00Z",
    }
    store.save_live_order_intent(root, status="repriced")
    store.save_live_order_intent(repriced, status="expired")

    class CancelledBrokerAdapter:
        def __init__(self, _config):
            pass

        def get_order(self, _order_id):
            return {"status": "CANCELLED"}

    sent = []

    def fake_send_lathi_alert(**kwargs):
        sent.append(kwargs)
        return AlertResult(attempted=True, ok=True, mode="spool")

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", CancelledBrokerAdapter)
    monkeypatch.setattr("kamandal_v2.live.execution.send_lathi_alert", fake_send_lathi_alert)
    config = _live_control()
    config["live"]["entry_reprice"]["terminal_unfilled_receipt"] = {"enabled": True, "mode": "spool"}

    first = sync_live_orders(config, store=store)
    second = sync_live_orders(config, store=store)

    receipt_order = next(order for order in first["orders"] if order.get("activity_receipt"))
    assert receipt_order["activity_receipt"]["attempt_count"] == 2
    assert receipt_order["activity_receipt"]["ok"] is True
    assert second["orders"] == []
    assert len(sent) == 1
    assert sent[0]["title"] == "Kamandal entry attempt completed: TSM unfilled"
    assert "Broker attempts: 2 (1 reprices)." in sent[0]["body"]
    assert "Limit path: $1.80 credit -> $1.45 credit." in sent[0]["body"]
    assert store.live_order_intent("ticket-final")["_ledger_status"] == "cancelled"


def test_render_terminal_entry_receipt_for_debit_entry() -> None:
    body = render_terminal_entry_receipt(
        [
            {
                "underlying": "NVDA",
                "structure": "call_diagonal",
                "limit_price": "6.56",
                "legs": [{"expiration": "2026-09-18"}],
            }
        ],
        broker_status="REJECTED",
    )

    assert "NVDA call diagonal was attempted but not filled." in body
    assert "Limit path: $6.56 debit." in body
    assert "no live position was opened" in body


def test_live_submit_blocks_entries_when_health_red(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.save_live_position_group(
        "group_red",
        {
            "group_id": "group_red",
            "underlying": "AAPL",
            "playbook_id": "call_spread",
            "structure": "call_spread",
            "candidate": {"underlying": "AAPL", "legs": []},
        },
    )
    store.save_live_reconciliation_issue(
        {
            "issue_id": "rec-entry-gate",
            "issue_type": "broker_qty_mismatch",
            "group_id": "group_red",
            "underlying": "AAPL",
            "status": "open",
        },
    )
    open_row = dict(zip(DAILY_PLAN_HEADER, ["" for _ in DAILY_PLAN_HEADER], strict=False))
    open_row["operator_action"] = APPROVE_LIVE
    open_row["mode"] = "live_advisory"
    open_row["trade_bundle"] = "bundle-health-gate"
    open_row["plan_detail_json"] = json.dumps(
        {
            "lane": "live_advisory",
            "order_ticket_json": {"ticket_hash": "open-gate-ticket", "order_id": "open-gate-order", "intent_type": "open"},
        }
    )
    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": [open_row]})

    live_control = _live_control()
    live_control["runtime"]["mode"] = "live"
    live_control["runtime"]["trading_enabled"] = True
    live_control["risk_manager"]["enabled"] = True
    live_control["risk_manager"]["max_account_snapshot_age_minutes"] = 0
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    executed = execute_live_approved(live_control, submit=True, store=store)

    assert executed["processed"] == 1
    assert executed["results"][0]["status"] == "blocked"
    assert executed["results"][0]["reason"].startswith("blocked_live_health_red:")
    assert "reconciliation_blocker" in executed["results"][0]["reason"]
    assert executed["health_gate"]["blocked"] is True
    assert executed["results"][0]["trade_bundle"] == "bundle-health-gate"
    with sqlite3.connect(tmp_path / "kamandal.db") as conn:
        row = conn.execute(
            "SELECT payload FROM events WHERE event_type = 'risk_manager_entry_gate_decision' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["risk_manager"]["enabled"] is True


def test_close_ticket_seed_salt_changes_order_identity() -> None:
    group = {
        "group_id": "group_salt",
        "plan_id": "plan_salt",
        "candidate_id": "cand_salt",
        "idea_id": "idea_salt",
        "underlying": "MRVL",
        "playbook_id": "put_spread",
        "structure": "put_spread",
        "candidate": {
            "net_credit": 1.2,
            "legs": [
                {"side": "sell", "option_type": "put", "expiration": "2026-07-17", "strike": 200.0, "quantity": 1},
                {"side": "buy", "option_type": "put", "expiration": "2026-07-17", "strike": 195.0, "quantity": 1},
            ],
        },
    }
    plain = build_close_ticket(group, close_net_credit=-0.62)
    plain_again = build_close_ticket(group, close_net_credit=-0.62)
    salted = build_close_ticket(group, close_net_credit=-0.62, seed_salt="2026-06-12:retry1")
    salted_again = build_close_ticket(group, close_net_credit=-0.62, seed_salt="2026-06-12:retry1")
    next_retry = build_close_ticket(group, close_net_credit=-0.62, seed_salt="2026-06-12:retry2")

    assert plain["order_id"] == plain_again["order_id"]
    assert salted["order_id"] == salted_again["order_id"]
    assert salted["ticket_hash"] == salted_again["ticket_hash"]
    assert salted["order_id"] != plain["order_id"]
    assert next_retry["order_id"] != salted["order_id"]
