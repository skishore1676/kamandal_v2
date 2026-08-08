from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from kamandal_v2.market.fixture import FixtureMarketDataProvider, FixturePreflightClient
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.strategy_lanes.management_runtime import run_csa_shadow_management
from kamandal_v2.strategy_lanes.management_runtime import _cooldown_elapsed, _strangle_roll_plans
from kamandal_v2.domain.models import OptionLeg
from kamandal_v2.strategy_lanes.reports import build_csa_scorecard, write_csa_scorecard
from kamandal_v2.strategy_lanes.runtime import run_csa_shadow_scan
from kamandal_v2.strategy_lanes.store import CsaStore


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
    assert first.filled_count == 1
    assert second.admitted_count == 0
    assert second.filled_count == 0
    assert len(store.open_lifecycles()) == 1
    assert len(store.rows("csa_shadow_order_intents")) == 1
    assert _baseline_counts(database) == before


def test_shadow_runtime_refuses_nonshadow_stage(tmp_path) -> None:
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

    assert not result.ok
    assert result.opportunity_count == 0
    assert any("shadow stage only" in error for error in result.errors)


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
    management = run_csa_shadow_management(
        {},
        sqlite_path=str(database),
        provider="fixture",
        tables=_tables(),
        market=market,
        observed_at="2026-08-08T12:15:00Z",
    )
    written = write_csa_scorecard(database, output_dir=tmp_path / "reports", trading_date="2026-08-08")
    scorecard = build_csa_scorecard(database, trading_date="2026-08-08")

    assert scan.filled_count == 1
    assert management.ok
    assert management.selected_actions == {"hold": 1}
    assert scorecard["runs"] == 2
    assert scorecard["zero_broker_effect"] is True
    assert scorecard["csa_live_intents"] == 0
    assert written.json_path.exists()
    assert written.markdown_path.exists()
    assert written.csv_path.exists()


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


def test_strangle_adjustment_moves_untested_side_inward_and_bounds_inversion() -> None:
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
    assert inversion["new"].strike == 88
    assert abs(inversion["new"].strike - put.strike) <= 5


def test_strangle_cooldown_uses_sheet_minutes_and_last_fill_timestamp() -> None:
    from kamandal_v2.strategy_lanes.policy import compile_csa_policy

    policy = compile_csa_policy(
        _tables()["playbooks"][0], source="google_sheet", read_at="2026-08-08T12:00:00Z"
    )
    assert policy is not None
    metadata = {"last_adjustment_at": "2026-08-08T12:00:00Z"}

    assert _cooldown_elapsed(metadata, policy, "2026-08-08T12:29:59Z") is False
    assert _cooldown_elapsed(metadata, policy, "2026-08-08T12:30:00Z") is True


def test_management_blocks_when_live_ledger_claims_a_shadow_contract(tmp_path) -> None:
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

    assert scan.filled_count == 1
    assert management.selected_actions == {"block": 1}
    assert len(CsaStore(database, read_only=True).rows("csa_shadow_order_intents")) == 1
