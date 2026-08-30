from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg
from kamandal_v2.market.public import PublicAdapter, _public_api_error_raw, occ_symbol, parse_occ_symbol
from kamandal_v2.planner.engine import _preflight_client


class _Response:
    def __init__(self, payload: dict, *, status_code: int = 200, headers: dict | None = None) -> None:
        self._payload = payload
        self.ok = status_code < 400
        self.status_code = status_code
        self.headers = headers or {}
        self.text = "{}"
        self.reason = "OK"

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FakeSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requests = []

    def request(self, method: str, url: str, **kwargs):  # noqa: ANN001
        self.requests.append({"method": method, "url": url, **kwargs})
        return _Response(self.payload)

    def post(self, url: str, **kwargs):  # noqa: ANN001, ARG002
        return _Response({"accessToken": "token", "expiresIn": 900})


class _SequencedSession:
    def __init__(self, responses):  # noqa: ANN001
        self.responses = list(responses)
        self.requests = []

    def request(self, method: str, url: str, **kwargs):  # noqa: ANN001
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


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


def test_public_adapter_retries_http_429_then_returns_payload() -> None:
    adapter = PublicAdapter({"broker": {"public": {"retry_attempts": 2, "retry_base_delay_seconds": 0}}})
    adapter._access_token = "token"
    adapter._expires_at = 10**12
    adapter._session = _SequencedSession([
        _Response({}, status_code=429, headers={"Retry-After": "0"}),
        _Response({"ok": True}),
    ])

    assert adapter._get("/test") == {"ok": True}
    assert len(adapter._session.requests) == 2


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


def test_public_broker_positions_preserve_cost_basis_and_strategy_ids(tmp_path) -> None:
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
            "positions": [
                {
                    "instrument": {"symbol": "MRVL260717P00250000", "type": "OPTION"},
                    "quantity": "2",
                    "side": "short",
                    "currentValue": "-2500.00",
                    "costBasis": {"unitCost": "12.50", "totalCost": "-2500.00"},
                    "strategyIds": ["strategy-1"],
                }
            ]
        }
    )

    positions = adapter.broker_positions()

    assert positions[0]["quantity"] == -2.0
    assert positions[0]["average_price"] == 12.5
    assert positions[0]["cost_basis"] == -2500.0
    assert positions[0]["current_value"] == -2500.0
    assert positions[0]["strategy_ids"] == ["strategy-1"]


def test_public_replace_order_uses_atomic_option_cancel_replace_payload(tmp_path) -> None:
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
    adapter._access_token = "token"
    adapter._expires_at = 10**12
    adapter._session = _FakeSession({"orderId": "replacement-order"})

    response = adapter.replace_order(
        "original-order",
        {
            "order_id": "replacement-order",
            "quantity": 1,
            "submit_payload": {
                "orderId": "replacement-order",
                "quantity": "1",
                "type": "LIMIT",
                "limitPrice": "-0.40",
                "expiration": {"timeInForce": "DAY"},
                "legs": [{"instrument": {"symbol": "XLF260814P00056000", "type": "OPTION"}}],
            },
        },
    )

    assert response == {"orderId": "replacement-order"}
    request = adapter._session.requests[0]
    assert request["method"] == "PUT"
    assert request["url"].endswith("/userapigateway/trading/acct/order")
    assert request["json"] == {
        "orderId": "original-order",
        "requestId": "replacement-order",
        "orderType": "LIMIT",
        "expiration": {"timeInForce": "DAY"},
        "quantity": "1",
        "limitPrice": "0.40",
    }
    assert adapter.supports_atomic_replace(
        {
            "submit_payload": {
                "limitPrice": "-0.40",
                "legs": [{}, {}],
            }
        }
    ) is False


def test_public_strangle_preflight_uses_broker_buying_power_requirement(tmp_path) -> None:
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
    adapter._access_token = "token"
    adapter._expires_at = 10**12
    adapter._session = _FakeSession({"buyingPowerRequirement": "1275.50"})
    legs = [
        OptionLeg("short_put", "sell", "put", 90, "2026-09-18", 1, 1.0, 0.9, 1.1, -0.16, 0.01, -0.02, 0.03, 500),
        OptionLeg("short_call", "sell", "call", 110, "2026-09-18", 1, 1.0, 0.9, 1.1, 0.16, 0.01, -0.02, 0.03, 500),
    ]
    candidate = Candidate(
        candidate_id="strangle-1",
        idea_id="idea-1",
        underlying="XYZ",
        playbook_id="short_strangle",
        structure="short_strangle",
        legs=legs,
        net_credit=2.0,
        estimated_bpr=2_200.0,
        greeks=Greeks(),
        liquidity_score=1.0,
        score=0.0,
    )

    result = adapter.preflight(candidate)

    assert result.ok is True
    assert result.bpr == 1_275.50
    assert result.raw["broker_bpr_provided"] is True
    assert result.raw["bpr_source"] == "broker_preflight"


def test_public_level_four_entitlement_error_is_structured() -> None:
    raw = _public_api_error_raw(
        'Public preflight failed: Public API POST /trading/<account>/preflight/multi-leg failed '
        'status=400: {"code":159,"message":"Naked strategies require Level 4"}'
    )

    assert raw == {
        "public_api_error": {
            "http_status": 400,
            "code": 159,
            "message": "Naked strategies require Level 4",
        }
    }
