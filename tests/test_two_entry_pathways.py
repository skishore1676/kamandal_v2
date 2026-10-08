"""Acceptance checks for the approved entry boundary, with no broker effects."""
from copy import deepcopy
from dataclasses import replace

import pytest

from test_plan_objective import _candidate, _control, _portfolio
from test_source_episode_compiler import (
    FakeClient, _event, _monthly_spread_fixture, _packet, _profile, _record,
)
from kamandal_v2.domain.models import Greeks, PreflightResult
from kamandal_v2.intelligence.observed_packages import observed_package_batch_from_dict
from kamandal_v2.intelligence.source_contract_verification import bind_interpreted_source_contracts
from kamandal_v2.intelligence.source_episode_compiler import PROMPT_SCHEMA, compile_source_episode_packet
from kamandal_v2.intelligence.source_episode_projection import project_source_episode_compilation
from kamandal_v2.live.execution import _ticket_preserves_source_contracts
from kamandal_v2.planner.plan_generator import generate_plans
from kamandal_v2.portfolio_sleeves import SleevePolicy, SleeveUsage
from kamandal_v2.stores.sqlite import LocalStore


def test_vertical_sheet_migration_preserves_existing_policies_and_switches():
    import runpy
    from pathlib import Path
    from test_observed_package_planning import _observed_calendar_row
    from kamandal_v2.strategy_engine.policy import compile_playbook_policies

    propose = runpy.run_path(str(Path(__file__).parents[1] / "scripts/apply_two_entry_pathways_sheet.py"))["proposed_tables"]
    template = _observed_calendar_row()
    template.update(playbook_id="guru_exact_call_calendar", accepted_inputs="exact_package",
                    source_mode="idea", max_contracts="1", live_max_bpr_per_order="1200")
    tables = {"playbooks": [template, {}, {}], "universe": [{"symbol": "SPY"}],
              "trade_sources": [{"source_id": source, "output_kind": "exact_package", "mode": "off", "live_structures": "long_call"}
                                for source in ("mike_butler", "greg_harmon")],
              "portfolio_sleeves": [{"lane": "current_idea", "max_bpr_pct": "40"},
                                    {"lane": "guru_exact", "max_bpr_pct": "40"},
                                    {"lane": "portfolio_total", "max_bpr_pct": "80"}]}
    proposed = propose(tables)
    assert proposed["playbooks"][0] == template
    assert proposed["universe"] == tables["universe"]
    assert [row["max_bpr_pct"] for row in proposed["portfolio_sleeves"]] == ["40", "40", "80"]
    assert all(row["mode"] == "off" for row in proposed["trade_sources"])
    assert all(row["live_structures"] == "long_call,call_spread,put_spread" for row in proposed["trade_sources"])
    assert compile_playbook_policies(proposed["playbooks"]).ok
    assert propose(proposed) == proposed
    assert tables["playbooks"][1:] == [{}, {}]


def copy_candidate(identity, *, bpr=500, published="2026-10-01T13:00:00Z", delta=10):
    result = _candidate(identity, underlying=identity.upper(), structure="long_call", bpr=bpr,
                        delta=delta, gamma=.1, theta=-10, vega=5, net_credit=-bpr/100, score=-100)
    result.metadata.update(input_kind="exact_package", source_profile="mike_butler",
                           source_opportunity_id=identity, source_verified=True,
                           source_verification_ref="bound-source", source_published_at=published)
    result.preflight = PreflightResult(True, bpr, "broker accepted", {"broker_bpr_provided": True})
    return result


def sleeve_control():
    result = _control()
    result["_live_sleeve_policy"] = SleevePolicy(40, 40, 80)
    result["_live_sleeve_usage"] = SleeveUsage(10000, 0, 0, 0, 0, 0, 0, 0)
    result["live"] = {"max_new_positions_per_plan": 3}
    return result


def test_guru_admission_is_invariant_to_ideas_preferences():
    control = sleeve_control()
    strict = deepcopy(control)
    strict["portfolio"].update(max_positions=0, max_bpr_per_underlying_pct=0.1,
                               target_delta=-9999, delta_band=.01,
                               delta_guard={"enabled": True, "max_delta": -1000, "min_delta": -1001})
    strict["planner"] = {"min_marginal_score": 999999, "hard_new_bpr_pct": .01}
    for config in (control, strict):
        plans = generate_plans([copy_candidate("opening")], _portfolio(), config)
        assert [c.candidate_id for c in plans[0].candidates] == ["opening"]
        assert plans[0].candidates[0].metadata["entry_decision"]["status"] == "admitted"


def test_guru_fifo_skips_unaffordable_and_records_measured_limit():
    expensive = copy_candidate("first", bpr=4100)
    second = copy_candidate("second", bpr=2500, published="2026-10-01T13:01:00Z")
    third = copy_candidate("third", bpr=2000, published="2026-10-01T13:02:00Z")
    small = copy_candidate("fourth", bpr=500, published="2026-10-01T13:03:00Z")
    plan, = generate_plans([small, third, second, expensive], _portfolio(), sleeve_control())
    assert [c.candidate_id for c in plan.candidates] == ["second", "fourth"]
    assert expensive.rejection_reason == third.rejection_reason == "sleeve_bpr_cap:guru_exact"
    receipt = third.metadata["entry_decision"]
    assert receipt["candidate_bpr"] == 2000 and receipt["reserved_bpr"] == 2500
    assert receipt["sleeve_limit_bpr"] == 4000


@pytest.mark.parametrize("broken,reason", [("source", "source_verification_required"),
                                          ("preflight", "live_preflight_bpr_required")])
def test_fifo_never_relaxes_evidence_or_broker_gate(broken, reason):
    candidate = copy_candidate("opening")
    if broken == "source":
        candidate.metadata["source_verified"] = False
    else:
        candidate.preflight = None
    assert generate_plans([candidate], _portfolio(), sleeve_control()) == []
    assert candidate.rejection_reason == reason


@pytest.mark.parametrize("bpr", [0, -1, float("nan"), float("inf")])
def test_fifo_requires_positive_finite_risk(bpr):
    candidate = copy_candidate("invalid_risk", bpr=bpr)
    assert generate_plans([candidate], _portfolio(), sleeve_control()) == []
    assert candidate.rejection_reason == "invalid_candidate_bpr"


def test_guru_positions_do_not_change_ideas_delta_or_ranking(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "state.db")
    idea = _candidate("idea", underlying="IDEA", structure="short_put", bpr=500,
                      delta=.1, gamma=0, theta=.02, vega=0, net_credit=1)
    control = sleeve_control()
    control["portfolio"]["delta_guard"] = {"enabled": True, "min_delta": -.5, "max_delta": .5}
    outcomes = []
    for guru_delta in (-100, 100):
        groups = [{"sleeve_id": "guru_exact", "underlying": "GURU",
                   "candidate": {"estimated_bpr": 1000, "greeks": {"delta": guru_delta}}}]
        monkeypatch.setattr(store, "open_live_position_groups", lambda: groups)
        account = replace(_portfolio(), greeks=Greeks(delta=guru_delta), bpr_used=1000, positions_count=1)
        ideas = store.live_portfolio_state(account, sleeve="current_idea")
        assert ideas.greeks.delta == 0 and ideas.positions_count == 0
        result = generate_plans([deepcopy(idea)], account, control, idea_portfolio=ideas)
        outcomes.append((result[0].score, [c.candidate_id for c in result[0].candidates]))
        assert result[0].portfolio_before.greeks.delta == guru_delta
    assert outcomes[0] == outcomes[1]


def test_pending_commitments_and_global_cap_are_still_shared():
    control = sleeve_control()
    control["_live_sleeve_usage"] = SleeveUsage(10000, 4000, 3800, 7800, 7000, 7000, 800, 0)
    candidate = copy_candidate("opening", bpr=300)
    assert not generate_plans([candidate], _portfolio(), control)
    assert candidate.rejection_reason == "sleeve_bpr_cap:guru_exact"
    control["_live_sleeve_usage"] = SleeveUsage(10000, 7500, 0, 7500, 7500, 7500, 0, 0)
    candidate = copy_candidate("opening", bpr=600)
    assert not generate_plans([candidate], _portfolio(), control)
    assert candidate.rejection_reason == "sleeve_bpr_cap:portfolio_total"


def text_projection():
    record, packet, response = _monthly_spread_fixture()
    client = FakeClient(response)
    compilation = compile_source_episode_packet(packet, _profile("greg_harmon"), client)
    projection = project_source_episode_compilation(compilation, packet, _profile("greg_harmon"), universe_symbols=["AMAT"])
    assert len(client.calls) == 1
    return record, packet, projection


def test_complete_text_uses_one_interpretation_and_binds_original_source():
    record, packet, projection = text_projection()
    assert projection.planner_ideas == ()
    batches, failures = bind_interpreted_source_contracts(projection.observed_batches, packet)
    assert not failures and len(batches) == 1
    package, = batches[0].packages
    assert package.source_verified and package.evidence_basis == "text"
    assert package.source_verification_method == "single_pass_contract_validation"
    assert package.image_sha256 == "" and package.media_index == 0
    assert package.source_published_at == record["source"]["published_at"]
    assert observed_package_batch_from_dict(batches[0].to_dict()) == batches[0]
    edited = deepcopy(packet)
    edited["records"][0]["literal"]["text"] += " edited"
    changed, failures = bind_interpreted_source_contracts(batches, edited)
    assert failures[0]["reason"] == "source_text_hash_mismatch"
    assert not changed[0].packages[0].source_verified
    ticket = {"underlying": package.symbol, "legs": [leg.to_dict() for leg in package.legs]}
    assert _ticket_preserves_source_contracts(ticket, package)
    ticket["legs"][0]["strike"] = "511"
    assert not _ticket_preserves_source_contracts(ticket, package)


@pytest.mark.parametrize("action", ["open", "scale_in"])
def test_incomplete_confirmed_opening_never_falls_back_to_ideas(action):
    record = _record("incomplete", "Opened a bullish call spread on $LULU", ["LULU"])
    response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": record["signal_id"],
                "events": [_event(action=action, projections=["idea"], exact_packages=[])]}]}
    compilation = compile_source_episode_packet(_packet([record]), _profile("mike_butler"), FakeClient(response))
    projection = project_source_episode_compilation(compilation, _packet([record]), _profile("mike_butler"), universe_symbols=["LULU"])
    assert not projection.planner_ideas and not projection.observed_batches
    assert "confirmed_opening_owned_by_guru_exact" in projection.observations[0]["reason"]


def test_directional_commentary_still_becomes_an_idea():
    record = _record("commentary", "Bullish on $LULU; a call diagonal may express this view", ["LULU"])
    response = {"schema": PROMPT_SCHEMA, "episodes": [{"signal_id": record["signal_id"], "events": [
        _event(action="commentary", direction="bullish", structure_hint="call_diagonal", projections=["idea"])]}]}
    packet = _packet([record])
    compilation = compile_source_episode_packet(packet, _profile("mike_butler"), FakeClient(response))
    projection = project_source_episode_compilation(compilation, packet, _profile("mike_butler"), universe_symbols=["LULU"])
    assert len(projection.planner_ideas) == 1
    assert "source_intent:commentary" in projection.planner_ideas[0]["thesis_tags"]
