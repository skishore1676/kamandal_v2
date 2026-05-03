from kamandal_v2.domain.models import OptionLeg
from kamandal_v2.market.public import occ_symbol, parse_occ_symbol
from kamandal_v2.planner.engine import _preflight_client


def test_occ_symbol_round_trip() -> None:
    leg = OptionLeg(
        role="short_call",
        side="sell",
        option_type="call",
        strike=465.0,
        expiration="2026-06-19",
        quantity=1,
        mid=1.25,
        bid=1.2,
        ask=1.3,
        delta=0.25,
        gamma=0.01,
        theta=-0.02,
        vega=0.03,
        open_interest=100,
    )

    symbol = occ_symbol("QQQ", leg)
    parsed = parse_occ_symbol(symbol)

    assert symbol == "QQQ260619C00465000"
    assert parsed == {
        "underlying": "QQQ",
        "expiration": "2026-06-19",
        "option_type": "call",
        "strike": 465.0,
    }


def test_preflight_client_unwraps_nested_market_adapters() -> None:
    class PublicLike:
        def preflight(self, candidate):  # noqa: ANN001
            return candidate

    class Wrapper:
        def __init__(self, inner):
            self.inner = inner

    public = PublicLike()

    assert _preflight_client(Wrapper(Wrapper(public))) is public
