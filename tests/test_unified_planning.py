from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kamandal_v2.config import load_control
from kamandal_v2.domain.models import Candidate, ChainSnapshot, Greeks, Idea, OptionLeg, OptionQuote, Plan, Playbook, PortfolioState, PreflightResult, UniverseEntry
from kamandal_v2.planner.candidate_builder import _match_rejections
from kamandal_v2.planner.engine import PlanRunResult
from kamandal_v2.seed import build_seed_tables, seed_headers
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.strategy_lanes.daily_policy import DailyPolicySnapshot, capture_daily_policy_snapshot, current_trading_date, policy_tables_hash
from kamandal_v2.strategy_lanes.operator_policy import OperatorPolicyBundle
from kamandal_v2.strategy_lanes.models import LaneId, LifecycleState
from kamandal_v2.strategy_lanes.store import CsaStore
from kamandal_v2.strategy_engine.ownership import retire_orphaned_pending_live_lifecycles
from kamandal_v2.strategy_engine.planning import run_unified_books, run_unified_fallback_plan
from kamandal_v2.live.execution import _advance_plan_fallbacks, execute_live_approved
from kamandal_v2.live.plan_fallback import attempt_event_type


def test_unified_planner_does_not_import_deprecated_scanner_runtime() -> None:
    from kamandal_v2.strategy_engine import planning

    source = Path(planning.__file__).read_text(encoding="utf-8")
    assert "kamandal_v2.strategy_lanes.runtime" not in source


def test_unified_fallback_rejects_a_different_policy_snapshot(tmp_path) -> None:  # noqa: ANN001
    snapshot = SimpleNamespace(
        trading_date="2026-08-21",
        snapshot_hash="current-hash",
        tables={"universe": [], "playbooks": []},
    )

    mismatches = (
        ({"date": "2026-08-20", "hash": "current-hash"}, "date no longer matches"),
        ({"date": "2026-08-21", "hash": "rank-one-hash"}, "hash no longer matches"),
    )
    for expected, message in mismatches:
        try:
            run_unified_fallback_plan(
                {},
                store=LocalStore(tmp_path / "state.db"),
                idea_paths=[],
                provider="fixture",
                daily_policy_snapshot=snapshot,
                expected_policy_snapshot=expected,
            )
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError("fallback with a different policy snapshot must fail closed")


def test_orphaned_pending_live_lifecycle_retires_before_next_plan(tmp_path) -> None:  # noqa: ANN001
    store = _migrated_store(tmp_path)
    typed = CsaStore(store.sqlite_path)
    typed.save_lifecycle(
        LifecycleState(
            lifecycle_id="orphan-live-entry",
            opportunity_id="orphan-opportunity",
            lane=LaneId.GENERIC_CLOSE_ONLY,
            version=1,
            status="pending_live_submission",
            active_legs=(),
            cashflow_ledger=(),
            opened_at="2026-08-17T14:30:00Z",
            updated_at="2026-08-17T14:30:00Z",
            policy_hash="fixture-policy",
            metadata={
                "execution_mode": "live",
                "unified_plan_id": "old-plan",
                "candidate_id": "old-candidate",
            },
        )
    )

    repairs = retire_orphaned_pending_live_lifecycles(store)
    assert len(repairs) == 1
    lifecycle = typed.lifecycle("orphan-live-entry")
    assert lifecycle.status == "entry_missed"
    assert lifecycle.metadata["entry_retirement_reason"] == "guarded_open_intent_lineage_terminal"


def _rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    # The production seed importer may intentionally discover an older
    # Kamandal checkout.  Planner unit tests must not change with the host's
    # filesystem, so exercise the checked-in fallback seed deterministically.
    with patch("kamandal_v2.seed.OLD_KAMANDAL_ROOT", Path("/__kamandal_v2_test_no_legacy__")):
        tables = build_seed_tables(load_control())
    headers = seed_headers()
    universe = [dict(zip(headers["universe"], [*row, *[""] * len(headers["universe"])])) for row in tables["universe"]]
    playbooks = [dict(zip(headers["playbooks"], [*row, *[""] * len(headers["playbooks"])])) for row in tables["playbooks"]]
    # Legacy seed fixtures predate the fail-closed Sheet quote-policy contract;
    # make the operator value explicit in this planning fixture.
    for playbook in playbooks:
        if str(playbook.get("enabled") or "").lower() == "true":
            if not playbook.get("max_bid_ask_pct"):
                playbook["max_bid_ask_pct"] = "0.25"
            structure = str(playbook.get("structure") or "")
            if not playbook.get("max_loss_multiple"):
                playbook["max_loss_multiple"] = "1" if structure in {"call_calendar", "put_calendar"} else "2"
            if not playbook.get("exit_dte_min"):
                playbook["exit_dte_min"] = "14" if structure in {"call_calendar", "put_calendar"} else "21"
            playbook["resting_profit_enabled"] = "FALSE"
            playbook["resting_profit_arm_progress_pct"] = "25"
    playbooks.append(
        {
            "playbook_id": "short_strangle_shadow",
            "enabled": "TRUE",
            "strategy_family": "short_strangle",
            "structure": "short_strangle",
            "csa_stage": "shadow",
            "source_mode": "market_scan",
            "dte_min": "30",
            "dte_max": "45",
            "short_delta_min": "0.14",
            "short_delta_max": "0.20",
            "max_bid_ask_pct": "0.25",
            "profit_target_pct": "40",
            "resting_profit_enabled": "TRUE",
            "resting_profit_arm_progress_pct": "25",
            "exit_dte_min": "21",
            "half_time_exit": "TRUE",
            "avoid_earnings": "TRUE",
            "loss_close_multiple": "3",
            "management_policy_json": json.dumps(
                {"lifecycle": {"tested_side_confirmation": 2, "roll": {"min_credit": 0.1}, "fill": {"max_attempts": 2, "price_increment": 0.05}}}
            ),
        }
    )
    return universe, playbooks


def _daily_snapshot(tmp_path, control: dict, universe: list[dict[str, object]], playbooks: list[dict[str, object]]):  # noqa: ANN001
    tables = {"universe": universe, "playbooks": playbooks}
    return DailyPolicySnapshot(
        trading_date=current_trading_date(control),
        captured_at="2026-08-15T12:00:00Z",
        snapshot_hash=policy_tables_hash(tables),
        tables=tables,
        path=Path(tmp_path / "policy" / "strategy_policy_fixture.json"),
        policy=OperatorPolicyBundle((), (), (), "2026-08-15T12:00:00Z", source="fixture"),
    )


def _migrated_store(tmp_path) -> LocalStore:  # noqa: ANN001
    database = tmp_path / "kamandal.db"
    store = LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    return store


def test_sheet_backed_unified_cli_reaches_planner_without_import_shadowing(tmp_path, monkeypatch, capsys) -> None:  # noqa: ANN001
    universe, playbooks = _rows()
    from kamandal_v2 import cli
    from kamandal_v2.strategy_engine import planning
    from kamandal_v2.strategy_lanes import daily_policy

    snapshot = SimpleNamespace(
        tables={"universe": universe, "playbooks": playbooks},
        snapshot_hash="snapshot-hash",
        trading_date="2026-08-17",
    )
    called = []
    monkeypatch.setattr(cli, "pull_sheet_tables", lambda _config: snapshot.tables)
    monkeypatch.setattr(daily_policy, "capture_daily_policy_snapshot", lambda _config, tables: snapshot)
    monkeypatch.setattr(
        planning,
        "run_unified_books",
        lambda *_args, **_kwargs: called.append(True) or SimpleNamespace(
            compilation=SimpleNamespace(errors=[], ok=True),
            live=SimpleNamespace(policy_ids=("live",), result=SimpleNamespace(plans=[]), errors=()),
            shadow=SimpleNamespace(policy_ids=("shadow",), result=SimpleNamespace(plans=[]), errors=()),
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["kamandal", "unified-plan", "--db", str(tmp_path / "kamandal.db"), "--provider", "fixture", "--config-source", "sheet"],
    )

    cli.main()

    assert called == [True]
    assert json.loads(capsys.readouterr().out)["policy_errors"] == []


def test_unified_books_keep_live_and_shadow_policy_ownership_isolated(tmp_path) -> None:
    universe, playbooks = _rows()
    control = load_control()
    snapshot = _daily_snapshot(tmp_path, control, universe, playbooks)
    result = run_unified_books(
        control,
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=_migrated_store(tmp_path),
        audit_root=tmp_path / "audit",
        daily_policy_snapshot=snapshot,
    )

    assert result.compilation.ok
    assert "short_strangle_shadow" not in result.live.policy_ids
    assert result.shadow.policy_ids == ("short_strangle_shadow",)
    assert result.live.errors == ()
    assert result.shadow.errors == ()
    assert result.live.result is not None
    assert result.shadow.result is not None


def test_live_fallback_book_does_not_advance_shadow(tmp_path) -> None:
    universe, playbooks = _rows()
    control = load_control()
    snapshot = _daily_snapshot(tmp_path, control, universe, playbooks)

    result = run_unified_books(
        control,
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=_migrated_store(tmp_path),
        audit_root=tmp_path / "audit",
        daily_policy_snapshot=snapshot,
        include_shadow=False,
    )

    assert result.live.result is not None
    assert result.shadow.policy_ids == ("short_strangle_shadow",)
    assert result.shadow.result is None
    assert result.shadow.errors == ()


def test_one_book_failure_does_not_erase_other_book(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    universe, playbooks = _rows()
    from kamandal_v2.strategy_engine import planning

    original = planning.run_plan

    def fail_shadow(config, **kwargs):  # noqa: ANN001
        if config["runtime"]["mode"] == "shadow":
            raise RuntimeError("shadow fixture failure")
        return original(config, **kwargs)

    monkeypatch.setattr(planning, "run_plan", fail_shadow)
    control = load_control()
    snapshot = _daily_snapshot(tmp_path, control, universe, playbooks)
    result = run_unified_books(
        control,
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=_migrated_store(tmp_path),
        audit_root=tmp_path / "audit",
        daily_policy_snapshot=snapshot,
    )

    assert result.live.result is not None
    assert result.live.errors == ()
    assert result.shadow.result is None
    assert result.shadow.errors == ("RuntimeError: shadow fixture failure",)


def test_market_scan_and_portfolio_hedge_inputs_join_the_same_book(tmp_path) -> None:
    universe, playbooks = _rows()
    for row in playbooks:
        if row["playbook_id"] == "short_strangle_shadow":
            row["source_mode"] = "market_scan"
    playbooks.append(
        {
            "playbook_id": "hedge_call_spread",
            "enabled": "TRUE",
            "strategy_family": "call_spread",
            "structure": "call_spread",
            "mode": "live",
            "source_mode": "portfolio_hedge",
            "dte_min": "30",
            "dte_max": "45",
            "short_delta_min": "0.20",
            "short_delta_max": "0.30",
            "spread_width": "5",
                "max_bid_ask_pct": "0.25",
                "profit_target_pct": "50",
                "resting_profit_enabled": "FALSE",
                "resting_profit_arm_progress_pct": "25",
                "max_loss_multiple": "2",
            "exit_dte_min": "21",
            "half_time_exit": "TRUE",
            "avoid_earnings": "TRUE",
            "management_policy_json": json.dumps({"lifecycle": {"portfolio_delta_trigger": -999, "hedge_underlyings": ["SPY"]}}),
        }
    )

    control = load_control()
    snapshot = _daily_snapshot(tmp_path, control, universe, playbooks)
    result = run_unified_books(
        control,
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=_migrated_store(tmp_path),
        audit_root=tmp_path / "audit",
        daily_policy_snapshot=snapshot,
    )

    assert result.compilation.ok
    assert any(idea.source == "market_scan" for idea in result.shadow.result.ideas)
    assert any(idea.source == "portfolio_hedge" for idea in result.live.result.ideas)
    assert result.shadow.result.metrics["match_gate_mode"] == result.live.result.metrics["match_gate_mode"] == "strict"
    assert result.shadow.result.metrics["candidate_filter_mode"] == result.live.result.metrics["candidate_filter_mode"] == "strict"


def test_market_scan_uses_quantitative_gates_without_invented_thesis_tags() -> None:
    idea = Idea.from_dict(
        {
            "idea_id": "market_scan:XYZ",
            "source": "market_scan",
            "underlying": "XYZ",
            "direction": "neutral",
            "horizon_days": 45,
            "operator_status": "approved",
        }
    )
    entry = UniverseEntry.from_row({"symbol": "XYZ", "enabled": "TRUE", "profile": "large_stocks"})
    playbook = Playbook.from_row(
        {
            "playbook_id": "short_strangle_shadow",
            "enabled": "TRUE",
            "strategy_family": "short_strangle",
            "structure": "short_strangle",
            "profiles": "large_stocks",
            "applicable_direction": "neutral",
            "applicable_thesis_tags": "vol_contraction, range_bound",
            "applicable_horizon_min": "30",
            "applicable_horizon_max": "60",
            "iv_percentile_min": "40",
            "iv_percentile_max": "100",
            "iv_rank_min": "30",
            "iv_rank_max": "100",
        }
    )

    reasons = _match_rejections(
        idea, entry, playbook, 55.0, 45.0, 0.35, "clear", underlying_price=100.0,
    )

    assert "thesis_tags_mismatch" not in reasons
    assert reasons == []


def test_unified_books_only_project_when_explicitly_requested(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    universe, playbooks = _rows()
    writes: list[set[str]] = []
    from kamandal_v2.planner import engine
    from kamandal_v2.strategy_engine import planning

    def write_daily_plan(_config, _rows, _header, *, replace_lanes):  # noqa: ANN001
        writes.append(replace_lanes)
        return 0

    monkeypatch.setattr(planning, "write_daily_plan", write_daily_plan)
    monkeypatch.setattr(engine, "write_daily_plan", write_daily_plan)
    control = load_control()
    snapshot = _daily_snapshot(tmp_path, control, universe, playbooks)
    run_unified_books(
        control,
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=_migrated_store(tmp_path),
        audit_root=tmp_path / "audit",
        write_sheet=True,
        daily_policy_snapshot=snapshot,
    )

    assert writes == [{"live_advisory"}, {"shadow"}]


def test_selected_shadow_plan_persists_one_typed_lifecycle_ticket_and_fill(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    universe, playbooks = _rows()
    shadow_rows = [row for row in playbooks if row["playbook_id"] == "short_strangle_shadow"]
    database = tmp_path / "kamandal.db"
    store = LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    candidate = Candidate(
        candidate_id="selected-strangle",
        idea_id="market_scan:XYZ",
        underlying="XYZ",
        playbook_id="short_strangle_shadow",
        structure="short_strangle",
        legs=[
            OptionLeg("short_put", "sell", "put", 90, "2026-10-16", 1, 1.05, 1.0, 1.1, -0.15, 0, 0, 0, 100),
            OptionLeg("short_call", "sell", "call", 110, "2026-10-16", 1, 1.05, 1.0, 1.1, 0.15, 0, 0, 0, 100),
        ],
        net_credit=2.0,
        estimated_bpr=1000,
        greeks=Greeks(),
        liquidity_score=1,
        score=1,
    )
    portfolio = PortfolioState(100_000, 100_000, 0, 0)
    result = PlanRunResult(
        plan_run_id="run_2026-08-15T12:00:00Z",
        ideas=[],
        candidates=[candidate],
        plans=[Plan("shadow-plan", 1, "eligible", [candidate], 1, 1000, 1, 99_000, portfolio, portfolio, operator_action="approve")],
        daily_plan_rows=[],
        metrics={"observed_at": "2026-08-15T12:00:00Z"},
        idea_diagnostics=[],
        rejection_summary=[],
    )
    from kamandal_v2.strategy_engine import planning

    monkeypatch.setattr(planning, "run_plan", lambda *_args, **_kwargs: result)
    for _ in range(2):
        unified = run_unified_books(
            load_control(),
            universe_rows=universe,
            playbook_rows=shadow_rows,
            idea_paths=[],
            store=store,
            audit_root=tmp_path / "audit",
        )
        assert unified.shadow.errors == ()
        assert unified.shadow.handoffs[0]["adapter_state"] == "filled"

    typed = CsaStore(database, read_only=True)
    lifecycle = typed.open_lifecycles()[0]
    assert lifecycle.status == "open"
    assert lifecycle.metadata["compiled_management_policy"]["policy_hash"] == lifecycle.policy_hash
    assert len(typed.rows("csa_lifecycles")) == 1
    assert len(typed.rows("csa_shadow_order_intents")) == 1
    assert len(typed.rows("csa_shadow_fills")) == 1


def test_selected_shadow_working_entry_advances_and_legacy_rows_are_not_canonical(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    universe, playbooks = _rows()
    shadow_rows = [row for row in playbooks if row["playbook_id"] == "short_strangle_shadow"]
    database = tmp_path / "kamandal.db"
    store = LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    candidate = Candidate(
        candidate_id="working-strangle",
        idea_id="market_scan:XYZ",
        underlying="XYZ",
        playbook_id="short_strangle_shadow",
        structure="short_strangle",
        legs=[
            OptionLeg("short_put", "sell", "put", 90, "2026-10-16", 1, 1.05, 1.0, 1.1, -0.15, 0, 0, 0, 100),
            OptionLeg("short_call", "sell", "call", 110, "2026-10-16", 1, 1.05, 1.0, 1.1, 0.15, 0, 0, 0, 100),
        ],
        net_credit=2.05,
        estimated_bpr=1000,
        greeks=Greeks(delta=1, theta=2),
        liquidity_score=1,
        score=1,
    )
    portfolio = PortfolioState(100_000, 100_000, 0, 0)
    result = PlanRunResult(
        plan_run_id="run_2026-08-17T12:00:00Z",
        ideas=[],
        candidates=[candidate],
        plans=[Plan("working-plan", 1, "eligible", [candidate], 1, 1000, 1, 99_000, portfolio, portfolio, operator_action="approve")],
        daily_plan_rows=[],
        metrics={"observed_at": "2026-08-17T12:00:00Z"},
        idea_diagnostics=[],
        rejection_summary=[],
    )

    class WorkingMarket:
        def chain_snapshot(self, underlying):  # noqa: ANN001
            return ChainSnapshot(
                chain_snapshot_id="working-chain",
                underlying=underlying,
                captured_at="2026-08-17T12:05:00Z",
                underlying_price=100,
                quotes=[
                    OptionQuote("XYZ", "2026-10-16", "put", 90, 1.0, 1.1, -0.15, 0, 0, 0, 0.2, 100),
                    OptionQuote("XYZ", "2026-10-16", "call", 110, 1.0, 1.1, 0.15, 0, 0, 0, 0.2, 100),
                ],
                source="fixture",
            )

    from kamandal_v2.strategy_engine import planning

    monkeypatch.setattr(planning, "run_plan", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(planning, "_market_provider", lambda *_args, **_kwargs: WorkingMarket())

    first = run_unified_books(
        load_control(), universe_rows=universe, playbook_rows=shadow_rows,
        idea_paths=[], store=store, audit_root=tmp_path / "audit",
    )
    assert first.shadow.handoffs[0]["adapter_state"] == "working"
    typed = CsaStore(database, read_only=True)
    assert typed.open_lifecycles()[0].status == "proposed"
    frozen_policy_hash = typed.working_shadow_orders()[0][0].policy_hash
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM shadow_fills").fetchone()[0] == 0

    # A later Sheet edit recompiles the playbook, but the already-working ticket
    # must continue with the policy and price path frozen when it was created.
    shadow_rows[0]["exit_dte_min"] = "20"
    empty_result = PlanRunResult(
        plan_run_id="run_2026-08-17T12:05:00Z",
        ideas=[], candidates=[], plans=[], daily_plan_rows=[],
        metrics={"observed_at": "2026-08-17T12:05:00Z"},
        idea_diagnostics=[], rejection_summary=[],
    )
    monkeypatch.setattr(planning, "run_plan", lambda *_args, **_kwargs: empty_result)
    second = run_unified_books(
        load_control(), universe_rows=universe, playbook_rows=shadow_rows,
        idea_paths=[], store=store, audit_root=tmp_path / "audit-2",
    )
    assert second.shadow.errors == ()
    assert CsaStore(database, read_only=True).open_lifecycles()[0].status == "open"
    assert CsaStore(database, read_only=True).open_lifecycles()[0].policy_hash == frozen_policy_hash
    assert store.open_shadow_candidate_ids() == {"working-strangle"}
    assert store.open_shadow_idea_ids() == {"market_scan:XYZ"}
    typed_portfolio = store.shadow_portfolio_state(portfolio)
    assert typed_portfolio.bpr_used == 1000
    assert typed_portfolio.greeks.delta == 1
    assert typed_portfolio.greeks.theta == 2


def test_working_shadow_entry_retires_when_playbook_leaves_shadow(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    universe, playbooks = _rows()
    shadow_rows = [row for row in playbooks if row["playbook_id"] == "short_strangle_shadow"]
    store = _migrated_store(tmp_path)
    database = store.sqlite_path
    candidate = Candidate(
        candidate_id="retired-working-strangle",
        idea_id="market_scan:XYZ",
        underlying="XYZ",
        playbook_id="short_strangle_shadow",
        structure="short_strangle",
        legs=[
            OptionLeg("short_put", "sell", "put", 90, "2026-10-16", 1, 1.05, 1.0, 1.1, -0.15, 0, 0, 0, 100),
            OptionLeg("short_call", "sell", "call", 110, "2026-10-16", 1, 1.05, 1.0, 1.1, 0.15, 0, 0, 0, 100),
        ],
        net_credit=2.05,
        estimated_bpr=1000,
        greeks=Greeks(),
        liquidity_score=1,
        score=1,
    )
    portfolio = PortfolioState(100_000, 100_000, 0, 0)
    working_result = PlanRunResult(
        plan_run_id="run_2026-08-17T12:00:00Z",
        ideas=[], candidates=[candidate],
        plans=[Plan("retire-working-plan", 1, "eligible", [candidate], 1, 1000, 1, 99_000, portfolio, portfolio, operator_action="approve")],
        daily_plan_rows=[], metrics={"observed_at": "2026-08-17T12:00:00Z"},
        idea_diagnostics=[], rejection_summary=[],
    )

    class WorkingMarket:
        def chain_snapshot(self, underlying):  # noqa: ANN001
            return ChainSnapshot(
                chain_snapshot_id="retire-working-chain",
                underlying=underlying,
                captured_at="2026-08-17T12:00:00Z",
                underlying_price=100,
                quotes=[
                    OptionQuote("XYZ", "2026-10-16", "put", 90, 1.0, 1.1, -0.15, 0, 0, 0, 0.2, 100),
                    OptionQuote("XYZ", "2026-10-16", "call", 110, 1.0, 1.1, 0.15, 0, 0, 0, 0.2, 100),
                ],
                source="fixture",
            )

    from kamandal_v2.strategy_engine import planning

    monkeypatch.setattr(planning, "run_plan", lambda *_args, **_kwargs: working_result)
    monkeypatch.setattr(planning, "_market_provider", lambda *_args, **_kwargs: WorkingMarket())
    first = run_unified_books(
        load_control(), universe_rows=universe, playbook_rows=shadow_rows,
        idea_paths=[], store=store, audit_root=tmp_path / "audit",
    )
    assert first.shadow.handoffs[0]["adapter_state"] == "working"

    shadow_rows[0]["mode"] = "live"
    second = run_unified_books(
        load_control(), universe_rows=universe, playbook_rows=shadow_rows,
        idea_paths=[], store=store, audit_root=tmp_path / "audit-2",
    )

    assert second.shadow.errors == ()
    typed = CsaStore(database, read_only=True)
    assert typed.working_shadow_orders() == []
    assert typed.rows("csa_shadow_order_intents")[0]["status"] == "missed"
    lifecycle = typed.rows("csa_lifecycles")[0]
    assert lifecycle["status"] == "entry_missed"
    fill = json.loads(typed.rows("csa_shadow_fills")[-1]["payload"])
    assert fill["quote_evidence"]["blocking"]["reason"] == "playbook_no_longer_routed_to_shadow"


def test_selected_live_plan_persists_guarded_intent_and_live_advisory_projection(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    database = tmp_path / "kamandal.db"
    store = LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    live_row = {
        "playbook_id": "live_call_spread",
        "enabled": "TRUE",
        "strategy_family": "call_spread",
        "structure": "call_spread",
        "mode": "live",
        "source_mode": "idea",
        "dte_min": "30",
        "dte_max": "45",
        "short_delta_min": "0.20",
        "short_delta_max": "0.30",
        "spread_width": "5",
        "max_bid_ask_pct": "0.25",
        "profit_target_pct": "50",
        "resting_profit_enabled": "FALSE",
        "resting_profit_arm_progress_pct": "25",
        "max_loss_multiple": "2",
        "exit_dte_min": "21",
        "half_time_exit": "TRUE",
        "avoid_earnings": "TRUE",
        "management_policy_json": "{}",
    }
    candidate = Candidate(
        candidate_id="selected-live",
        idea_id="idea-live",
        underlying="XYZ",
        playbook_id="live_call_spread",
        structure="call_spread",
        legs=[
            OptionLeg("long_call", "buy", "call", 100, "2026-10-16", 1, 2.05, 2.0, 2.1, 0.5, 0, 0, 0, 100),
            OptionLeg("short_call", "sell", "call", 105, "2026-10-16", 1, 0.85, 0.8, 0.9, 0.2, 0, 0, 0, 100),
        ],
        net_credit=1.2,
        estimated_bpr=500,
        greeks=Greeks(),
        liquidity_score=1,
        score=1,
    )
    portfolio = PortfolioState(100_000, 100_000, 0, 0)
    result = PlanRunResult(
        # Real planner identifiers are compact.  Snapshot identity must not be
        # inferred by slicing this value.
        plan_run_id="run_20260815T120000Z",
        ideas=[], candidates=[candidate],
        plans=[Plan("live-plan", 1, "eligible", [candidate], 1, 500, 0.5, 99_500, portfolio, portfolio)],
        daily_plan_rows=[], metrics={}, idea_diagnostics=[], rejection_summary=[],
    )
    from kamandal_v2.strategy_engine import planning

    monkeypatch.setattr(planning, "run_plan", lambda *_args, **_kwargs: result)
    control = load_control()
    control.setdefault("live", {})["entry_approval_mode"] = "auto_top_plan"
    tables = {
        "universe": [{"symbol": "XYZ", "enabled": "TRUE", "profile": "large_cap"}],
        "playbooks": [live_row],
    }
    monkeypatch.setenv("KAMANDAL_STRATEGY_POLICY_SNAPSHOT_DIR", str(tmp_path / "policy"))
    snapshot = capture_daily_policy_snapshot(
        control,
        trading_date=current_trading_date(control),
        tables=tables,
        captured_at="2026-08-15T12:00:00Z",
    )
    store.save_live_order_intent(
        {
            "ticket_hash": "historical-repriced-ticket",
            "order_id": "historical-repriced-order",
            "intent_type": "open",
            "plan_id": "live-plan",
            "candidate_id": "selected-live",
            "underlying": "XYZ",
            "created_at": "2026-08-14T14:00:00Z",
        },
        status="repriced",
    )
    unified = run_unified_books(
        control,
        universe_rows=snapshot.tables["universe"],
        playbook_rows=snapshot.tables["playbooks"],
        idea_paths=[],
        store=store,
        audit_root=tmp_path / "audit",
        daily_policy_snapshot=snapshot,
    )

    assert unified.live.errors == ()
    live_intent = store.live_order_intents_by_status({"pending_approval"})[0]
    assert live_intent["intent_type"] == "open"
    assert live_intent["csa_lifecycle_id"]
    assert live_intent["csa_policy_snapshot_date"] == snapshot.trading_date
    assert live_intent["csa_policy_snapshot_hash"] == snapshot.snapshot_hash
    assert live_intent["csa_compiled_policy_hash"] == live_intent["csa_policy_hash"]
    assert CsaStore(database, read_only=True).lifecycle(live_intent["csa_lifecycle_id"]).status == "pending_live_submission"
    detail = json.loads(dict(zip(seed_headers()["daily_plan"], unified.live.result.daily_plan_rows[0], strict=False))["plan_detail_json"])
    assert detail["lane"] == "live_advisory"
    assert detail["order_ticket_json"]["ticket_hash"]

    # The executor uses the persisted snapshot and its real authorization
    # function; only the broker construction is inert for this dry run.
    monkeypatch.setattr("kamandal_v2.live.execution.pull_sheet_tables", lambda _config: {"daily_plan": []})
    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", lambda _config: object())
    store.update_live_order_intent_status(live_intent["ticket_hash"], "stage_approved_pending_submit")
    executed = execute_live_approved(control, submit=False, store=store)

    assert executed["source"] == "stage_authorized_ledger"
    assert executed["results"][0]["status"] == "dry_run"


def test_active_unified_path_replans_once_into_typed_plan_two(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    database = tmp_path / "unified-fallback.db"
    store = LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    live_row = {
        "playbook_id": "live_call_spread",
        "enabled": "TRUE",
        "strategy_family": "call_spread",
        "structure": "call_spread",
        "mode": "live",
        "source_mode": "idea",
        "dte_min": "30",
        "dte_max": "45",
        "short_delta_min": "0.20",
        "short_delta_max": "0.30",
        "spread_width": "5",
        "max_bid_ask_pct": "0.25",
        "profit_target_pct": "50",
        "resting_profit_enabled": "FALSE",
        "resting_profit_arm_progress_pct": "25",
        "max_loss_multiple": "2",
        "exit_dte_min": "21",
        "half_time_exit": "TRUE",
        "avoid_earnings": "TRUE",
        "management_policy_json": "{}",
    }
    tables = {
        "universe": [{"symbol": "XYZ", "enabled": "TRUE", "profile": "large_cap"}],
        "playbooks": [live_row],
    }
    control = load_control()
    control.setdefault("live", {}).update(
        {
            "entry_approval_mode": "auto_top_plan",
            "plan_fallback": {"enabled": True, "max_attempts": 2, "auto_submit": False},
        }
    )
    monkeypatch.setenv("KAMANDAL_STRATEGY_POLICY_SNAPSHOT_DIR", str(tmp_path / "policy"))
    snapshot = capture_daily_policy_snapshot(
        control,
        trading_date=current_trading_date(control),
        tables=tables,
        captured_at="2026-08-21T14:00:00Z",
    )
    portfolio = PortfolioState(100_000, 100_000, 0, 0, greeks=Greeks())

    def candidate(candidate_id: str) -> Candidate:
        return Candidate(
            candidate_id=candidate_id,
            idea_id=f"idea-{candidate_id}",
            underlying="XYZ",
            playbook_id="live_call_spread",
            structure="call_spread",
            legs=[
                OptionLeg("short_call", "sell", "call", 100, "2026-10-16", 1, 2.0, 1.95, 2.05, 0.25, 0, 0, 0, 100),
                OptionLeg("long_call", "buy", "call", 105, "2026-10-16", 1, 1.0, 0.95, 1.05, 0.15, 0, 0, 0, 100),
            ],
            net_credit=1.0,
            estimated_bpr=400,
            greeks=Greeks(),
            liquidity_score=1.0,
            score=1.0,
            preflight=PreflightResult(True, 400, "fixture", {"request": {"limitPrice": "-1.00"}}),
        )

    first = PlanRunResult(
        plan_run_id="run-unified-rank-one",
        ideas=[],
        candidates=[candidate("rank-one-candidate")],
        plans=[Plan("unified-rank-one", 1, "eligible", [candidate("rank-one-candidate")], 1, 400, 0.4, 99_600, portfolio, portfolio)],
        daily_plan_rows=[], metrics={}, idea_diagnostics=[], rejection_summary=[],
    )
    second = PlanRunResult(
        plan_run_id="run-unified-rank-two",
        ideas=[],
        candidates=[candidate("rank-two-candidate")],
        plans=[Plan("unified-rank-two", 1, "eligible", [candidate("rank-two-candidate")], 1, 400, 0.4, 99_600, portfolio, portfolio)],
        daily_plan_rows=[], metrics={}, idea_diagnostics=[], rejection_summary=[],
    )
    from kamandal_v2.strategy_engine import planning

    results = iter([first, second])
    monkeypatch.setattr(planning, "run_plan", lambda *_args, **_kwargs: next(results))
    initial = run_unified_books(
        control,
        universe_rows=tables["universe"],
        playbook_rows=tables["playbooks"],
        idea_paths=["fixture.yaml"],
        provider="fixture",
        store=store,
        audit_root=tmp_path / "audit",
        daily_policy_snapshot=snapshot,
    )
    assert initial.live.errors == ()
    root = store.live_order_intents_by_type("open")[0]
    campaign_id = f"unified:{snapshot.trading_date}:unified-rank-one"
    state = store.latest_event(attempt_event_type(campaign_id))
    assert state is not None
    assert state["config_source"] == "unified-plan"
    assert state["daily_policy_snapshot"]["hash"] == snapshot.snapshot_hash
    assert root["stage_authorized"] is True
    assert root["csa_lifecycle_id"]

    store.update_live_order_intent_status(root["ticket_hash"], "cancelled")
    monkeypatch.setattr("kamandal_v2.live.execution.submission_window", lambda *_args, **_kwargs: {"allowed": True})
    decisions = _advance_plan_fallbacks(control, store)

    assert decisions[0]["status"] == "fallback_ready"
    child_state = store.latest_event(attempt_event_type(campaign_id))
    assert child_state["plan_id"] == "unified-rank-two"
    child = store.live_order_intent(child_state["ticket_hashes"][0])
    assert child["csa_lifecycle_id"]
    assert child["csa_compiled_policy_hash"] == child["csa_policy_hash"]
    assert child["csa_policy_snapshot_hash"] == snapshot.snapshot_hash
    assert child["csa_policy_snapshot_date"] == snapshot.trading_date
    assert child["stage_authorized"] is True
