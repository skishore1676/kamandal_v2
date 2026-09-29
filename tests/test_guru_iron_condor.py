from dataclasses import replace
import json

import pytest

from kamandal_v2.config import load_control
from kamandal_v2.domain.models import ChainSnapshot, OptionQuote, PortfolioState, PreflightResult
from kamandal_v2.live.expiry_day import expiry_day_buffers, expiry_day_window
from kamandal_v2.market.public import occ_symbol, parse_occ_symbol
from kamandal_v2.live.orders import _closing_leg
from kamandal_v2.intelligence.observed_packages import ObservedLegEvidence
from kamandal_v2.strategy_lanes.daily_policy import DailyPolicySnapshot, policy_tables_hash
from kamandal_v2.strategy_lanes.operator_policy import OperatorPolicyBundle
from kamandal_v2.strategy_lanes.store import CsaStore
from test_observed_package_planning import _observed_calendar_row, _batch, _migrated_store, _Market


MANAGEMENT = {'lifecycle': {'close_only': True, 'fill': {'max_attempts': 4, 'price_increment': .05},
                          'expiry_day_entry_minutes_before_close': 60,
                          'expiry_day_exit_minutes_before_close': 30}}


def condor_row():
    row = _observed_calendar_row()
    row.update(playbook_id='guru_exact_iron_condor', strategy_family='iron_condor', structure='iron_condor',
               leg_count=4, mode='live', csa_stage='live', source_mode='idea', accepted_inputs='exact_package',
               dte_min=0, dte_max=60, long_dte_min='', long_dte_max='', max_contracts=1,
               exit_dte_min=0, half_time_exit='FALSE', avoid_earnings='FALSE', resting_profit_enabled='FALSE',
               live_max_bpr_per_order=1200, management_policy_json=json.dumps(MANAGEMENT))
    return row


@pytest.mark.parametrize('when,entry,exit_', [
    ('2026-09-29T18:00:00Z', True, False),
    ('2026-09-29T19:00:00Z', False, False),
    ('2026-09-29T19:30:00Z', False, True),
    ('2026-11-27T16:59:00Z', True, False),
    ('2026-11-27T17:00:00Z', False, False),
    ('2026-11-27T17:30:00Z', False, True),
])
def test_expiry_windows_follow_regular_and_early_close(when, entry, exit_):
    config = load_control()
    window = expiry_day_window(config, MANAGEMENT, 'SPX', when)
    assert (window['entry_allowed'], window['exit_due']) == (entry, exit_)


@pytest.mark.parametrize('entry,exit_', [(30, 30), (15, 10), (60, '30'), (True, 30), (181, 30)])
def test_invalid_buffers_fail_closed(entry, exit_):
    with pytest.raises(ValueError):
        expiry_day_buffers({'lifecycle': {'expiry_day_entry_minutes_before_close': entry,
                                         'expiry_day_exit_minutes_before_close': exit_}})


def test_exact_spx_condor_entry_hold_close_preserves_contracts(tmp_path, monkeypatch):
    from kamandal_v2.strategy_engine import planning
    from kamandal_v2.live.execution import _adopt_csa_live_fill, _source_route_blocker
    from kamandal_v2.strategy_lanes.management_runtime import run_live_lifecycle_management
    now = '2026-09-29T16:00:00Z'
    row = condor_row()
    sources = [{'source_id': source, 'output_kind': kind, 'mode': 'live' if kind == 'exact_package' else 'off',
                'live_structures': 'iron_condor' if kind == 'exact_package' else ''}
               for source in ('mike_butler', 'greg_harmon') for kind in ('idea', 'exact_package')]
    tables = {'universe': [], 'playbooks': [row], 'trade_sources': sources}
    snapshot = DailyPolicySnapshot(now[:10], now, policy_tables_hash(tables), tables, tmp_path/'policy.json',
                                   OperatorPolicyBundle((), (), (), now, source='fixture'))
    control = load_control()
    control['portfolio']['sleeves_source'] = ''
    control['runtime']['observed_at'] = now
    control['risk_manager']['enabled'] = False
    specs = [('put', 7650, 'buy', 2), ('put', 7655, 'sell', 3),
             ('call', 7700, 'sell', 3), ('call', 7705, 'buy', 2)]

    class Market(_Market):
        def chain_snapshot(self, underlying):
            assert underlying == 'SPX'
            return ChainSnapshot('spx-exact', underlying, self.captured_at, 7675, [
                OptionQuote(underlying, now[:10], kind, strike, mid-.05, mid+.05,
                            .2 if kind=='call' else -.2, .01, -.02, .03, .2, 500, 100,
                            broker_symbol=f'SPXW260929{kind[0].upper()}{strike*1000:08d}')
                for kind,strike,side,mid in specs], 'fixture_exact')
        def account_state(self):
            return PortfolioState(100000, 100000, 0, 0)
        def preflight(self, candidate):
            assert len(candidate.legs) == 4
            return PreflightResult(True, 300, 'preview accepted', {'broker_bpr_provided': True,
                                                                 'response': {'buyingPowerRequirement': 300}})
    market = Market(captured_at=now)
    monkeypatch.setattr(planning, '_market_provider', lambda *a, **kw: market)
    package = replace(_batch().packages[0], symbol='SPX', product_type='index_option', structure='iron_condor',
                      legs=tuple(ObservedLegEvidence(1, now[:10], str(strike), kind,
                                 'BTO' if side=='buy' else 'STO', side, 'open') for kind,strike,side,mid in specs),
                      source_published_at='2026-09-29T15:15:33Z', source_valid_until='2026-09-30T15:15:33Z',
                      source_verified=True, source_verification_ref='sv_independent_fixture')
    store = _migrated_store(tmp_path)
    result = planning.run_unified_books(control, universe_rows=tables['universe'], playbook_rows=[row],
        idea_paths=[], provider='fixture', store=store, audit_root=tmp_path/'audit', daily_policy_snapshot=snapshot,
        trade_source_rows=sources, observed_package_batches=(replace(_batch(), packages=(package,)),))
    assert result.compilation.ok, result.compilation.errors
    assert result.live.errors == ()
    assert len(result.live.handoffs) == 1, [(c.rejection_reason,c.reasons) for c in result.live.result.candidates]
    ticket, = store.live_order_intents_by_type('open')
    assert ticket['source_valid_until'] == '2026-09-29T14:00:00-05:00'
    assert _source_route_blocker(ticket, sources) == ''
    for leg in ticket['legs']:
        symbol = occ_symbol('SPX', _closing_leg(leg))
        assert symbol == leg['broker_symbol']
        assert parse_occ_symbol(symbol)['underlying'] == 'SPX'
    assert _adopt_csa_live_fill(store, ticket, {'averagePrice':'2.00','filledAt':now})['status'] == 'open'
    for observed, expected in [('2026-09-29T16:05:00Z', 'hold'), ('2026-09-29T19:30:00Z', 'close')]:
        result = run_live_lifecycle_management(control, sqlite_path=str(store.sqlite_path), provider='fixture',
                  tables=tables, market=Market(captured_at=observed), observed_at=observed)
        assert result.ok, result.errors
        assert result.selected_actions == {expected: 1}
    close, = store.live_order_intents_by_type('close')
    assert [leg['instrument_id'] for leg in close['legs']] == [leg['broker_symbol'] for leg in ticket['legs']]
    assert {leg['side'] for leg in close['legs']} == {'buy','sell'}
    assert _adopt_csa_live_fill(store, close, {'averagePrice':'2.00','filledAt':'2026-09-29T19:31:00Z'})['status'] == 'closed'
    assert CsaStore(store.sqlite_path).lifecycle(ticket['csa_lifecycle_id']).status == 'closed'


def test_same_day_policy_requires_explicit_intraday_exit():
    from kamandal_v2.strategy_engine.policy import compile_playbook_policy, PolicyError
    row = condor_row()
    assert compile_playbook_policy(row).structure == 'iron_condor'
    for changes in ({'management_policy_json': '{}'}, {'half_time_exit': 'TRUE'}, {'exit_dte_min': 1}):
        with pytest.raises(PolicyError):
            compile_playbook_policy({**row, **changes})


def test_exact_condor_safety_gates_reject_expired_oversized_and_wrong_root():
    from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg, Playbook
    from kamandal_v2.planner.observed_package_candidates import _exact_condor_contract_rejections
    legs = [OptionLeg.from_quote(OptionQuote('SPX','2026-09-29', kind, strike, 2, 2.1,
              .2,.01,-.02,.03,.2,500,broker_symbol=f'SPXW260929{kind[0].upper()}{strike*1000:08d}'),
              role=('short' if side=='sell' else 'long')+'_'+kind, side=side, quantity=1)
              for kind,strike,side in [('put',7650,'buy'),('put',7655,'sell'),('call',7700,'sell'),('call',7705,'buy')]]
    book = Playbook.from_row(condor_row())
    base = Candidate('test','test','SPX',book.playbook_id,'iron_condor',legs,2,300,Greeks(),1,0,
                     metadata={'source_valid_until':'2026-09-30T15:00:00Z'})
    def reject(candidate=base, when='2026-09-29T16:00:00Z'):
        return _exact_condor_contract_rejections(candidate,book,{'runtime':{'observed_at':when}},management=MANAGEMENT)
    assert reject() == []
    assert 'exact_condor_expiry_day_entry_window_closed' in reject(when='2026-09-29T19:00:00Z')
    assert 'exact_condor_dte_outside_policy' in reject(when='2026-09-30T16:00:00Z')
    assert 'exact_condor_quantity_above_policy' in reject(replace(base,legs=[replace(l,quantity=2) for l in legs]))
    assert 'exact_condor_requires_pm_settled_spxw' in reject(replace(base,legs=[replace(l,broker_symbol=l.broker_symbol.replace('SPXW','SPX')) for l in legs]))
    assert 'exact_condor_risk_above_order_cap' in reject(replace(base,estimated_bpr=1300))


def test_sheet_migration_preserves_existing_policies_and_source_switches():
    import runpy
    from pathlib import Path
    propose = runpy.run_path(str(Path(__file__).parents[1]/'scripts/apply_guru_iron_condor_sheet.py'))['proposed_tables']
    template = condor_row()
    template.update(playbook_id='iron_condor_default', accepted_inputs='idea', dte_min='35', dte_max='50',
                    exit_dte_min='21', half_time_exit='TRUE', live_max_bpr_per_order='500')
    sources = [{'source_id':s,'output_kind':'exact_package','mode':'off','live_structures':'short_strangle'}
               for s in ('mike_butler','greg_harmon')]
    tables = {'playbooks':[template,{}], 'trade_sources':sources,'universe':[],
              'portfolio_sleeves':[{'lane':'guru_exact','max_bpr_pct':'40'}]}
    result = propose(tables)
    assert result['playbooks'][0] == template
    assert result['playbooks'][1]['live_max_bpr_per_order'] == '500'
    assert result['portfolio_sleeves'] == tables['portfolio_sleeves']
    assert result['universe'] == []
    assert all(r['mode']=='off' and r['live_structures']=='short_strangle,iron_condor' for r in result['trade_sources'])
    assert propose(result) == result
    assert tables['playbooks'][1] == {}
