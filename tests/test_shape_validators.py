from datetime import date, timedelta

from kamandal_v2.domain.models import OptionLeg
from kamandal_v2.planner.shape_validators import validate_structure


def _leg(role: str, side: str, option_type: str, strike: float, expiration: str) -> OptionLeg:
    return OptionLeg(
        role=role,
        side=side,
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        quantity=1,
        mid=1.0,
        bid=0.95,
        ask=1.05,
        delta=0.25 if option_type == "call" else -0.25,
        gamma=0.01,
        theta=-0.02,
        vega=0.03,
        open_interest=1000,
    )


def test_call_calendar_shape_requires_near_short_far_long() -> None:
    near = (date.today() + timedelta(days=30)).isoformat()
    far = (date.today() + timedelta(days=60)).isoformat()
    legs = [
        _leg("short_near", "sell", "call", 105, near),
        _leg("long_far", "buy", "call", 105, far),
    ]

    assert validate_structure("call_calendar", legs, 100).valid


def test_iron_condor_shape_requires_four_defined_risk_legs() -> None:
    expiry = (date.today() + timedelta(days=45)).isoformat()
    legs = [
        _leg("long_put", "buy", "put", 90, expiry),
        _leg("short_put", "sell", "put", 95, expiry),
        _leg("short_call", "sell", "call", 105, expiry),
        _leg("long_call", "buy", "call", 110, expiry),
    ]

    assert validate_structure("iron_condor", legs, 100).valid


def test_call_spread_rejects_bad_strike_order() -> None:
    expiry = (date.today() + timedelta(days=45)).isoformat()
    legs = [
        _leg("short_call", "sell", "call", 105, expiry),
        _leg("long_call", "buy", "call", 102, expiry),
    ]

    result = validate_structure("call_spread", legs, 100)

    assert not result.valid
    assert result.reason == "call_spread_strike_order_invalid"


def test_short_strangle_shape_requires_otm_put_and_call() -> None:
    expiry = (date.today() + timedelta(days=45)).isoformat()
    legs = [
        _leg("short_put", "sell", "put", 90, expiry),
        _leg("short_call", "sell", "call", 110, expiry),
    ]

    assert validate_structure("short_strangle", legs, 100).valid


def test_jade_lizard_shape_requires_short_put_and_call_credit_spread() -> None:
    expiry = (date.today() + timedelta(days=45)).isoformat()
    legs = [
        _leg("short_put", "sell", "put", 90, expiry),
        _leg("short_call", "sell", "call", 110, expiry),
        _leg("long_call", "buy", "call", 115, expiry),
    ]

    assert validate_structure("jade_lizard", legs, 100).valid


def test_long_option_shapes_accept_single_long_leg() -> None:
    expiry = (date.today() + timedelta(days=45)).isoformat()

    assert validate_structure("long_call", [_leg("long_call", "buy", "call", 110, expiry)], 100).valid
    assert validate_structure("long_put", [_leg("long_put", "buy", "put", 90, expiry)], 100).valid
