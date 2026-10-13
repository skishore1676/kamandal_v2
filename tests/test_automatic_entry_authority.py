"""One automatic entry queue: admission recovery, reservations and dispatch."""
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from kamandal_v2.config import load_control
from kamandal_v2.domain.models import PortfolioState, PreflightResult
from kamandal_v2.live import execution
from kamandal_v2.planner.plan_generator import generate_plans
from kamandal_v2.portfolio_sleeves import SleevePolicy, live_sleeve_usage, occupied_source_opportunities
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine import planning
from kamandal_v2.strategy_lanes.daily_policy import DailyPolicySnapshot, policy_tables_hash
from kamandal_v2.strategy_lanes.operator_policy import OperatorPolicyBundle
from test_observed_package_planning import _observed_calendar_row, _batch, _Market, _migrated_store
from test_plan_objective import _control, _candidate, _portfolio


def entry(key, *, plan=None, authority=None, budget=500):
    return {"ticket_hash":key,"order_id":key,"candidate_id":key,"plan_id":plan or key,"plan_rank":1,
            "intent_type":"open","underlying":key.upper(),"sleeve_id":"current_idea","entry_risk_budget":budget,
            "stage_authorized":True,"csa_lifecycle_id":f"life-{key}","entry_authority":authority}


def test_unchanged_guru_evidence_recovers_preflight_without_duplicate_ticket(tmp_path, monkeypatch):
    now="2026-10-12T19:30:00Z"; near="2026-10-16"; far="2026-11-20"
    row=_observed_calendar_row()
    row.update(playbook_id="guru_exact_call_calendar",mode="live",csa_stage="live",source_mode="idea",accepted_inputs="exact_package",
               dte_min=1,dte_max=90,long_dte_min=2,long_dte_max=180,max_contracts=1,sizing_method="fixed_contracts",sizing_value=1,
               exit_dte_min=0,half_time_exit="FALSE",avoid_earnings="FALSE",live_max_bpr_per_order=1200)
    sources=[{"source_id":s,"output_kind":k,"mode":"live" if k=="exact_package" else "off","live_structures":"call_calendar" if k=="exact_package" else ""}
             for s in ["greg_harmon","mike_butler"] for k in ["idea","exact_package"]]
    tables={"universe":[],"playbooks":[row],"trade_sources":sources}
    daily=DailyPolicySnapshot(now[:10],now,policy_tables_hash(tables),tables,tmp_path/"policy.json",OperatorPolicyBundle((),(),(),now,source="fixture"))
    config=load_control();config["portfolio"]["sleeves_source"]="";config["risk_manager"]["enabled"]=False;config["runtime"]["observed_at"]=now
    class Market(_Market):
        def chain_snapshot(self,symbol):
            chain=super().chain_snapshot(symbol)
            return replace(chain,quotes=[replace(chain.quotes[0],expiration=near),replace(chain.quotes[1],expiration=far)])
    market=Market(captured_at=now);market.account_state=lambda:PortfolioState(100000,100000,0,0)
    monkeypatch.setattr(planning,"_market_provider",lambda *a,**kw:market)
    package=_batch().packages[0]
    package=replace(package,legs=(replace(package.legs[0],expiration=near),replace(package.legs[1],expiration=far)),
                    source_published_at="2026-10-12T18:30:00Z",source_valid_until="2026-10-12T21:00:00Z",
                    source_verified=True,source_verification_ref="fixture-bound")
    store=_migrated_store(tmp_path)
    args=dict(universe_rows=[],playbook_rows=[row],idea_paths=[],provider="fixture",store=store,audit_root=tmp_path/"audit",
              daily_policy_snapshot=daily,trade_source_rows=sources,guru_only=True,include_shadow=False,
              observed_package_batches=(replace(_batch(),packages=(package,)),))
    market.preflight=lambda _:PreflightResult(False,500,"temporary preview failure",{"broker_bpr_provided":True})
    first=planning.run_unified_books(config,**args)
    assert not first.live.errors and not first.live.handoffs
    market.preflight=lambda _:PreflightResult(True,500,"recovered preview",{"broker_bpr_provided":True,"response":{"buyingPowerRequirement":500}})
    second=planning.run_unified_books(config,**args)
    assert not second.live.errors and len(second.live.handoffs)==1
    ticket,=store.live_order_intents_by_type("open")
    assert ticket["entry_authority"]=="guru_intake" and ticket["_ledger_status"]=="stage_approved_pending_submit"
    assert ticket["source_valid_until"]==package.source_valid_until
    third=planning.run_unified_books(config,**args)
    assert not third.live.handoffs and len(store.live_order_intents_by_type("open"))==1


@pytest.mark.parametrize("empty",[False,True])
def test_replacement_selection_releases_old_capacity_atomically_even_when_empty(tmp_path, empty):
    store=LocalStore(tmp_path/"queue.db")
    store.save_live_order_intent(entry("old",budget=4000))
    account=_portfolio();config=_control();config["_live_sleeve_policy"]=SleevePolicy(40,40,80)
    candidate=_candidate("fresh",underlying="NEW",structure="put_spread",bpr=500,delta=0,gamma=0,theta=1,vega=0,net_credit=1)
    config["_live_sleeve_usage"]=live_sleeve_usage(store,account)
    assert generate_plans([candidate],account,config)==[]
    replace_hashes=store.supersedable_automatic_entries()
    config["_live_sleeve_usage"]=live_sleeve_usage(store,account,exclude_ticket_hashes=replace_hashes)
    assert len(generate_plans([candidate],account,config))==1
    assert store.live_order_intent("old")["_ledger_status"]=="pending_approval"  # calculation did not release it
    if not empty:store.save_live_order_intent(entry("new"))
    selection={"trading_date":"2026-10-12","plan_id":"" if empty else "new","ticket_hashes":[] if empty else ["new"]}
    assert store.commit_automatic_selection(selection,replace_hashes=replace_hashes)==["old"]
    assert store.live_order_intent("old")["_ledger_status"]=="retired_superseded_entry_revision"
    assert store.latest_event("live_current_selection")["authority"]=="automatic_entry_ledger"
    assert live_sleeve_usage(store,account).pending_bpr==(0 if empty else 500)


@pytest.mark.parametrize("protection",["submitted","submit_uncertain","partial","broker_status","broker_identity","independent_guru","replacement"])
def test_replacement_never_excludes_real_or_independent_commitments(tmp_path, protection):
    store=LocalStore(tmp_path/"safe.db");t=entry("old",authority="automatic_plan")
    status="stage_approved_pending_submit"
    if protection in {"submitted","submit_uncertain"}:status=protection
    if protection=="partial":status="partially_filled"
    if protection=="broker_identity":t["broker_order_id"]="owned"
    if protection=="independent_guru":t["guru_intake_staged"]=True
    if protection=="replacement":t["parent_ticket_hash"]="parent"
    store.save_live_order_intent(t,status=status)
    if protection=="broker_status":store.record_live_order_status("old","WORKING",{},ticket_hash="old")
    assert store.supersedable_automatic_entries()==set()


def test_broker_claim_during_planning_aborts_selection_without_releasing_risk(tmp_path):
    store=LocalStore(tmp_path/"race.db");store.save_live_order_intent(entry("old"));replace_hashes=store.supersedable_automatic_entries()
    store.save_live_order_intent(entry("new"))
    assert store.transition_live_order_intent_status("old",expected_statuses={"pending_approval"},status="submit_uncertain")
    with pytest.raises(RuntimeError,match="predecessor changed"):
        store.commit_automatic_selection({"trading_date":"2026-10-12","plan_id":"new","ticket_hashes":["new"]},replace_hashes=replace_hashes)
    assert store.live_order_intent("old")["_ledger_status"]=="submit_uncertain"
    assert store.live_order_intent("new")["_ledger_status"]=="pending_approval"
    assert store.latest_event("live_current_selection") is None


def test_filled_source_cannot_be_excluded_by_replacement_read(tmp_path):
    store=LocalStore(tmp_path/"filled.db");t={**entry("filled"),"source_id":"greg_harmon","source_opportunity_id":"source-opening"}
    store.save_live_order_intent(t,status="filled")
    assert ("greg_harmon","source-opening") in occupied_source_opportunities(store,exclude_ticket_hashes={"filled"})


def test_auto_queue_skips_local_hold_and_never_reads_cockpit(tmp_path,monkeypatch):
    store=LocalStore(tmp_path/"dispatch.db")
    for key,venue in [("first","tasty_primary"),("second","public_primary")]:
        store.save_live_order_intent({**entry(key,authority="guru_intake"),"execution_venue":venue,"created_at":key},status="stage_approved_pending_submit")
    monkeypatch.setattr(execution,"pull_sheet_tables",lambda _:pytest.fail("cockpit cannot be automatic authority"))
    monkeypatch.setattr(execution,"_daily_basket_cap_allows",lambda *a:True)
    seen=[]
    def evaluate(c,s,t,**kw):
        seen.append(t["ticket_hash"])
        return {"results":[{"status":"blocked" if t["ticket_hash"]=="first" else "submitted"}],"health_gate":{"risk_manager":{"blocked":False}}}
    monkeypatch.setattr(execution,"_execute_stage_entry",evaluate)
    result=execution.execute_live_approved({"live":{"entry_approval_mode":"auto_top_plan","max_live_entry_submits_per_run":1}},store=store,submit=True)
    assert seen==["first","second"] and result["submitted"]==1


def test_policy_binding_cannot_revive_a_claimed_ticket(tmp_path):
    store=LocalStore(tmp_path/"bind.db");t=entry("opening")
    store.save_live_order_intent(t,status="submit_uncertain")
    assert not store.bind_live_entry_policy(t,expected_statuses={"pending_approval","stage_approved_pending_submit"})
    assert store.live_order_intent("opening")["_ledger_status"]=="submit_uncertain"


def test_final_submit_claim_refuses_authority_retired_during_preflight(tmp_path,monkeypatch):
    from test_live_lane import _ticket_candidate,_live_control
    from kamandal_v2.live.orders import build_open_ticket
    store=LocalStore(tmp_path/"claim.db")
    ticket=build_open_ticket(type("Plan",(),{"plan_id":"plan","plan_rank":1})(),_ticket_candidate("c","idea","MSFT"))
    store.save_live_order_intent(ticket,status="stage_approved_pending_submit")
    monkeypatch.setattr(execution,"submission_window",lambda *a,**kw:{"allowed":True})
    monkeypatch.setattr(execution,"_ticket_fresh",lambda *a:True)
    monkeypatch.setattr(execution,"_assert_submit_allowed",lambda *a,**kw:None)
    def preflight(a,t):
        store.update_live_order_intent_status(t["ticket_hash"],"retired_superseded_entry_revision")
        return PreflightResult(True,500,"ok",{})
    monkeypatch.setattr(execution,"_preflight_ticket_with_entry_risk",preflight)
    broker=type("Broker",(),{"place_order_ticket":lambda *a:pytest.fail("retired authority cannot POST")})()
    config=_live_control();config["portfolio"]["sleeves_source"]=""
    result=execution._execute_ticket(config,broker,store,ticket,submit=True,close=False)
    assert result["reason"]=="ticket_authority_changed_before_submission"


def test_failed_source_still_evaluates_valid_sibling_and_job_stays_failed(tmp_path):
    binary=tmp_path/"mock-kamandal";calls=tmp_path/"calls"
    binary.write_text('''#!/bin/bash
echo "$1" >> "$FAKE_CALLS"
case "$1" in
activate-correspondent-signals) echo '{"status":"degraded","observed_package_batch_count":1}'; exit 1 ;;
unified-plan) echo '{"ok":true}'; exit 0 ;;
*) exit 9 ;;
esac
''');binary.chmod(0o755)
    env={**os.environ,"KAMANDAL_BIN":str(binary),"KAMANDAL_FORCE_RUN":"1","KAMANDAL_RUNLOCK_ROOT":str(tmp_path/"locks"),
         "FAKE_CALLS":str(calls),"KAMANDAL_X_SOURCE_DOC_DIR":str(tmp_path/"sources"),"KAMANDAL_X_BOOKMARK_DIGEST_DIR":str(tmp_path/"digest"),
         "KAMANDAL_ACTIVE_IDEAS_DIR":str(tmp_path/"ideas")}
    result=subprocess.run(["bash","scripts/run_x_bookmark_extraction.sh"],env=env,capture_output=True,text=True)
    assert result.returncode==1
    assert calls.read_text().splitlines()==["activate-correspondent-signals","unified-plan"]


def test_failed_prepared_handoff_keeps_prior_authority_and_reservation(tmp_path):
    store = LocalStore(tmp_path / 'handoff.db')
    old = entry('old')
    store.save_live_order_intent(old)
    store.commit_automatic_selection({'trading_date': '2026-10-12', 'plan_id': 'old', 'ticket_hashes': ['old']}, replace_hashes=set())
    replacement = entry('new')
    replacement.pop('csa_lifecycle_id')
    with pytest.raises(ValueError, match='frozen lifecycle handoff'):
        store.commit_automatic_selection({'trading_date': '2026-10-12', 'plan_id': 'new', 'ticket_hashes': ['new']},
            replace_hashes=store.supersedable_automatic_entries(), prepared_tickets=[replacement])
    assert store.live_order_intent('old')['_ledger_status'] == 'stage_approved_pending_submit'
    assert store.live_order_intent('new') is None
    assert store.latest_event('live_current_selection')['plan_id'] == 'old'


@pytest.mark.parametrize('global_block,limit,expected', [(True, 3, ['a']), (False, 2, ['a', 'b'])])
def test_auto_queue_preserves_global_breaker_and_submit_bound(tmp_path, monkeypatch, global_block, limit, expected):
    store = LocalStore(tmp_path / 'bound.db')
    for key in ['a', 'b', 'c']:
        store.save_live_order_intent({**entry(key, authority='automatic_plan'), 'created_at': key}, status='stage_approved_pending_submit')
    monkeypatch.setattr(execution, '_daily_basket_cap_allows', lambda *a: True)
    seen = []
    def evaluate(c, s, t, **kw):
        seen.append(t['ticket_hash'])
        return {'results': [{'status': 'blocked' if global_block else 'submitted'}],
                'health_gate': {'risk_manager': {'blocked': global_block}}}
    monkeypatch.setattr(execution, '_execute_stage_entry', evaluate)
    execution.execute_live_approved({'live': {'entry_approval_mode': 'auto_top_plan', 'max_live_entry_submits_per_run': limit}}, store=store, submit=True)
    assert seen == expected


def test_guru_preflight_latency_crossing_cutoff_prevents_post(tmp_path, monkeypatch):
    from test_live_lane import _ticket_candidate, _live_control
    from kamandal_v2.live.orders import build_open_ticket
    from kamandal_v2.live.option_sessions import submission_window
    store = LocalStore(tmp_path / 'cutoff.db')
    ticket = build_open_ticket(type('Plan', (), {'plan_id': 'plan', 'plan_rank': 1})(), _ticket_candidate('c', 'idea', 'MSFT'))
    ticket.update(source_output_kind='exact_package', sleeve_id='guru_exact')
    store.save_live_order_intent(ticket, status='stage_approved_pending_submit')
    clock = [datetime(2026, 10, 12, 19, 54, 59, tzinfo=UTC)]
    monkeypatch.setattr(execution, 'submission_window', lambda c, t, **kw: submission_window(c, t, now=clock[0], **kw))
    monkeypatch.setattr(execution, '_ticket_fresh', lambda *a: True)
    monkeypatch.setattr(execution, '_fresh_source_route_blocker', lambda *a: '')
    def preflight(*a):
        clock[0] = datetime(2026, 10, 12, 19, 55, 1, tzinfo=UTC)
        return PreflightResult(True, 500, 'ok', {})
    monkeypatch.setattr(execution, '_preflight_ticket_with_entry_risk', preflight)
    broker = type('Broker', (), {'place_order_ticket': lambda *a: pytest.fail('late Guru cannot POST')})()
    result = execution._execute_ticket(_live_control(), broker, store, ticket, submit=True, close=False)
    assert result['status'] == 'deferred_entry_cutoff'
    assert result['submission_window']['evaluated_at'].endswith('14:55:01-05:00')
