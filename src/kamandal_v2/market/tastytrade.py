"""tastytrade OAuth, account, preflight, and guarded order adapter."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from kamandal_v2.domain.models import Candidate, ChainSnapshot, Greeks, OptionLeg, PortfolioState, PreflightResult
from kamandal_v2.paths import resolve_path


DEFAULT_API_BASE_URL = "https://api.tastytrade.com"
USER_AGENT = "kamandal-v2/0.1"


def tasty_option_symbol(underlying: str, leg: OptionLeg | dict[str, Any]) -> str:
    expiration = str(_leg_value(leg, "expiration")).replace("-", "")
    if len(expiration) != 8:
        raise ValueError(f"Invalid option expiration: {expiration}")
    option_type = str(_leg_value(leg, "option_type") or _leg_value(leg, "optionType")).lower()
    flag = "C" if option_type == "call" else "P"
    strike = int(round(float(_leg_value(leg, "strike")) * 1000))
    return f"{underlying.upper().ljust(6)}{expiration[2:]}{flag}{strike:08d}"


def parse_tasty_option_symbol(symbol: str) -> dict[str, Any]:
    match = re.match(r"^(.{1,6})(\d{6})([CP])(\d{8})$", symbol)
    if not match:
        raise ValueError(f"Unsupported tastytrade option symbol: {symbol}")
    root, yymmdd, flag, strike = match.groups()
    return {
        "underlying": root.strip().upper(),
        "expiration": f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}",
        "option_type": "call" if flag == "C" else "put",
        "strike": int(strike) / 1000.0,
    }


class TastytradeAdapter:
    def __init__(self, config: dict[str, Any]) -> None:
        tasty_cfg = ((config.get("broker") or {}).get("tastytrade") or {})
        self.api_base_url = str(tasty_cfg.get("api_base_url") or DEFAULT_API_BASE_URL).rstrip("/")
        self.client_id = str(tasty_cfg.get("client_id") or "")
        self.client_secret = str(tasty_cfg.get("client_secret") or "")
        self.refresh_token = str(tasty_cfg.get("refresh_token") or "")
        self.account_number = str(tasty_cfg.get("account_number") or "")
        self.oauth_scopes = _as_list(tasty_cfg.get("oauth_scopes") or ["read", "trade"])
        self.api_version = str(tasty_cfg.get("api_version") or "")
        self.session_file = resolve_path(str(tasty_cfg.get("session_file") or "config/tastytrade_session.json"))
        self.account_cache_file = resolve_path(str(tasty_cfg.get("account_cache_file") or "config/tastytrade_account.json"))
        self._session = requests.Session()
        self._access_token = ""
        self._expires_at = 0.0
        self._market_metric_cache: dict[str, dict[str, Any]] = {}

    def available(self) -> bool:
        return bool(self.client_secret and self.refresh_token)

    def account_state(self) -> PortfolioState:
        self._require_available()
        account_number = self._account_number()
        balance = _data(self._get(f"/accounts/{account_number}/balances"))
        positions = _items(self._get(f"/accounts/{account_number}/positions"))
        account_size = _find_number(
            balance,
            ("net-liquidating-value", "net_liquidating_value", "account-value", "accountValue"),
            default=0.0,
        )
        buying_power = _find_number(
            balance,
            ("option-buying-power", "option_buying_power", "equity-buying-power", "cash-available-to-withdraw", "cash-balance"),
            default=account_size,
        )
        margin_used = _find_number(
            balance,
            ("maintenance-requirement", "maintenance_requirement", "margin-equity", "margin_requirement"),
            default=max(account_size - buying_power, 0.0),
        )
        return PortfolioState(
            account_size=account_size or buying_power,
            buying_power=buying_power or account_size,
            bpr_used=max(margin_used, 0.0),
            positions_count=len(positions),
            greeks=Greeks(),
            per_underlying_bpr={},
        )

    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        raise RuntimeError(
            "tastytrade broker is configured, but Kamandal does not yet use DXLink for tastytrade quotes/Greeks. "
            "Keep market provider on fixture/public until the quote streamer is wired."
        )

    def iv_percentile(self, underlying: str) -> float | None:
        metric = self._market_metric(underlying)
        return _find_optional_number(metric, ("iv-percentile", "implied-volatility-percentile"))

    def iv_rank(self, underlying: str) -> float | None:
        metric = self._market_metric(underlying)
        return _find_optional_number(metric, ("iv-rank", "implied-volatility-rank"))

    def iv_abs(self, underlying: str) -> float | None:
        metric = self._market_metric(underlying)
        return _find_optional_number(metric, ("implied-volatility-index", "implied-volatility", "iv-index"))

    def event_status(self, underlying: str) -> str:
        return "unknown"

    def preflight(self, candidate: Candidate) -> PreflightResult:
        self._require_available()
        try:
            price, price_effect = _price_from_net_credit(candidate.net_credit)
            payload = self._order_payload(
                candidate.underlying,
                candidate.legs,
                price=price,
                price_effect=price_effect,
                intent_type="open",
            )
            response = self._post(f"/accounts/{self._account_number()}/orders/dry-run", payload)
            return _preflight_result(payload, response, default_bpr=candidate.estimated_bpr)
        except Exception as exc:  # noqa: BLE001
            return PreflightResult(ok=False, bpr=candidate.estimated_bpr, message=f"tastytrade preflight failed: {exc}", raw={"source": "tastytrade"})

    def preflight_ticket(self, ticket: dict[str, Any]) -> PreflightResult:
        self._require_available()
        try:
            payload = self._order_payload_from_ticket(ticket)
            response = self._post(f"/accounts/{self._account_number()}/orders/dry-run", payload)
            return _preflight_result(payload, response, default_bpr=0.0)
        except Exception as exc:  # noqa: BLE001
            return PreflightResult(ok=False, bpr=0.0, message=f"tastytrade ticket preflight failed: {exc}", raw={"source": "tastytrade"})

    def place_order_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        self._require_available()
        response = self._post(f"/accounts/{self._account_number()}/orders", self._order_payload_from_ticket(ticket))
        return _order_response(response)

    def get_order(self, order_id: str) -> dict[str, Any]:
        self._require_available()
        return _order_response(self._get(f"/accounts/{self._account_number()}/orders/{order_id}"))

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        self._require_available()
        return _order_response(self._delete(f"/accounts/{self._account_number()}/orders/{order_id}"))

    def _order_payload_from_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        public_limit = float(ticket.get("limit_price") or 0.0)
        price, price_effect = _price_from_ticket_limit(public_limit)
        return self._order_payload(
            str(ticket.get("underlying") or ""),
            list(ticket.get("legs") or []),
            price=price,
            price_effect=price_effect,
            intent_type=str(ticket.get("intent_type") or "open"),
            external_identifier=str(ticket.get("order_id") or ticket.get("ticket_hash") or ""),
        )

    def _order_payload(
        self,
        underlying: str,
        legs: list[OptionLeg | dict[str, Any]],
        *,
        price: str,
        price_effect: str,
        intent_type: str,
        external_identifier: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "time-in-force": "Day",
            "order-type": "Limit",
            "price": price,
            "price-effect": price_effect,
            "legs": [
                {
                    "instrument-type": "Equity Option",
                    "symbol": tasty_option_symbol(underlying, leg),
                    "quantity": str(int(_leg_value(leg, "quantity") or 1)),
                    "action": _action_for_leg(leg, intent_type),
                }
                for leg in legs
            ],
        }
        if external_identifier:
            payload["external-identifier"] = external_identifier
        return payload

    def _market_metric(self, underlying: str) -> dict[str, Any]:
        self._require_available()
        symbol = underlying.upper()
        if symbol in self._market_metric_cache:
            return self._market_metric_cache[symbol]
        payload = self._get("/market-metrics", params={"symbols": symbol})
        items = _items(payload)
        for item in items:
            if isinstance(item, dict) and str(item.get("symbol") or "").upper() == symbol:
                self._market_metric_cache[symbol] = item
                return item
        metric = items[0] if items else {}
        self._market_metric_cache[symbol] = metric if isinstance(metric, dict) else {}
        return self._market_metric_cache[symbol]

    def _token(self) -> str:
        if self._access_token and self._expires_at > time.time() + 60:
            return self._access_token
        cached = _read_json(self.session_file)
        expires_at = float((cached or {}).get("expiration_timestamp") or 0)
        if cached and cached.get("access_token") and expires_at > time.time() + 60:
            self._access_token = str(cached["access_token"])
            self._expires_at = expires_at
            return self._access_token
        payload: dict[str, Any] = {
            "refresh_token": self.refresh_token,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }
        if self.client_id:
            payload["client_id"] = self.client_id
        if self.oauth_scopes:
            payload["scope"] = " ".join(self.oauth_scopes)
        response = self._session.post(f"{self.api_base_url}/oauth/token", json=payload, headers=self._headers(), timeout=30)
        if not response.ok:
            raise RuntimeError(f"tastytrade OAuth refresh failed status={response.status_code}: {_error_body(response)}")
        payload = response.json()
        self._access_token = str(payload.get("access_token") or "")
        self._expires_at = time.time() + int(payload.get("expires_in") or 900)
        if not self._access_token:
            raise RuntimeError("tastytrade OAuth response did not contain access_token")
        _write_json(self.session_file, {"access_token": self._access_token, "expiration_timestamp": int(self._expires_at)})
        return self._access_token

    def _get(self, endpoint: str, *, params: Any = None) -> dict[str, Any]:
        return self._request("GET", endpoint, params=params)

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", endpoint, json_data=payload)

    def _delete(self, endpoint: str) -> dict[str, Any]:
        return self._request("DELETE", endpoint)

    def _request(self, method: str, endpoint: str, *, params: Any = None, json_data: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._session.request(
            method,
            f"{self.api_base_url}{endpoint}",
            headers=self._headers(token=self._token()),
            params=params,
            json=json_data,
            timeout=30,
        )
        if not response.ok:
            body = _redact_error_text(response.text[:1000] if response.text else "", self.account_number)
            safe_endpoint = _redact_error_text(endpoint, self.account_number)
            raise RuntimeError(f"tastytrade API {method} {safe_endpoint} failed status={response.status_code}: {body}")
        return response.json() if response.text else {}

    def _account_number(self) -> str:
        if self.account_number:
            return self.account_number
        cached = _read_json(self.account_cache_file)
        if cached and cached.get("account_number"):
            self.account_number = str(cached["account_number"])
            return self.account_number
        accounts = _items(self._get("/customers/me/accounts"))
        if not accounts:
            raise RuntimeError("No tastytrade account returned")
        account = accounts[0].get("account") if isinstance(accounts[0], dict) else {}
        account_number = str((account or accounts[0]).get("account-number") or "")
        if not account_number:
            raise RuntimeError("No tastytrade account-number returned")
        self.account_number = account_number
        _write_json(self.account_cache_file, {"account_number": account_number})
        return self.account_number

    def _headers(self, *, token: str = "") -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.api_version:
            headers["Accept-Version"] = self.api_version
        return headers

    def _require_available(self) -> None:
        if not self.available():
            raise RuntimeError("tastytrade OAuth credentials are not configured")


def _action_for_leg(leg: OptionLeg | dict[str, Any], intent_type: str) -> str:
    side = str(_leg_value(leg, "side") or "").lower()
    open_close = "Open" if intent_type == "open" else "Close"
    return ("Buy" if side == "buy" else "Sell") + f" to {open_close}"


def _price_from_net_credit(net_credit: float) -> tuple[str, str]:
    return f"{abs(float(net_credit)):.2f}", "Credit" if float(net_credit) > 0 else "Debit"


def _price_from_ticket_limit(limit_price: float) -> tuple[str, str]:
    return f"{abs(float(limit_price)):.2f}", "Credit" if float(limit_price) < 0 else "Debit"


def _preflight_result(payload: dict[str, Any], response: dict[str, Any], *, default_bpr: float) -> PreflightResult:
    errors = _find_list(response, ("errors",))
    bpr = _find_number(
        response,
        ("impact", "change-in-buying-power", "isolated-order-margin-requirement", "buying-power-requirement"),
        default=default_bpr,
    )
    if errors:
        return PreflightResult(ok=False, bpr=abs(bpr), message="tastytrade preflight returned errors", raw={"request": payload, "response": response})
    return PreflightResult(ok=True, bpr=round(abs(bpr), 2), message="tastytrade preflight ok", raw={"request": payload, "response": response})


def _order_response(response: dict[str, Any]) -> dict[str, Any]:
    data = _data(response)
    order = data.get("order") if isinstance(data, dict) else {}
    if not isinstance(order, dict):
        order = data if isinstance(data, dict) else {}
    order_id = str(order.get("id") or order.get("order-id") or "")
    fallback_status = data.get("status") if isinstance(data, dict) else ""
    status = str(order.get("status") or fallback_status)
    return {"orderId": order_id, "status": status.upper(), "raw": response}


def _data(payload: dict[str, Any]) -> Any:
    return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload


def _items(payload: dict[str, Any]) -> list[Any]:
    data = _data(payload)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    if isinstance(data, list):
        return data
    return []


def _leg_value(leg: OptionLeg | dict[str, Any], key: str) -> Any:
    if isinstance(leg, dict):
        return leg.get(key)
    return getattr(leg, key)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in (value or []) if str(item).strip()]


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _error_body(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:1000] if response.text else response.reason
    code = payload.get("error_code") or payload.get("error") or payload.get("code") or ""
    description = payload.get("error_description") or payload.get("message") or ""
    if code or description:
        return f"{code}: {description}".strip(": ")
    return json.dumps(payload, sort_keys=True)[:1000]


def _redact_error_text(value: str, account_number: str = "") -> str:
    redacted = value
    if account_number:
        redacted = redacted.replace(account_number, "<account>")
    return re.sub(r"\b\d[A-Z0-9]{7}\b", "<account>", redacted)


def _find_number(payload: Any, keys: tuple[str, ...], *, default: float) -> float:
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in keys:
                if key in item:
                    parsed = _as_float(item[key], default)
                    if parsed:
                        return parsed
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return default


def _find_optional_number(payload: Any, keys: tuple[str, ...]) -> float | None:
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in keys:
                if key in item:
                    try:
                        return float(item[key])
                    except (TypeError, ValueError):
                        continue
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return None


def _find_list(payload: Any, keys: tuple[str, ...]) -> list[Any]:
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in keys:
                value = item.get(key)
                if isinstance(value, list):
                    return value
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return []


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
