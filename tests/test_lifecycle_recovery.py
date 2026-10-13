"""Recovery acceptance across observations, ledger, broker effects and fills."""
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from kamandal_v2.domain.models import PreflightResult
from kamandal_v2.live import execution
from kamandal_v2.live.orders import ticket_hash
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.management_runtime import run_live_lifecycle_management
from kamandal_v2.strategy_lanes.models import LaneId
from test_resting_profit_orders import _RuntimeMarket, _runtime_store
from test_strangle_management_repair import seed, snapshot, CONFIG

LATE = "2026-10-12T20:00:00Z"
NEXT = "2026-10-13T14:00:00Z"


def close_fixture(tmp_path):
    database, typed = _runtime_store(tmp_path, "live")
    local = LocalStore(database)
    result = run_live_lifecycle_management({}, sqlite_path=database, market=_RuntimeMarket(LATE), observed_at=LATE)
    assert result.ok, result.errors
    ticket, = local.live_order_intents_by_status({"stage_approved_pending_submit"})
    return local, typed, ticket


@pytest.mark.parametrize("status", ["deferred_market_closed", "blocked_preflight_stale", "blocked_preflight_failed"])
def test_unsubmitted_close_recovers_same_price_with_fresh_evidence(tmp_path, status):
    local, typed, original = close_fixture(tmp_path)
    local.update_live_order_intent_status(original["ticket_hash"], status)
    result = run_live_lifecycle_management({}, sqlite_path=str(local.sqlite_path), market=_RuntimeMarket(NEXT), observed_at=NEXT)
    queued, = execution._staged_lifecycle_management_tickets(local, {})
    assert result.ok and result.live_intent_count == 1
    assert queued["ticket_hash"] == original["ticket_hash"] == ticket_hash(queued)
    assert queued["created_at"] == NEXT
    assert queued["decision_observation_id"] != original["decision_observation_id"]
    assert queued["limit_price"] == original["limit_price"]
    assert typed.lifecycle("life").policy_hash == "policy"
    # A replay/restart with the already queued commitment creates nothing else.
    repeat = run_live_lifecycle_management({}, sqlite_path=str(local.sqlite_path), market=_RuntimeMarket(NEXT), observed_at=NEXT)
    assert repeat.live_intent_count == 0
    assert len(execution._staged_lifecycle_management_tickets(local, {})) == 1


@pytest.mark.parametrize("broker_status", ["CANCELLED", "REJECTED", "EXPIRED"])
def test_terminal_unfilled_close_has_new_client_identity_once(tmp_path, broker_status):
    local, typed, original = close_fixture(tmp_path)
    local.record_live_order_status(original["order_id"], broker_status,
                                   {"status": broker_status, "filledQuantity": "0"}, ticket_hash=original["ticket_hash"])
    local.update_live_order_intent_status(original["ticket_hash"], broker_status.lower())
    assert local.stage_lifecycle_close({**original, "created_at": NEXT})
    recovered, = execution._staged_lifecycle_management_tickets(local, {})
    assert recovered["order_id"] != original["order_id"]
    assert recovered["ticket_hash"] == ticket_hash(recovered)
    assert recovered["csa_policy_hash"] == original["csa_policy_hash"]
    assert recovered["legs"] == original["legs"]
    assert not local.stage_lifecycle_close({**original, "created_at": NEXT})
    # A later local-only preview failure refreshes that recovered identity.
    local.update_live_order_intent_status(recovered["ticket_hash"], "blocked_preflight_failed")
    assert local.stage_lifecycle_close({**original, "created_at": NEXT})
    refreshed, = execution._staged_lifecycle_management_tickets(local, {})
    assert refreshed["ticket_hash"] == recovered["ticket_hash"] == ticket_hash(refreshed)


@pytest.mark.parametrize("status", ["submit_uncertain", "partially_filled", "fill_economics_pending", "submitted", "replace_waiting_cancel"])
def test_unknown_or_working_close_is_never_replayed(tmp_path, status):
    local, _, ticket = close_fixture(tmp_path)
    local.update_live_order_intent_status(ticket["ticket_hash"], status)
    assert not local.stage_lifecycle_close({**ticket, "created_at": NEXT})
    assert local.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == status


def test_terminal_partial_fill_and_unconfirmed_cancel_do_not_authorize_recovery(tmp_path):
    local, _, ticket = close_fixture(tmp_path)
    local.record_live_order_status(ticket["order_id"], "CANCELLED", {"filledQuantity": "0.5"}, ticket_hash=ticket["ticket_hash"])
    local.update_live_order_intent_status(ticket["ticket_hash"], "cancelled")
    assert not local.stage_lifecycle_close(ticket)
    with local._connect() as conn:
        conn.execute("DELETE FROM live_order_status")  # Local fixture only.
    local.record_live_order_status(ticket["order_id"], "CLOSE_EXPIRE_CANCEL_REQUESTED", {}, ticket_hash=ticket["ticket_hash"])
    local.update_live_order_intent_status(ticket["ticket_hash"], "expired_eod")
    assert not local.stage_lifecycle_close(ticket)


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-1", "unresolved"])
def test_invalid_terminal_fill_quantity_cannot_authorize_close_replay(tmp_path, quantity):
    local, _, ticket = close_fixture(tmp_path)
    local.record_live_order_status(ticket["order_id"], "CANCELLED", {"filledQuantity": quantity}, ticket_hash=ticket["ticket_hash"])
    local.update_live_order_intent_status(ticket["ticket_hash"], "cancelled")
    assert not local.stage_lifecycle_close(ticket)


def test_eod_cancel_request_is_confirmed_before_same_price_close_recovery(tmp_path, monkeypatch):
    local, _, ticket = close_fixture(tmp_path)
    local.update_live_order_intent_status(ticket["ticket_hash"], "expired_eod")
    local.record_live_order_status(ticket["order_id"], "CLOSE_EXPIRE_CANCEL_REQUESTED", {}, ticket_hash=ticket["ticket_hash"])
    assert not local.stage_lifecycle_close(ticket)
    broker = SimpleNamespace(get_order=lambda _: {"status": "CANCELED", "filledQuantity": 0})
    monkeypatch.setattr(execution, "broker_adapter", lambda _: broker)
    result = execution.sync_live_orders({}, store=local, manage_entries=False)
    assert result["synced"] == 1
    assert local.stage_lifecycle_close(ticket)
    recovered, = execution._staged_lifecycle_management_tickets(local, {})
    assert recovered["order_id"] != ticket["order_id"]


def test_typed_position_projection_keeps_missing_economics_unknown():
    snapshot = execution._entry_snapshot_from_lineage(
        {"csa_lifecycle_id": "life", "limit_price": -1, "order_id": "o", "ticket_hash": "h"},
        {"status": "FILLED"}, execution_fills=[{"filled_quantity": 1, "average_price": None, "net_credit": 1}])
    assert snapshot["economics_status"] == "pending"
    assert snapshot["entry_value"] is None and snapshot["entry_net_cashflow"] is None


@pytest.mark.parametrize("missing", ["price", "time", "nonfinite_price", "invalid_time"])
def test_fill_economics_wait_for_broker_evidence_then_commit_once(tmp_path, missing, monkeypatch):
    local, typed, ticket = close_fixture(tmp_path)
    response = {"status": "FILLED", "filledQuantity": "1", "averagePrice": "1.25", "filledAt": NEXT}
    if missing == "price": response.pop("averagePrice")
    elif missing == "time": response.pop("filledAt")
    elif missing == "nonfinite_price": response["averagePrice"] = "NaN"
    else: response["filledAt"] = "unresolved"
    deferred = execution._adopt_csa_live_fill(local, ticket, response, filled_quantity=1)
    assert deferred["economics_pending"]
    assert typed.lifecycle("life").status == "open"
    assert len(typed.lifecycle("life").cashflow_ledger) == 1
    assert local.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "fill_economics_pending"
    assert not local.stage_lifecycle_close(ticket)
    complete = {"status": "FILLED", "filledQuantity": "1", "averagePrice": "1.25", "filledAt": NEXT}
    monkeypatch.setattr(execution, "broker_adapter", lambda *a, **k: SimpleNamespace(get_order=lambda _: complete))
    synced = execution.sync_live_orders({}, store=local, manage_entries=False)
    assert synced["synced"] == 1
    assert typed.lifecycle("life").status == "closed"
    assert typed.lifecycle("life").cashflow_ledger[-1]["amount"] == pytest.approx(-1.25)
    assert execution._adopt_csa_live_fill(local, ticket, complete)["idempotent"]
    assert len(typed.lifecycle("life").cashflow_ledger) == 2


def test_earnings_loss_uses_frozen_policy_and_shared_confirmation(tmp_path):
    database, typed = _runtime_store(tmp_path, "live")
    lifecycle = typed.lifecycle("life")
    raw = dict(lifecycle.metadata["compiled_management_policy"])
    raw["lane"] = "earnings_calendar"
    raw["resolved_fields"] = {**raw["resolved_fields"], "max_loss_multiple": .05}
    typed.save_lifecycle(replace(lifecycle, lane=LaneId.EARNINGS_CALENDAR,
        metadata={**lifecycle.metadata, "compiled_management_policy": raw, "cumulative_cashflow": -1.5},
        cashflow_ledger=({"amount": -1.5, "filled_at": "2026-08-20T15:00:00Z"},)))
    first = "2026-08-28T16:00:00Z"
    second = "2026-08-28T16:05:00Z"
    for stamp in [first, second]:
        result = run_live_lifecycle_management({}, sqlite_path=database, market=_RuntimeMarket(stamp), observed_at=stamp)
        assert result.ok, result.errors
    ticket, = LocalStore(database).live_order_intents_by_type("close")
    assert ticket["exit_reason"] == "earnings_loss_exit"
    assert ticket["csa_action_reason_class"] == "adverse_price_loss"
    assert len(ticket["legs"]) == 2


def test_tasty_strangle_deferred_close_runs_bounded_ladder_and_real_fill_adoption(tmp_path, monkeypatch):
    from kamandal_v2.market.tastytrade import TastytradeAdapter
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", execution.LIVE_SUBMIT_CONFIRM)
    local, typed = seed(tmp_path)
    # Existing frozen strangle policy, time exit; zero bid on the short call.
    first = run_live_lifecycle_management(CONFIG, sqlite_path=str(local.sqlite_path),
        market=SimpleNamespace(chain_snapshot=lambda _: snapshot(LATE)), observed_at=LATE)
    assert first.ok, first.errors
    original, = local.live_order_intents_by_type("close")
    execution._defer_ticket_for_window(local, original, {"reason": "close_cutoff_reached", "intent_type": "close", "allowed": False})
    second = run_live_lifecycle_management(CONFIG, sqlite_path=str(local.sqlite_path),
        market=SimpleNamespace(chain_snapshot=lambda _: snapshot(NEXT)), observed_at=NEXT)
    assert second.ok
    ticket, = execution._staged_lifecycle_management_tickets(local, CONFIG)
    clock = [datetime.fromisoformat(NEXT)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return clock[0].astimezone(tz or UTC)
    monkeypatch.setattr(execution, "datetime", Clock)
    real_window = execution.submission_window
    monkeypatch.setattr(execution, "submission_window", lambda c,t,**kw: real_window(c,t,**{**kw,"now":clock[0]}))
    monkeypatch.setattr(execution, "_close_reprice_due", lambda *a: True)
    class Broker:
        def __init__(self): self.posts=[]; self.replacements=[]; self.active={}
        def preflight_ticket(self, t): return PreflightResult(True, 1, "native fixture preview", {})
        def place_order_ticket(self, t):
            # Exercise the real Tasty payload builder, without auth or network.
            payload=TastytradeAdapter({})._order_payload_from_ticket(t)
            assert payload["price-effect"] == "Debit"
            assert all(x["action"] == "Buy to Close" for x in payload["legs"])
            self.posts.append(payload); self.active[t["order_id"]]={"orderId":t["order_id"],"status":"WORKING"}
            return {"orderId":t["order_id"]}
        def supports_atomic_replace(self,t): return True
        def replace_order(self, old, t):
            self.replacements.append(TastytradeAdapter({})._order_payload_from_ticket(t))
            self.active[old]={"status":"CANCELLED","filledQuantity":"0"}
            self.active[t["order_id"]]={"orderId":t["order_id"],"status":"WORKING"}
            return {"orderId":t["order_id"]}
        def get_order(self, oid): return self.active[oid]
    broker=Broker()
    monkeypatch.setattr(execution,"broker_adapter",lambda *a,**k:broker)
    result=execution.execute_live_approved(CONFIG,store=local,submit=True,close=True)
    assert result["results"][0]["status"]=="submitted"
    initial=float(ticket["limit_price"])
    natural=-ticket["exit_natural_net"]/100
    for minutes in [10,20]:
        clock[0]=datetime.fromisoformat(NEXT).replace(minute=minutes)
        execution.sync_live_orders(CONFIG,store=local)
    assert len(broker.posts)==1 and len(broker.replacements)==2
    prices=[float(p["price"]) for p in broker.replacements]
    assert initial <= prices[0] <= prices[1] <= natural+.001
    assert prices[1] == pytest.approx(natural, abs=.05)
    last=next(t for t in local.live_order_intents_by_status({"submitted"}) if t["intent_type"]=="close")
    broker.active[last["order_id"]]={"status":"FILLED","filledQuantity":"1","averagePrice":str(prices[-1]),"filledAt":"2026-10-13T14:21:00Z"}
    execution.sync_live_orders(CONFIG,store=local)
    final=typed.lifecycle("ms-lifecycle")
    assert final.status=="closed" and final.policy_hash=="frozen-ms-policy"
    assert len(final.cashflow_ledger)==2
    assert final.cashflow_ledger[-1]["amount"] == pytest.approx(-prices[-1])


@pytest.mark.parametrize('time_value', [1789567660991, '1789567660991'])
def test_broker_epoch_fill_time_is_real_evidence(tmp_path, time_value):
    local, typed, ticket = close_fixture(tmp_path)
    result = execution._adopt_csa_live_fill(local, ticket,
        {'status': 'FILLED', 'filledQuantity': 1, 'averagePrice': 1.25, 'filledAt': time_value})
    assert result['saved']
    assert typed.lifecycle('life').cashflow_ledger[-1]['filled_at'].startswith('2026-09-16T')


@pytest.mark.parametrize('quantity', [None, ''])
def test_terminal_cancel_without_fill_quantity_is_not_unfilled_proof(tmp_path, quantity):
    local, _, ticket = close_fixture(tmp_path)
    local.record_live_order_status(ticket['order_id'], 'CANCELLED', {'filledQuantity': quantity}, ticket_hash=ticket['ticket_hash'])
    local.update_live_order_intent_status(ticket['ticket_hash'], 'cancelled')
    assert not local.stage_lifecycle_close(ticket)
