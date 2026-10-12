from dataclasses import replace
from datetime import UTC, datetime

import pytest

from test_strangle_management_repair import seed, snapshot, run, CONFIG
from kamandal_v2.live.health import _collect_mark_events, _mark_overview
from kamandal_v2.live.execution import _repriced_close_limit_price


def cheap(stamp='2026-10-07T18:40:00Z', ask=0.20):
    snap = snapshot(stamp, spot=210, replacement=False)
    for quote in snap.quotes:
        quote.bid, quote.ask = 0, ask
    return snap


def test_one_valid_wide_quote_stages_joint_close_and_replay_does_not_duplicate(tmp_path):
    local, store = seed(tmp_path)
    first = run(local, cheap())
    assert first.ok and first.live_intent_count == 1
    replay = run(local, cheap(), now='2026-10-07T18:41:00Z')
    assert replay.ok and replay.live_intent_count == 0
    ticket = local.live_order_intents_by_type('close')[0]
    assert ticket['exit_natural_net'] == pytest.approx(-40)
    assert len(ticket['legs']) == 2
    assert all(leg['side'] == 'buy' and leg['effect'] == 'close' for leg in ticket['legs'])


@pytest.mark.parametrize('case', ['budget', 'quantity', 'positive_wide', 'invalid', 'stale'])
def test_close_policy_accepts_width_and_size_but_rejects_bad_quotes(tmp_path, case):
    local, store = seed(tmp_path)
    if case == 'quantity':
        lc = store.lifecycle('ms-lifecycle')
        store.save_lifecycle(replace(lc, active_legs=tuple({**leg, 'quantity': 2} for leg in lc.active_legs)))
    snap = cheap(ask=0.30 if case == 'budget' else 0.20)
    if case == 'positive_wide': snap.quotes[0].bid = 0.01
    if case == 'invalid': snap.quotes[0].bid = -0.01
    result = run(local, snap, now='2026-10-07T19:00:00Z' if case == 'stale' else None)
    assert result.ok
    assert result.live_intent_count == (0 if case in {'invalid', 'stale'} else 1)


def test_ordinary_valid_zero_bid_exit_does_not_wait(tmp_path):
    local, _ = seed(tmp_path)
    result = run(local, snapshot('2026-10-07T18:40:00Z'))
    assert result.ok and result.live_intent_count == 1


def test_event_exit_adds_no_confirmation_wait(tmp_path):
    from kamandal_v2.events.earnings import EarningsStore, EarningsSnapshot
    local, _ = seed(tmp_path)
    EarningsStore(local.sqlite_path).save(EarningsSnapshot(symbol='MS', fetched_date='2026-10-07', next_earnings_date='2026-10-08', source='fixture', confirmed=True))
    result = run(local, cheap())
    assert result.ok and result.live_intent_count == 1
    ticket = local.live_order_intents_by_type('close')[0]
    assert ticket['exit_reason'] == 'mandatory_event_exit'
    assert ticket['execution_envelope']['boundary'] == 'natural'


def test_all_reprice_steps_respect_natural_boundary(tmp_path):
    local, _ = seed(tmp_path)
    run(local, cheap())
    ticket = local.live_order_intents_by_type('close')[0]
    for attempt in range(2):
        price = _repriced_close_limit_price({**ticket, 'reprice_attempt': attempt}, CONFIG)
        assert float(price) <= 0.40
        ticket['limit_price'] = price
    assert ticket['limit_price'] == '0.40'


def test_adverse_exit_keeps_existing_loss_debounce_without_new_quote_wait(tmp_path):
    local, store = seed(tmp_path)
    lc = store.lifecycle('ms-lifecycle')
    store.save_lifecycle(replace(lc, cashflow_ledger=({'amount': 0.05, 'filled_at': lc.opened_at},), metadata={**lc.metadata, 'cumulative_cashflow': 0.05}))
    result = run(local, cheap('2026-09-30T18:40:00Z'))
    assert result.ok and result.live_intent_count == 0
    lc = store.lifecycle('ms-lifecycle')
    assert lc.metadata['mark_selected_reason'] == 'loss_watch_debouncing'
    result = run(local, cheap('2026-09-30T18:45:00Z'))
    assert result.ok and result.live_intent_count == 1
    assert local.live_order_intents_by_type('close')[0]['exit_reason'] == 'loss_stage_close'


def test_exception_cannot_override_ownership_or_working_order_conflict(tmp_path):
    local, _ = seed(tmp_path)
    run(local, cheap())
    run(local, cheap('2026-10-07T18:45:00Z'))
    ticket = local.live_order_intents_by_type('close')[0]
    local.update_live_order_intent_status(ticket['ticket_hash'], 'submitted')
    result = run(local, cheap('2026-10-07T18:50:00Z'))
    assert result.ok and result.live_intent_count == 0
    assert result.selected_actions == {'block': 1}


def test_replacement_ticket_retains_budget_and_joint_legs(tmp_path):
    from kamandal_v2.live.execution import _repriced_close_ticket
    local, _ = seed(tmp_path)
    run(local, cheap())
    run(local, cheap('2026-10-07T18:45:00Z', 0.21))
    ticket = local.live_order_intents_by_type('close')[0]
    first = _repriced_close_ticket(ticket, CONFIG)
    second = _repriced_close_ticket(first, CONFIG)
    assert second['bounded_close_confirmation'] == ticket['bounded_close_confirmation']
    assert second['limit_price'] == '0.40'
    assert second['legs'] == ticket['legs']
    assert second['submit_payload']['limitPrice'] == '0.40'


def test_tiny_buyback_preserves_minimum_executable_tick(tmp_path):
    local, _ = seed(tmp_path)
    result = run(local, cheap(ask=0.01))
    assert result.ok and result.live_intent_count == 1
    ticket = local.live_order_intents_by_type('close')[0]
    assert ticket['limit_price'] == '0.05'
    assert ticket['exit_natural_net'] == -5
    assert _repriced_close_limit_price(ticket, CONFIG) == '0.05'
