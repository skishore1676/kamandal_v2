from copy import deepcopy
from dataclasses import replace

import pytest

from kamandal_v2.domain.models import PreflightResult
from kamandal_v2.live.execution import _repriced_open_ticket, _reprice_live_entry_order, _preflight_with_entry_pricing
from kamandal_v2.live.orders import _accepted_preflight_limit_price
from kamandal_v2.live.pricing import entry_campaign
from kamandal_v2.stores.sqlite import LocalStore
from tests.test_entry_pricing_plan_fallback import _campaign_config, _credit_candidate
from tests.test_tastytrade_adapter import _adapter


@pytest.mark.parametrize("side", ["credit", "debit"])
def test_tasty_uses_shared_frozen_campaign_for_preflight_and_replacements(tmp_path, side):
    candidate = _credit_candidate()
    if side == "debit":
        candidate = replace(candidate, net_credit=-1.0)
    adapter = _adapter(tmp_path)
    config = _campaign_config(absolute_allowance_cap=.10)
    adapter._config.update(config)
    prices = list(entry_campaign(candidate, config).prices)
    assert len(prices) == 3
    candidate.preflight = adapter.preflight(candidate)
    assert candidate.preflight.ok
    metadata = candidate.preflight.raw["entry_pricing"]
    assert metadata["campaign"]["prices"] == prices
    assert _accepted_preflight_limit_price(candidate) == prices[0]
    assert candidate.preflight.raw["request"]["price"] == f"{abs(float(prices[0])):.2f}"
    assert candidate.preflight.raw["request"]["price-effect"] == side.capitalize()
    root = {"order_id": "root", "ticket_hash": "root", "limit_price": prices[0],
            "preflight": candidate.preflight.to_dict(), "intent_type": "open", "legs": [],
            "submit_payload": {"limitPrice": prices[0]}}
    midpoint = _repriced_open_ticket(root, config)
    midpoint["preflight"] = _preflight_with_entry_pricing(PreflightResult(True, 400, "fresh", {}).to_dict(), midpoint)
    terminal = _repriced_open_ticket(midpoint, config)
    assert [root["limit_price"], midpoint["limit_price"], terminal["limit_price"]] == prices
    assert terminal["preflight"]["raw"]["entry_pricing"] == metadata
    assert abs(abs(float(prices[-1])) - 1) <= .10
    assert terminal["submit_payload"]["limitPrice"] == prices[-1]
    with pytest.raises(ValueError, match="no valid next price"):
        _repriced_open_ticket(terminal, config)


def test_missing_economic_bound_blocks_before_tasty_dry_run(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._config.update(_campaign_config(absolute_allowance_cap=.10))
    candidate = replace(_credit_candidate(), entry_credit_floor=None, entry_economic_bound_source="")
    result = adapter.preflight(candidate)
    assert not result.ok
    assert "economic_bound_missing" in result.message
    assert adapter._session.requests == []


@pytest.mark.parametrize("metadata,current,reason", [
    ({}, "-5.40", "metadata_missing"),
    ({"side": "credit", "base_mid_limit": 5.4, "improved_limit": 5.4}, "-5.40", "no_price_change"),
    ({"side": "credit", "campaign": {"enabled": True, "prices": ["-5.40", "-5.50"]}}, "-5.40", "not_more_executable"),
    ({"side": "credit", "campaign": {"enabled": True, "prices": ["-5.40", "5.30"]}}, "-5.40", "side_changed"),
])
def test_invalid_reprice_never_calls_broker(tmp_path, monkeypatch, metadata, current, reason):
    from kamandal_v2.live import execution
    monkeypatch.setattr(execution, "_assert_submit_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(execution, "submission_window", lambda *_args, **_kwargs: {"allowed": True})
    class NoBrokerCalls:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected broker operation: {name}")
    root = {"order_id": "root", "ticket_hash": "root", "limit_price": current,
            "preflight": {"raw": {"entry_pricing": metadata}}, "intent_type": "open"}
    before = deepcopy(root)
    store = LocalStore(tmp_path / "state.db")
    result = _reprice_live_entry_order(NoBrokerCalls(), store, {}, root, {"status": "WORKING"})
    assert result["reprice_status"] == "skipped"
    assert reason in result["reprice_message"]
    assert root == before
    assert store.latest_event("live_order_reprice_skipped")["reason"] == result["reprice_message"]


@pytest.mark.parametrize("fresh_bpr,expected", [(400, "submitted"), (450, "campaign_preflight_blocked")])
def test_tasty_replacement_rechecks_frozen_risk_budget(tmp_path, monkeypatch, fresh_bpr, expected):
    from types import SimpleNamespace
    from kamandal_v2.live import execution
    from kamandal_v2.live.orders import build_open_ticket
    adapter = _adapter(tmp_path)
    config = _campaign_config(absolute_allowance_cap=.10)
    adapter._config.update(config)
    candidate = replace(_credit_candidate(), structure="short_strangle", execution_venue="tasty_primary")
    candidate.preflight = adapter.preflight(candidate)
    root = build_open_ticket(SimpleNamespace(plan_id="plan", plan_rank=1), candidate)
    root["entry_risk_budget"] = 412.34
    root["broker_order_id"] = "broker-parent"
    store = LocalStore(tmp_path / "state.db")
    store.save_live_order_intent(root, status="submitted")
    calls = []
    def preflight(ticket):
        calls.append(("preflight", ticket["limit_price"]))
        return PreflightResult(True, fresh_bpr, "fresh", {"broker_bpr_provided": True})
    def replace_order(parent, ticket):
        calls.append(("replace", ticket["limit_price"]))
        assert parent == "broker-parent"
        assert ticket["legs"] == root["legs"]
        return {"orderId": "broker-child", "status": "WORKING"}
    monkeypatch.setattr(adapter, "preflight_ticket", preflight)
    monkeypatch.setattr(adapter, "replace_order", replace_order)
    monkeypatch.setattr(execution, "_assert_submit_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(execution, "submission_window", lambda *_args, **_kwargs: {"allowed": True})
    result = _reprice_live_entry_order(adapter, store, config, root, {"status": "WORKING"})
    assert result["reprice_status"] == expected
    prices = root["preflight"]["raw"]["entry_pricing"]["campaign"]["prices"]
    assert calls[0] == ("preflight", prices[1])
    if expected == "submitted":
        assert calls[1:] == [("replace", prices[1])]
        child = store.live_order_intent(result["reprice_ticket_hash"])
        assert child["preflight"]["raw"]["entry_pricing"] == root["preflight"]["raw"]["entry_pricing"]
        assert child["entry_risk_budget"] == root["entry_risk_budget"]
    else:
        assert len(calls) == 1
        assert store.live_order_intent(root["ticket_hash"])["_ledger_status"] == "submitted"
        assert "exceeds_approved_risk_budget" in result["reprice_message"]


def test_csa_open_ticket_preserves_accepted_entry_price_and_metadata(tmp_path):
    from kamandal_v2.live.orders import build_csa_live_ticket
    from kamandal_v2.strategy_lanes.models import LegEffect, LaneId, LegSide, StrategyTicket, TicketLeg
    candidate = _credit_candidate()
    adapter = _adapter(tmp_path)
    adapter._config.update(_campaign_config(absolute_allowance_cap=.10))
    candidate.preflight = adapter.preflight(candidate)
    legs = tuple(TicketLeg(
        instrument_id=f"TSLA  261016C00{int(leg.strike)}000", side=LegSide(leg.side),
        effect=LegEffect.OPEN, quantity=1, option_type=leg.option_type,
        expiration=leg.expiration, strike=leg.strike, role=leg.role,
    ) for leg in candidate.legs)
    ticket = StrategyTicket(
        legs=legs, limit_price=1.0, order_kind="credit", ticket_id="strategy", action_id="action",
        lifecycle_id="lifecycle", lifecycle_version=1, metadata={"execution_venue": "tasty_primary"}, underlying="TSLA",
        lane=LaneId.SHORT_STRANGLE, created_at="2026-09-10T16:00:00Z", policy_hash="policy",
    )
    live = build_csa_live_ticket(ticket, entry_candidate=candidate)
    assert live["limit_price"] == "-1.01"
    assert live["submit_payload"]["limitPrice"] == "-1.01"
    assert live["preflight"] == candidate.preflight.to_dict()
