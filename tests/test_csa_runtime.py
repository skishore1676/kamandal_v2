from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from kamandal_v2.market.fixture import FixtureMarketDataProvider, FixturePreflightClient
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.strategy_lanes.management_runtime import run_csa_live_management, run_csa_shadow_management
from kamandal_v2.strategy_lanes.management_runtime import _cooldown_elapsed, _strangle_roll_plans
from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg, PreflightResult
from kamandal_v2.strategy_lanes.reports import (
    build_csa_scorecard,
    build_csa_weekly_economics,
    render_csa_scorecard,
    write_csa_scorecard,
    write_csa_weekly_economics,
)
from kamandal_v2.strategy_lanes.runtime import _resolve_preflight, run_csa_live_scan, run_csa_shadow_scan
from kamandal_v2.strategy_lanes.models import (
    LaneId,
    LegEffect,
    LegSide,
    LifecycleState,
    StrategyTicket,
    TicketLeg,
)
from kamandal_v2.strategy_lanes.operator_policy import load_csa_operator_policy
from kamandal_v2.strategy_lanes.store import CsaStore
from kamandal_v2.live.execution import _adopt_csa_live_fill, execute_live_approved
from kamandal_v2.live.orders import build_csa_live_ticket


def _tables():  # noqa: ANN202
    return {
        "universe": [{"symbol": "XYZ", "enabled": "TRUE", "profile": "large_cap"}],
        "playbooks": [
            {
                "playbook_id": "short_strangle_csa",
                "enabled": "TRUE",
                "strategy_family": "short_strangle",
                "structure": "short_strangle",
                "csa_stage": "shadow",
                "source_mode": "market_scan",
                "management_policy_json": json.dumps(
                    {
                        "lifecycle": {
                            "tested_side_confirmation": 2,
                            "roll": {"min_credit": 0.1, "duration_trigger_dte": 21},
                            "adjustment_limit": 2,
                            "inversion": {"allowed": True, "max_width": 5},
                            "cooldown": {"minutes": 30},
                            "loss_stages": {"watch_multiple": 2, "close_multiple": 3},
                            "fill": {"max_attempts": 4, "price_increment": 0.05},
                            "live_management_mode": "close_only",
                        }
                    }
                ),
                "sizing_method": "fixed_contracts",
                "sizing_value": 1,
                "max_contracts": 1,
                "score_weight_credit": 1,
                "score_weight_pop": 1,
                "score_weight_liquidity": 1,
                "score_weight_spread": 1,
                "max_bid_ask_pct": 1,
                "min_option_oi": 1,
                "dte_min": 30,
                "dte_max": 60,
                "short_delta_min": 0.1,
                "short_delta_max": 0.2,
                "iv_rank_min": 35,
                "iv_rank_max": 100,
                "profit_target_pct": 50,
                "exit_dte_min": 21,
                "live_max_bpr_per_order": 2500,
                "universe_expansion_enabled": "TRUE",
                "underlying_price_min": 50,
                "underlying_price_max": 250,
            }
        ],
        "daily_plan": [],
    }


def test_public_level_four_rejection_uses_tastytrade_bpr_in_shadow_only() -> None:
    policy = load_csa_operator_policy({}, tables=_tables(), read_at="2026-08-10T12:00:00Z").policies[0]
    candidate = Candidate(
        candidate_id="candidate",
        idea_id="idea",
        underlying="XYZ",
        playbook_id=policy.playbook_id,
        structure="short_strangle",
        legs=[],
        net_credit=2.0,
        estimated_bpr=3100.0,
        greeks=Greeks(),
        liquidity_score=1.0,
        score=0.0,
    )
    public = PreflightResult(
        ok=False,
        bpr=3100.0,
        message="Level 4 required",
        raw={"public_api_error": {"http_status": 400, "code": 159}},
    )

    class TastytradeDryRun:
        def preflight(self, _candidate):  # noqa: ANN001, ANN202
            return PreflightResult(
                ok=False,
                bpr=3922.62,
                message="margin check failed",
                raw={"response": {"data": {"buying-power-effect": {"change-in-buying-power": "3922.62"}}}},
            )

    shadow = _resolve_preflight(
        candidate,
        policy,
        public,
        shadow_bpr_preflight=TastytradeDryRun(),
        execution_mode="shadow",
    )
    live = _resolve_preflight(
        candidate,
        policy,
        public,
        shadow_bpr_preflight=TastytradeDryRun(),
        execution_mode="live",
    )

    assert shadow.ok is True
    assert shadow.bpr == 3922.62
    assert shadow.raw["bpr_broker"] == "tastytrade"
    assert shadow.raw["public_live_eligibility"] == "level_4_required"
    assert live is public


def _baseline_counts(database):  # noqa: ANN001, ANN202
    with sqlite3.connect(database) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ideas", "candidates", "plans", "shadow_fills", "live_order_intents", "live_positions")
        }


def _portfolio_tables():  # noqa: ANN202
    row = {
        "playbook_id": "call_spread_hedge_csa",
        "enabled": "TRUE",
        "strategy_family": "call_vertical",
        "structure": "call_spread",
        "csa_stage": "shadow",
        "source_mode": "portfolio_hedge",
        "management_policy_json": json.dumps(
            {
                "lifecycle": {
                    "close_only": True,
                    "portfolio_delta_trigger": 25,
                    "hedge_underlyings": ["SPY"],
                    "fill": {"max_attempts": 4, "price_increment": 0.05},
                }
            }
        ),
        "sizing_method": "fixed_contracts",
        "sizing_value": 1,
        "max_contracts": 1,
        "score_weight_credit": 1,
        "score_weight_pop": 1,
        "score_weight_liquidity": 1,
        "score_weight_spread": 1,
        "max_bid_ask_pct": 1,
        "min_option_oi": 1,
        "dte_min": 30,
        "dte_max": 60,
        "short_delta_min": 0.2,
        "short_delta_max": 0.35,
        "spread_width": 5,
        "profit_target_pct": 50,
        "max_loss_multiple": 2,
        "exit_dte_min": 21,
        "live_max_bpr_per_order": 1000,
    }
    return {"universe": [], "playbooks": [row], "daily_plan": []}


def test_shadow_scan_runs_end_to_end_without_baseline_or_broker_effects(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    before = _baseline_counts(database)
    market = FixtureMarketDataProvider(account_size=100_000)
    preflight = FixturePreflightClient()

    first = run_csa_shadow_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=_tables(),
        market=market,
        preflight=preflight,
        observed_at="2026-08-08T12:00:00Z",
    )
    second = run_csa_shadow_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=_tables(),
        market=market,
        preflight=preflight,
        observed_at="2026-08-08T12:05:00Z",
    )

    store = CsaStore(database, read_only=True)
    assert first.ok
    assert first.opportunity_count == 1
    assert first.candidate_count > 0
    assert first.admitted_count == 1
    assert first.filled_count == 0
    assert second.filled_count == 1
    assert second.admitted_count == 0
    assert len(store.open_lifecycles()) == 1
    assert len(store.rows("csa_shadow_order_intents")) == 1
    assert _baseline_counts(database) == before


def test_shadow_scan_uses_paper_account_and_ignores_live_account_entry_bars(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    live_store = LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    live_store.save_live_position_group(
        "existing-live-position",
        {"underlying": "XYZ", "candidate": {"underlying": "XYZ", "legs": []}},
        status="open",
    )
    csa_store = CsaStore(database)
    csa_store.save_lifecycle(
        LifecycleState(
            lifecycle_id="existing-live-csa",
            opportunity_id="existing-live-opportunity",
            lane=LaneId.SHORT_STRANGLE,
            version=1,
            status="open",
            active_legs=(),
            cashflow_ledger=(),
            opened_at="2026-08-08T11:00:00Z",
            updated_at="2026-08-08T11:00:00Z",
            policy_hash="old-live-policy",
            metadata={
                "execution_mode": "live",
                "underlying": "XYZ",
                "playbook_id": "short_strangle_csa",
                "bpr": 50_000,
            },
        )
    )
    tables = _tables()
    tables["playbooks"][0]["live_max_bpr_per_order"] = 1
    config = {
        "runtime": {"mode": "live"},
        "shadow": {
            "account_size_override": 20_000,
            "buying_power_override": 20_000,
            "bpr_used_override": 0,
        },
    }

    result = run_csa_shadow_scan(
        config,
        sqlite_path=str(database),
        provider="fixture",
        tables=tables,
        market=FixtureMarketDataProvider(account_size=100),
        preflight=FixturePreflightClient(),
        observed_at="2026-08-08T12:00:00Z",
    )

    assert result.ok
    assert result.admitted_count == 1
    shadow_lifecycles = [
        item
        for item in CsaStore(database, read_only=True).open_lifecycles()
        if item.metadata.get("execution_mode") == "shadow"
    ]
    assert len(shadow_lifecycles) == 1
    assert shadow_lifecycles[0].metadata["bpr"] > 100


def test_shadow_scan_reserves_csa_bpr_against_shared_paper_buying_power(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    tables = _tables()
    tables["universe"] = [
        {"symbol": "AAA", "enabled": "TRUE", "profile": "large_cap"},
        {"symbol": "BBB", "enabled": "TRUE", "profile": "large_cap"},
    ]
    config = {
        "runtime": {"mode": "live"},
        "shadow": {
            "account_size_override": 2_500,
            "buying_power_override": 2_500,
            "bpr_used_override": 0,
        },
    }

    result = run_csa_shadow_scan(
        config,
        sqlite_path=str(database),
        provider="fixture",
        tables=tables,
        market=FixtureMarketDataProvider(account_size=100_000),
        preflight=FixturePreflightClient(),
        observed_at="2026-08-08T12:00:00Z",
    )

    assert result.ok
    assert result.opportunity_count == 2
    assert result.admitted_count == 1
    assert len(CsaStore(database, read_only=True).open_lifecycles()) == 1


def test_shadow_runtime_ignores_nonshadow_stage_without_duplicate_execution(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    tables = _tables()
    tables["playbooks"][0]["csa_stage"] = "pilot_live"

    result = run_csa_shadow_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=tables,
        market=FixtureMarketDataProvider(),
        preflight=FixturePreflightClient(),
        observed_at="2026-08-08T12:00:00Z",
    )

    assert result.ok
    assert result.opportunity_count == 0
    assert result.errors == ()


def test_empty_scorecard_is_no_data_not_green(tmp_path) -> None:
    report = build_csa_scorecard(tmp_path / "missing.db", trading_date="2026-08-08")

    assert report["evidence_status"] == "NO_DATA"
    assert "Verdict: **NO_DATA**" in render_csa_scorecard(report)
    assert "GREEN" not in render_csa_scorecard(report)


def test_weekly_economics_uses_terminal_cashflows_and_same_day_open_marks(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    store = CsaStore(database)
    common = {
        "opportunity_id": "opportunity",
        "lane": LaneId.SHORT_STRANGLE,
        "version": 3,
        "active_legs": (),
        "policy_hash": "policy",
    }
    store.save_lifecycle(
        LifecycleState(
            lifecycle_id="closed",
            status="closed",
            cashflow_ledger=(
                {"amount": 1.5, "filled_at": "2026-08-04T15:00:00Z"},
                {"amount": -0.5, "filled_at": "2026-08-06T15:00:00Z"},
            ),
            opened_at="2026-08-04T15:00:00Z",
            updated_at="2026-08-06T15:00:00Z",
            metadata={
                "playbook_id": "short_strangle_csa",
                "execution_mode": "shadow",
                "bpr": 2_000,
                "adjustment_count": 1,
                "policy": {"stage": "shadow"},
            },
            **common,
        )
    )
    store.save_lifecycle(
        LifecycleState(
            lifecycle_id="open",
            status="open",
            cashflow_ledger=({"amount": 1.0, "filled_at": "2026-08-07T15:00:00Z"},),
            opened_at="2026-08-07T15:00:00Z",
            updated_at="2026-08-07T19:45:00Z",
            metadata={
                "playbook_id": "short_strangle_csa",
                "execution_mode": "shadow",
                "bpr": 1_500,
                "last_marked_at": "2026-08-07T19:45:00Z",
                "mark_pnl_price": 0.25,
                "policy": {"stage": "shadow"},
            },
            **common,
        )
    )

    report = build_csa_weekly_economics(database, through_date="2026-08-07")
    row = report["economic_rows"][0]

    assert report["schema"] == "kamandal.strategy_weekly_economics.v1"
    assert report["period_start"] == "2026-08-03"
    assert report["receipt"]["status"] == "ok"
    assert row["closed_in_period"] == 1
    assert row["active_open"] == 1
    assert row["wins"] == 1
    assert row["realized_pnl_usd"] == 100.0
    assert row["open_unrealized_pnl_usd"] == 25.0
    assert row["total_pnl_usd"] == 125.0
    assert row["realized_return_on_bpr_pct"] == 5.0
    assert row["economic_status"] == "observed"
    assert report["recommendation_authority"] is False
    assert report["sheet_write_authority"] is False

    written = write_csa_weekly_economics(
        database,
        output_dir=tmp_path / "reports",
        through_date="2026-08-07",
    )
    assert written.json_path.exists()
    assert written.markdown_path.exists()
    assert written.csv_path.exists()


def test_weekly_economics_fails_open_pnl_closed_when_same_day_mark_is_missing(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    CsaStore(database).save_lifecycle(
        LifecycleState(
            lifecycle_id="open",
            opportunity_id="opportunity",
            lane=LaneId.SHORT_STRANGLE,
            version=2,
            status="open",
            active_legs=(),
            cashflow_ledger=({"amount": 1.0, "filled_at": "2026-08-07T15:00:00Z"},),
            opened_at="2026-08-07T15:00:00Z",
            updated_at="2026-08-07T19:45:00Z",
            policy_hash="policy",
            metadata={
                "playbook_id": "short_strangle_csa",
                "execution_mode": "shadow",
                "bpr": 1_500,
                "last_marked_at": "2026-08-06T19:45:00Z",
                "mark_pnl_price": 0.25,
                "policy": {"stage": "shadow"},
            },
        )
    )

    row = build_csa_weekly_economics(database, through_date="2026-08-07")["economic_rows"][0]

    assert row["economic_status"] == "partial"
    assert row["open_unrealized_pnl_usd"] is None
    assert row["total_pnl_usd"] is None
    assert row["quality_issues"] == ["open_lifecycle_missing_same_day_mark"]


def test_weekly_economics_does_not_invent_close_pnl_without_verified_fill(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    CsaStore(database).save_lifecycle(
        LifecycleState(
            lifecycle_id="unknown-close",
            opportunity_id="opportunity",
            lane=LaneId.CALL_VERTICAL,
            version=1,
            status="closed",
            active_legs=(),
            cashflow_ledger=({"amount": 0.88, "filled_at": "2026-08-20T15:00:00Z"},),
            opened_at="2026-08-20T15:00:00Z",
            updated_at="2026-08-21T19:45:00Z",
            policy_hash="policy",
            metadata={
                "playbook_id": "call_spread_default",
                "execution_mode": "live",
                "bpr": 500,
                "terminal_economics_status": "reconciled_without_fill",
                "policy": {"stage": "live"},
            },
        )
    )

    report = build_csa_weekly_economics(database, through_date="2026-08-21")
    row = report["economic_rows"][0]

    assert row["closed_in_period"] == 1
    assert row["economically_complete_closed"] == 0
    assert row["economically_unknown_closed"] == 1
    assert row["known_realized_pnl_usd"] == 0.0
    assert row["realized_pnl_usd"] is None
    assert row["total_pnl_usd"] is None
    assert row["economic_status"] == "partial"
    assert row["quality_issues"] == ["closed_lifecycle_terminal_economics_unknown"]
    assert report["book_totals"]["live"]["realized_pnl_usd"] is None
    assert report["book_totals"]["live"]["known_realized_pnl_usd"] == 0.0


def test_weekly_economics_never_combines_live_and_shadow_books(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    store = CsaStore(database)
    for execution_mode, amount in (("live", 1.0), ("shadow", -0.4)):
        store.save_lifecycle(
            LifecycleState(
                lifecycle_id=f"{execution_mode}-close",
                opportunity_id=f"{execution_mode}-opportunity",
                lane=LaneId.SHORT_STRANGLE,
                version=1,
                status="closed",
                active_legs=(),
                cashflow_ledger=({"amount": amount, "filled_at": "2026-08-21T19:45:00Z"},),
                opened_at="2026-08-20T15:00:00Z",
                updated_at="2026-08-21T19:45:00Z",
                policy_hash=f"{execution_mode}-policy",
                metadata={
                    "playbook_id": f"{execution_mode}_playbook",
                    "execution_mode": execution_mode,
                    "bpr": 1_000,
                    "policy": {"stage": execution_mode},
                },
            )
        )

    report = build_csa_weekly_economics(database, through_date="2026-08-21")

    assert "totals" not in report
    assert report["book_totals"]["live"]["realized_pnl_usd"] == 100.0
    assert report["book_totals"]["shadow"]["realized_pnl_usd"] == -40.0


def test_stage_change_fails_closed_while_shadow_order_is_working(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    tables = _tables()
    market = FixtureMarketDataProvider()
    first = run_csa_shadow_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=tables,
        market=market,
        preflight=FixturePreflightClient(),
        observed_at="2026-08-08T12:00:00Z",
    )
    tables["playbooks"][0]["csa_stage"] = "pilot_live"

    changed = run_csa_shadow_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=tables,
        market=market,
        preflight=FixturePreflightClient(),
        observed_at="2026-08-08T12:05:00Z",
    )

    assert first.filled_count == 0
    assert not changed.ok
    assert any("must resolve before" in error for error in changed.errors)


def test_live_stage_routes_one_intent_to_guarded_ledger_without_broker_submission(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    tables = _tables()
    tables["playbooks"][0]["csa_stage"] = "pilot_live"

    class BrokerAuthoritativeFixture:
        def preflight(self, candidate):  # noqa: ANN001
            return type(FixturePreflightClient().preflight(candidate))(
                ok=True,
                bpr=candidate.estimated_bpr,
                message="broker authoritative fixture",
                raw={"bpr_source": "broker_preflight", "broker_bpr_provided": True},
            )

    result = run_csa_live_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=tables,
        market=FixtureMarketDataProvider(account_size=100_000),
        preflight=BrokerAuthoritativeFixture(),
        observed_at="2026-08-08T12:00:00Z",
    )

    intents = LocalStore(database, read_only=True).live_order_intents_by_status(
        {"stage_approved_pending_submit"}
    )
    assert result.ok
    assert result.execution_mode == "live"
    assert result.live_intent_count == 1
    assert len(intents) == 1
    assert intents[0]["csa_stage"] == "pilot_live"
    assert intents[0]["csa_playbook_id"] == "short_strangle_csa"
    assert intents[0]["pilot_contract_cap"] == 1
    assert intents[0]["stage_authorized"] is True

    repeated = run_csa_live_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=tables,
        market=FixtureMarketDataProvider(account_size=100_000),
        preflight=BrokerAuthoritativeFixture(),
        observed_at="2026-08-08T12:05:00Z",
    )
    assert repeated.live_intent_count == 0
    assert len(
        LocalStore(database, read_only=True).live_order_intents_by_status(
            {"stage_approved_pending_submit"}
        )
    ) == 1
    scorecard = build_csa_scorecard(database, trading_date="2026-08-08")
    experiment = scorecard["experiments"][0]
    assert scorecard["evidence_status"] == "COLLECTING"
    assert scorecard["zero_broker_effect"] is False
    assert scorecard["zero_unexpected_broker_effect"] is True
    assert scorecard["unexpected_broker_effects"] == 0
    assert experiment["stage"] == "pilot_live"
    assert experiment["live_intents"] == {"stage_approved_pending_submit": 1}


def test_live_fill_advances_same_lifecycle_and_management_stages_reusable_close(tmp_path, monkeypatch) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    tables = _tables()
    tables["playbooks"][0]["csa_stage"] = "live"
    management = json.loads(tables["playbooks"][0]["management_policy_json"])
    management["lifecycle"]["loss_stages"]["close_multiple"] = 0
    tables["playbooks"][0]["management_policy_json"] = json.dumps(management)

    class BrokerAuthoritativeFixture:
        def preflight(self, candidate):  # noqa: ANN001
            return type(FixturePreflightClient().preflight(candidate))(
                ok=True,
                bpr=candidate.estimated_bpr,
                message="broker authoritative fixture",
                raw={"bpr_source": "broker_preflight", "broker_bpr_provided": True},
            )

    scan = run_csa_live_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=tables,
        market=FixtureMarketDataProvider(account_size=100_000),
        preflight=BrokerAuthoritativeFixture(),
        observed_at="2026-08-08T12:00:00Z",
    )
    live_store = LocalStore(database)
    entry = live_store.live_order_intents_by_status({"stage_approved_pending_submit"})[0]
    adopted = _adopt_csa_live_fill(live_store, entry, {"averagePrice": "1.00", "filledAt": "2026-08-08T12:01:00Z"})

    result = run_csa_live_management(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=tables,
        market=FixtureMarketDataProvider(account_size=100_000),
        observed_at="2026-08-08T12:15:00Z",
    )
    staged = live_store.live_order_intents_by_status({"stage_approved_pending_submit"})
    close_ticket = next(item for item in staged if item["intent_type"] == "close")

    assert scan.live_intent_count == 1
    assert adopted["status"] == "open"
    assert result.ok
    assert result.live_intent_count == 1
    assert result.selected_actions == {"close": 1}
    assert close_ticket["csa_action_type"] == "close"
    assert {leg["openCloseIndicator"] for leg in close_ticket["submit_payload"]["legs"]} == {"CLOSE"}

    monkeypatch.setattr(
        "kamandal_v2.live.execution.pull_sheet_tables",
        lambda _config: (_ for _ in ()).throw(AssertionError("frozen lifecycle management must not read the Sheet")),
    )
    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", lambda _config: object())
    executor_config = {"live": {"exit_submit_source": "ledger"}}

    executed = execute_live_approved(executor_config, submit=False, close=True, store=live_store)

    assert executed["management"] is True
    assert executed["source"] == "frozen_lifecycle_ledger"
    assert executed["results"][0]["status"] == "dry_run"

    # A later-day adjustment uses the same frozen lifecycle authority even
    # when the current Sheet projection is absent.  Keep this as a real
    # executor path: do not replace stage authorization with an allow stub.
    live_store.update_live_order_intent_status(close_ticket["ticket_hash"], "close_filled")
    adjustment = json.loads(json.dumps(close_ticket))
    adjustment["ticket_hash"] = "adjust-" + str(close_ticket["ticket_hash"])
    adjustment["order_id"] = "adjust-" + str(close_ticket["order_id"])
    adjustment["intent_type"] = "adjust"
    adjustment["csa_action_type"] = "adjust"
    adjustment["csa_strategy_ticket"]["metadata"]["action_type"] = "adjust"
    live_store.save_live_order_intent(adjustment, status="stage_approved_pending_submit")

    adjusted = execute_live_approved(executor_config, submit=False, close=True, store=live_store)

    assert adjusted["management"] is True
    assert adjusted["source"] == "frozen_lifecycle_ledger"
    assert adjusted["results"][0]["status"] == "dry_run"


def test_canonical_close_fill_atomically_retires_live_book_projection(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    store = LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    group_id = "live_group_amzn_calendar"
    lifecycle = LifecycleState(
        lifecycle_id="adopt:" + group_id,
        opportunity_id="legacy:" + group_id,
        lane=LaneId.EARNINGS_CALENDAR,
        version=2,
        status="open",
        active_legs=(
            {
                "side": "buy",
                "effect": "open",
                "quantity": 1,
                "option_type": "call",
                "expiration": "2026-10-16",
                "strike": 290.0,
                "role": "long_call",
            },
        ),
        cashflow_ledger=(),
        opened_at="2026-08-03T14:30:00Z",
        updated_at="2026-08-21T13:30:00Z",
        policy_hash="policy",
        metadata={
            "execution_mode": "live",
            "position_projection_id": group_id,
            "underlying": "AMZN",
        },
    )
    CsaStore(database).save_lifecycle(lifecycle)
    store.save_live_position_group(
        group_id,
        {
            "group_id": group_id,
            "underlying": "AMZN",
            "candidate": {
                "underlying": "AMZN",
                "legs": [
                    {
                        "expiration": "2026-10-16",
                        "option_type": "call",
                        "strike": 290.0,
                        "quantity": 1,
                        "side": "buy",
                    }
                ],
            },
        },
    )
    store.save_live_position(
        "amzn-long-call",
        group_id,
        {
            "underlying": "AMZN",
            "structure": "call_calendar",
            "option_type": "call",
            "expiration": "2026-10-16",
            "strike": 290.0,
            "quantity": 1,
            "side": "buy",
        },
    )
    strategy_ticket = StrategyTicket(
        ticket_id="close-amzn-calendar",
        action_id="close-amzn",
        lifecycle_id=lifecycle.lifecycle_id,
        lifecycle_version=lifecycle.version,
        lane=lifecycle.lane,
        underlying="AMZN",
        order_kind="credit",
        limit_price=3.30,
        legs=(
            TicketLeg(
                instrument_id="AMZN  261016C00290000",
                side=LegSide.SELL,
                effect=LegEffect.CLOSE,
                quantity=1,
                option_type="call",
                expiration="2026-10-16",
                strike=290.0,
                role="long_call",
            ),
        ),
        policy_hash="policy",
        created_at="2026-08-21T13:30:00Z",
        metadata={
            "action_type": "close",
            "position_projection_id": group_id,
        },
    )
    live_ticket = build_csa_live_ticket(strategy_ticket)
    store.save_live_order_intent(live_ticket, status="submitted")

    result = _adopt_csa_live_fill(
        store,
        live_ticket,
        {"averagePrice": "3.3103", "filledAt": "2026-08-21T13:30:05Z"},
    )

    assert live_ticket["position_projection_id"] == group_id
    assert live_ticket["group_id"] == group_id
    assert result["status"] == "closed"
    assert result["projection_retired"] is True
    assert CsaStore(database).lifecycle(lifecycle.lifecycle_id).status == "closed"
    assert store.open_live_position_groups() == []
    closed_group = store.closed_live_position_groups()[0]
    assert closed_group["_status"] == "closed_by_canonical_lifecycle"
    assert store.live_order_intent(live_ticket["ticket_hash"])["_ledger_status"] == "close_filled"


def test_management_and_scorecard_complete_the_broker_inert_runtime_loop(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    market = FixtureMarketDataProvider(account_size=100_000)
    scan = run_csa_shadow_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=_tables(),
        market=market,
        preflight=FixturePreflightClient(),
        observed_at="2026-08-08T12:00:00Z",
    )
    followup = run_csa_shadow_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=_tables(),
        market=market,
        preflight=FixturePreflightClient(),
        observed_at="2026-08-08T12:05:00Z",
    )
    management = run_csa_shadow_management(
        {},
        sqlite_path=str(database),
        provider="fixture",
        # Management must govern the immutable policy embedded when this
        # lifecycle was opened.  A later Sheet edit (including removal of the
        # playbook) cannot orphan or rewrite the open trade.
        tables={"universe": [], "playbooks": [], "daily_plan": []},
        market=market,
        observed_at="2026-08-08T12:15:00Z",
    )
    written = write_csa_scorecard(database, output_dir=tmp_path / "reports", trading_date="2026-08-08")
    scorecard = build_csa_scorecard(database, trading_date="2026-08-08")

    assert scan.filled_count == 0
    assert followup.filled_count == 1
    assert management.ok
    assert management.selected_actions == {"hold": 1}
    assert scorecard["runs"] == 3
    assert scorecard["evidence_status"] == "COLLECTING"
    assert scorecard["experiments"][0]["playbook_id"] == "short_strangle_csa"
    assert scorecard["zero_broker_effect"] is True
    assert scorecard["csa_live_intents"] == 0
    marked = CsaStore(database, read_only=True).open_lifecycles()[0]
    assert marked.metadata["last_marked_at"] == "2026-08-08T12:15:00Z"
    assert marked.metadata["mark_source"] == "natural_close_quote"
    assert "mark_pnl_price" in marked.metadata
    assert written.json_path.exists()
    assert written.markdown_path.exists()
    assert written.csv_path.exists()


def test_shadow_management_skips_unfilled_proposed_lifecycles(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    market = FixtureMarketDataProvider(account_size=100_000)
    scan = run_csa_shadow_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=_tables(),
        market=market,
        preflight=FixturePreflightClient(),
        observed_at="2026-08-08T12:00:00Z",
    )
    management = run_csa_shadow_management(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=_tables(),
        market=market,
        observed_at="2026-08-08T12:15:00Z",
    )

    proposed = CsaStore(database, read_only=True).open_lifecycles()[0]

    assert scan.filled_count == 0
    assert proposed.status == "proposed"
    assert proposed.active_legs == ()
    assert management.ok
    assert management.lifecycle_count == 0
    assert management.selected_actions == {}


def test_portfolio_hedge_source_uses_open_live_ledger_delta(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    baseline_store = LocalStore(database)
    baseline_store.save_live_position_group(
        "group-1",
        {
            "underlying": "XYZ",
            "candidate": {
                "underlying": "XYZ",
                "estimated_bpr": 500,
                "greeks": {"delta": 30, "gamma": 0, "theta": 0, "vega": 0},
            },
        },
        status="open",
    )
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")

    result = run_csa_shadow_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=_portfolio_tables(),
        market=FixtureMarketDataProvider(account_size=100_000),
        preflight=FixturePreflightClient(),
        observed_at="2026-08-08T12:00:00Z",
    )

    assert result.ok
    assert result.opportunity_count == 1
    assert result.candidate_count > 0


def test_strangle_adjustment_moves_untested_side_inward_without_inversion() -> None:
    policy_tables = _tables()
    from kamandal_v2.strategy_lanes.policy import compile_csa_policy

    policy = compile_csa_policy(
        policy_tables["playbooks"][0], source="google_sheet", read_at="2026-08-08T12:00:00Z"
    )
    assert policy is not None
    expiration = (date.today() + timedelta(days=45)).isoformat()
    market = FixtureMarketDataProvider()
    base = market.chain_snapshot("XYZ")
    quote_type = type(base.quotes[0])
    quotes = [
        quote_type("XYZ", expiration, "put", 90, 1.0, 1.1, -0.15, 0, 0, 0, 1000),
        quote_type("XYZ", expiration, "call", 110, 1.0, 1.1, 0.15, 0, 0, 0, 1000),
        quote_type("XYZ", expiration, "call", 100, 1.4, 1.5, 0.18, 0, 0, 0, 1000),
        quote_type("XYZ", expiration, "call", 88, 1.6, 1.7, 0.19, 0, 0, 0, 1000),
    ]
    snapshot = type(base)("test", "XYZ", "2026-08-08T12:00:00Z", 89, quotes, "fixture")
    put = OptionLeg.from_quote(quotes[0], role="short_put", side="sell")
    call = OptionLeg.from_quote(quotes[1], role="short_call", side="sell")

    ordinary, inversion = _strangle_roll_plans("put", put, call, snapshot, policy)

    assert ordinary["new"].strike == 100
    assert put.strike < ordinary["new"].strike < call.strike
    assert inversion is None


def test_strangle_cooldown_uses_sheet_minutes_and_last_fill_timestamp() -> None:
    from kamandal_v2.strategy_lanes.policy import compile_csa_policy

    policy = compile_csa_policy(
        _tables()["playbooks"][0], source="google_sheet", read_at="2026-08-08T12:00:00Z"
    )
    assert policy is not None
    metadata = {"last_adjustment_at": "2026-08-08T12:00:00Z"}

    assert _cooldown_elapsed(metadata, policy, "2026-08-08T12:29:59Z") is False
    assert _cooldown_elapsed(metadata, policy, "2026-08-08T12:30:00Z") is True


def test_shadow_management_ignores_live_contract_ownership(tmp_path) -> None:
    database = tmp_path / "kamandal.db"
    baseline = LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    market = FixtureMarketDataProvider(account_size=100_000)
    scan = run_csa_shadow_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=_tables(),
        market=market,
        preflight=FixturePreflightClient(),
        observed_at="2026-08-08T12:00:00Z",
    )
    followup = run_csa_shadow_scan(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=_tables(),
        market=market,
        preflight=FixturePreflightClient(),
        observed_at="2026-08-08T12:05:00Z",
    )
    lifecycle = CsaStore(database, read_only=True).open_lifecycles()[0]
    baseline.save_live_position_group(
        "live-overlap",
        {
            "underlying": "XYZ",
            "candidate": {"underlying": "XYZ", "legs": list(lifecycle.active_legs)},
        },
        status="open",
    )

    management = run_csa_shadow_management(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=_tables(),
        market=market,
        observed_at="2026-08-08T12:15:00Z",
    )

    assert scan.filled_count == 0
    assert followup.filled_count == 1
    assert management.selected_actions == {"hold": 1}
    assert len(CsaStore(database, read_only=True).rows("csa_shadow_order_intents")) == 1
