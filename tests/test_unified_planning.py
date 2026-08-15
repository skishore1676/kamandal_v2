from __future__ import annotations

import json

from kamandal_v2.config import load_control
from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg, Plan, PortfolioState
from kamandal_v2.planner.engine import PlanRunResult
from kamandal_v2.seed import build_seed_tables, seed_headers
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.strategy_lanes.store import CsaStore
from kamandal_v2.strategy_engine.planning import run_unified_books


def _rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    tables = build_seed_tables(load_control())
    headers = seed_headers()
    universe = [dict(zip(headers["universe"], [*row, *[""] * len(headers["universe"])])) for row in tables["universe"]]
    playbooks = [dict(zip(headers["playbooks"], [*row, *[""] * len(headers["playbooks"])])) for row in tables["playbooks"]]
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
            "exit_dte_min": "21",
            "management_policy_json": json.dumps(
                {"lifecycle": {"tested_side_confirmation": 2, "roll": {"min_credit": 0.1}, "fill": {"max_attempts": 2, "price_increment": 0.05}}}
            ),
        }
    )
    return universe, playbooks


def test_unified_books_keep_live_and_shadow_policy_ownership_isolated(tmp_path) -> None:
    universe, playbooks = _rows()
    control = load_control()
    result = run_unified_books(
        control,
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=LocalStore(tmp_path / "kamandal.db"),
        audit_root=tmp_path / "audit",
    )

    assert result.compilation.ok
    assert "short_strangle_shadow" not in result.live.policy_ids
    assert result.shadow.policy_ids == ("short_strangle_shadow",)
    assert result.live.errors == ()
    assert result.shadow.errors == ()
    assert result.live.result is not None
    assert result.shadow.result is not None


def test_one_book_failure_does_not_erase_other_book(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    universe, playbooks = _rows()
    from kamandal_v2.strategy_engine import planning

    original = planning.run_plan

    def fail_shadow(config, **kwargs):  # noqa: ANN001
        if config["runtime"]["mode"] == "shadow":
            raise RuntimeError("shadow fixture failure")
        return original(config, **kwargs)

    monkeypatch.setattr(planning, "run_plan", fail_shadow)
    result = run_unified_books(
        load_control(),
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=LocalStore(tmp_path / "kamandal.db"),
        audit_root=tmp_path / "audit",
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
            "management_policy_json": json.dumps({"lifecycle": {"portfolio_delta_trigger": -999, "hedge_underlyings": ["SPY"]}}),
        }
    )

    result = run_unified_books(
        load_control(),
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=LocalStore(tmp_path / "kamandal.db"),
        audit_root=tmp_path / "audit",
    )

    assert result.compilation.ok
    assert any(idea.source == "market_scan" for idea in result.shadow.result.ideas)
    assert any(idea.source == "portfolio_hedge" for idea in result.live.result.ideas)


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
    run_unified_books(
        load_control(),
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=LocalStore(tmp_path / "kamandal.db"),
        audit_root=tmp_path / "audit",
        write_sheet=True,
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


def test_selected_live_plan_persists_guarded_intent_and_live_advisory_projection(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    database = tmp_path / "kamandal.db"
    store = LocalStore(database)
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
        plan_run_id="run_2026-08-15T12:00:00Z",
        ideas=[], candidates=[candidate],
        plans=[Plan("live-plan", 1, "eligible", [candidate], 1, 500, 0.5, 99_500, portfolio, portfolio)],
        daily_plan_rows=[], metrics={}, idea_diagnostics=[], rejection_summary=[],
    )
    from kamandal_v2.strategy_engine import planning

    monkeypatch.setattr(planning, "run_plan", lambda *_args, **_kwargs: result)
    control = load_control()
    control.setdefault("live", {})["entry_approval_mode"] = "auto_top_plan"
    unified = run_unified_books(
        control,
        universe_rows=[{"symbol": "XYZ", "enabled": "TRUE", "profile": "large_cap"}],
        playbook_rows=[live_row], idea_paths=[], store=store, audit_root=tmp_path / "audit",
    )

    assert unified.live.errors == ()
    assert store.live_order_intents_by_status({"pending_approval"})[0]["intent_type"] == "open"
    detail = json.loads(dict(zip(seed_headers()["daily_plan"], unified.live.result.daily_plan_rows[0], strict=False))["plan_detail_json"])
    assert detail["lane"] == "live_advisory"
    assert detail["order_ticket_json"]["ticket_hash"]
