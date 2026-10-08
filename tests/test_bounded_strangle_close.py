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


def test_two_fresh_similar_quotes_stage_one_joint_bounded_close(tmp_path):
    local, store = seed(tmp_path)
    first = run(local, cheap())
    assert first.ok and first.live_intent_count == 0
    mark = local.latest_canonical_live_lifecycle_mark('ms-group')
    assert mark['selected_action_type'] == 'close'
    assert mark['quote_blockers'] == ['bounded_close_awaiting_confirmation']
    # Re-reading a cached snapshot cannot count as a second observation.
    run(local, cheap(), now='2026-10-07T18:41:00Z')
    assert store.lifecycle('ms-lifecycle').metadata['bounded_close_confirmation']['confirmations'] == 1
    second = run(local, cheap('2026-10-07T18:45:00Z', 0.21))
    assert second.ok and second.live_intent_count == 1
    ticket = local.live_order_intents_by_type('close')[0]
    receipt = ticket['bounded_close_confirmation']
    assert receipt['confirmations'] == 2
    assert receipt['zero_bid_buyback_dollars'] == pytest.approx(42)
    assert receipt['package_concession_dollars'] == pytest.approx(24)
    assert ticket['exit_natural_net'] == pytest.approx(-45)
    assert all(leg['side'] == 'buy' and leg['effect'] == 'close' for leg in ticket['legs'])
    assert len(ticket['legs']) == 2
    # Existing profit floor may keep the order inside natural; never beyond it.
    assert float(_repriced_close_limit_price(ticket, CONFIG)) <= 0.45


@pytest.mark.parametrize('middle', ['changed', 'stale', 'invalid', 'gap', 'version', 'older'])
def test_confirmation_resets_on_broken_evidence(tmp_path, middle):
    local, store = seed(tmp_path)
    run(local, cheap())
    snap = cheap('2026-10-07T18:45:00Z')
    if middle == 'changed':
        snap.quotes[0].ask, snap.quotes[1].ask = 0.24, 0.16
    elif middle == 'stale':
        run(local, cheap(), now='2026-10-07T19:00:00Z')
    elif middle == 'invalid':
        bad = cheap('2026-10-07T18:42:00Z'); bad.quotes[0].ask = float('nan')
        run(local, bad)
    elif middle == 'gap':
        snap.captured_at = '2026-10-07T18:55:00Z'
    elif middle == 'version':
        lc = store.lifecycle('ms-lifecycle'); store.save_lifecycle(replace(lc, version=lc.version + 1))
    elif middle == 'older':
        snap.captured_at = '2026-10-07T18:39:00Z'
    result = run(local, snap)
    assert result.ok, result.errors
    assert result.live_intent_count == 0
    assert store.lifecycle('ms-lifecycle').metadata['bounded_close_confirmation']['confirmations'] == 1


@pytest.mark.parametrize('case', ['budget', 'quantity', 'positive_wide', 'invalid'])
def test_persistence_cannot_override_bounds_or_bad_quotes(tmp_path, case):
    local, store = seed(tmp_path)
    if case == 'quantity':
        lc = store.lifecycle('ms-lifecycle')
        store.save_lifecycle(replace(lc, active_legs=tuple({**leg, 'quantity': 2} for leg in lc.active_legs)))
    for stamp in ['2026-10-07T18:40:00Z', '2026-10-07T18:45:00Z']:
        snap = cheap(stamp, 0.30 if case == 'budget' else 0.20)
        if case == 'positive_wide': snap.quotes[0].bid = 0.01
        if case == 'invalid': snap.quotes[0].bid = -0.01
        result = run(local, snap)
        assert result.ok and result.live_intent_count == 0
    if case != 'invalid':
        mark = local.latest_canonical_live_lifecycle_mark('ms-group')
        events = []
        _collect_mark_events(_mark_overview('ms-group', mark, config=CONFIG, now=datetime(2026,10,7,18,45,tzinfo=UTC)), events, config=CONFIG)
        assert any(e['reason'] == 'bounded_close_action_needed' and e['operator_state'] == 'operator_needed' for e in events)


def test_ordinary_valid_zero_bid_exit_does_not_wait(tmp_path):
    local, _ = seed(tmp_path)
    result = run(local, snapshot('2026-10-07T18:40:00Z'))
    assert result.ok and result.live_intent_count == 1


def test_unconfirmed_close_escalates_in_existing_health(tmp_path):
    local, _ = seed(tmp_path)
    run(local, cheap())
    mark = local.latest_canonical_live_lifecycle_mark('ms-group')
    events = []
    _collect_mark_events(_mark_overview('ms-group', mark, config=CONFIG, now=datetime(2026,10,7,20,0,tzinfo=UTC)), events, config=CONFIG)
    assert any(e['reason'] == 'bounded_close_action_needed' for e in events)


def test_event_exit_adds_no_confirmation_wait(tmp_path):
    from kamandal_v2.events.earnings import EarningsStore, EarningsSnapshot
    local, _ = seed(tmp_path)
    EarningsStore(local.sqlite_path).save(EarningsSnapshot(symbol='MS', fetched_date='2026-10-07', next_earnings_date='2026-10-08', source='fixture', confirmed=True))
    result = run(local, cheap())
    assert result.ok and result.live_intent_count == 1
    ticket = local.live_order_intents_by_type('close')[0]
    assert ticket['exit_reason'] == 'mandatory_event_exit'
    assert ticket['bounded_close_confirmation']['status'] == 'urgent_admission'
    assert ticket['bounded_close_confirmation']['confirmations'] == 1


def test_very_cheap_close_uses_valid_tick_with_rounding_in_budget(tmp_path):
    local, _ = seed(tmp_path)
    run(local, cheap(ask=0.01))
    result = run(local, cheap('2026-10-07T18:45:00Z', ask=0.01))
    assert result.ok and result.live_intent_count == 1
    ticket = local.live_order_intents_by_type('close')[0]
    assert ticket['limit_price'] == '0.05'
    assert ticket['exit_natural_net'] == -5
    assert ticket['bounded_close_confirmation']['package_concession_dollars'] == 4
    assert _repriced_close_limit_price(ticket, CONFIG) == '0.05'


def test_all_reprice_steps_respect_frozen_dollar_ceiling(tmp_path):
    local, _ = seed(tmp_path)
    run(local, cheap())
    run(local, cheap('2026-10-07T18:45:00Z', ask=0.21))
    ticket = local.live_order_intents_by_type('close')[0]
    for attempt in range(2):
        price = _repriced_close_limit_price({**ticket, 'reprice_attempt': attempt}, CONFIG)
        assert float(price) <= 0.45
        assert float(price) * 20 == pytest.approx(round(float(price) * 20))
        ticket['limit_price'] = price
    assert ticket['limit_price'] == '0.45'
    with pytest.raises(ValueError, match='boundary_invalid'):
        _repriced_close_limit_price({**ticket, 'exit_natural_net': -100}, CONFIG)


def test_adverse_exit_keeps_existing_loss_debounce_without_new_quote_wait(tmp_path):
    local, store = seed(tmp_path)
    lc = store.lifecycle('ms-lifecycle')
    store.save_lifecycle(replace(lc, cashflow_ledger=({'amount': 0.05, 'filled_at': lc.opened_at},), metadata={**lc.metadata, 'cumulative_cashflow': 0.05}))
    result = run(local, cheap('2026-09-30T18:40:00Z'))
    assert result.ok and result.live_intent_count == 0
    lc = store.lifecycle('ms-lifecycle')
    assert lc.metadata['bounded_close_confirmation']['status'] == 'urgent_admission'
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
    assert second['limit_price'] == '0.45'
    assert second['legs'] == ticket['legs']
    assert second['submit_payload']['limitPrice'] == '0.45'
