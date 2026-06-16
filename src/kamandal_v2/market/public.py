"""Public.com market, preflight, and guarded order adapter."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Sequence

import requests

from kamandal_v2.domain.models import Candidate, ChainSnapshot, Greeks, OptionLeg, OptionQuote, PortfolioState, PreflightResult, utc_now
from kamandal_v2.live.pricing import candidate_entry_limit_price, entry_price_metadata
from kamandal_v2.paths import resolve_path


def occ_symbol(underlying: str, leg: OptionLeg) -> str:
    expiration = leg.expiration.replace("-", "")
    if len(expiration) != 8:
        raise ValueError(f"Invalid option expiration: {leg.expiration}")
    flag = "C" if leg.option_type.lower() == "call" else "P"
    strike = int(round(float(leg.strike) * 1000))
    return f"{underlying.upper()}{expiration[2:]}{flag}{strike:08d}"


def parse_occ_symbol(symbol: str) -> dict[str, Any]:
    match = re.match(r"^([A-Z.]+?)(\d{6})([CP])(\d{8})$", symbol.upper())
    if not match:
        raise ValueError(f"Unsupported OCC option symbol: {symbol}")
    root, yymmdd, flag, strike = match.groups()
    return {
        "underlying": root,
        "expiration": f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}",
        "option_type": "call" if flag == "C" else "put",
        "strike": int(strike) / 1000.0,
    }


def _candidate_risk_bpr(candidate: Candidate, public_bpr: float) -> float:
    """Use Public preflight as a ceiling signal, not as a credit-spread shrinker."""

    return round(max(abs(float(public_bpr or 0.0)), float(candidate.estimated_bpr or 0.0)), 2)


class PublicAdapter:
    def __init__(self, config: dict[str, Any], *, expiration_dates: Sequence[str] | None = None) -> None:
        self._config = config
        public_cfg = ((config.get("broker") or {}).get("public") or {})
        self.secret_token = str(public_cfg.get("secret_token") or "")
        self.api_base_url = str(public_cfg.get("api_base_url") or "https://api.public.com").rstrip("/")
        self.auth_endpoint = str(public_cfg.get("auth_endpoint") or "https://api.public.com/userapiauthservice/personal/access-tokens")
        self.account_id = str(public_cfg.get("account_id") or "")
        self.session_file = resolve_path(str(public_cfg.get("session_file") or "config/public_session.json"))
        self.account_cache_file = resolve_path(str(public_cfg.get("account_cache_file") or "config/public_account.json"))
        self.token_validity_minutes = int(public_cfg.get("token_validity_minutes") or 60)
        self.expiration_dates = list(expiration_dates or _configured_expiration_dates(public_cfg))
        self._session = requests.Session()
        self._access_token = ""
        self._expires_at = 0.0

    def available(self) -> bool:
        return bool(self.secret_token)

    def account_state(self) -> PortfolioState:
        self._require_available()
        payload = self.portfolio_payload()
        buying_power_raw = _find_optional_number(payload, ("optionsBuyingPower", "buyingPower", "cashOnlyBuyingPower"))
        account_size_raw = _find_optional_number(payload, ("netLiquidationValue", "equityValue", "accountValue"))
        account_size = account_size_raw if account_size_raw is not None else _equity_total(payload)
        buying_power = buying_power_raw if buying_power_raw is not None else account_size
        if (account_size is None or account_size <= 0) and (buying_power is None or buying_power <= 0):
            raise RuntimeError("Public account payload did not include account size or buying power")
        account_size = float(account_size or buying_power or 0.0)
        buying_power = float(buying_power if buying_power is not None else account_size)
        positions = _find_list(payload, ("positions", "optionPositions", "equityPositions"))
        return PortfolioState(
            account_size=account_size,
            buying_power=buying_power,
            bpr_used=max(account_size - buying_power, 0.0),
            positions_count=len(positions),
            greeks=Greeks(),
            per_underlying_bpr={},
        )

    def portfolio_payload(self) -> dict[str, Any]:
        self._require_available()
        return self._get(f"/userapigateway/trading/{self._account_id()}/portfolio/v2")

    def broker_positions(self) -> list[dict[str, Any]]:
        payload = self.portfolio_payload()
        raw_positions = _find_list(payload, ("positions", "optionPositions", "equityPositions"))
        positions = []
        for index, raw in enumerate(raw_positions):
            if not isinstance(raw, dict):
                continue
            normalized = _normalize_public_position(raw, index=index)
            positions.append(normalized)
        return positions

    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        self._require_available()
        symbol = underlying.upper()
        underlying_price = self._underlying_price(symbol)
        quotes: list[OptionQuote] = []
        seen: set[str] = set()
        for expiration in self.expiration_dates:
            try:
                chain = self._post(
                    f"/userapigateway/marketdata/{self._account_id()}/option-chain",
                    {
                        "instrument": {"symbol": symbol, "type": "EQUITY"},
                        "expirationDate": expiration,
                    },
                )
            except Exception:
                continue
            quotes.extend(self._parse_chain_items(chain.get("calls", []) or [], seen))
            quotes.extend(self._parse_chain_items(chain.get("puts", []) or [], seen))
        if not quotes:
            raise RuntimeError(f"Public returned no option quotes for {symbol}")
        return ChainSnapshot(
            chain_snapshot_id=f"public_chain_{symbol}_{date.today().isoformat()}",
            underlying=symbol,
            captured_at=utc_now(),
            underlying_price=underlying_price,
            quotes=quotes,
            source="public",
        )

    def iv_percentile(self, underlying: str) -> float | None:
        return None

    def iv_rank(self, underlying: str) -> float | None:
        return None

    def iv_abs(self, underlying: str) -> float | None:
        return None

    def event_status(self, underlying: str) -> str:
        return "unknown"

    def preflight(self, candidate: Candidate) -> PreflightResult:
        self._require_available()
        payload = self._order_payload(candidate)
        try:
            endpoint = "single-leg" if len(candidate.legs) == 1 else "multi-leg"
            response = self._post(
                f"/userapigateway/trading/{self._account_id()}/preflight/{endpoint}",
                payload,
            )
            raw_bpr = _find_number(response, ("buyingPowerRequirement", "buyingPowerEffect", "estimatedBuyingPower"), default=candidate.estimated_bpr)
            bpr = _candidate_risk_bpr(candidate, raw_bpr)
            return PreflightResult(
                ok=True,
                bpr=bpr,
                message="Public preflight ok",
                raw={
                    "request": payload,
                    "response": response,
                    "entry_pricing": entry_price_metadata(candidate, self._config),
                    "public_bpr_raw": raw_bpr,
                },
            )
        except Exception as exc:  # noqa: BLE001
            if _is_nickel_increment_rejection(exc):
                retry_payload = dict(payload)
                retry_payload["limitPrice"] = candidate_entry_limit_price(candidate, self._config, nickel=True)
                try:
                    response = self._post(
                        f"/userapigateway/trading/{self._account_id()}/preflight/{endpoint}",
                        retry_payload,
                    )
                    raw_bpr = _find_number(response, ("buyingPowerRequirement", "buyingPowerEffect", "estimatedBuyingPower"), default=candidate.estimated_bpr)
                    bpr = _candidate_risk_bpr(candidate, raw_bpr)
                    return PreflightResult(
                        ok=True,
                        bpr=bpr,
                        message="Public preflight ok after nickel tick retry",
                        raw={
                            "request": retry_payload,
                            "response": response,
                            "retry_reason": str(exc),
                            "entry_pricing": entry_price_metadata(candidate, self._config),
                            "public_bpr_raw": raw_bpr,
                        },
                    )
                except Exception as retry_exc:  # noqa: BLE001
                    message = f"Public preflight failed: {_safe_public_error(retry_exc)}"
                    raw = {"source": "public", "first_error": _safe_public_error(exc)}
                    raw.update(_public_invalid_order_raw(message))
                    return PreflightResult(ok=False, bpr=candidate.estimated_bpr, message=message, raw=raw)
            message = f"Public preflight failed: {_safe_public_error(exc)}"
            raw = {"source": "public"}
            raw.update(_public_invalid_order_raw(message))
            return PreflightResult(ok=False, bpr=candidate.estimated_bpr, message=message, raw=raw)

    def preflight_ticket(self, ticket: dict[str, Any]) -> PreflightResult:
        self._require_available()
        try:
            payload = dict(ticket.get("submit_payload") or {})
            payload.pop("orderId", None)
            if "type" in payload and "orderType" not in payload:
                payload["orderType"] = payload.pop("type")
            endpoint = "single-leg" if payload.get("instrument") else "multi-leg"
            response = self._post(
                f"/userapigateway/trading/{self._account_id()}/preflight/{endpoint}",
                payload,
            )
            bpr = _find_number(response, ("buyingPowerRequirement", "buyingPowerEffect", "estimatedBuyingPower"), default=0.0)
            if not bpr:
                return PreflightResult(ok=False, bpr=0.0, message="Public preflight missing buyingPowerRequirement", raw={"request": payload, "response": response})
            return PreflightResult(ok=True, bpr=round(abs(bpr), 2), message="Public ticket preflight ok", raw={"request": payload, "response": response})
        except Exception as exc:  # noqa: BLE001
            message = f"Public ticket preflight failed: {_safe_public_error(exc)}"
            raw = {"source": "public"}
            raw.update(_public_invalid_order_raw(message))
            return PreflightResult(ok=False, bpr=0.0, message=message, raw=raw)

    def place_order_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        self._require_available()
        submit_payload = dict(ticket.get("submit_payload") or {})
        if not submit_payload.get("orderId"):
            raise ValueError("live order ticket missing orderId")
        endpoint = "order" if len(submit_payload.get("legs") or []) <= 1 and submit_payload.get("instrument") else "order/multileg"
        submit_payload = _normalise_order_payload(submit_payload, multileg=endpoint == "order/multileg")
        return self._post(f"/userapigateway/trading/{self._account_id()}/{endpoint}", submit_payload)

    def get_order(self, order_id: str) -> dict[str, Any]:
        self._require_available()
        return self._get(f"/userapigateway/trading/{self._account_id()}/order/{order_id}")

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        self._require_available()
        return self._delete(f"/userapigateway/trading/{self._account_id()}/order/{order_id}")

    def _order_payload(self, candidate: Candidate) -> dict[str, Any]:
        quantity = "1"
        limit_price = candidate_entry_limit_price(candidate, self._config)
        if len(candidate.legs) == 1:
            leg = candidate.legs[0]
            return {
                "expiration": {"timeInForce": "DAY"},
                "quantity": quantity,
                "limitPrice": limit_price,
                "instrument": {"symbol": occ_symbol(candidate.underlying, leg), "type": "OPTION"},
                "orderSide": leg.side.upper(),
                "openCloseIndicator": "OPEN",
                "orderType": "LIMIT",
            }
        return {
            "quantity": quantity,
            "limitPrice": limit_price,
            "expiration": {"timeInForce": "DAY"},
            "orderType": "LIMIT",
            "legs": [
                {
                    "instrument": {"symbol": occ_symbol(candidate.underlying, leg), "type": "OPTION"},
                    "side": leg.side.upper(),
                    "openCloseIndicator": "OPEN",
                    "ratioQuantity": int(leg.quantity or 1),
                }
                for leg in candidate.legs
            ],
        }

    def _parse_chain_items(self, items: Sequence[dict[str, Any]], seen: set[str]) -> list[OptionQuote]:
        parsed: list[OptionQuote] = []
        greeks_missing: list[tuple[str, int]] = []
        for item in items:
            if str(item.get("outcome", "SUCCESS")).upper() != "SUCCESS":
                continue
            instrument = item.get("instrument") or {}
            option_symbol = str(instrument.get("symbol") or "").upper()
            if not option_symbol or option_symbol in seen:
                continue
            seen.add(option_symbol)
            details = item.get("optionDetails") or {}
            greeks = (details.get("greeks") or {}) if isinstance(details, dict) else {}
            parsed_symbol = parse_occ_symbol(option_symbol)
            quote = OptionQuote(
                underlying=parsed_symbol["underlying"],
                expiration=parsed_symbol["expiration"],
                option_type=parsed_symbol["option_type"],
                strike=_as_float(details.get("strikePrice"), parsed_symbol["strike"]),
                bid=_as_float(item.get("bid")),
                ask=_as_float(item.get("ask")),
                delta=_as_float(greeks.get("delta")),
                gamma=_as_float(greeks.get("gamma")),
                theta=_as_float(greeks.get("theta")),
                vega=_as_float(greeks.get("vega")),
                iv=_as_float(greeks.get("impliedVolatility")),
                open_interest=int(_as_float(item.get("openInterest"), _as_float(details.get("openInterest"), 0.0))),
                volume=int(_as_float(item.get("volume"), 0.0)),
            )
            if quote.bid <= 0 and quote.ask <= 0:
                continue
            if quote.delta == 0.0 and quote.gamma == 0.0 and quote.theta == 0.0:
                greeks_missing.append((option_symbol, len(parsed)))
            parsed.append(quote)

        if greeks_missing:
            symbols = [symbol for symbol, _index in greeks_missing]
            greeks_by_symbol = self._greeks_batch(symbols)
            for symbol, index in greeks_missing:
                greeks = greeks_by_symbol.get(symbol) or {}
                quote = parsed[index]
                parsed[index] = OptionQuote(
                    underlying=quote.underlying,
                    expiration=quote.expiration,
                    option_type=quote.option_type,
                    strike=quote.strike,
                    bid=quote.bid,
                    ask=quote.ask,
                    delta=_as_float(greeks.get("delta"), quote.delta),
                    gamma=_as_float(greeks.get("gamma"), quote.gamma),
                    theta=_as_float(greeks.get("theta"), quote.theta),
                    vega=_as_float(greeks.get("vega"), quote.vega),
                    iv=_as_float(greeks.get("impliedVolatility"), quote.iv),
                    open_interest=quote.open_interest,
                    volume=quote.volume,
                )
        return parsed

    def _underlying_price(self, symbol: str) -> float:
        payload = self._post(
            f"/userapigateway/marketdata/{self._account_id()}/quotes",
            {"instruments": [{"symbol": symbol, "type": "EQUITY"}]},
        )
        for item in payload.get("quotes", []) or []:
            instrument = item.get("instrument") or {}
            if str(instrument.get("symbol") or "").upper() != symbol:
                continue
            price = _as_float(item.get("last"))
            if price > 0:
                return price
            bid = _as_float(item.get("bid"))
            ask = _as_float(item.get("ask"))
            if bid > 0 and ask > 0:
                return (bid + ask) / 2.0
        raise RuntimeError(f"Public returned no equity quote for {symbol}")

    def _greeks_batch(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not symbols:
            return {}
        payload = self._get(
            f"/userapigateway/option-details/{self._account_id()}/greeks",
            params=[("osiSymbols", symbol) for symbol in symbols],
        )
        result: dict[str, dict[str, Any]] = {}
        for item in payload.get("greeks", []) or []:
            symbol = str(item.get("symbol") or "")
            if symbol:
                result[symbol] = item.get("greeks") or {}
        return result

    def _token(self) -> str:
        if self._access_token and self._expires_at > time.time() + 300:
            return self._access_token
        cached = _read_json(self.session_file)
        expires_at = float((cached or {}).get("expiration_timestamp") or 0)
        if cached and cached.get("access_token") and expires_at > time.time() + 300:
            self._access_token = str(cached["access_token"])
            self._expires_at = expires_at
            return self._access_token
        response = self._session.post(
            self.auth_endpoint,
            json={"secret": self.secret_token, "validityInMinutes": self.token_validity_minutes},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self._access_token = str(payload.get("accessToken") or "")
        self._expires_at = time.time() + int(payload.get("expiresIn") or self.token_validity_minutes * 60)
        if not self._access_token:
            raise RuntimeError("Public auth response did not contain accessToken")
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
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
            params=params,
            json=json_data,
            timeout=30,
        )
        if not response.ok:
            body = response.text[:1000] if response.text else ""
            raise RuntimeError(f"Public API {method} {endpoint} failed status={response.status_code}: {body}")
        return response.json() if response.text else {}

    def _account_id(self) -> str:
        if self.account_id:
            return self.account_id
        cached = _read_json(self.account_cache_file)
        if cached and cached.get("accountId"):
            self.account_id = str(cached["accountId"])
            return self.account_id
        payload = self._get("/userapigateway/trading/account")
        accounts = payload.get("accounts") or []
        if not accounts or not accounts[0].get("accountId"):
            raise RuntimeError("No Public accountId returned")
        self.account_id = str(accounts[0]["accountId"])
        _write_json(self.account_cache_file, {"accountId": self.account_id})
        return self.account_id

    def _require_available(self) -> None:
        if not self.available():
            raise RuntimeError("Public credentials are not configured")


def _configured_expiration_dates(public_cfg: dict[str, Any]) -> list[str]:
    start_dte = int(public_cfg.get("option_chain_start_dte") or 21)
    end_dte = int(public_cfg.get("option_chain_end_dte") or 90)
    max_expirations = int(public_cfg.get("option_chain_max_expirations") or 8)
    return _nearest_fridays(start_dte=start_dte, end_dte=end_dte, max_expirations=max_expirations)


def _nearest_fridays(start_dte: int = 21, end_dte: int = 90, max_expirations: int = 8) -> list[str]:
    today = date.today()
    expirations: list[str] = []
    for days_ahead in range(start_dte, end_dte + 1):
        candidate = today + timedelta(days=days_ahead)
        if candidate.weekday() == 4:
            expirations.append(candidate.isoformat())
    return expirations[:max_expirations]


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


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _find_number(payload: dict[str, Any], keys: tuple[str, ...], *, default: float) -> float:
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
                        return float(str(item[key]).replace(",", ""))
                    except (TypeError, ValueError):
                        continue
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return None


def _equity_total(payload: dict[str, Any]) -> float | None:
    equity_rows = payload.get("equity")
    if not isinstance(equity_rows, list):
        return None
    total = 0.0
    found = False
    for row in equity_rows:
        if not isinstance(row, dict) or "value" not in row:
            continue
        try:
            total += float(str(row["value"]).replace(",", ""))
            found = True
        except (TypeError, ValueError):
            continue
    return total if found else None


def _candidate_raw_limit_price(candidate: Candidate) -> str:
    if len(candidate.legs) > 1 and candidate.net_credit > 0:
        return f"-{abs(candidate.net_credit):.2f}"
    return f"{abs(candidate.net_credit):.2f}"


def _candidate_nickel_limit_price(candidate: Candidate) -> str:
    if len(candidate.legs) > 1 and candidate.net_credit > 0:
        return f"-{_nickel_price(abs(candidate.net_credit), rounding=ROUND_FLOOR)}"
    return _nickel_price(abs(candidate.net_credit), rounding=ROUND_CEILING)


def _is_nickel_increment_rejection(exc: Exception) -> bool:
    message = str(exc)
    return "increments of $0.05" in message or '"code":104' in message or "'code': 104" in message


def _nickel_price(value: float, *, rounding: str) -> str:
    price = Decimal(str(value))
    nickel = Decimal("0.05")
    rounded = (price / nickel).to_integral_value(rounding=rounding) * nickel
    return f"{rounded.quantize(Decimal('0.01'))}"


def _normalise_order_payload(payload: dict[str, Any], *, multileg: bool) -> dict[str, Any]:
    if multileg and "orderType" in payload and "type" not in payload:
        payload["type"] = payload.pop("orderType")
    if not multileg and "type" in payload and "orderType" not in payload:
        payload["orderType"] = payload.pop("type")
    if "quantity" in payload:
        payload["quantity"] = str(payload["quantity"])
    return payload


def _safe_public_error(exc: Exception) -> str:
    return re.sub(r"/trading/[^/]+/", "/trading/<account>/", str(exc))


def _public_invalid_order_raw(message: str) -> dict[str, Any]:
    details = _public_invalid_order_details(message)
    return {"public_invalid_order": details} if details else {}


def _public_invalid_order_details(message: str) -> dict[str, Any]:
    match = re.search(
        r"cancel an invalid order.*?cancel the order to close\s+([A-Z.]+)\s+\$([0-9.]+)\s+(Call|Put)\s+([A-Z][a-z]{2}\s+\d{1,2},\s+'\d{2})",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return {}
    underlying, strike, option_type, expiration_label = match.groups()
    return {
        "action_required": "cancel_invalid_close_order",
        "underlying": underlying.upper(),
        "strike": float(strike),
        "option_type": option_type.lower(),
        "expiration_label": expiration_label,
        "requires_explicit_broker_cancel_approval": True,
    }


def _normalize_public_position(raw: dict[str, Any], *, index: int) -> dict[str, Any]:
    instrument = _find_first_dict(raw, ("instrument", "security", "option", "equity"))
    cost_basis = raw.get("costBasis") if isinstance(raw.get("costBasis"), dict) else {}
    symbol = str(
        instrument.get("symbol")
        or raw.get("symbol")
        or raw.get("osiSymbol")
        or raw.get("occSymbol")
        or ""
    ).upper()
    position_type = str(instrument.get("type") or raw.get("type") or raw.get("instrumentType") or "").upper()
    quantity = _find_number(raw, ("quantity", "qty", "filledQuantity", "longQuantity", "shortQuantity"), default=0.0)
    if not quantity:
        long_qty = _find_number(raw, ("longQuantity", "longQty"), default=0.0)
        short_qty = _find_number(raw, ("shortQuantity", "shortQty"), default=0.0)
        quantity = long_qty - short_qty
    side = str(raw.get("side") or raw.get("positionSide") or "").lower()
    if side in {"short", "sell"} and quantity > 0:
        quantity *= -1
    parsed: dict[str, Any] = {}
    if symbol:
        try:
            parsed = parse_occ_symbol(symbol)
            position_type = "OPTION"
        except ValueError:
            parsed = {"underlying": symbol}
    return {
        "broker": "public",
        "raw_index": index,
        "symbol": symbol,
        "occ_symbol": symbol if position_type == "OPTION" or parsed.get("expiration") else "",
        "underlying": str(parsed.get("underlying") or symbol).upper(),
        "expiration": parsed.get("expiration"),
        "option_type": parsed.get("option_type"),
        "strike": parsed.get("strike"),
        "quantity": quantity,
        "asset_type": "option" if parsed.get("expiration") else "equity",
        "average_price": _find_optional_number(
            raw,
            ("averagePrice", "average-price", "averageOpenPrice", "costBasisPrice"),
        ) or _find_optional_number(cost_basis, ("unitCost", "unit-cost", "averagePrice")),
        "cost_basis": _find_optional_number(cost_basis, ("totalCost", "total-cost", "costBasis")),
        "current_value": _find_optional_number(raw, ("currentValue", "marketValue", "value")),
        "strategy_ids": _as_string_list(raw.get("strategyIds") or raw.get("strategy_ids")),
        "raw": raw,
    }


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if value in (None, ""):
        return []
    return [str(value)]


def _find_first_dict(payload: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in keys:
                value = item.get(key)
                if isinstance(value, dict):
                    return value
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return {}


def _find_list(payload: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
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
