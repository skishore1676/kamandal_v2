from kamandal_v2.domain.models import OptionLeg
from kamandal_v2.market.public import PublicAdapter, occ_symbol, parse_occ_symbol
from kamandal_v2.planner.engine import _preflight_client


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.ok = True
        self.status_code = 200
        self.text = "{}"
        self.reason = "OK"

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FakeSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def request(self, method: str, url: str, **kwargs):  # noqa: ANN001, ARG002
        return _Response(self.payload)

    def post(self, url: str, **kwargs):  # noqa: ANN001, ARG002
        return _Response({"accessToken": "token", "expiresIn": 900})


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


def test_public_adapter_uses_configured_expiration_window() -> None:
    adapter = PublicAdapter({
        "broker": {
            "public": {
                "option_chain_start_dte": 21,
                "option_chain_end_dte": 90,
                "option_chain_max_expirations": 8,
            }
        }
    })

    assert len(adapter.expiration_dates) == 8


def test_public_account_state_reads_nested_buying_power_and_equity_total(tmp_path) -> None:
    adapter = PublicAdapter(
        {
            "broker": {
                "public": {
                    "secret_token": "secret",
                    "account_id": "acct",
                    "session_file": str(tmp_path / "session.json"),
                    "account_cache_file": str(tmp_path / "account.json"),
                }
            }
        }
    )
    adapter._session = _FakeSession(
        {
            "buyingPower": {
                "cashOnlyBuyingPower": "0.00",
                "buyingPower": "0.00",
                "optionsBuyingPower": "0.00",
            },
            "equity": [
                {"type": "OPTIONS_LONG", "value": "2342.00"},
                {"type": "CASH", "value": "8165.30"},
            ],
            "positions": [{"instrument": {"symbol": "AMZN260821C00265000"}}],
        }
    )

    account = adapter.account_state()

    assert account.account_size == 10507.30
    assert account.buying_power == 0.0
    assert account.positions_count == 1
