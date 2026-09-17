from datetime import date
from pathlib import Path
from dataclasses import replace
import subprocess

import pytest

from kamandal_v2.domain.models import Idea, Playbook, PortfolioState, UniverseEntry
from kamandal_v2.sources.my_ideas import convert_rows, import_my_ideas
from kamandal_v2.strategy_engine.policy import compile_playbook_policy, ExecutionMode
from kamandal_v2.strategy_engine.planning import _source_groups
from tests.test_unified_policy import _strangle_row
from tests.test_my_ideas import FakeClient


def manual_row(**changes):
    return {"date": date.today().isoformat(), "ticker": "MS", "type_of_trade": "Strangle",
            "direction": "Neutral", "horizon_days": "45", **changes}


def test_manual_strangle_is_explicit_neutral_and_structure_bound():
    ideas, statuses = convert_rows([manual_row()], universe_symbols={"MS"}, today=date.today())
    assert statuses[0]["status"] == "imported"
    idea = ideas[0]
    assert idea["allowed_structures"] == ["short_strangle"]
    assert idea["strategy_hint"] == "short_strangle"
    assert "range_bound" in idea["thesis_tags"]
    assert idea["idea_id"].endswith("_strangle")
    for changes in ({"direction": "bull"}, {"status": "cancelled"}, {"date": "2020-01-01"}):
        assert not convert_rows([manual_row(**changes)], universe_symbols={"MS"}, today=date.today())[0]


def test_manual_source_only_runs_for_requested_ticker_and_enabled_input():
    rows, _ = convert_rows([manual_row(), manual_row(ticker="NTAP")], universe_symbols={"MS", "NTAP"}, today=date.today())
    ideas = [Idea.from_dict(row) for row in rows]
    ideas.append(Idea("guru", "correspondent:mike_butler", "MS", "neutral", strategy_hint="short_strangle"))
    policy = compile_playbook_policy(_strangle_row(mode="live", accepted_inputs="market_scan,operator_idea", execution_venue="tasty_primary"))
    args = dict(policies=(policy,), portfolio=PortfolioState(100000, 100000, 0, 0), mode=ExecutionMode.LIVE, trade_source_policies=None)
    universe = [UniverseEntry("MS", True), UniverseEntry("NTAP", True)]
    groups = _source_groups(ideas, universe, [Playbook.from_row(policy.fields)], **args, manual_strangle_symbol="MS")
    assert len(groups) == 1
    assert [idea.underlying for idea in groups[0].ideas] == ["MS"]
    assert groups[0].ideas[0].source == "operator_sheet"
    automatic = _source_groups(ideas, universe, [Playbook.from_row(policy.fields)], **args)
    assert all(idea.source != "operator_sheet" for group in automatic for idea in group.ideas)
    assert any(group.source_name == "market_scan" for group in automatic)
    disabled = replace(policy, accepted_inputs=("market_scan",))
    args["policies"] = (disabled,)
    assert not _source_groups(ideas, universe, [Playbook.from_row(policy.fields)], **args, manual_strangle_symbol="MS")


def test_withdrawn_manual_row_clears_cached_import(tmp_path):
    client = FakeClient([manual_row()], [{"symbol": "MS", "enabled": "TRUE"}])
    result = import_my_ideas({}, client=client, ideas_dir=tmp_path, write_sheet=False)
    path = Path(result["ideas_path"])
    assert "short_strangle" in path.read_text()
    client.tabs["my_ideas"] = []
    import_my_ideas({}, client=client, ideas_dir=tmp_path, write_sheet=False)
    assert "ideas: []" in path.read_text()


@pytest.mark.parametrize("plan_exit", [0, 1])
def test_manual_entry_script_never_executes_after_failed_plan(tmp_path, plan_exit):
    script = tmp_path / "run_manual_strangle.sh"
    script.write_text(Path("scripts/run_manual_strangle.sh").read_text())
    fake = tmp_path / "planner"
    fake.write_text(f"#!/bin/bash\nexit {plan_exit}\n")
    fake.chmod(0o755)
    (tmp_path / "common.sh").write_text(f'KAMANDAL_BIN="{fake}"\nrequire_trading_day() {{ :; }}\nrequire_market_window() {{ :; }}\nwith_lock() {{ shift; "$@"; }}\n')
    executor = tmp_path / "run_live_approved_orders.sh"
    marker = tmp_path / "executed"
    executor.write_text(f'#!/bin/bash\ntouch "{marker}"\n')
    executor.chmod(0o755)
    result = subprocess.run(["bash", str(script), "MS"], capture_output=True)
    assert (result.returncode == 0) == (plan_exit == 0)
    assert marker.exists() == (plan_exit == 0)


def test_manual_sheet_row_reaches_selected_typed_ticket_with_reserved_cap(tmp_path, monkeypatch):
    import yaml
    from kamandal_v2.config import load_control
    from kamandal_v2.strategy_engine import planning
    from kamandal_v2.live.execution import _fresh_entry_preflight_blocker
    from kamandal_v2.domain.models import PreflightResult
    from tests.test_unified_planning import _daily_snapshot, _migrated_store
    from tests.test_exact_strangle_routing import Market

    control = load_control()
    control["runtime"]["manual_strangle_symbol"] = "MS"
    control["risk_manager"]["enabled"] = False
    row = _strangle_row(mode="live", csa_stage="pilot_live", accepted_inputs="market_scan,operator_idea",
                        execution_venue="tasty_primary", live_max_bpr_per_order=2500, leg_count=2,
                        dte_min=20, dte_max=50, iv_rank_min=50, iv_rank_max=100)
    universe = [{"symbol": "MS", "enabled": "TRUE"}, {"symbol": "NTAP", "enabled": "TRUE"}]
    snapshot = _daily_snapshot(tmp_path, control, universe, [row])
    ideas, _ = convert_rows([manual_row(), manual_row(ticker="NTAP")], universe_symbols={"MS", "NTAP"}, today=date.today())
    path = tmp_path / "ideas.yaml"
    path.write_text(yaml.safe_dump({"ideas": ideas}))
    market = Market()
    market.bpr = 2457.52
    market.account_state = lambda: PortfolioState(100000, 100000, 0, 0)
    monkeypatch.setattr(planning, "_market_provider", lambda *_args, **_kwargs: market)
    monkeypatch.setattr(planning, "VenueAwareMarket", lambda inner, *_args, **_kwargs: inner)
    store = _migrated_store(tmp_path)
    result = planning.run_unified_books(control, universe_rows=universe, playbook_rows=[row],
                idea_paths=[path], provider="public", store=store, audit_root=tmp_path / "audit",
                daily_policy_snapshot=snapshot, include_shadow=False)
    assert result.live.errors == ()
    assert len(result.live.handoffs) == 1
    ticket, = store.live_order_intents_by_type("open")
    assert ticket["underlying"] == "MS"
    assert ticket["structure"] == "short_strangle"
    assert ticket["entry_risk_budget"] == 2500
    assert result.live.result.plans[0].total_bpr == 2500
    assert result.live.result.candidates[0].metadata["broker_bpr_estimate"] == 2457.52
    assert _fresh_entry_preflight_blocker(ticket, PreflightResult(True, 2493.58, "fresh", {"broker_bpr_provided": True})) == ""
    assert _fresh_entry_preflight_blocker(ticket, PreflightResult(True, 2501, "fresh", {"broker_bpr_provided": True})) == "fresh_preflight_exceeds_approved_risk_budget"
