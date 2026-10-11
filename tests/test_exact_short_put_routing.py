from dataclasses import replace

import pytest

from kamandal_v2.domain.models import Playbook, PreflightResult
from kamandal_v2.intelligence.trade_sources import compile_trade_source_policies
from kamandal_v2.planner.observed_package_candidates import build_observed_package_candidates
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.policy import compile_playbook_policy
from tests.test_exact_strangle_routing import Market as BaseMarket, _package as strangle, NOW
from tests.test_unified_policy import _row


def package():
    p = strangle()
    return replace(p, structure='short_put', legs=(p.legs[0],))


def row():
    return _row(playbook_id='guru_exact_short_put', strategy_family='short_put', structure='short_put',
                mode='live', csa_stage='live', accepted_inputs='exact_package', max_contracts='1',
                live_max_bpr_per_order=2500, execution_venue='public_primary', leg_count=1,
                max_bid_ask_pct=.08, profit_target_pct=40, max_loss_multiple=2.5,
                management_policy_json={'lifecycle':{'close_only':True,'fill':{'max_attempts':4,'price_increment':.05}}})


class Market(BaseMarket):
    def preflight(self, candidate):
        assert candidate.execution_venue == 'public_primary'
        assert [(l.strike, l.side, l.option_type) for l in candidate.legs] == [(90, 'sell', 'put')]
        self.calls += 1
        return PreflightResult(True, self.bpr, 'fixture native BPR',
                               {'broker_bpr_provided': self.bpr_provided, 'bpr_source': 'public_preflight', 'response': {'buyingPowerRequirement':self.bpr} if self.bpr_provided else {}})


def build(tmp_path, *, p=None, market=None, source_mode='live'):
    policy_row = row()
    source = compile_trade_source_policies([dict(source_id='mike_butler', output_kind='exact_package',
                                                 mode=source_mode, live_structures='short_put')])
    assert source.ok
    return build_observed_package_candidates((p or package(),), policies=(compile_playbook_policy(policy_row),),
        playbooks=[Playbook.from_row(policy_row)], market=market or Market(), store=LocalStore(tmp_path/'state.db'),
        config={'runtime': {'mode':'live', 'observed_at':NOW}}, trade_source_policies=source.by_key(), universe=[], mode='live')


def test_exact_short_put_preserves_contract_and_native_risk(tmp_path):
    c, = build(tmp_path)
    assert c.eligible
    assert c.estimated_bpr == 1800
    assert [(l.role, l.quantity) for l in c.legs] == [('short_put', 1)]
    assert c.net_credit > 0


@pytest.mark.parametrize('mode', ['off', 'observe', 'shadow'])
def test_source_switch_blocks_live(tmp_path, mode):
    assert build(tmp_path, source_mode=mode) == []


@pytest.mark.parametrize('change', [dict(side='buy'), dict(option_type='call'), dict(effect='close')])
def test_wrong_source_leg_is_not_substituted(tmp_path, change):
    p=package()
    assert build(tmp_path, p=replace(p, legs=(replace(p.legs[0], **change),))) == []


def test_expired_source_is_not_replayed(tmp_path):
    assert build(tmp_path, p=replace(package(), source_valid_until='2026-09-08T13:30:00Z')) == []


def test_broker_cash_secured_requirement_above_cap_rejected(tmp_path):
    m=Market(); m.bpr=8900
    c, = build(tmp_path, market=m)
    assert not c.eligible
    assert 'bpr' in c.rejection_reason.lower() or 'risk' in c.rejection_reason.lower()


def test_missing_broker_bpr_cannot_pass_final_gate(tmp_path):
    from kamandal_v2.live.advisory import _preflight_bpr_incomplete
    from kamandal_v2.live.execution import _fresh_entry_preflight_blocker
    m=Market(); m.bpr_provided=False
    c, = build(tmp_path, market=m)
    assert _preflight_bpr_incomplete(c)
    assert _fresh_entry_preflight_blocker({'intent_type':'open', 'structure':'short_put', 'source_output_kind':'exact_package', 'entry_risk_budget':2500}, c.preflight)


@pytest.mark.parametrize('bid,ask', [(0,1), (.1,2)])
def test_unactionable_quotes_cannot_reach_preflight(tmp_path, bid, ask):
    m=Market(); original=m.chain_snapshot
    m.chain_snapshot=lambda symbol: replace(original(symbol), quotes=[replace(q,bid=bid,ask=ask) for q in original(symbol).quotes])
    c, = build(tmp_path, market=m)
    assert not c.eligible
    assert m.calls == 0


def test_short_put_frozen_management_and_close_ticket(tmp_path):
    from dataclasses import asdict
    from kamandal_v2.live.orders import build_close_ticket
    policy=compile_playbook_policy(row())
    assert policy.structure == 'short_put'
    c, = build(tmp_path)
    ticket=build_close_ticket({'group_id':'fixture', 'candidate':asdict(c)}, limit_price=.60)
    assert len(ticket['legs']) == 1
    assert ticket['legs'][0]['side'] == 'buy'
    assert ticket['legs'][0]['quantity'] == 1
    assert ticket['legs'][0]['strike'] == 90


def test_sheet_migration_preserves_existing_controls():
    from copy import deepcopy
    from scripts.apply_exact_short_put_sheet import proposed_tables
    from kamandal_v2.strategy_engine.policy import compile_playbook_policies
    template=row()
    template.update(playbook_id='short_put_premium_harvest', enabled='FALSE', accepted_inputs='idea', max_contracts='2')
    tables={'playbooks':[template, {}], 'trade_sources':[
        {'source_id':s, 'output_kind':'exact_package', 'mode':'live', 'live_structures':'call_spread'}
        for s in ('greg_harmon','mike_butler')], 'portfolio_sleeves':[{'lane':'current_idea','max_bpr_pct':'40'}]}
    before=deepcopy(tables)
    result=proposed_tables(tables)
    assert tables == before
    assert result['playbooks'][0] == template
    assert result['portfolio_sleeves'] == tables['portfolio_sleeves']
    assert result['playbooks'][1]['max_contracts'] == '1'
    assert result['playbooks'][1]['accepted_inputs'] == 'exact_package'
    assert compile_playbook_policies(result['playbooks']).ok
    assert proposed_tables(result) == result


def test_exact_short_put_enters_canonical_lifecycle_without_broker_effects(tmp_path, monkeypatch):
    from kamandal_v2.config import load_control
    from kamandal_v2.domain.models import PortfolioState
    from kamandal_v2.strategy_engine import planning
    from kamandal_v2.strategy_lanes.daily_policy import DailyPolicySnapshot, policy_tables_hash
    from kamandal_v2.strategy_lanes.operator_policy import OperatorPolicyBundle
    from kamandal_v2.strategy_lanes.store import CsaStore
    from tests.test_observed_package_planning import _batch, _migrated_store
    sources=[dict(source_id=s, output_kind=o, mode='live' if o=='exact_package' else 'off',
                  live_structures='short_put' if o=='exact_package' else '')
             for s in ('greg_harmon','mike_butler') for o in ('idea','exact_package')]
    universe=[{'symbol':'XYZ','enabled':'TRUE','notes':''}]
    tables={'universe':universe, 'playbooks':[row()], 'trade_sources':sources}
    snapshot=DailyPolicySnapshot(NOW[:10],NOW,policy_tables_hash(tables),tables,tmp_path/'policy.json',
                                OperatorPolicyBundle((),(),(),NOW,source='fixture'))
    control=load_control()
    control['portfolio']['sleeves_source']=''
    control['portfolio']['hard_max_bpr_utilization_pct']=55
    control['runtime']['observed_at']=NOW
    control['live']['max_bpr_per_order']=2500
    control['risk_manager']['enabled']=False
    market=Market(); market.account_state=lambda:PortfolioState(100000,100000,0,0)
    monkeypatch.setattr(planning,'_market_provider',lambda *_a,**_k:market)
    store=_migrated_store(tmp_path)
    result=planning.run_unified_books(control,universe_rows=universe,playbook_rows=[row()],idea_paths=[],provider='fixture',
        store=store,audit_root=tmp_path/'audit',daily_policy_snapshot=snapshot,trade_source_rows=sources,
        observed_package_batches=(replace(_batch(),packages=(package(),)),))
    assert result.compilation.ok
    assert not result.live.errors
    assert len(result.live.handoffs)==1, ([(c.rejection_reason, c.reasons) for c in result.live.result.candidates], result.live.result.plans)
    lifecycle=CsaStore(store.sqlite_path).lifecycle(result.live.handoffs[0]['lifecycle_id'])
    assert lifecycle.lane.value=='generic_close_only'
    assert lifecycle.status=='pending_live_submission'
    assert lifecycle.metadata['compiled_management_policy']['resolved_fields']['profit_target_pct']==40
    assert int(lifecycle.metadata['compiled_management_policy']['resolved_fields']['exit_dte_min'])==21
    ticket,=store.live_order_intents_by_type('open')
    assert ticket['source_output_kind']=='exact_package'
    assert len(ticket['legs'])==1
