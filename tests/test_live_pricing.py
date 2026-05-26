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


def test_entry_pricing_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICING_MODE", "mid")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_IMPROVEMENT_PCT_OF_SPREAD", "25")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_MIN_IMPROVEMENT", "0.02")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_MAX_IMPROVEMENT", "0.15")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_APPLY_TO_CREDIT", "false")
    monkeypatch.setenv("KAMANDAL_ENTRY_PRICE_APPLY_TO_DEBIT", "true")

    control = load_control()
    pricing = control["live"]["entry_pricing"]

    assert pricing["mode"] == "mid"
    assert pricing["improvement_pct_of_spread"] == 25
    assert pricing["min_improvement"] == 0.02
    assert pricing["max_improvement"] == 0.15
    assert pricing["apply_to_credit"] is False
    assert pricing["apply_to_debit"] is True
