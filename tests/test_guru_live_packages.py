from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from test_observed_package_planning import _batch, _observed_calendar_row
from test_plan_objective import _candidate, _control, _portfolio
from kamandal_v2.domain.models import ChainSnapshot, OptionQuote, Playbook, PreflightResult
from kamandal_v2.intelligence.atomic_packages import combine_calendar_openings
from kamandal_v2.intelligence.observed_packages import ObservedLegEvidence
from kamandal_v2.intelligence.trade_sources import compile_trade_source_policies
from kamandal_v2.planner.observed_package_candidates import build_observed_package_candidates
from kamandal_v2.planner.plan_generator import generate_plans
from kamandal_v2.portfolio_sleeves import occupied_source_opportunities
from kamandal_v2.live.execution import _fresh_exact_evidence_blocker
from kamandal_v2.stores.sqlite import LocalStore


def calendars():
    base = replace(_batch().packages[0], source_verified=True, source_verification_ref='verified',
                   source_opening_package_count=3, source_published_at='2026-08-27T13:00:00Z',
                   source_valid_until='2026-08-27T16:00:00Z')
    return tuple(replace(base, opportunity_group_id=f'opp-{i//2}', source_event_id=f'event-{i}',
                         package_signature=f'sig-{i}', evidence_revision_id=f'rev-{i}',
                         legs=tuple(replace(leg, strike=str(280+i*10), option_type='put' if i==2 else 'call') for leg in base.legs),
                         structure='put_calendar' if i==2 else 'call_calendar') for i in range(3))


def test_atomic_group_spans_opportunities_and_revokes_on_member_change(monkeypatch):
    members = calendars()
    combined, = combine_calendar_openings(members)
    assert combined.structure == 'calendar_bundle' and len(combined.legs) == 6
    reversed_group, = combine_calendar_openings(reversed(members))
    assert (combined.opportunity_group_id, combined.package_signature, combined.evidence_revision_id) == (
        reversed_group.opportunity_group_id, reversed_group.package_signature, reversed_group.evidence_revision_id)
    assert len(combine_calendar_openings(members[:2])) == 2
    assert len(combine_calendar_openings((*members[:2], replace(members[2], source_verified=False)))) == 3
    ticket = dict(source_id=combined.source_profile, source_event_id=combined.source_event_id,
                  source_opportunity_id=combined.opportunity_group_id, source_package_signature=combined.package_signature,
                  source_evidence_revision_id=combined.evidence_revision_id, source_verification_ref=combined.source_verification_ref,
                  underlying=combined.symbol, legs=[leg.to_dict() for leg in combined.legs])
    import kamandal_v2.live.execution as execution
    monkeypatch.setattr(execution, 'load_observed_package_feed', lambda _: [SimpleNamespace(packages=members[:2]), SimpleNamespace(packages=members[2:])])
    assert _fresh_exact_evidence_blocker({}, ticket) == ''
    monkeypatch.setattr(execution, 'load_observed_package_feed', lambda _: [SimpleNamespace(packages=members[:2])])
    assert _fresh_exact_evidence_blocker({}, ticket) == 'entry_exact_evidence_superseded'
    changed = (*members[:2], replace(members[2], evidence_revision_id='edited'))
    monkeypatch.setattr(execution, 'load_observed_package_feed', lambda _: [SimpleNamespace(packages=changed)])
    assert _fresh_exact_evidence_blocker({}, ticket) == 'entry_exact_evidence_superseded'


def test_group_duplicate_aliases_survive_pending_and_open(tmp_path):
    store = LocalStore(tmp_path/'state.db')
    ticket = dict(ticket_hash='t', order_id='o', plan_id='p', candidate_id='c', intent_type='open',
                  source_id='mike_butler', source_opportunity_id='group', source_opportunity_ids=['opp-0','opp-1'])
    store.save_live_order_intent(ticket,status='submitted')
    expected = {('mike_butler',v) for v in ['group','opp-0','opp-1']}
    assert occupied_source_opportunities(store) == expected
    assert occupied_source_opportunities(store,exclude_ticket_hash='t') == set()
    store.save_live_position_group('g',ticket,status='open')
    assert occupied_source_opportunities(store,exclude_ticket_hash='t') == expected


@pytest.mark.parametrize('structure,terms',[
    ('long_call', [('2026-10-16',90,'buy',1,3)]),
    ('call_butterfly',[('2026-10-16',90,'buy',1,12),('2026-10-16',100,'sell',2,6),('2026-10-16',110,'buy',1,2)]),
    ('put_butterfly',[('2026-10-16',90,'buy',1,2),('2026-10-16',100,'sell',2,6),('2026-10-16',110,'buy',1,12)]),
    ('call_crab',[('2026-11-20',90,'buy',1,15),('2026-10-16',100,'sell',2,8),('2026-10-16',110,'buy',1,3)]),
    ('calendar_bundle',[]),
])
def test_live_exact_shapes_require_broker_bpr_and_keep_cash_cap(tmp_path,structure,terms):
    option_type='put' if structure=='put_butterfly' else 'call'
    if structure=='calendar_bundle':
        packages=calendars()
        terms=[(l.expiration,float(l.strike),l.side,l.quantity,3 if l.side=='buy' else 2) for p in packages for l in p.legs]
        observed='2026-08-27T14:00:00Z'
        quotes=[OptionQuote('ADSK',l.expiration,l.option_type,float(l.strike),mid-.01,mid+.01,.5,.02,-.1,.08,.4,500,100)
                for p in packages for l in p.legs for mid in [3 if l.side=='buy' else 2]]
    else:
        legs=tuple(ObservedLegEvidence(quantity=q,expiration=e,strike=str(k),option_type=option_type,
                  order_code='BTO' if side=='buy' else 'STO',side=side,effect='open') for e,k,side,q,_ in terms)
        packages=(replace(calendars()[0],structure=structure,legs=legs,source_opening_package_count=1,
                          source_published_at='2026-09-25T14:00:00Z',source_valid_until='2026-09-25T17:00:00Z'),)
        observed='2026-09-25T14:05:00Z'
        quotes=[OptionQuote('ADSK',e,option_type,k,mid-.01,mid+.01,.5,.02,-.1,.08,.4,500,100) for e,k,side,q,mid in terms]
    row=_observed_calendar_row()
    row.update(structure=structure,strategy_family=structure,leg_count=len(terms),mode='live',csa_stage='live',
               source_mode='idea',accepted_inputs='exact_package',dte_min=1,dte_max=120,long_dte_min=2,long_dte_max=180,
               max_contracts=1 if structure in {'long_call','calendar_bundle'} else 2,live_max_bpr_per_order=1200)
    playbook=Playbook.from_row(row)
    policy=SimpleNamespace(playbook_id=playbook.playbook_id,accepted_inputs=('exact_package',),mode=SimpleNamespace(value='live'),structure=structure,fields=row,management={})
    source=compile_trade_source_policies([dict(source_id='mike_butler',output_kind='exact_package',mode='live',
            live_structures=structure if structure!='calendar_bundle' else 'calendar_bundle,call_calendar,put_calendar')]).by_key()
    class Market:
        bpr=500
        def chain_snapshot(self,_): return ChainSnapshot('snapshot','ADSK',observed,100,quotes,'fixture')
        def preflight(self,_): return PreflightResult(True,self.bpr,'accepted',{'broker_bpr_provided':True})
    market=Market()
    def build():
        return build_observed_package_candidates(packages,policies=(policy,),playbooks=[playbook],market=market,
             store=LocalStore(tmp_path/'candidate.db'),config={'runtime':{'observed_at':observed}},trade_source_policies=source,mode='live')
    candidate,=build()
    assert candidate.eligible, candidate.rejection_reason
    assert [l.quantity for l in candidate.legs] == [q for _,_,_,q,_ in terms]
    if structure=='calendar_bundle': assert {'opp-0','opp-1'} <= set(candidate.metadata['source_opportunity_ids'])
    market.bpr=1500
    rejected,=build()
    assert rejected.rejection_reason=='exact_broker_risk_above_order_cap'


def test_verified_copy_bypasses_income_score_but_never_risk_limits():
    c=_candidate('copy',underlying='ABC',structure='long_call',bpr=500,delta=0,gamma=0,theta=-10,vega=0,net_credit=-5,score=-100)
    c.metadata.update(input_kind='exact_package',source_verified=True,source_verification_ref='verified')
    c.preflight=PreflightResult(True,500,'ok',{'broker_bpr_provided':True,'response':{'buyingPowerRequirement':500}})
    control=_control();control['planner']={'min_marginal_score':1000000}
    assert generate_plans([c],_portfolio(),control)
    c.metadata['source_verified']=False
    assert not generate_plans([c],_portfolio(),control)
    c.metadata['source_verified']=True
    c.estimated_bpr=100000
    assert not generate_plans([c],_portfolio(),control)

@pytest.mark.parametrize('structure,expiry_day', [(s,False) for s in ['long_call','call_butterfly','put_butterfly','call_crab','calendar_bundle','call_spread','put_spread']] + [('call_butterfly',True),('put_butterfly',True)])
def test_live_handoff_and_full_package_exit(tmp_path, monkeypatch, structure, expiry_day):
    from kamandal_v2.config import load_control
    from kamandal_v2.domain.models import PortfolioState
    from kamandal_v2.strategy_engine import planning
    from kamandal_v2.strategy_lanes.daily_policy import DailyPolicySnapshot, policy_tables_hash
    from kamandal_v2.strategy_lanes.operator_policy import OperatorPolicyBundle
    from kamandal_v2.strategy_lanes.management_runtime import run_live_lifecycle_management
    from kamandal_v2.live.execution import _adopt_csa_live_fill
    from test_observed_package_planning import _migrated_store
    now='2026-08-27T14:00:00Z'
    near,far=('2026-08-27' if expiry_day else '2026-08-28'),'2026-09-04'
    kind='put' if structure in {'put_butterfly','put_spread'} else 'call'
    terms={
        'long_call':[(near,90,'buy',1,3)],
        'call_butterfly':[(near,90,'buy',1,12),(near,100,'sell',2,6),(near,110,'buy',1,2)],
        'put_butterfly':[(near,90,'buy',1,2),(near,100,'sell',2,6),(near,110,'buy',1,12)],
        'call_crab':[(far,90,'buy',1,15),(near,100,'sell',2,8),(near,110,'buy',1,3)],
        'call_spread':[(near,90,'buy',1,12),(near,100,'sell',1,6)],
        'put_spread':[(near,110,'buy',1,12),(near,100,'sell',1,6)],
    }.get(structure,[])
    if structure=='calendar_bundle':
        packages=calendars()
        specs=[(l.expiration,float(l.strike),l.side,l.quantity,3 if l.side=='buy' else 2,l.option_type) for p in packages for l in p.legs]
    else:
        specs=[(*t,kind) for t in terms]
        legs=tuple(ObservedLegEvidence(q,e,str(k),kind,'BTO' if side=='buy' else 'STO',side,'open') for e,k,side,q,mid in terms)
        packages=(replace(calendars()[0],structure=structure,legs=legs,source_opening_package_count=1),)
    row=_observed_calendar_row()
    row.update(playbook_id='guru_fixture',structure=structure,strategy_family=structure,leg_count=len(specs),
        mode='live',csa_stage='live',source_mode='idea',accepted_inputs='exact_package',source_profiles='',
        dte_min=1,dte_max=120,long_dte_min=2,long_dte_max=180,max_contracts=1 if len(specs) in {1,6} else 2,
        live_max_bpr_per_order=1200,exit_dte_min=0,half_time_exit='FALSE',resting_profit_enabled='FALSE',
        management_policy_json=json.dumps({'lifecycle':{'close_only':True,'fill':{'max_attempts':4,'price_increment':.05}}}))
    if expiry_day:
        row.update(dte_min=0,management_policy_json=json.dumps({'lifecycle':{'close_only':True,'fill':{'max_attempts':4,'price_increment':.05},'expiry_day_entry_minutes_before_close':60,'expiry_day_exit_minutes_before_close':30}}))
    allowed=structure if structure!='calendar_bundle' else 'calendar_bundle,call_calendar,put_calendar'
    sources=[dict(source_id=s,output_kind=k,mode='live' if k=='exact_package' else 'off',live_structures=allowed if k=='exact_package' else '')
             for s in ['mike_butler','greg_harmon'] for k in ['idea','exact_package']]
    tables={'universe':[],'playbooks':[row],'trade_sources':sources}
    snapshot=DailyPolicySnapshot(now[:10],now,policy_tables_hash(tables),tables,tmp_path/'policy.json',OperatorPolicyBundle((),(),(),now,source='fixture'))
    config=load_control();config['portfolio']['sleeves_source']='';config['runtime']['observed_at']=now;config['risk_manager']['enabled']=False
    config['runtime']['venue_portfolios'] = {
        venue: PortfolioState(50000, 50000, 0, 0).to_dict() for venue in ('public_primary', 'tasty_primary')
    }
    class Market:
        def __init__(self,when): self.when=when
        def chain_snapshot(self,_):
            return ChainSnapshot('chain','ADSK',self.when,100,[OptionQuote('ADSK',e,t,k,mid-.01,mid+.01,.0,.0,-.01,.0,.4,1000,100)
                  for e,k,side,q,mid,t in specs],'fixture')
        def account_state(self): return PortfolioState(100000,100000,0,0)
        def preflight(self,candidate): return PreflightResult(True,500,'ok',{'broker_bpr_provided':True,'response':{'buyingPowerRequirement':500}})
    market=Market(now)
    monkeypatch.setattr(planning,'_market_provider',lambda *a,**kw:market)
    store=_migrated_store(tmp_path)
    result=planning.run_unified_books(config,universe_rows=[],playbook_rows=[row],idea_paths=[],provider='fixture',store=store,
        audit_root=tmp_path/'audit',daily_policy_snapshot=snapshot,trade_source_rows=sources,observed_package_batches=(replace(_batch(),packages=packages),))
    assert result.compilation.ok, result.compilation.errors
    assert not result.live.errors, result.live.errors
    assert len(result.live.handoffs)==1, [(c.rejection_reason,c.reasons) for c in result.live.result.candidates]
    ticket,=store.live_order_intents_by_type('open')
    assert ticket['capital_scope_venues'] == ['public_primary', 'tasty_primary']
    assert len(ticket['legs'])==len(specs)
    assert [l['quantity'] for l in ticket['legs']]==[q for e,k,side,q,mid,t in specs]
    assert _adopt_csa_live_fill(store,ticket,{'averagePrice':str(abs(result.live.result.candidates[0].net_credit)),'filledAt':now})['status']=='open'
    for when,expected in [('2026-08-27T14:05:00Z','hold'),('2026-08-27T19:30:00Z' if expiry_day else '2026-08-28T14:00:00Z','close')]:
        result=run_live_lifecycle_management(config,sqlite_path=str(store.sqlite_path),provider='fixture',tables=tables,market=Market(when),observed_at=when)
        assert result.ok, result.errors
        assert result.selected_actions=={expected:1}
    close,=store.live_order_intents_by_type('close')
    assert len(close['legs'])==len(specs)
    assert [l['quantity'] for l in close['legs']]==[q for e,k,side,q,mid,t in specs]
    assert [l['side'].lower() for l in close['legs']]==['sell' if side=='buy' else 'buy' for e,k,side,q,mid,t in specs]


def test_sheet_activation_preserves_switches_caps_and_unrelated_policy():
    import runpy
    from kamandal_v2.config import load_control
    from kamandal_v2.strategy_engine.sheet_policy_gate import validate_sheet_policy
    base=_observed_calendar_row()
    base.update(playbook_id='guru_exact_call_calendar',source_mode='idea',accepted_inputs='exact_package',source_profiles='',
                live_max_bpr_per_order='1200',mode='live',csa_stage='live',resting_profit_enabled='FALSE')
    rows=[base]
    for shape,n in [('long_call',1),('call_butterfly',3),('put_butterfly',3),('call_crab',3)]:
        rows.append(dict(base,playbook_id=f'guru_exact_{shape}_shadow',structure=shape,strategy_family=shape,leg_count=n,
                         mode='shadow',csa_stage='shadow',max_contracts='2',dte_min='1',dte_max='120',long_dte_min='2',long_dte_max='180'))
    rows.append({'range_gate_required':'FALSE','resting_profit_enabled':'FALSE'})
    from kamandal_v2.seed import seed_headers, build_seed_tables
    universe=[dict(zip(seed_headers()['universe'], row)) for row in build_seed_tables(load_control())['universe']]
    tables={'playbooks':rows,'universe':universe,
            'trade_sources':[dict(source_id=s,output_kind=k,mode='off',notes='',live_structures='call_calendar,put_calendar' if k=='exact_package' else '') for s in ['mike_butler','greg_harmon'] for k in ['idea','exact_package']],
            'portfolio_sleeves':[dict(lane=s,max_bpr_pct=str(v),meaning='fixture') for s,v in [('current_idea',40),('guru_exact',40),('portfolio_total',80)]]}
    propose=runpy.run_path('scripts/apply_guru_live_packages_sheet.py')['proposed_tables']
    result=propose(tables)
    assert result['playbooks'][0]==base
    assert result['portfolio_sleeves']==tables['portfolio_sleeves']
    assert all(row['mode']=='off' for row in result['trade_sources'])
    assert all(float(row['live_max_bpr_per_order'])==1200 for row in result['playbooks'])
    assert propose(result)==result
    gate=validate_sheet_policy(load_control(),tables=result)
    assert gate.ok,json.dumps(gate.to_dict())


@pytest.mark.parametrize('status', ['filled', 'manual_fill_recorded', 'partially_filled_terminal'])
def test_source_opening_remains_consumed_after_close_and_day_boundary(tmp_path, status):
    store = LocalStore(tmp_path/'state.db')
    ticket = dict(ticket_hash='consumed', order_id='o', plan_id='p', candidate_id='c', intent_type='open',
                  source_id='greg_harmon', source_opportunity_id='original-opening', source_opportunity_ids=['original-alias'])
    store.save_live_order_intent(ticket, status=status)
    assert store.open_live_position_groups() == []
    expected = {('greg_harmon','original-opening'), ('greg_harmon','original-alias')}
    assert occupied_source_opportunities(store) == expected
    assert occupied_source_opportunities(store, exclude_ticket_hash='consumed') == expected
    failed = dict(ticket, ticket_hash='unfilled', order_id='retry', source_opportunity_id='new-opening', source_opportunity_ids=[])
    store.save_live_order_intent(failed, status='cancelled')
    assert ('greg_harmon', 'new-opening') not in occupied_source_opportunities(store)
