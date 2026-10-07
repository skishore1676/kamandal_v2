from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from kamandal_v2.domain.models import OptionLeg, PortfolioState, PreflightResult
from kamandal_v2.live.advisory import _live_candidate_policy
from kamandal_v2.live.execution import _fresh_entry_preflight_blocker, _repriced_open_ticket
from kamandal_v2.live.orders import build_open_ticket
from kamandal_v2.market.public import PublicAdapter
from kamandal_v2.planner.candidate_builder import _apply_preflight_bpr
from kamandal_v2.stores.sqlite import LocalStore
from tests.test_entry_pricing_plan_fallback import _campaign_config, _credit_candidate


def spx_candidate():
    # October 1 production regression: $520 midpoint, $518.92 first preflight,
    # but $523.92 at the already planned $5.20 midpoint retry.
    return replace(
        _credit_candidate(), underlying="SPX", structure="put_butterfly",
        net_credit=-5.20, estimated_bpr=520, entry_debit_ceiling=12,
        metadata={"input_kind": "exact_package", "source_profile": "mike_butler"},
        legs=[
            OptionLeg("long_lower", "buy", "put", 7300, "2026-10-30", 1, 29.25, 29.1, 29.4, -.1426, 0, 0, 0, 5000, broker_symbol="SPXW261030P07300000"),
            OptionLeg("long_upper", "buy", "put", 7500, "2026-10-30", 1, 56.85, 56.7, 57, -.2688, 0, 0, 0, 3500, broker_symbol="SPXW261030P07500000"),
            OptionLeg("short_body", "sell", "put", 7400, "2026-10-30", 2, 40.45, 40.3, 40.6, -.1955, 0, 0, 0, 3300, broker_symbol="SPXW261030P07400000"),
        ],
    )


def adapter_config():
    config = _campaign_config(absolute_allowance_cap=.10)
    config["live"]["entry_pricing"].update(min_improvement=.10, max_improvement=.10)
    config["broker"] = {"public": {"secret_token": "fixture", "account_id": "fixture"}}
    return config


def test_public_spx_reserves_campaign_with_fees_and_preserves_frozen_ticket(monkeypatch):
    candidate = spx_candidate()
    config = adapter_config()
    adapter = PublicAdapter(config)
    requests = []

    def preflight_only(endpoint, payload):
        assert endpoint.endswith('/preflight/multi-leg')
        requests.append(deepcopy(payload))
        return {"buyingPowerRequirement": round(float(payload["limitPrice"]) * 100 + 3.92, 2)}

    monkeypatch.setattr(adapter, "_post", preflight_only)
    result = adapter.preflight(candidate)
    assert result.ok
    prices = result.raw["entry_pricing"]["campaign"]["prices"]
    assert prices[:2] == ["5.15", "5.20"]
    assert [r['limitPrice'] for r in requests] == prices
    assert len(requests) == 3
    assert all(r['legs'] == requests[0]['legs'] for r in requests)
    assert result.raw['request']['limitPrice'] == '5.15'
    assert result.raw['response']['buyingPowerRequirement'] == 518.92
    assert result.bpr == round(float(prices[-1]) * 100 + 3.92, 2)
    assert result.bpr > 523.92

    candidate.preflight = result
    _apply_preflight_bpr(candidate, result)
    root = build_open_ticket(SimpleNamespace(plan_id='plan', plan_rank=1), candidate)
    assert root['entry_risk_budget'] == result.bpr
    assert root['entry_risk_budget'] < 1200
    midpoint = _repriced_open_ticket(root, config)
    terminal = _repriced_open_ticket(midpoint, config)
    for ticket, price in zip([root, midpoint, terminal], prices):
        assert ticket['entry_risk_budget'] == root['entry_risk_budget']
        assert ticket['legs'] == root['legs']
        fresh = PreflightResult(True, round(float(price)*100+3.92, 2), 'broker', {'broker_bpr_provided': True})
        assert _fresh_entry_preflight_blocker(ticket, fresh) == ''
    increased = PreflightResult(True, result.bpr+1, 'broker', {'broker_bpr_provided': True})
    assert _fresh_entry_preflight_blocker(terminal, increased) == 'fresh_preflight_exceeds_approved_risk_budget'
    legacy = {**root, 'entry_risk_budget': 520}
    assert _fresh_entry_preflight_blocker(legacy, PreflightResult(True, 523.92, 'broker', {'broker_bpr_provided': True})) == 'fresh_preflight_exceeds_approved_risk_budget'


@pytest.mark.parametrize('failure', ['missing', 'zero', 'nonfinite', 'rejected'])
def test_public_campaign_requires_each_step_to_have_broker_risk(monkeypatch, failure):
    adapter = PublicAdapter(adapter_config())
    calls = []

    def preflight(endpoint, payload):
        calls.append(payload['limitPrice'])
        if len(calls) == 3:
            if failure == 'rejected':
                raise RuntimeError('insufficient buying power')
            return {'missing': {}, 'zero': {'buyingPowerRequirement': 0}, 'nonfinite': {'buyingPowerRequirement': float('nan')}}[failure]
        return {'buyingPowerRequirement': float(payload['limitPrice'])*100+3.92}

    monkeypatch.setattr(adapter, '_post', preflight)
    result = adapter.preflight(spx_candidate())
    assert not result.ok
    assert len(calls) == 3


def test_public_credit_campaign_reserves_lower_credit_and_fees(monkeypatch):
    adapter = PublicAdapter(adapter_config())
    candidate = _credit_candidate()
    monkeypatch.setattr(adapter, '_post', lambda _endpoint, payload: {
        'buyingPowerRequirement': round(float(payload['limitPrice'])*100 + .09, 2),
    })
    result = adapter.preflight(candidate)
    assert result.ok
    last = abs(float(result.raw['entry_pricing']['campaign']['prices'][-1]))
    assert result.bpr == round(500 - last*100 + .09, 2)
    assert result.bpr > candidate.estimated_bpr


def test_campaign_reservation_still_obeys_sheet_package_cap(monkeypatch, tmp_path):
    candidate = replace(_credit_candidate(), reasons=['live_max_bpr_per_order=400'])
    config = adapter_config()
    adapter = PublicAdapter(config)
    monkeypatch.setattr(adapter, '_post', lambda _endpoint, payload: {
        'buyingPowerRequirement': round(float(payload['limitPrice'])*100 + .09, 2),
    })
    candidate.preflight = adapter.preflight(candidate)
    _apply_preflight_bpr(candidate, candidate.preflight)
    assert candidate.estimated_bpr == 405.09
    _live_candidate_policy([candidate], LocalStore(tmp_path/'state.db'), config,
                           PortfolioState(account_size=10000, buying_power=10000, bpr_used=0, positions_count=0))
    assert candidate.rejection_reason == 'live_bpr_above_max:405.09>400.0'


def test_public_terminal_tick_rejection_rechecks_normalized_campaign(monkeypatch):
    config = adapter_config()
    config['live']['entry_pricing']['campaign']['absolute_allowance_cap'] = .04
    adapter = PublicAdapter(config)
    calls = []

    def preflight(endpoint, payload):
        price = float(payload['limitPrice'])
        calls.append(payload['limitPrice'])
        if abs(price / .05 - round(price / .05)) > 1e-8:
            raise RuntimeError('limit price must be in increments of $0.05')
        return {'buyingPowerRequirement': round(price*100+3.92, 2)}

    monkeypatch.setattr(adapter, '_post', preflight)
    candidate = spx_candidate()
    result = adapter.preflight(candidate)
    assert result.ok
    campaign = result.raw['entry_pricing']['campaign']
    assert campaign['prices'] == ['5.15', '5.20']
    assert calls[-2:] == ['5.15', '5.20']
    assert result.bpr == 523.92
    assert result.raw['request']['limitPrice'] == '5.15'
    assert [check['limit_price'] for check in result.raw['campaign_bpr_checks']] == campaign['prices']


def test_subtick_campaign_preflights_both_prices_and_stops_at_midpoint(monkeypatch):
    candidate = spx_candidate()
    candidate.net_credit = -4.285
    candidate.estimated_bpr = 428.5
    config = adapter_config()
    config['live']['entry_pricing'].update(min_improvement=.013, max_improvement=.013)
    adapter = PublicAdapter(config)
    requests = []

    def preflight(endpoint, payload):
        assert endpoint.endswith('/preflight/multi-leg')
        requests.append(deepcopy(payload))
        return {'buyingPowerRequirement': round(float(payload['limitPrice'])*100+3.92, 2)}

    monkeypatch.setattr(adapter, '_post', preflight)
    result = adapter.preflight(candidate)
    assert result.ok
    assert [r['limitPrice'] for r in requests] == ['4.27', '4.28']
    assert result.bpr == 431.92
    candidate.preflight = result
    _apply_preflight_bpr(candidate, result)
    root = build_open_ticket(SimpleNamespace(plan_id='plan', plan_rank=1), candidate)
    midpoint = _repriced_open_ticket(root, config)
    assert midpoint['limit_price'] == '4.28'
    assert midpoint['entry_risk_budget'] == root['entry_risk_budget'] == 431.92
    assert midpoint['legs'] == root['legs']
    with pytest.raises(ValueError, match='no valid next price'):
        _repriced_open_ticket(midpoint, config)
