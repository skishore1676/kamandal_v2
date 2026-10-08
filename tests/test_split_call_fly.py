"""QQQ October 5 regression: one exact, bounded, managed four-leg package."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_source_episode_compiler import FakeClient, _event, _packet, _profile, _record
from test_observed_package_planning import _observed_calendar_row
from kamandal_v2.domain.models import ChainSnapshot, OptionQuote, Playbook, PreflightResult
from kamandal_v2.intelligence.source_episode_compiler import PROMPT_SCHEMA, compile_source_episode_packet
from kamandal_v2.intelligence.source_episode_projection import project_source_episode_compilation
from kamandal_v2.intelligence.source_contract_verification import bind_interpreted_source_contracts
from kamandal_v2.intelligence.source_shorthand import resolve_declared_text_contracts
from kamandal_v2.intelligence.trade_sources import compile_trade_source_policies
from kamandal_v2.planner.observed_package_candidates import build_observed_package_candidates, _candidate, _hydrate_exact_legs
from kamandal_v2.planner.shape_validators import validate_exact_structure
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.policy import compile_playbook_policy
from kamandal_v2.strategy_lanes.policy import compile_csa_policy

NOW = '2026-10-05T19:40:00Z'
TEXT = 'added some $QQQ Oct 23 Exp 760/775-780/795 split wing butterfly call spreads'


def fixture():
    record = _record('2107192209900785941', TEXT, ['QQQ'], classification='trade_journal', published_at='2026-10-05T19:32:27Z')
    legs = [{'expiration': 'Oct 23 2026', 'strike': str(strike), 'quantity': 1, 'option_type': 'call', 'order_code': code}
            for strike, code in zip((760, 775, 780, 795), ('BTO', 'STO', 'STO', 'BTO'))]
    response = {'schema': PROMPT_SCHEMA, 'episodes': [{'signal_id': record['signal_id'], 'events': [
        _event(action='scale_in', symbol='QQQ', direction='bullish', structure_hint='call_spread', projections=['exact_package', 'idea'],
               exact_packages=[{'complete': True, 'blocker': None, 'displayed_price': None, 'field_provenance': ['text'], 'legs': legs}])]}]}
    packet = _packet([record]); packet['generated_at'] = NOW
    return record, packet, response


def package():
    record, packet, response = fixture()
    compilation = compile_source_episode_packet(packet, _profile('greg_harmon'), FakeClient(response))
    event = compilation.episodes[0]['events'][0]
    assert event['structure_hint'] == 'split_call_fly'
    assert event['exact_packages'][0]['complete']
    projection = project_source_episode_compilation(compilation, packet, _profile('greg_harmon'), universe_symbols=['QQQ'])
    assert not projection.failures and not projection.planner_ideas
    batches, failures = bind_interpreted_source_contracts(projection.observed_batches, packet)
    assert not failures
    result, = batches[0].packages
    assert result.structure == 'split_call_fly' and result.source_verified
    assert result.source_published_at == record['source']['published_at']
    return result


def row():
    value = _observed_calendar_row()
    value.update(playbook_id='guru_exact_split_call_fly', structure='split_call_fly', strategy_family='split_call_fly',
                 mode='live', csa_stage='live', source_mode='idea', accepted_inputs='exact_package',
                 dte_min=1, exit_dte_min=0, half_time_exit='FALSE', resting_profit_enabled='FALSE', leg_count=4)
    return value


def chain():
    return ChainSnapshot('qqq-chain', 'QQQ', NOW, 770, [
        OptionQuote('QQQ', '2026-10-23', 'call', strike, price-.02, price+.02, .3, .01, -.05, .1, .3, 500, 100)
        for strike, price in ((760, 20), (775, 11), (780, 8), (795, 3))], 'fixture')


def test_source_to_live_candidate_and_frozen_management(tmp_path):
    source = package()
    policy = compile_playbook_policy(row())
    csa = compile_csa_policy(row(), source='google_sheet', read_at=NOW)
    assert csa.lane.value == 'generic_close_only'
    assert policy.accepted_inputs == ('exact_package',)
    market = SimpleNamespace(chain_snapshot=lambda *_a, **_k: chain(),
        preflight=lambda c: PreflightResult(True, 410, 'fixture', {'broker_bpr_provided': True, 'response': {'buyingPowerRequirement': 410}}))
    sources = compile_trade_source_policies([{'source_id': 'greg_harmon', 'output_kind': 'exact_package', 'mode': 'live', 'live_structures': 'split_call_fly'}]).by_key()
    candidates = build_observed_package_candidates([source], policies=(policy,), playbooks=[Playbook.from_row(row())],
        market=market, store=LocalStore(tmp_path/'state.db'), config={'runtime': {'observed_at': NOW}}, trade_source_policies=sources, mode='live')
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.eligible, candidate.rejection_reason
    assert len(candidate.legs) == 4 and candidate.net_credit == -4 and candidate.estimated_bpr == 410
    assert candidate.entry_debit_ceiling == 12
    assert [(l.side, l.strike, l.quantity) for l in candidate.legs] == [('buy',760,1),('sell',775,1),('sell',780,1),('buy',795,1)]


def test_cached_repair_preserves_identity_deadline_and_evidence():
    record, packet, response = fixture()
    old_profile = deepcopy(_profile('greg_harmon'))
    old_profile['episode_interpreter']['composite_structure_rules'] = []
    old_profile['episode_interpreter'].pop('text_contract_convention')
    old = compile_source_episode_packet(packet, old_profile, FakeClient(response)).episodes[0]
    before = deepcopy(old)
    fixed = resolve_declared_text_contracts(record, old, _profile('greg_harmon'))
    assert old == before
    assert fixed['events'][0]['structure_hint'] == 'split_call_fly'
    assert fixed['events'][0]['exact_packages'][0]['complete']
    for key in ('episode_id', 'published_at'):
        assert fixed[key] == old[key]
    for key in ('event_id', 'opportunity_group_id', 'source_valid_until'):
        assert fixed['events'][0].get(key) == old['events'][0].get(key)
    old['events'][0]['exact_packages'][0]['legs'][0]['strike'] = '759'
    assert resolve_declared_text_contracts(record, old, _profile('greg_harmon')) == old


@pytest.mark.parametrize('mutation', ['ratio', 'side', 'expiry', 'put', 'duplicate'])
def test_geometry_rejects_conflicting_contracts(mutation):
    legs = _hydrate_exact_legs(package(), chain().quotes)
    if mutation == 'ratio': legs[1].quantity = 2
    if mutation == 'side': legs[1].side = 'buy'
    if mutation == 'expiry': legs[1].expiration = '2026-10-30'
    if mutation == 'put': legs[1].option_type = 'put'
    if mutation == 'duplicate': legs[1].strike = legs[0].strike
    assert not validate_exact_structure('split_call_fly', legs, 770).valid


def test_unequal_wings_include_terminal_loss_and_reduce_price_budget():
    original = package()
    source = replace(original, legs=tuple(replace(l, strike='800') if l.strike=='795' else l for l in original.legs))
    snapshot = chain(); snapshot.quotes[-1].strike = 800
    legs = _hydrate_exact_legs(source, snapshot.quotes)
    candidate = _candidate(source, Playbook.from_row(row()), legs, chain_snapshot=snapshot)
    assert candidate.estimated_bpr == 900  # $400 debit plus $500 unequal-wing loss.
    assert candidate.entry_debit_ceiling == 7


def test_full_ticket_fill_and_whole_package_exit(tmp_path, monkeypatch):
    from kamandal_v2.config import load_control
    from kamandal_v2.domain.models import PortfolioState
    from kamandal_v2.strategy_engine import planning
    from kamandal_v2.strategy_lanes.daily_policy import DailyPolicySnapshot, policy_tables_hash
    from kamandal_v2.strategy_lanes.operator_policy import OperatorPolicyBundle
    from kamandal_v2.strategy_lanes.management_runtime import run_live_lifecycle_management
    from kamandal_v2.live.execution import _adopt_csa_live_fill, _source_route_blocker
    from test_observed_package_planning import _batch, _migrated_store
    source = package()
    sources = [{'source_id': s, 'output_kind': k, 'mode': 'live' if k == 'exact_package' else 'off',
                'live_structures': 'split_call_fly' if k == 'exact_package' else ''}
               for s in ('greg_harmon', 'mike_butler') for k in ('idea', 'exact_package')]
    tables = {'universe': [], 'playbooks': [row()], 'trade_sources': sources}
    snapshot = DailyPolicySnapshot(NOW[:10], NOW, policy_tables_hash(tables), tables, tmp_path/'policy.json',
                                   OperatorPolicyBundle((), (), (), NOW, source='fixture'))
    control = load_control(); control['portfolio']['sleeves_source'] = ''; control['runtime']['observed_at'] = NOW
    control['risk_manager']['enabled'] = False
    market = SimpleNamespace(chain_snapshot=lambda *_a, **_k: chain(), account_state=lambda: PortfolioState(100000, 100000, 0, 0),
        preflight=lambda c: PreflightResult(True, 410, 'fixture', {'broker_bpr_provided': True, 'response': {'buyingPowerRequirement': 410}}),
        iv_percentile=lambda _: 30, iv_rank=lambda _: 30, iv_abs=lambda _: .3, event_status=lambda _: 'unknown')
    monkeypatch.setattr(planning, '_market_provider', lambda *_a, **_k: market)
    store = _migrated_store(tmp_path)
    result = planning.run_unified_books(control, universe_rows=[], playbook_rows=[row()], idea_paths=[], provider='fixture',
        store=store, audit_root=tmp_path/'audit', daily_policy_snapshot=snapshot, trade_source_rows=sources,
        observed_package_batches=(replace(_batch(), source_profile='greg_harmon', packages=(source,)),))
    assert result.compilation.ok and not result.live.errors
    assert len(result.live.handoffs) == 1, [(c.rejection_reason,c.reasons) for c in result.live.result.candidates]
    ticket, = store.live_order_intents_by_type('open')
    assert len(ticket['legs']) == 4 and ticket['structure'] == 'split_call_fly'
    assert _source_route_blocker(ticket, sources) == ''
    adopted = _adopt_csa_live_fill(store, ticket, {'averagePrice': '4.00', 'filledAt': NOW})
    assert adopted['status'] == 'open'
    managed = run_live_lifecycle_management(control, sqlite_path=str(store.sqlite_path), provider='fixture', tables=tables,
                                            market=market, observed_at=NOW)
    assert managed.ok, managed.errors
    assert managed.selected_actions == {'hold': 1}
    due_at = '2026-10-23T14:00:00Z'
    market.chain_snapshot = lambda *_a, **_k: replace(chain(), captured_at=due_at)
    due = run_live_lifecycle_management(control, sqlite_path=str(store.sqlite_path), provider='fixture', tables=tables,
                                        market=market, observed_at=due_at)
    assert due.ok, due.errors
    assert due.selected_actions == {'close': 1}
    exits = store.live_order_intents_by_type('close')
    assert len(exits) == 1 and len(exits[0]['legs']) == 4


def test_sheet_change_is_scoped_bounded_and_idempotent():
    from scripts.apply_split_call_fly_sheet import proposed_tables
    from kamandal_v2.strategy_engine.policy import compile_playbook_policies
    template = row(); template.update(playbook_id='guru_exact_call_calendar', strategy_family='call_calendar', structure='call_calendar', max_contracts='1')
    tables = {'playbooks': [template, {}], 'universe': [{'symbol': 'QQQ'}],
              'trade_sources': [{'source_id': s, 'output_kind': 'exact_package', 'mode': 'off', 'live_structures': 'call_spread'} for s in ('greg_harmon','mike_butler')],
              'portfolio_sleeves': [{'lane': 'guru_exact', 'max_bpr_pct': '40'}]}
    before = deepcopy(tables)
    proposed = proposed_tables(tables)
    assert tables == before and proposed_tables(proposed) == proposed
    assert proposed['playbooks'][0] == tables['playbooks'][0]
    assert proposed['trade_sources'][1] == tables['trade_sources'][1]
    assert proposed['trade_sources'][0]['mode'] == 'off'
    assert proposed['universe'] == tables['universe'] and proposed['portfolio_sleeves'] == tables['portfolio_sleeves']
    assert compile_playbook_policies(proposed['playbooks']).ok
