"""Daily entry capacity follows each sleeve and confirmed broker outcomes."""
import pytest
from kamandal_v2.live import execution
from kamandal_v2.stores.sqlite import LocalStore

START = '2026-10-13 05:00:00'


def ticket(key, sleeve='current_idea', plan=None):
    return dict(ticket_hash=key, order_id=key, plan_id=plan or key, candidate_id=key,
                intent_type='open', underlying='XYZ', sleeve_id=sleeve,
                execution_venue='tasty_primary', created_at='2026-10-13T14:00:00Z')


def attempt(store, t, status='submitted'):
    store.save_live_order_intent(t, status=status)
    store.record_live_order_attempt(t, action='submit_open', submit=True, ok=True,
                                   request_payload={}, response_payload={})
    with store._connect() as conn:
        conn.execute("UPDATE live_order_attempts SET created_at='2026-10-13 14:00:00' WHERE ticket_hash=?", (t['ticket_hash'],))


def terminal(store, t, quantity=0, status='CANCELLED'):
    store.record_live_order_status(t['order_id'], status, {'filledQuantity': quantity}, ticket_hash=t['ticket_hash'])
    store.update_live_order_intent_status(t['ticket_hash'], status.lower())
    with store._connect() as conn:
        conn.execute("UPDATE live_order_status SET created_at='2026-10-13 14:01:00' WHERE ticket_hash=?", (t['ticket_hash'],))


def test_three_per_sleeve_with_same_plan_replacements_and_mixed_basket(tmp_path, monkeypatch):
    s=LocalStore(tmp_path/'state.db');c={'live':{'max_live_baskets_per_day':3}}
    monkeypatch.setattr(execution,'_market_day_start',lambda _:START)
    for n in range(3):attempt(s,ticket(f'idea{n}'))
    attempt(s,ticket('replacement',plan='idea0'))
    assert not execution._daily_basket_cap_allows(c,s,ticket('fourth'))
    assert execution._daily_basket_cap_allows(c,s,ticket('guru','guru_exact'))
    # Same plan ID in another sleeve does not inherit the other sleeve's quota.
    for n in range(3):attempt(s,ticket(f'guru{n}','guru_exact',plan=f'idea{n}'))
    assert not execution._daily_basket_cap_allows(c,s,ticket('fourth-guru','guru_exact'))
    assert execution._daily_basket_cap_allows(c,s,ticket('sibling','guru_exact',plan='idea0'))
    assert len(s.live_entry_plan_ids_since(START,sleeve_id='current_idea'))==3
    assert len(s.live_entry_plan_ids_since(START,sleeve_id='guru_exact'))==3


def test_refund_requires_all_attempts_terminal_and_unfilled(tmp_path):
    s=LocalStore(tmp_path/'state.db');a=ticket('parent');b=ticket('child',plan='parent');b['parent_ticket_hash']='parent'
    attempt(s,a);attempt(s,b);terminal(s,a)
    assert s.live_entry_plan_ids_since(START,sleeve_id='current_idea')=={'parent'}
    terminal(s,b)
    # An unsubmitted sibling does not retain a slot; it must pass the gate anew.
    s.save_live_order_intent(ticket('unsubmitted',plan='parent'),status='stage_approved_pending_submit')
    assert s.live_entry_plan_ids_since(START,sleeve_id='current_idea')==set()


@pytest.mark.parametrize('quantity',[None,'','NaN','inf',-1,'broken',0.5,False])
def test_cancelled_without_affirmative_zero_proof_keeps_slot(tmp_path,quantity):
    s=LocalStore(tmp_path/'state.db');t=ticket('one');attempt(s,t);terminal(s,t,quantity)
    assert s.live_entry_plan_ids_since(START)=={'one'}


@pytest.mark.parametrize('state',['submitted','submit_uncertain','partially_filled','filled','fill_economics_pending','replace_cancel_pending'])
def test_active_or_filled_local_state_cannot_refund(tmp_path,state):
    s=LocalStore(tmp_path/'state.db');t=ticket('one');attempt(s,t)
    terminal(s,t);s.update_live_order_intent_status('one',state)
    assert s.live_entry_plan_ids_since(START)=={'one'}


def test_partial_history_survives_later_zero_terminal_and_closed_projection(tmp_path):
    s=LocalStore(tmp_path/'state.db');t=ticket('one');attempt(s,t)
    s.record_live_order_status('one','WORKING',{'filledQuantity':0.5},ticket_hash='one')
    terminal(s,t)
    assert s.live_entry_plan_ids_since(START)=={'one'}
    s.save_live_position('position','group',dict(plan_id='one',underlying='XYZ',structure='short_strangle',sleeve_id='current_idea'),status='closed')
    assert s.live_entry_plan_ids_since(START)=={'one'}


def test_native_terminal_legs_prove_zero_without_rewriting_receipts(tmp_path):
    s=LocalStore(tmp_path/'state.db');t=ticket('one');attempt(s,t)
    raw={'data':{'order':{'id':'one','status':'Expired','leg-count':2,'legs':[
        {'quantity':1,'remaining-quantity':1,'fills':[]}, {'quantity':1,'remaining-quantity':1,'fills':[]}]}}}
    s.record_live_order_status('one','EXPIRED',{'raw':raw},ticket_hash='one');s.update_live_order_intent_status('one','expired')
    with s._connect() as conn:
        conn.execute("UPDATE live_order_status SET created_at='2026-10-13 14:01:00'")
    before=s.live_order_status_history({'one'})
    assert s.live_entry_plan_ids_since(START)==set()
    assert s.live_order_status_history({'one'})==before


def test_no_broker_receipt_or_later_uncertain_attempt_keeps_slot(tmp_path):
    s=LocalStore(tmp_path/'state.db');t=ticket('one');attempt(s,t,status='cancelled')
    assert s.live_entry_plan_ids_since(START)=={'one'}
    terminal(s,t)
    with s._connect() as c:
        c.execute("UPDATE live_order_status SET created_at='2026-10-13 14:00:00'")
        c.execute("UPDATE live_order_attempts SET created_at='2026-10-13 14:01:00'")
    assert s.live_entry_plan_ids_since(START)=={'one'}


def test_old_fills_do_not_consume_today_and_legacy_position_is_conservative(tmp_path):
    s=LocalStore(tmp_path/'state.db');t=ticket('old');t['created_at']='2026-10-13T04:00:00Z';s.save_live_order_intent(t,status='filled')
    assert s.live_entry_plan_ids_since(START)==set()
    s.save_live_position('legacy','group',dict(plan_id='adopted',underlying='XYZ',structure='short_strangle'))
    with s._connect() as conn:
        conn.execute("UPDATE live_positions SET opened_at='2026-10-13 14:00:00'")
    assert s.live_entry_plan_ids_since(START,sleeve_id='current_idea')=={'adopted'}
    assert not s.live_entry_plan_ids_since(START,sleeve_id='guru_exact')


def test_real_queue_gate_does_not_let_ideas_exhaust_guru(tmp_path, monkeypatch):
    s=LocalStore(tmp_path/'queue.db')
    monkeypatch.setattr(execution,'_market_day_start',lambda _:START)
    for n in range(3):attempt(s,ticket(f'idea{n}'))
    for key,sleeve in [('a-idea','current_idea'),('b-guru','guru_exact')]:
        t=ticket(key,sleeve);t['entry_authority']='automatic_plan' if sleeve=='current_idea' else 'guru_intake'
        s.save_live_order_intent(t,status='stage_approved_pending_submit')
    seen=[]
    def fake_broker_path(config,store,t,**kw):
        seen.append(t['ticket_hash']);attempt(store,t)
        return {'results':[{'status':'submitted'}]}
    monkeypatch.setattr(execution,'_execute_stage_entry',fake_broker_path)
    result=execution._execute_automatic_entries({'live':{'max_live_baskets_per_day':3,'max_live_entry_submits_per_run':1}},s,submit=True)
    assert seen==['b-guru']
    assert result['results'][0]['sleeve_id']=='current_idea'
    assert result['results'][0]['reason']=='max_live_baskets_per_day_reached'
    assert result['submitted']==1


def test_refunded_plan_does_not_bypass_a_subsequently_full_sleeve(tmp_path,monkeypatch):
    s=LocalStore(tmp_path/'refunded.db');monkeypatch.setattr(execution,'_market_day_start',lambda _:START)
    old=ticket('refunded');attempt(s,old);terminal(s,old)
    attempt(s,ticket('new'))
    assert not execution._daily_basket_cap_allows({'live':{'max_live_baskets_per_day':1}},s,old)


def test_filled_guru_position_is_not_charged_to_ideas(tmp_path):
    s=LocalStore(tmp_path/'position.db')
    s.save_live_position('guru','group',dict(plan_id='guru-plan',underlying='XYZ',structure='long_call',sleeve_id='guru_exact'))
    with s._connect() as conn:conn.execute("UPDATE live_positions SET opened_at='2026-10-13 14:00:00'")
    assert s.live_entry_plan_ids_since(START,sleeve_id='guru_exact')=={'guru-plan'}
    assert s.live_entry_plan_ids_since(START,sleeve_id='current_idea')==set()
