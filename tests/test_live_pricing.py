from kamandal_v2.config import load_control
from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg
from kamandal_v2.live.pricing import candidate_entry_limit_price, entry_price_metadata
from kamandal_v2.market.public import PublicAdapter


def _long_call_candidate() -> Candidate:
    return Candidate(
        candidate_id="cand_long",
        idea_id="idea",
        underlying="AMZN",
        playbook_id="long_call_directional",
        structure="long_call",
        legs=[
            OptionLeg(
                role="long_call",
                side="buy",
                option_type="call",
                strike=265,
                expiration="2026-08-21",
                quantity=1,
                mid=18.75,
                bid=18.55,
                ask=18.95,
                delta=0.5,
                gamma=0.0,
                theta=0.0,
                vega=0.0,
                open_interest=1000,
            )
        ],
        net_credit=-18.75,
        estimated_bpr=1875,
        greeks=Greeks(),
        liquidity_score=1.0,
        score=1.0,
    )


def _credit_spread_candidate() -> Candidate:
    return Candidate(
        candidate_id="cand_credit",
        idea_id="idea",
        underlying="TSLA",
        playbook_id="call_spread_default",
        structure="call_spread",
        legs=[
            OptionLeg(
                role="short_call",
                side="sell",
                option_type="call",
                strike=200,
                expiration="2026-07-17",
                quantity=1,
                mid=2.00,
                bid=1.95,
                ask=2.05,
                delta=0.25,
                gamma=0.0,
                theta=0.0,
                vega=0.0,
                open_interest=1000,
            ),
            OptionLeg(
                role="long_call",
                side="buy",
                option_type="call",
                strike=205,
                expiration="2026-07-17",
                quantity=1,
                mid=1.00,
                bid=0.95,
                ask=1.05,
                delta=0.15,
                gamma=0.0,
                theta=0.0,
                vega=0.0,
                open_interest=1000,
            ),
        ],
        net_credit=1.00,
        estimated_bpr=400,
        greeks=Greeks(),
        liquidity_score=1.0,
        score=1.0,
    )


def _config() -> dict:
    return {
        "live": {
            "entry_pricing": {
                "mode": "improved_mid",
                "improvement_pct_of_spread": 10,
                "min_improvement": 0.01,
                "max_improvement": 0.10,
                "apply_to_credit": True,
                "apply_to_debit": True,
            }
        },
        "broker": {"public": {}},
    }


def _public_config() -> dict:
    config = _config()
    config["broker"]["public"] = {"secret_token": "test", "account_id": "acct"}
    return config


def test_entry_pricing_improves_debit_buys_below_mid() -> None:
    candidate = _long_call_candidate()

    assert candidate_entry_limit_price(candidate, _config()) == "18.71"
    assert candidate_entry_limit_price(candidate, _config(), nickel=True) == "18.70"

    metadata = entry_price_metadata(candidate, _config())
    assert metadata["side"] == "debit"
    assert metadata["base_mid_limit"] == 18.75
    assert metadata["improved_limit"] == 18.71
    assert metadata["improvement"] == 0.04


def test_entry_pricing_improves_multileg_credits_above_mid() -> None:
    candidate = _credit_spread_candidate()

    assert candidate_entry_limit_price(candidate, _config()) == "-1.02"
    assert candidate_entry_limit_price(candidate, _config(), nickel=True) == "-1.05"


def test_public_order_payload_uses_entry_pricing_policy() -> None:
    adapter = PublicAdapter(_config())
    debit_payload = adapter._order_payload(_long_call_candidate())
    credit_payload = adapter._order_payload(_credit_spread_candidate())

    assert debit_payload["limitPrice"] == "18.71"
    assert credit_payload["limitPrice"] == "-1.02"


def test_liquidity_adjusted_pricing_demands_more_improvement_for_low_oi() -> None:
    candidate = _credit_spread_candidate()
    for leg in candidate.legs:
        leg.open_interest = 0
    config = _config()
    config["live"]["entry_pricing"] = {
        "mode": "liquidity_adjusted_mid",
        "improvement_pct_of_spread": 10,
        "low_oi_improvement_pct_of_spread": 20,
        "very_low_oi_improvement_pct_of_spread": 35,
        "good_oi_threshold": 500,
        "low_oi_threshold": 100,
        "min_improvement": 0.01,
        "max_improvement": 0.10,
        "apply_to_credit": True,
        "apply_to_debit": True,
    }

    assert candidate_entry_limit_price(candidate, config) == "-1.07"
    metadata = entry_price_metadata(candidate, config)
    assert metadata["min_open_interest"] == 0
    assert metadata["improvement_pct_of_spread"] == 35


def test_liquidity_adjusted_pricing_uses_nonlinear_width_improvement() -> None:
    candidate = _credit_spread_candidate()
    candidate.legs[0].bid = 1.00
    candidate.legs[0].ask = 3.00
    candidate.legs[1].bid = 0.50
    candidate.legs[1].ask = 1.50
    config = _config()
    config["live"]["entry_pricing"] = {
        "mode": "liquidity_adjusted_mid",
        "improvement_pct_of_spread": 10,
        "low_oi_improvement_pct_of_spread": 20,
        "very_low_oi_improvement_pct_of_spread": 35,
        "good_oi_threshold": 500,
        "low_oi_threshold": 100,
        "min_improvement": 0.01,
        "max_improvement": 0.10,
        "max_improvement_by_liquidity_tier": {
            "tight": 0.05,
            "normal": 0.10,
            "wide": 0.15,
            "very_wide": 0.25,
            "extreme": 0.35,
        },
        "max_improvement_pct_of_premium": 40,
        "normal_bid_ask_pct": 0.30,
        "width_improvement_max_pct_of_spread": 45,
        "width_improvement_curve": 0.85,
        "apply_to_credit": True,
        "apply_to_debit": True,
    }

    metadata = entry_price_metadata(candidate, config)

    assert metadata["execution_liquidity_tier"] == "extreme"
    assert metadata["max_bid_ask_pct"] == 1.0
    assert metadata["raw_improvement_pct_of_spread"] > 35
    assert metadata["max_improvement_cap"] == 0.35
    assert candidate_entry_limit_price(candidate, config) == "-1.35"


def test_liquidity_adjusted_pricing_uses_tier_cap_for_very_wide_basket() -> None:
    candidate = _credit_spread_candidate()
    candidate.legs[0].bid = 1.50
    candidate.legs[0].ask = 2.40
    candidate.legs[1].bid = 0.65
    candidate.legs[1].ask = 1.05
    config = _config()
    config["live"]["entry_pricing"] = {
        "mode": "liquidity_adjusted_mid",
        "improvement_pct_of_spread": 10,
        "low_oi_improvement_pct_of_spread": 20,
        "very_low_oi_improvement_pct_of_spread": 35,
        "good_oi_threshold": 500,
        "low_oi_threshold": 100,
        "min_improvement": 0.01,
        "max_improvement": 0.10,
        "max_improvement_by_liquidity_tier": {
            "tight": 0.05,
            "normal": 0.10,
            "wide": 0.15,
            "very_wide": 0.25,
            "extreme": 0.35,
        },
        "max_improvement_pct_of_premium": 40,
        "normal_bid_ask_pct": 0.30,
        "width_improvement_max_pct_of_spread": 45,
        "width_improvement_curve": 0.85,
        "apply_to_credit": True,
        "apply_to_debit": True,
    }

    metadata = entry_price_metadata(candidate, config)

    assert metadata["execution_liquidity_tier"] == "very_wide"
    assert metadata["max_improvement_cap"] == 0.25
    assert candidate_entry_limit_price(candidate, config) == "-1.25"


def test_public_preflight_retries_rejected_penny_price_with_favorable_nickel(monkeypatch) -> None:
    adapter = PublicAdapter(_public_config())
    calls = []

    def fake_post(_endpoint, payload):  # noqa: ANN001
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError('{"code":104,"message":"Limit prices must be in increments of $0.05"}')
        return {"buyingPowerRequirement": 1870}

    monkeypatch.setattr(adapter, "_post", fake_post)

    result = adapter.preflight(_long_call_candidate())

    assert result.ok is True
    assert calls[0]["limitPrice"] == "18.71"
    assert calls[1]["limitPrice"] == "18.70"
    assert result.raw["request"]["limitPrice"] == "18.70"


def test_public_preflight_does_not_shrink_credit_spread_risk(monkeypatch) -> None:
    adapter = PublicAdapter(_public_config())

    def fake_post(_endpoint, _payload):  # noqa: ANN001
        return {"buyingPowerRequirement": "-102.00", "orderValue": "-102.00"}

    monkeypatch.setattr(adapter, "_post", fake_post)

    result = adapter.preflight(_credit_spread_candidate())

    assert result.ok is True
    assert result.bpr == 400.0
    assert result.raw["public_bpr_raw"] == -102.0


def test_public_preflight_sanitizes_and_structures_invalid_order_failure(monkeypatch) -> None:
    adapter = PublicAdapter(_public_config())

    def fake_post(_endpoint, _payload):  # noqa: ANN001
        raise RuntimeError(
            "Public API POST /userapigateway/trading/5OS69079/preflight/multi-leg failed status=400: "
            "{\"code\":117,\"message\":\"Before placing this order, you must first cancel an invalid order in your portfolio. "
            "Please cancel the order to close GOOGL $350 Put Jul 17, '26 and then try again.\"}"
        )

    monkeypatch.setattr(adapter, "_post", fake_post)

    result = adapter.preflight(_credit_spread_candidate())

    assert result.ok is False
    assert "5OS69079" not in result.message
    assert "/trading/<account>/" in result.message
    assert result.raw["public_invalid_order"] == {
        "action_required": "cancel_invalid_close_order",
        "underlying": "GOOGL",
        "strike": 350.0,
        "option_type": "put",
        "expiration_label": "Jul 17, '26",
        "requires_explicit_broker_cancel_approval": True,
    }


def test_entry_pricing_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICING_MODE", "mid")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_IMPROVEMENT_PCT_OF_SPREAD", "25")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_MIN_IMPROVEMENT", "0.02")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_MAX_IMPROVEMENT", "0.15")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_VERY_WIDE_MAX_IMPROVEMENT", "0.33")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_MAX_IMPROVEMENT_PCT_OF_PREMIUM", "45")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_APPLY_TO_CREDIT", "false")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_APPLY_TO_DEBIT", "true")

    control = load_control()
    pricing = control["live"]["entry_pricing"]

    assert pricing["mode"] == "mid"
    assert pricing["improvement_pct_of_spread"] == 25
    assert pricing["min_improvement"] == 0.02
    assert pricing["max_improvement"] == 0.15
    assert pricing["max_improvement_by_liquidity_tier"]["very_wide"] == 0.33
    assert pricing["max_improvement_pct_of_premium"] == 45
    assert pricing["apply_to_credit"] is False
    assert pricing["apply_to_debit"] is True
