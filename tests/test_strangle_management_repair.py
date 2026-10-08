from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from kamandal_v2.domain.models import ChainSnapshot, OptionQuote, PreflightResult
from kamandal_v2.live import execution
from kamandal_v2.live.health import _collect_mark_events, _mark_overview
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.management_runtime import run_live_lifecycle_management
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.strategy_lanes.models import CsaStage, LaneId, LifecycleState, SourceMode
from kamandal_v2.strategy_lanes.policy import CsaPolicy
from kamandal_v2.strategy_lanes.store import CsaStore


EXPIRY = '2026-10-30'
CONFIG = {'runtime': {'mode': 'live', 'trading_enabled': True}, 'live': {'exit_submit_source': 'ledger', 'exit_reprice': {'enabled': True, 'after_minutes': 10, 'max_reprices': 2, 'step_multipliers': [0.5, 1.0], 'expire_after_minutes': 390}}}


def seed(tmp_path):
    db = tmp_path / 'test.db'
    local = LocalStore(db)
    migrate_csa_database(db, dry_run=False, backup_dir=tmp_path / 'backups')
    policy = CsaPolicy('short_strangle_high_iv', LaneId.SHORT_STRANGLE, CsaStage.LIVE, SourceMode.MARKET_SCAN,
        {'lifecycle': {'tested_side_confirmation': 2, 'adjustment_limit': 2, 'cooldown': {'minutes': 30}, 'roll': {'min_credit': 0.1}, 'loss_stages': {'watch_multiple': 2, 'close_multiple': 3}, 'fill': {'max_attempts': 4, 'price_increment': 0.05}}},
        {'max_bid_ask_pct': 0.2, 'profit_target_pct': 40, 'exit_dte_min': 21, 'half_time_exit': True, 'exit_pre_event_days': 5, 'avoid_earnings': True, 'management_delta_target': 0.3, 'management_delta_max': 0.4, 'execution_venue': 'tasty_primary'},
        'frozen-ms-policy', 'fixture', '2026-09-14T17:10:11Z')
    legs = tuple({'role': role, 'side': 'sell', 'effect': 'open', 'option_type': kind, 'strike': strike, 'expiration': EXPIRY, 'quantity': 1} for role, kind, strike in [('short_put', 'put', 190.0), ('short_call', 'call', 235.0)])
    lifecycle = LifecycleState('ms-lifecycle', 'ms-opportunity', LaneId.SHORT_STRANGLE, 2, 'open', legs,
        ({'amount': 5.03, 'filled_at': '2026-09-14T17:10:11Z'},), '2026-09-14T17:10:11Z', '2026-09-14T17:10:11Z', policy.policy_hash,
        {'underlying': 'MS', 'execution_mode': 'live', 'compiled_management_policy': policy.to_dict(), 'cumulative_cashflow': 5.03, 'position_projection_id': 'ms-group', 'execution_venue': 'tasty_primary'})
    store = CsaStore(db)
    store.save_lifecycle(lifecycle)
    return local, store


def snapshot(stamp='2026-09-30T18:47:54Z', *, spot=188.72, replacement=True):
    def q(kind, strike, bid, ask, delta):
        return OptionQuote(underlying='MS', expiration=EXPIRY, option_type=kind, strike=strike, bid=bid, ask=ask, delta=delta, gamma=0, theta=0, vega=0, iv=0.4, open_interest=1000)
    quotes = [q('put', 190, 7.9, 8.3, -0.5), q('call', 235, 0, 0.47, 0.0298)]
    if replacement:
        quotes.append(q('call', 200, 3.0, 3.3, 0.2921))
    return ChainSnapshot('ms-history', 'MS', stamp, spot, quotes, 'retained-fixture')


def run(local, snap, *, now=None):
    return run_live_lifecycle_management(CONFIG, sqlite_path=str(local.sqlite_path), market=SimpleNamespace(chain_snapshot=lambda _: snap), observed_at=now or snap.captured_at)


def confirm(local, **kwargs):
    first = run(local, snapshot(**kwargs))
    assert first.ok, first.errors
    second = run(local, snapshot('2026-09-30T18:52:54Z', **kwargs))
    assert second.ok, second.errors
    return second


def test_ms_zero_bid_call_does_not_disable_put_test_or_roll(tmp_path):
    local, store = seed(tmp_path)
    result = confirm(local)
    assert result.selected_actions == {'adjust': 1}
    assert result.live_intent_count == 1
    lifecycle = store.lifecycle('ms-lifecycle')
    assert lifecycle.metadata['tested_side_confirmations'] == 2
    ticket = local.live_order_intents_by_type('adjust')[0]
    assert [(x['side'], x['effect'], x['strike']) for x in ticket['legs']] == [('buy', 'close', 235), ('sell', 'open', 200)]
    assert ticket['exit_natural_net'] == pytest.approx(253)
    assert ticket['execution_envelope']['minimum_credit'] == 0.1
    mark = local.latest_canonical_live_lifecycle_mark('ms-group')
    assert mark['selected_action_type'] == 'adjust'
    assert mark['execution_quote_scope'] == 'untested_side_replacement'


def test_missing_replacement_is_visible_and_escalates_in_existing_health(tmp_path):
    local, store = seed(tmp_path)
    result = confirm(local, replacement=False)
    assert result.selected_actions == {'adjust': 1}
    assert result.live_intent_count == 0
    lifecycle = store.lifecycle('ms-lifecycle')
    assert lifecycle.metadata['tested_side_confirmations'] == 2
    assert lifecycle.metadata['mark_execution_status'] == 'waiting_valid_quote'
    mark = local.latest_canonical_live_lifecycle_mark('ms-group')
    assert mark['quote_blockers'] == ['adjustment_no_eligible_credit_roll']
    overview = _mark_overview('ms-group', mark, config=CONFIG, now=datetime(2026, 9, 30, 21, 0, tzinfo=UTC))
    events = []
    _collect_mark_events(overview, events, config=CONFIG)
    assert any(e['reason'] == 'adjustment_quote_stalled' and e['operator_state'] == 'operator_needed' for e in events)


def test_deduplicated_snapshot_does_not_count_twice(tmp_path):
    local, store = seed(tmp_path)
    snap = snapshot()
    run(local, snap)
    run(local, snap, now='2026-09-30T18:52:54Z')
    assert store.lifecycle('ms-lifecycle').metadata['tested_side_confirmations'] == 1
    assert not local.live_order_intents_by_type('adjust')


@pytest.mark.parametrize('spot', [float('nan'), 0, -1])
def test_invalid_underlying_cannot_confirm_test(tmp_path, spot):
    local, store = seed(tmp_path)
    result = confirm(local, spot=spot)
    assert result.live_intent_count == 0
    assert store.lifecycle('ms-lifecycle').metadata['tested_side_confirmations'] == 0
    assert store.lifecycle('ms-lifecycle').metadata['mark_selected_reason'] == 'strangle_test_observation_invalid'


def test_stale_quote_cannot_confirm_test(tmp_path):
    local, store = seed(tmp_path)
    result = run(local, snapshot(), now='2026-09-30T19:47:54Z')
    assert result.ok
    assert store.lifecycle('ms-lifecycle').metadata['tested_side_confirmations'] == 0


def test_test_starts_at_strike_not_at_arbitrary_percentage_distance(tmp_path):
    local, store = seed(tmp_path)
    confirm(local, spot=190.01)
    assert store.lifecycle('ms-lifecycle').metadata['tested_side_confirmations'] == 0
    run(local, snapshot('2026-09-30T19:00:00Z', spot=190))
    assert store.lifecycle('ms-lifecycle').metadata['tested_side_confirmations'] == 1


def test_half_time_exit_beats_adjustment_and_accepts_valid_zero_bid_buyback(tmp_path):
    local, store = seed(tmp_path)
    result = run(local, snapshot('2026-10-07T18:47:50Z'))
    assert result.ok, result.errors
    assert result.selected_actions == {'close': 1}
    assert result.live_intent_count == 1
    ticket = local.live_order_intents_by_type('close')[0]
    assert ticket['exit_reason'] == 'half_time_exit'
    assert {(x['side'], x['effect']) for x in ticket['legs']} == {('buy', 'close')}
    assert ticket['exit_natural_net'] == pytest.approx(-877)
    assert not local.live_order_intents_by_type('adjust')


@pytest.mark.parametrize('bid,ask', [(0, 0), (-0.01, 0.27), (0.5, 0.27), (float('nan'), 0.27), (0, float('inf')), (0, 5)])
def test_invalid_or_excessively_wide_close_quotes_still_block(tmp_path, bid, ask):
    local, store = seed(tmp_path)
    snap = snapshot('2026-10-07T18:47:50Z')
    snap.quotes[1].bid, snap.quotes[1].ask = bid, ask
    result = run(local, snap)
    assert result.ok, result.errors
    assert result.live_intent_count == 0
    assert store.lifecycle('ms-lifecycle').metadata['mark_execution_status'] == 'waiting_valid_quote'


def test_untouched_leg_bad_quote_does_not_block_valid_roll(tmp_path):
    local, store = seed(tmp_path)
    for stamp in ['2026-09-30T18:47:54Z', '2026-09-30T18:52:54Z']:
        snap = snapshot(stamp)
        snap.quotes[0].bid, snap.quotes[0].ask = 0, 0
        result = run(local, snap)
        assert result.ok, result.errors
    assert result.live_intent_count == 1
    assert local.live_order_intents_by_type('adjust')


def test_midpoint_credit_is_insufficient_without_natural_minimum(tmp_path):
    local, store = seed(tmp_path)
    for stamp in ['2026-09-30T18:47:54Z', '2026-09-30T18:52:54Z']:
        snap = snapshot(stamp)
        snap.quotes[2].bid, snap.quotes[2].ask = 0.5, 0.55
        result = run(local, snap)
    assert result.live_intent_count == 0
    assert store.lifecycle('ms-lifecycle').metadata['mark_quote_blockers'] == ['adjustment_no_eligible_credit_roll']


def test_adjustment_limit_is_preserved(tmp_path):
    local, store = seed(tmp_path)
    lc = store.lifecycle('ms-lifecycle')
    store.save_lifecycle(replace(lc, metadata={**lc.metadata, 'adjustment_count': 2}))
    result = confirm(local)
    assert result.live_intent_count == 0
    assert store.lifecycle('ms-lifecycle').metadata['mark_selected_reason'] == 'strangle_adjustment_limit_reached'


def test_adjustment_reprices_to_natural_credit_and_never_debit(tmp_path):
    local, _ = seed(tmp_path)
    confirm(local)
    ticket = local.live_order_intents_by_type('adjust')[0]
    first = execution._repriced_close_ticket(ticket, CONFIG)
    second = execution._repriced_close_ticket(first, CONFIG)
    assert float(ticket['limit_price']) < float(first['limit_price']) < float(second['limit_price']) < 0
    assert second['limit_price'] == '-2.53'
    assert second['legs'] == ticket['legs']
    bad = deepcopy(ticket)
    bad['exit_natural_net'] = 9
    with pytest.raises(ValueError, match='credit_boundary'):
        execution._repriced_close_ticket(bad, CONFIG)


def test_working_adjustment_uses_real_sync_reprice_path_with_broker_preflight(tmp_path, monkeypatch):
    local, _ = seed(tmp_path)
    confirm(local)
    ticket = local.live_order_intents_by_type('adjust')[0]
    local.update_live_order_intent_status(ticket['ticket_hash'], 'submitted')
    calls = []

    class Broker:
        def get_order(self, _):
            return {'status': 'WORKING', 'createdAt': ticket['created_at']}
        def preflight_ticket(self, t):
            calls.append(('preflight', t['limit_price']))
            return PreflightResult(True, 100, 'ok', {})
        def replace_order(self, order_id, t):
            calls.append(('replace', t['limit_price']))
            return {'orderId': t['order_id']}

    monkeypatch.setattr(execution, 'broker_adapter', lambda _: Broker())
    monkeypatch.setattr(execution, '_broker_for_ticket', lambda *args: Broker())
    monkeypatch.setattr(execution, 'datetime', type('Clock', (datetime,), {'now': classmethod(lambda cls, tz=None: datetime(2026, 9, 30, 19, 5, tzinfo=UTC))}))
    monkeypatch.setattr('kamandal_v2.live.option_sessions.datetime', execution.datetime)
    monkeypatch.setenv('KAMANDAL_LIVE_SUBMIT_CONFIRM', 'I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS')
    result = execution.sync_live_orders(CONFIG, store=local)
    assert result['orders'][0]['reprice_status'] == 'submitted'
    assert [x[0] for x in calls] == ['preflight', 'replace']
    child = local.live_order_intent(result['orders'][0]['reprice_ticket_hash'])
    assert child['intent_type'] == 'adjust'
    assert child['legs'] == ticket['legs']


def test_adjustment_reprice_cannot_use_exit_only_window(tmp_path, monkeypatch):
    local, _ = seed(tmp_path)
    confirm(local)
    ticket = local.live_order_intents_by_type('adjust')[0]
    monkeypatch.setattr(execution, 'submission_window', lambda config, ticket, close: {'allowed': False, 'reason': 'entry_cutoff_reached'})
    monkeypatch.setenv('KAMANDAL_LIVE_SUBMIT_CONFIRM', 'I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS')
    result = execution._reprice_live_close_order(SimpleNamespace(), local, CONFIG, ticket, {'status': 'WORKING'})
    assert result['reprice_status'] == 'deferred_market_closed'


def test_adjustment_execution_uses_frozen_lifecycle_not_new_entry_source_identity(tmp_path, monkeypatch):
    local, _ = seed(tmp_path)
    confirm(local)
    calls = []
    class Broker:
        def preflight_ticket(self, ticket):
            calls.append('preflight')
            return PreflightResult(True, 100, 'ok', {})
        def place_order_ticket(self, ticket):
            calls.append('submit')
            return {'orderId': ticket['order_id']}
    monkeypatch.setattr(execution, 'broker_adapter', lambda _: Broker())
    monkeypatch.setattr(execution, '_broker_for_ticket', lambda *args: Broker())
    monkeypatch.setattr(execution, 'datetime', type('Clock', (datetime,), {'now': classmethod(lambda cls, tz=None: datetime(2026, 9, 30, 18, 54, tzinfo=UTC))}))
    monkeypatch.setattr('kamandal_v2.live.option_sessions.datetime', execution.datetime)
    monkeypatch.setattr(execution, 'pull_portfolio_sleeves', lambda _: pytest.fail('Management must not be interpreted as a new source opening'))
    monkeypatch.setenv('KAMANDAL_LIVE_SUBMIT_CONFIRM', 'I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS')
    config = deepcopy(CONFIG)
    config['portfolio'] = {'sleeves_source': 'sheet'}
    result = execution.execute_live_approved(config, submit=True, close=True, store=local)
    assert result['results'][0]['status'] == 'submitted'
    assert calls == ['preflight', 'submit']


def test_filled_repriced_adjustment_reconciles_two_shorts_and_consumes_one_episode(tmp_path):
    local, store = seed(tmp_path)
    confirm(local)
    parent = local.live_order_intents_by_type('adjust')[0]
    child = execution._repriced_close_ticket(execution._repriced_close_ticket(parent, CONFIG), CONFIG)
    local.save_live_order_intent(child, status='submitted')
    result = execution._adopt_csa_live_fill(local, child, {'status': 'FILLED', 'averagePrice': '2.53', 'filledQuantity': '1', 'updatedAt': '2026-09-30T19:15:00Z'}, filled_quantity=1)
    lifecycle = store.lifecycle('ms-lifecycle')
    assert result
    assert [(leg['role'], leg['strike']) for leg in lifecycle.active_legs] == [('short_put', 190), ('short_call', 200)]
    assert lifecycle.metadata['adjustment_count'] == 1
    assert lifecycle.metadata['cumulative_cashflow'] == pytest.approx(7.56)
    assert lifecycle.metadata['strangle_test_episode']['consumed'] is True
    # A replay of the same broker fill cannot add another credit or adjustment.
    execution._adopt_csa_live_fill(local, child, {'status': 'FILLED', 'averagePrice': '2.53', 'filledQuantity': '1', 'updatedAt': '2026-09-30T19:15:00Z'}, filled_quantity=1)
    assert store.lifecycle('ms-lifecycle').metadata['adjustment_count'] == 1
