from __future__ import annotations

import json
import stat

import pytest

from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg
from kamandal_v2.market.broker import broker_adapter
from kamandal_v2.market.tastytrade import TastytradeAdapter, parse_tasty_option_symbol, tasty_option_symbol, _order_response


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
        self.balance_payload = {
            "net-liquidating-value": "25000",
            "option-buying-power": "18000",
            "maintenance-requirement": "7000",
        }

    def post(self, url: str, **kwargs) -> _Response:
        self.requests.append({"method": "POST", "url": url, **kwargs})
        return _Response({"access_token": "token-123", "expires_in": 900})

    def request(self, method: str, url: str, **kwargs) -> _Response:
        self.requests.append({"method": method, "url": url, **kwargs})
        if url.endswith("/customers/me/accounts"):
            return _Response({"data": {"items": [{"account": {"account-number": "5WT00000"}}]}})
        if url.endswith("/accounts/5WT00000/balances"):
            return _Response({"data": self.balance_payload})
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
                    "client_secret": "secret",
                    "refresh_token": "refresh",
                    "account_number": "5WT00000",
                    "api_version": "20250813",
                    "orders_api_version": "20260427",
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
    assert token_request["url"] == "https://api.tastyworks.com/oauth/token"
    assert "client_id" not in token_request["json"]
    assert token_request["json"]["scope"] == "read trade"
    assert token_request["headers"]["User-Agent"] == "kamandal-v2/0.1"
    assert "Accept-Version" not in token_request["headers"]
    assert stat.S_IMODE((tmp_path / "session.json").stat().st_mode) == 0o600


def test_tastytrade_account_state_uses_derivative_capacity_and_preserves_zero_usage(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter._session.balance_payload = {
        "net-liquidating-value": "15200",
        "derivative-buying-power": "15200",
        "equity-buying-power": "30400",
        "used-derivative-buying-power": "0.0",
        "maintenance-requirement": "0.0",
        "margin-equity": "15200",
    }

    account = adapter.account_state()

    assert account.account_size == 15200.0
    assert account.buying_power == 15200.0
    assert account.bpr_used == 0.0
    assert account.positions_count == 1


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
    assert result.raw["broker_bpr_provided"] is True
    assert result.raw["bpr_source"] == "tastytrade_dry_run"
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
    assert result.raw["broker_bpr_provided"] is True
    assert result.raw["bpr_source"] == "tastytrade_dry_run"
    assert result.raw["response"]["error"]["errors"][0]["code"] == "margin_check_failed_with_flags"


def test_tastytrade_market_metrics_use_symbols_query(tmp_path) -> None:
    adapter = _adapter(tmp_path)

    bundle = adapter.volatility_metrics("TSLA")
    assert bundle["symbol"] == "TSLA"
    assert bundle["iv_abs"] == 42.0
    assert bundle["iv_rank"] == 0.0
    assert bundle["iv_percentile"] == 71.2
    assert bundle["provider"] == "tastytrade"
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


def test_tastytrade_builds_two_leg_strangle_open_close_and_adjust(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    short_put = {
        "role": "short_put",
        "side": "sell",
        "effect": "open",
        "option_type": "put",
        "strike": 430.0,
        "expiration": "2026-06-19",
        "quantity": 1,
    }
    short_call = {
        "role": "short_call",
        "side": "sell",
        "effect": "open",
        "option_type": "call",
        "strike": 465.0,
        "expiration": "2026-06-19",
        "quantity": 1,
    }

    opened = adapter._order_payload_from_ticket({
        "order_id": "client-open",
        "underlying": "QQQ",
        "intent_type": "open",
        "limit_price": "-2.50",
        "legs": [short_put, short_call],
    })
    closed = adapter._order_payload_from_ticket({
        "order_id": "client-close",
        "underlying": "QQQ",
        "intent_type": "close",
        "limit_price": "1.25",
        "legs": [
            {**short_put, "side": "buy", "effect": "close"},
            {**short_call, "side": "buy", "effect": "close"},
        ],
    })
    adjusted = adapter._order_payload_from_ticket({
        "order_id": "client-adjust",
        "underlying": "QQQ",
        "intent_type": "adjust",
        "limit_price": "-0.20",
        "legs": [
            {**short_call, "side": "buy", "effect": "close"},
            {**short_call, "strike": 475.0, "side": "sell", "effect": "open"},
        ],
    })

    assert [leg["action"] for leg in opened["legs"]] == ["Sell to Open", "Sell to Open"]
    assert opened["price-effect"] == "Credit"
    assert [leg["action"] for leg in closed["legs"]] == ["Buy to Close", "Buy to Close"]
    assert closed["price-effect"] == "Debit"
    assert [leg["action"] for leg in adjusted["legs"]] == ["Buy to Close", "Sell to Open"]
    assert adjusted["external-identifier"] == "client-adjust"


def test_tastytrade_atomic_replace_dry_runs_then_patches_with_pinned_order_version(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    ticket = {
        "order_id": "client-replacement",
        "underlying": "QQQ",
        "intent_type": "open",
        "limit_price": "-2.40",
        "legs": [{**_leg().to_dict(), "effect": "open"}],
    }

    response = adapter.replace_order("123", ticket)

    assert response["orderId"] == "123"
    assert response["status"] == "WORKING"
    order_requests = [request for request in adapter._session.requests if "/orders/123" in request["url"]]
    assert [request["method"] for request in order_requests] == ["POST", "PATCH"]
    assert order_requests[0]["url"].endswith("/accounts/5WT00000/orders/123/dry-run")
    assert order_requests[1]["url"].endswith("/accounts/5WT00000/orders/123")
    assert all(request["headers"]["Accept-Version"] == "20260427" for request in order_requests)
    assert order_requests[1]["json"]["external-identifier"] == "client-replacement"
    assert "legs" not in order_requests[1]["json"]


def test_tastytrade_replacement_stops_on_http_success_with_preflight_errors(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(adapter, "_post", lambda *_args: {"data": {"errors": [{"code": "margin-check-failed"}]}})
    writes = []
    monkeypatch.setattr(adapter, "_patch", lambda *args: writes.append(args))
    ticket = {"order_id": "replacement", "underlying": "QQQ", "intent_type": "open", "limit_price": "-1.25", "legs": [_leg().to_dict()]}
    with pytest.raises(RuntimeError, match="replacement dry-run rejected"):
        adapter.replace_order("123", ticket)
    assert writes == []


def test_tastytrade_order_response_normalizes_partial_fill_fields(tmp_path) -> None:
    adapter = _adapter(tmp_path)

    class PartialFillSession(_FakeSession):
        def request(self, method: str, url: str, **kwargs) -> _Response:
            self.requests.append({"method": method, "url": url, **kwargs})
            return _Response({"data": {"order": {
                "id": 321,
                "status": "Partially Filled",
                "filled-quantity": "1",
                "remaining-quantity": "1",
                "average-price": "2.45",
                "updated-at": "2026-08-23T15:00:00Z",
            }}})

    adapter._session = PartialFillSession()
    result = adapter.get_order("321")

    assert result == {
        "orderId": "321",
        "status": "PARTIALLY_FILLED",
        "raw": result["raw"],
        "filledQuantity": "1",
        "remainingQuantity": "1",
        "averagePrice": "2.45",
        "updatedAt": "2026-08-23T15:00:00Z",
    }


def test_tastytrade_fill_waits_for_all_legs_and_uses_actual_execution_prices():
    def leg(symbol, action, price):
        return {"symbol": symbol, "quantity": "1", "remaining-quantity": "0", "action": action,
                "fills": [{"fill-price": str(price), "quantity": "1", "filled-at": "2026-09-08T14:10:00Z"}]}
    order = {"id": 321, "status": "Filled", "price": "1.80", "legs": [
        leg("XYZ   261016P00090000", "Sell to Open", .95),
        leg("XYZ   261016C00110000", "Sell to Open", 1.10),
    ]}
    complete = _order_response({"data": order})
    assert complete["status"] == "FILLED"
    assert complete["averagePrice"] == 2.05
    assert complete["filledQuantity"] == 1
    order["legs"][1]["fills"] = []
    pending = _order_response({"data": order})
    assert pending["status"] == "FILL_PENDING_DETAILS"
    assert "averagePrice" not in pending
    order["legs"] = []
    assert _order_response({"data": order})["status"] == "FILL_PENDING_DETAILS"


def test_tastytrade_mixed_adjustment_fill_is_net_credit_not_sum_of_premiums():
    order = {"id": 322, "status": "Filled", "legs": [
        {"quantity": 1, "action": "Buy to Close", "fills": [{"fill-price": .5, "quantity": 1, "filled-at": "2026-09-08T14:10:00Z"}]},
        {"quantity": 1, "action": "Sell to Open", "fills": [{"fill-price": .8, "quantity": 1, "filled-at": "2026-09-08T14:10:00Z"}]},
    ]}
    assert _order_response({"data": order})["averagePrice"] == .3


def test_tastytrade_recovers_client_identity_without_posting(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    reads = []
    def get(endpoint, *, params):
        reads.append((endpoint, params))
        return {"data": {"items": [{"id": 345, "status": "Live", "external-identifier": "client-1"}]}}
    monkeypatch.setattr(adapter, "_get", get)
    recovered = adapter.find_order_by_client_id("client-1", created_at="2026-09-08T14:00:00Z")
    assert recovered["orderId"] == "345"
    assert len(reads) == 1
    assert adapter._session.requests == []


def test_tastytrade_live_order_fails_closed_without_explicit_account(tmp_path) -> None:
    adapter = TastytradeAdapter({"broker": {"tastytrade": {
        "client_secret": "secret",
        "refresh_token": "refresh",
        "orders_api_version": "20260427",
        "session_file": str(tmp_path / "session.json"),
        "account_cache_file": str(tmp_path / "account.json"),
    }}})
    adapter._session = _FakeSession()

    with pytest.raises(RuntimeError, match="explicit_account_number_missing"):
        adapter.place_order_ticket({"order_id": "client", "underlying": "QQQ", "legs": []})
    assert adapter._session.requests == []


def test_tastytrade_live_order_fails_closed_without_explicit_orders_version(tmp_path) -> None:
    adapter = TastytradeAdapter({"broker": {"tastytrade": {
        "client_secret": "secret",
        "refresh_token": "refresh",
        "account_number": "5WT00000",
        "session_file": str(tmp_path / "session.json"),
        "account_cache_file": str(tmp_path / "account.json"),
    }}})

    with pytest.raises(RuntimeError, match="orders_api_version_missing"):
        adapter.get_order("123")


def test_tastytrade_readiness_and_contract_matrix_are_broker_inert(tmp_path) -> None:
    adapter = _adapter(tmp_path)

    report = adapter.configuration_report()
    matrix = adapter.order_contract_matrix()

    assert report["ready"] is True
    assert report["network_used"] is False
    assert report["api_base_url"] == "https://api.tastyworks.com"
    assert report["api_base_url_documented"] is True
    assert report["capabilities"]["dxlink_quotes"] is False
    assert matrix["network_used"] is False
    assert matrix["synthetic_contracts_only"] is True
    assert [leg["action"] for leg in matrix["payloads"]["open"]["legs"]] == ["Sell to Open", "Sell to Open"]
    assert [leg["action"] for leg in matrix["payloads"]["close"]["legs"]] == ["Buy to Close", "Buy to Close"]
    assert [leg["action"] for leg in matrix["payloads"]["adjust"]["legs"]] == ["Buy to Close", "Sell to Open"]
    assert matrix["payloads"]["replace"]["price"] == "2.60"
    assert matrix["payloads"]["replace"]["price-effect"] == "Credit"
    assert adapter._session.requests == []


def test_tastytrade_readiness_warns_on_legacy_api_host_without_blocking(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter.api_base_url = "https://api.tastytrade.com"

    report = adapter.configuration_report()

    assert report["ready"] is True
    assert report["api_base_url_documented"] is False
    assert report["api_base_url_warning"] == "api_base_url_not_currently_documented_by_tastytrade"


def test_tastytrade_readiness_requires_secret_and_refresh_token(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter.client_secret = ""

    report = adapter.configuration_report()

    assert report["ready"] is False
    assert "oauth_credentials_missing" in report["reasons"]
