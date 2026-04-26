"""Structure shape validators."""

from __future__ import annotations

from dataclasses import dataclass

from kamandal_v2.domain.models import OptionLeg

SUPPORTED_VALIDATOR_STRUCTURES = {
    "short_put",
    "put_spread",
    "call_spread",
    "iron_condor",
    "call_calendar",
    "put_calendar",
    "put_diagonal",
    "call_diagonal",
    "short_strangle",
    "jade_lizard",
    "long_call",
    "long_put",
}


@dataclass(slots=True)
class ValidationResult:
    valid: bool
    reason: str = ""


def validate_structure(structure: str, legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    validators = {
        "short_put": _short_put,
        "long_call": _long_call,
        "long_put": _long_put,
        "put_spread": _put_spread,
        "call_spread": _call_spread,
        "iron_condor": _iron_condor,
        "call_calendar": _call_calendar,
        "put_calendar": _put_calendar,
        "put_diagonal": _put_diagonal,
        "call_diagonal": _call_diagonal,
        "short_strangle": _short_strangle,
        "jade_lizard": _jade_lizard,
    }
    validator = validators.get(structure)
    if validator is None:
        return ValidationResult(False, f"unsupported_structure:{structure}")
    return validator(legs, underlying_price)


def _short_put(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 1:
        return ValidationResult(False, "short_put_requires_one_leg")
    leg = legs[0]
    if leg.option_type != "put" or leg.side != "sell":
        return ValidationResult(False, "short_put_requires_short_put")
    if leg.strike >= underlying_price:
        return ValidationResult(False, "short_put_must_be_otm")
    return ValidationResult(True)


def _long_call(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 1:
        return ValidationResult(False, "long_call_requires_one_leg")
    leg = legs[0]
    if leg.option_type != "call" or leg.side != "buy":
        return ValidationResult(False, "long_call_requires_long_call")
    return ValidationResult(True)


def _long_put(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 1:
        return ValidationResult(False, "long_put_requires_one_leg")
    leg = legs[0]
    if leg.option_type != "put" or leg.side != "buy":
        return ValidationResult(False, "long_put_requires_long_put")
    return ValidationResult(True)


def _put_spread(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 2 or any(leg.option_type != "put" for leg in legs):
        return ValidationResult(False, "put_spread_requires_two_puts")
    shorts = [leg for leg in legs if leg.side == "sell"]
    longs = [leg for leg in legs if leg.side == "buy"]
    if len(shorts) != 1 or len(longs) != 1:
        return ValidationResult(False, "put_spread_requires_one_short_one_long")
    if shorts[0].expiration != longs[0].expiration:
        return ValidationResult(False, "put_spread_requires_same_expiry")
    if not (longs[0].strike < shorts[0].strike < underlying_price):
        return ValidationResult(False, "put_spread_strike_order_invalid")
    return ValidationResult(True)


def _call_spread(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 2 or any(leg.option_type != "call" for leg in legs):
        return ValidationResult(False, "call_spread_requires_two_calls")
    shorts = [leg for leg in legs if leg.side == "sell"]
    longs = [leg for leg in legs if leg.side == "buy"]
    if len(shorts) != 1 or len(longs) != 1:
        return ValidationResult(False, "call_spread_requires_one_short_one_long")
    if shorts[0].expiration != longs[0].expiration:
        return ValidationResult(False, "call_spread_requires_same_expiry")
    if not (underlying_price < shorts[0].strike < longs[0].strike):
        return ValidationResult(False, "call_spread_strike_order_invalid")
    return ValidationResult(True)


def _iron_condor(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 4:
        return ValidationResult(False, "iron_condor_requires_four_legs")
    if len({leg.expiration for leg in legs}) != 1:
        return ValidationResult(False, "iron_condor_requires_same_expiry")
    puts = sorted([leg for leg in legs if leg.option_type == "put"], key=lambda leg: leg.strike)
    calls = sorted([leg for leg in legs if leg.option_type == "call"], key=lambda leg: leg.strike)
    if len(puts) != 2 or len(calls) != 2:
        return ValidationResult(False, "iron_condor_requires_two_puts_two_calls")
    if puts[0].side != "buy" or puts[1].side != "sell":
        return ValidationResult(False, "iron_condor_put_wing_invalid")
    if calls[0].side != "sell" or calls[1].side != "buy":
        return ValidationResult(False, "iron_condor_call_wing_invalid")
    if not (puts[1].strike < underlying_price < calls[0].strike):
        return ValidationResult(False, "iron_condor_short_strikes_not_around_spot")
    return ValidationResult(True)


def _call_calendar(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 2 or any(leg.option_type != "call" for leg in legs):
        return ValidationResult(False, "call_calendar_requires_two_calls")
    shorts = [leg for leg in legs if leg.side == "sell"]
    longs = [leg for leg in legs if leg.side == "buy"]
    if len(shorts) != 1 or len(longs) != 1:
        return ValidationResult(False, "call_calendar_requires_short_and_long")
    if shorts[0].strike != longs[0].strike:
        return ValidationResult(False, "call_calendar_requires_same_strike")
    if shorts[0].expiration >= longs[0].expiration:
        return ValidationResult(False, "call_calendar_requires_short_near_long_far")
    return ValidationResult(True)


def _put_calendar(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 2 or any(leg.option_type != "put" for leg in legs):
        return ValidationResult(False, "put_calendar_requires_two_puts")
    shorts = [leg for leg in legs if leg.side == "sell"]
    longs = [leg for leg in legs if leg.side == "buy"]
    if len(shorts) != 1 or len(longs) != 1:
        return ValidationResult(False, "put_calendar_requires_short_and_long")
    if shorts[0].strike != longs[0].strike:
        return ValidationResult(False, "put_calendar_requires_same_strike")
    if shorts[0].expiration >= longs[0].expiration:
        return ValidationResult(False, "put_calendar_requires_short_near_long_far")
    return ValidationResult(True)


def _put_diagonal(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 2 or any(leg.option_type != "put" for leg in legs):
        return ValidationResult(False, "put_diagonal_requires_two_puts")
    shorts = [leg for leg in legs if leg.side == "sell"]
    longs = [leg for leg in legs if leg.side == "buy"]
    if len(shorts) != 1 or len(longs) != 1:
        return ValidationResult(False, "put_diagonal_requires_short_and_long")
    if shorts[0].expiration >= longs[0].expiration:
        return ValidationResult(False, "put_diagonal_requires_short_near_long_far")
    if longs[0].strike < shorts[0].strike:
        return ValidationResult(False, "put_diagonal_requires_long_strike_at_or_above_short")
    return ValidationResult(True)


def _call_diagonal(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 2 or any(leg.option_type != "call" for leg in legs):
        return ValidationResult(False, "call_diagonal_requires_two_calls")
    shorts = [leg for leg in legs if leg.side == "sell"]
    longs = [leg for leg in legs if leg.side == "buy"]
    if len(shorts) != 1 or len(longs) != 1:
        return ValidationResult(False, "call_diagonal_requires_short_and_long")
    if shorts[0].expiration >= longs[0].expiration:
        return ValidationResult(False, "call_diagonal_requires_short_near_long_far")
    if longs[0].strike > shorts[0].strike:
        return ValidationResult(False, "call_diagonal_requires_long_strike_at_or_below_short")
    return ValidationResult(True)


def _short_strangle(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 2 or any(leg.side != "sell" for leg in legs):
        return ValidationResult(False, "strangle_requires_two_short_legs")
    puts = [leg for leg in legs if leg.option_type == "put"]
    calls = [leg for leg in legs if leg.option_type == "call"]
    if len(puts) != 1 or len(calls) != 1:
        return ValidationResult(False, "strangle_requires_put_and_call")
    if puts[0].expiration != calls[0].expiration:
        return ValidationResult(False, "strangle_requires_same_expiry")
    if not (puts[0].strike < underlying_price < calls[0].strike):
        return ValidationResult(False, "strangle_strikes_not_around_spot")
    return ValidationResult(True)


def _jade_lizard(legs: list[OptionLeg], underlying_price: float) -> ValidationResult:
    if len(legs) != 3:
        return ValidationResult(False, "jade_lizard_requires_three_legs")
    if len({leg.expiration for leg in legs}) != 1:
        return ValidationResult(False, "jade_lizard_requires_same_expiry")
    puts = [leg for leg in legs if leg.option_type == "put"]
    calls = sorted([leg for leg in legs if leg.option_type == "call"], key=lambda leg: leg.strike)
    if len(puts) != 1 or len(calls) != 2:
        return ValidationResult(False, "jade_lizard_requires_one_put_two_calls")
    if puts[0].side != "sell":
        return ValidationResult(False, "jade_lizard_requires_short_put")
    if calls[0].side != "sell" or calls[1].side != "buy":
        return ValidationResult(False, "jade_lizard_call_spread_invalid")
    if not (puts[0].strike < underlying_price < calls[0].strike < calls[1].strike):
        return ValidationResult(False, "jade_lizard_strike_order_invalid")
    return ValidationResult(True)
