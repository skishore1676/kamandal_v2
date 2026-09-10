from datetime import date, timedelta
import pytest
from kamandal_v2 import sheets
from kamandal_v2.live import execution
from kamandal_v2.stores.sqlite import LocalStore

HEADER = ['plan_date', 'plan_rank', 'plan_id', 'mode', 'operator_action', 'operator_notes']

def test_stale_publisher_cannot_erase_current_selection(monkeypatch):
    monkeypatch.setattr(sheets.GoogleSheetClient, 'from_config', lambda c: pytest.fail('stale publication reached Sheet'))
    old = (date.today() - timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match='current-day'):
        sheets._write_daily_plan_locked({}, [[old, 1, 'old', 'live_advisory', '', '']], HEADER, replace_lanes={'live_advisory'})

def test_repeated_publication_preserves_other_lanes_and_compacts_history(monkeypatch):
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=1)).isoformat()
    history = dict(zip(HEADER, [old, 1, 'old', 'live_advisory', '', 'keep operator note']))
    shadow = dict(zip(HEADER, [today, 1, 'shadow', 'shadow', '', '']))
    state = [history, history.copy(), shadow]
    class Client:
        def read_tab(self, title): return state.copy()
        def replace_plan_values(self, title, *, header, rows):
            state[:] = [dict(zip(header, r)) for r in rows]
            return len(rows)
    monkeypatch.setattr(sheets.GoogleSheetClient, 'from_config', lambda c: Client())
    rows = [[today, 1, 'current', 'live_advisory', 'APPROVE_LIVE', '']]
    for _ in range(3):
        sheets._write_daily_plan_locked({}, rows, HEADER, replace_lanes={'live_advisory'})
    assert len(state) == 3
    assert state[0]['operator_notes'] == 'keep operator note'
    assert state[-1]['plan_id'] == 'current'
    assert state[-1]['operator_action'] == 'APPROVE_LIVE'
    assert shadow in state

def test_order_sync_ignores_historical_fallback_campaign_even_if_legacy_flag_enabled(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / 'state.db')
    store.event('live_plan_attempt:old', {'status': 'fallback_ready', 'plan_id': 'old'})
    monkeypatch.setattr(execution, '_sync_live_orders_locked', lambda *a, **kw: {'synced': 0, 'orders': []})
    monkeypatch.setattr(execution, 'write_daily_plan', lambda *a, **kw: pytest.fail('sync published a plan'))
    result = execution.sync_live_orders({'live': {'plan_fallback': {'enabled': True, 'auto_submit': True}}}, store=store)
    assert result == {'synced': 0, 'orders': []}
    assert store.latest_event('live_plan_attempt:old')['status'] == 'fallback_ready'


def test_plan_publication_uses_one_raw_write_and_clears_only_old_tail():
    calls = []
    class Worksheet:
        def get_all_values(self): return [["old"]] * 5
        def clear(self): pytest.fail("must never clear the cockpit before publication")
        def update(self, **kw): calls.append(kw)
        def freeze(self, **kw): pass
    client = object.__new__(sheets.GoogleSheetClient)
    client._worksheet = lambda *a, **kw: Worksheet()
    client._retry = lambda fn, **kw: fn()
    client.replace_plan_values("daily_plan", header=["plan_id", "operator_notes"], rows=[["current", "=literal text"]])
    assert len(calls) == 1
    assert calls[0]["value_input_option"] == "RAW"
    assert calls[0]["values"][1] == ["current", "=literal text"]
    assert calls[0]["values"][2:] == [["", ""]] * 3


def test_normal_daily_cap_counts_broker_effects_not_old_campaigns(tmp_path):
    store = LocalStore(tmp_path / 'state.db')
    store.event('live_plan_attempt:old', {'status': 'fallback_ready', 'attempted_plan_ids': ['old1', 'old2']})
    config = {'live': {'max_live_baskets_per_day': 1}}
    assert execution._daily_basket_cap_allows(config, store, {'plan_id': 'new'})
    ticket = {'ticket_hash': 'uncertain', 'order_id': 'id', 'plan_id': 'admitted', 'candidate_id': 'candidate', 'intent_type': 'open'}
    store.save_live_order_intent(ticket, status='submit_uncertain')
    store.record_live_order_attempt(ticket, action='submit_open', submit=True, ok=False, request_payload={}, response_payload={'error': 'uncertain'})
    assert not execution._daily_basket_cap_allows(config, store, {'plan_id': 'new'})
    assert execution._daily_basket_cap_allows(config, store, {'plan_id': 'admitted'})
