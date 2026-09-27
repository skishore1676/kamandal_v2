from datetime import date, timedelta
from dataclasses import replace

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


def test_exact_butterfly_requires_symmetric_one_two_one_same_expiry() -> None:
    expiry = (date.today() + timedelta(days=45)).isoformat()
    legs = [
        _leg("long_lower", "buy", "call", 90, expiry),
        replace(_leg("short_body", "sell", "call", 100, expiry), quantity=2),
        _leg("long_upper", "buy", "call", 110, expiry),
    ]
    assert validate_structure("call_butterfly", legs, 100).valid
    assert validate_structure("call_butterfly", [*legs[:2], replace(legs[2], strike=115)], 100).reason == (
        "butterfly_requires_symmetric_wings"
    )
    assert validate_structure("call_butterfly", [legs[0], replace(legs[1], quantity=1), legs[2]], 100).reason == (
        "butterfly_requires_long_short2_long"
    )


def test_call_crab_requires_two_near_shorts_and_later_lower_long() -> None:
    near = (date.today() + timedelta(days=30)).isoformat()
    far = (date.today() + timedelta(days=90)).isoformat()
    legs = [
        _leg("long_far", "buy", "call", 90, far),
        replace(_leg("short_body", "sell", "call", 100, near), quantity=2),
        _leg("long_near_wing", "buy", "call", 110, near),
    ]
    assert validate_structure("call_crab", legs, 100).valid
    assert validate_structure("call_crab", [replace(legs[0], expiration=near), *legs[1:]], 100).reason == (
        "call_crab_requires_near_and_far_expiry"
    )
    assert validate_structure("call_crab", [replace(legs[0], strike=105), *legs[1:]], 100).reason == (
        "call_crab_ratio_or_strikes_invalid"
    )
