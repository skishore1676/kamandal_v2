from __future__ import annotations

import json

from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg
from kamandal_v2.market.broker import broker_adapter
from kamandal_v2.market.tastytrade import TastytradeAdapter, parse_tasty_option_symbol, tasty_option_symbol


class _Response:
    def __init__(self, payload: dict, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code <= 399
        self.text = json.dumps(payload)
        self.reason = "OK"

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"status={self.status_code}")


class _FakeSession:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def post(self, url: str, **kwargs) -> _Response:
        self.requests.append({"method": "POST", "url": url, **kwargs})
        return _Response({"access_token": "token-123", "expires_in": 900})

    def request(self, method: str, url: str, **kwargs) -> _Response:
        self.requests.append({"method": method, "url": url, **kwargs})
        if url.endswith("/customers/me/accounts"):
            return _Response({"data": {"items": [{"account": {"account-number": "5WT00000"}}]}})
        if url.endswith("/accounts/5WT00000/balances"):
            return _Response({"data": {"net-liquidating-value": "25000", "option-buying-power": "18000", "maintenance-requirement": "7000"}})
        if url.endswith("/accounts/5WT00000/positions"):
            return _Response({"data": {"items": [{"symbol": "SPY"}]}})
        if url.endswith("/accounts/5WT00000/orders/dry-run"):
            return _Response({"data": {"buying-power-effect": {"impact": "412.34"}}})
        if url.endswith("/market-metrics"):
            return _Response(
                {
                    "data": {
                        "items": [
                            {
                                "symbol": "TSLA",
                                "implied-volatility-index": "0.42",
                                "implied-volatility-rank": "0",
                                "implied-volatility-percentile": "71.2",
                            }
                        ]
                    }
                }
            )
        return _Response({"data": {"order": {"id": 123, "status": "Received"}}})


def _adapter(tmp_path) -> TastytradeAdapter:
    adapter = TastytradeAdapter(
        {
            "broker": {
                "tastytrade": {
                    "client_id": "client-id",
                    "client_secret": "secret",
                    "refresh_token": "refresh",
                    "api_version": "20250813",
                    "session_file": str(tmp_path / "session.json"),
                    "account_cache_file": str(tmp_path / "account.json"),
                }
            }
        }
    )
    adapter._session = _FakeSession()
    return adapter


def _leg() -> OptionLeg:
    return OptionLeg(
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


def test_tasty_option_symbol_round_trip() -> None:
    symbol = tasty_option_symbol("QQQ", _leg())

    assert symbol == "QQQ   260619C00465000"
    assert parse_tasty_option_symbol(symbol) == {
        "underlying": "QQQ",
        "expiration": "2026-06-19",
        "option_type": "call",
        "strike": 465.0,
    }


def test_tastytrade_account_state_fetches_oauth_and_account(tmp_path) -> None:
    adapter = _adapter(tmp_path)

    account = adapter.account_state()

    assert account.account_size == 25000.0
    assert account.buying_power == 18000.0
    assert account.bpr_used == 7000.0
    assert account.positions_count == 1
    token_request = adapter._session.requests[0]
    assert token_request["url"] == "https://api.tastytrade.com/oauth/token"
    assert token_request["json"]["client_id"] == "client-id"
    assert token_request["json"]["scope"] == "read trade"
    assert token_request["headers"]["User-Agent"] == "kamandal-v2/0.1"
    assert token_request["headers"]["Accept-Version"] == "20250813"


def test_tastytrade_preflight_builds_open_option_order(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    candidate = Candidate(
        candidate_id="candidate",
        idea_id="idea",
        underlying="QQQ",
        playbook_id="call_spread",
        structure="call_spread",
        legs=[_leg()],
        net_credit=1.25,
        estimated_bpr=125.0,
        greeks=Greeks(),
        liquidity_score=1.0,
        score=0.0,
    )

    result = adapter.preflight(candidate)

    assert result.ok is True
    assert result.bpr == 412.34
    request = result.raw["request"]
    assert request["price"] == "1.25"
    assert request["price-effect"] == "Credit"
    assert request["legs"][0]["action"] == "Sell to Open"
    assert request["legs"][0]["symbol"] == "QQQ   260619C00465000"


def test_tastytrade_preflight_preserves_bpr_from_failed_margin_check(tmp_path) -> None:
    adapter = _adapter(tmp_path)

    class MarginFailureSession(_FakeSession):
        def request(self, method: str, url: str, **kwargs) -> _Response:
            self.requests.append({"method": method, "url": url, **kwargs})
            if url.endswith("/customers/me/accounts"):
                return _Response({"data": {"items": [{"account": {"account-number": "5WT00000"}}]}})
            return _Response(
                {
                    "data": {"buying-power-effect": {"change-in-buying-power": "3922.61658"}},
                    "error": {
                        "errors": [
                            {
                                "code": "margin_check_failed_with_flags",
                                "message": "Account does not have sufficient buying power.",
                            }
                        ]
                    },
                },
                status_code=422,
            )

    adapter._session = MarginFailureSession()
    candidate = Candidate(
        candidate_id="candidate",
        idea_id="idea",
        underlying="QQQ",
        playbook_id="short_strangle",
        structure="short_strangle",
        legs=[_leg()],
        net_credit=1.25,
        estimated_bpr=125.0,
        greeks=Greeks(),
        liquidity_score=1.0,
        score=0.0,
    )

    result = adapter.preflight(candidate)

    assert result.ok is False
    assert result.bpr == 3922.61658
    assert result.raw["response"]["error"]["errors"][0]["code"] == "margin_check_failed_with_flags"


def test_tastytrade_market_metrics_use_symbols_query(tmp_path) -> None:
    adapter = _adapter(tmp_path)

    assert adapter.iv_abs("TSLA") == 42.0
    assert adapter.iv_rank("TSLA") == 0.0
    assert adapter.iv_percentile("TSLA") == 71.2

    metric_requests = [
        request
        for request in adapter._session.requests
        if request["method"] == "GET" and request["url"].endswith("/market-metrics")
    ]
    assert metric_requests
    assert metric_requests[0]["params"] == {"symbols": "TSLA"}
    assert len(metric_requests) == 1


def test_tastytrade_market_metrics_normalize_fractional_scale(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter._market_metric_cache["AMZN"] = {
        "symbol": "AMZN",
        "implied-volatility-index": "0.317353744",
        "implied-volatility-rank": "0.1063",
        "implied-volatility-percentile": "0.226038885",
    }

    assert adapter.iv_abs("AMZN") == 31.7354
    assert adapter.iv_rank("AMZN") == 10.63
    assert adapter.iv_percentile("AMZN") == 22.6039


def test_broker_adapter_uses_tastytrade_when_active() -> None:
    adapter = broker_adapter({"broker": {"active": "tastytrade"}})

    assert isinstance(adapter, TastytradeAdapter)
