"""Live position marking and exit decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from kamandal_v2.domain.models import Playbook
from kamandal_v2.events.earnings import EarningsStore

CONTRACT_MULTIPLIER = 100.0


@dataclass(frozen=True)
class LiveExitPolicy:
    profit_target_trigger_pct: float = 95.0
    min_profit_to_trigger: float = 5.0
    require_fresh_quotes: bool = True
    max_loss_action: str = "review"
    max_loss_requires_confirmation: bool = True
    default_max_loss_multiple_credit: float | None = None
    default_max_loss_multiple_debit: float | None = None
    short_strike_buffer_pct: float = 0.0
    loss_watch_max_leg_bid_ask_pct: float = 1.0
    loss_watch_confirmations_required: int = 2
    loss_watch_window_minutes: int = 120


def live_exit_policy(config: dict[str, Any] | None) -> LiveExitPolicy:
    live_cfg = ((config or {}).get("live") or {})
    raw = live_cfg.get("exit_pricing") or {}
    if not isinstance(raw, dict):
        raw = {}
    return LiveExitPolicy(
        profit_target_trigger_pct=_as_float(raw.get("profit_target_trigger_pct"), 95.0),
        min_profit_to_trigger=_as_float(raw.get("min_profit_to_trigger"), 5.0),
        require_fresh_quotes=_as_bool(raw.get("require_fresh_quotes"), True),
        max_loss_action=_max_loss_action(raw.get("max_loss_action")),
        max_loss_requires_confirmation=_as_bool(raw.get("max_loss_requires_confirmation"), True),
        default_max_loss_multiple_credit=_optional_positive_float(raw.get("default_max_loss_multiple_credit")),
        default_max_loss_multiple_debit=_optional_positive_float(raw.get("default_max_loss_multiple_debit")),
        short_strike_buffer_pct=max(_as_float(raw.get("short_strike_buffer_pct"), 0.0), 0.0),
        loss_watch_max_leg_bid_ask_pct=max(_as_float(raw.get("loss_watch_max_leg_bid_ask_pct"), 1.0), 0.0),
        loss_watch_confirmations_required=max(_as_int(raw.get("loss_watch_confirmations_required"), 2), 1),
        loss_watch_window_minutes=max(_as_int(raw.get("loss_watch_window_minutes"), 120), 1),
    )


def mark_live_group(
    group: dict[str, Any],
    quote_map: dict[tuple[str, str, float], dict[str, Any]],
    playbook: Playbook | None,
    *,
    quote_fresh: bool,
    config: dict[str, Any],
    underlying_price: float | None = None,
) -> dict[str, Any]:
    candidate = group.get("candidate") or {}
    legs = list(candidate.get("legs") or [])
    entry_net = _entry_net_cashflow(group) * CONTRACT_MULTIPLIER
    entry_value = abs(entry_net)
    entry_kind = "credit" if entry_net > 0 else "debit"
    close_mid_net = 0.0
    close_natural_net = 0.0
    missing_quotes: list[str] = []
    marked_legs = []
    max_leg_bid_ask_pct = 0.0
    short_leg_delta_abs: float | None = None
    for leg in legs:
        key = (str(leg.get("expiration")), str(leg.get("option_type")), float(leg.get("strike") or 0.0))
        quote = quote_map.get(key)
        if quote is None:
            missing_quotes.append(":".join(map(str, key)))
            marked_legs.append({"leg": leg, "missing_quote": True})
            continue
        qty = int(leg.get("quantity") or 1)
        bid = float(quote.get("bid") or 0.0)
        ask = float(quote.get("ask") or 0.0)
        mid = (bid + ask) / 2.0
        if mid > 0:
            max_leg_bid_ask_pct = max(max_leg_bid_ask_pct, (ask - bid) / mid)
        if str(leg.get("side") or "").lower() == "sell":
            raw_delta = quote.get("delta", leg.get("delta"))
            try:
                delta_abs = abs(float(raw_delta))
            except (TypeError, ValueError):
                delta_abs = None
            if delta_abs is not None:
                short_leg_delta_abs = max(short_leg_delta_abs or 0.0, delta_abs)
        if str(leg.get("side") or "").lower() == "sell":
            leg_mid_net = -mid * qty * CONTRACT_MULTIPLIER
            leg_natural_net = -ask * qty * CONTRACT_MULTIPLIER
        else:
            leg_mid_net = mid * qty * CONTRACT_MULTIPLIER
            leg_natural_net = bid * qty * CONTRACT_MULTIPLIER
        close_mid_net += leg_mid_net
        close_natural_net += leg_natural_net
        marked_legs.append({
            "leg": leg,
            "quote": quote,
            "close_mid_net": round(leg_mid_net, 2),
            "close_natural_net": round(leg_natural_net, 2),
        })

    policy = live_exit_policy(config)
    pnl_mid = entry_net + close_mid_net
    pnl_natural = entry_net + close_natural_net
    profit_target_pct = normalize_profit_target_pct(playbook.profit_target_pct if playbook else 50.0)
    target_profit = entry_value * (profit_target_pct / 100.0)
    target_close_net = target_profit - entry_net
    progress = (pnl_mid / target_profit) * 100.0 if target_profit > 0 else 0.0
    loss = max(-pnl_mid, 0.0)
    natural_loss = max(-pnl_natural, 0.0)
    max_loss_multiple = playbook.max_loss_multiple if playbook else None
    if max_loss_multiple is None:
        max_loss_multiple = (
            policy.default_max_loss_multiple_credit if entry_kind == "credit" else policy.default_max_loss_multiple_debit
        )
    short_strike_state = _short_strike_state(legs, underlying_price, policy.short_strike_buffer_pct)
    return {
        "group_id": group.get("group_id"),
        "underlying": group.get("underlying") or candidate.get("underlying"),
        "playbook_id": group.get("playbook_id") or candidate.get("playbook_id"),
        "structure": group.get("structure") or candidate.get("structure"),
        "opened_at": group.get("opened_at"),
        "entry_kind": entry_kind,
        "entry_net_cashflow": round(entry_net, 2),
        "entry_value": round(entry_value, 2),
        "close_mid_net": round(close_mid_net, 2),
        "close_natural_net": round(close_natural_net, 2),
        "pnl_mid": round(pnl_mid, 2),
        "pnl_natural": round(pnl_natural, 2),
        "pnl_pct_of_entry_value": round((pnl_mid / entry_value) * 100.0, 2) if entry_value > 0 else 0.0,
        "loss_mid": round(loss, 2),
        "loss_natural": round(natural_loss, 2),
        "loss_multiple_of_entry": round((loss / entry_value), 4) if entry_value > 0 else 0.0,
        "natural_loss_multiple_of_entry": round((natural_loss / entry_value), 4) if entry_value > 0 else 0.0,
        "close_debit_multiple_of_entry": round((abs(close_mid_net) / entry_value), 4) if entry_kind == "credit" and close_mid_net < 0 and entry_value > 0 else 0.0,
        "natural_close_debit_multiple_of_entry": round((abs(close_natural_net) / entry_value), 4) if entry_kind == "credit" and close_natural_net < 0 and entry_value > 0 else 0.0,
        "max_loss_multiple": max_loss_multiple,
        "max_loss_watch": _max_loss_watch(entry_kind, close_mid_net, loss, entry_value, max_loss_multiple),
        "underlying_price": underlying_price,
        "short_strike_state": short_strike_state,
        "short_leg_delta_abs": short_leg_delta_abs,
        "max_leg_bid_ask_pct": round(max_leg_bid_ask_pct, 4),
        "profit_target_pct": profit_target_pct,
        "target_profit": round(target_profit, 2),
        "target_close_net": round(target_close_net, 2),
        "target_close_value": round(abs(target_close_net), 2),
        "target_progress_pct": round(progress, 2),
        "trigger_progress_pct": policy.profit_target_trigger_pct,
        "quote_fresh": bool(quote_fresh),
        "missing_quotes": missing_quotes,
        "legs": legs,
        "marked_legs": marked_legs,
        "dte": position_dte(group),
    }


def live_exit_decision(
    mark: dict[str, Any],
    playbook: Playbook | None,
    earnings: EarningsStore,
    config: dict[str, Any],
) -> dict[str, Any]:
    policy = live_exit_policy(config)
    action = "hold"
    reason = "no_exit"
    urgency = "low"
    close_net = float(mark.get("close_mid_net") or 0.0)
    missing_quotes = bool(mark.get("missing_quotes"))
    if policy.require_fresh_quotes and not bool(mark.get("quote_fresh")):
        reason = "fresh_quotes_missing"
        urgency = "normal"
    elif missing_quotes:
        reason = "missing_quotes"
        urgency = "normal"
    else:
        exit_pre_event_days = playbook.exit_pre_event_days if playbook else None
        days_to_earnings = earnings_days(mark, earnings)
        dte = mark.get("dte") or {}
        exit_dte_min = int(playbook.exit_dte_min if playbook else 21)
        half_time_exit = bool(playbook.half_time_exit) if playbook else True
        max_loss_decision = _max_loss_decision(mark, policy)
        if max_loss_decision:
            action = str(max_loss_decision["action"])
            reason = str(max_loss_decision["reason"])
            urgency = str(max_loss_decision["urgency"])
        elif exit_pre_event_days is not None and days_to_earnings is not None and days_to_earnings <= exit_pre_event_days:
            action = "close"
            reason = "pre_event"
            urgency = "high"
        elif _profit_target_reached(mark, policy):
            action = "close"
            reason = "profit_target"
            urgency = "normal"
            close_net = _target_close_limit_net(mark)
        elif dte.get("remaining") is not None and dte["remaining"] <= exit_dte_min:
            action = "close"
            reason = "dte_target"
            urgency = "normal"
        elif half_time_exit and dte.get("remaining") is not None and dte.get("half_time_threshold") is not None and dte["remaining"] <= dte["half_time_threshold"]:
            action = "close"
            reason = "half_time"
            urgency = "normal"
    return {
        "fill_id": mark.get("group_id"),
        "group_id": mark.get("group_id"),
        "underlying": mark.get("underlying"),
        "playbook_id": mark.get("playbook_id"),
        "structure": mark.get("structure"),
        "action": action,
        "reason": reason,
        "urgency": urgency,
        "entry_kind": mark.get("entry_kind"),
        "entry_value": mark.get("entry_value"),
        "entry_net_cashflow": mark.get("entry_net_cashflow"),
        "close_mid_net": mark.get("close_mid_net"),
        "close_natural_net": mark.get("close_natural_net"),
        "recommended_close_net": round(close_net, 2),
        "mid_pnl": mark.get("pnl_mid"),
        "natural_pnl": mark.get("pnl_natural"),
        "pnl_mid": mark.get("pnl_mid"),
        "pnl_natural": mark.get("pnl_natural"),
        "pnl_pct_of_entry_value": mark.get("pnl_pct_of_entry_value"),
        "loss_mid": mark.get("loss_mid"),
        "loss_natural": mark.get("loss_natural"),
        "loss_multiple_of_entry": mark.get("loss_multiple_of_entry"),
        "natural_loss_multiple_of_entry": mark.get("natural_loss_multiple_of_entry"),
        "close_debit_multiple_of_entry": mark.get("close_debit_multiple_of_entry"),
        "natural_close_debit_multiple_of_entry": mark.get("natural_close_debit_multiple_of_entry"),
        "max_loss_multiple": mark.get("max_loss_multiple"),
        "max_loss_watch": mark.get("max_loss_watch"),
        "short_strike_state": mark.get("short_strike_state"),
        "max_leg_bid_ask_pct": mark.get("max_leg_bid_ask_pct"),
        "short_leg_delta_abs": mark.get("short_leg_delta_abs"),
        "target_profit": mark.get("target_profit"),
        "target_close_net": mark.get("target_close_net"),
        "target_close_value": mark.get("target_close_value"),
        "target_progress_pct": mark.get("target_progress_pct"),
        "profit_target_pct": mark.get("profit_target_pct"),
        "trigger_progress_pct": mark.get("trigger_progress_pct"),
        "dte_remaining": (mark.get("dte") or {}).get("remaining"),
        "entry_dte": (mark.get("dte") or {}).get("entry"),
        "half_time_threshold": (mark.get("dte") or {}).get("half_time_threshold"),
        "exit_dte_min": int(playbook.exit_dte_min if playbook else 21),
        "exit_pre_event_days": playbook.exit_pre_event_days if playbook else None,
        "quote_fresh": mark.get("quote_fresh"),
        "missing_quotes": mark.get("missing_quotes") or [],
        "mark": mark,
    }


def normalize_profit_target_pct(raw: Any, default: float = 50.0) -> float:
    value = _as_float(raw, default)
    if 0.0 < value < 1.0:
        value *= 100.0
    return max(value, 0.0)


def position_dte(group: dict[str, Any]) -> dict[str, int | None]:
    candidate = group.get("candidate") or {}
    expirations = []
    for leg in candidate.get("legs") or []:
        raw = str(leg.get("expiration") or "")
        if raw:
            try:
                expirations.append(date.fromisoformat(raw))
            except ValueError:
                pass
    if not expirations:
        return {"entry": None, "remaining": None, "half_time_threshold": None}
    short_expiration = min(expirations)
    opened = parse_opened_date(str(group.get("opened_at") or ""))
    today = date.today()
    entry_dte = max((short_expiration - opened).days, 0) if opened else None
    remaining = (short_expiration - today).days
    return {"entry": entry_dte, "remaining": remaining, "half_time_threshold": entry_dte // 2 if entry_dte is not None else None}


def parse_opened_date(raw: str) -> date | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt).date()
        except ValueError:
            continue
    return None


def earnings_days(mark: dict[str, Any], earnings: EarningsStore) -> int | None:
    latest = earnings.latest(str(mark.get("underlying") or ""))
    if latest is None or not latest.next_earnings_date:
        return None
    try:
        event_date = date.fromisoformat(latest.next_earnings_date)
    except ValueError:
        return None
    return (event_date - date.today()).days


def _profit_target_reached(mark: dict[str, Any], policy: LiveExitPolicy) -> bool:
    target_profit = float(mark.get("target_profit") or 0.0)
    if target_profit <= 0:
        return False
    pnl_mid = float(mark.get("pnl_mid") or 0.0)
    pnl_natural = float(mark.get("pnl_natural") or 0.0)
    trigger_profit = target_profit * (policy.profit_target_trigger_pct / 100.0)
    return pnl_mid >= trigger_profit and pnl_natural >= policy.min_profit_to_trigger


def _max_loss_decision(mark: dict[str, Any], policy: LiveExitPolicy) -> dict[str, Any] | None:
    if not bool(mark.get("max_loss_watch")):
        return None
    observations = mark.get("loss_watch_observations") or {}
    observed_count = int(observations.get("count") or 0)
    required_count = max(int(policy.loss_watch_confirmations_required or 1), 1)
    if observed_count < required_count:
        return {"action": "hold", "reason": "loss_watch_debouncing", "urgency": "normal"}
    quote_too_wide = float(mark.get("max_leg_bid_ask_pct") or 0.0) > policy.loss_watch_max_leg_bid_ask_pct
    short_strike_state = mark.get("short_strike_state") or {}
    confirmations = []
    if bool(short_strike_state.get("breached")):
        confirmations.append("short_strike_breached")
    elif bool(short_strike_state.get("within_buffer")):
        confirmations.append("short_strike_within_buffer")
    if quote_too_wide:
        return {"action": "review", "reason": "loss_watch_quote_block", "urgency": "high"}
    if policy.max_loss_action == "close":
        return {"action": "close", "reason": "max_loss", "urgency": "critical"}
    if policy.max_loss_action == "close_when_confirmed":
        if not policy.max_loss_requires_confirmation or "short_strike_breached" in confirmations:
            return {"action": "close", "reason": "max_loss", "urgency": "critical"}
    return {"action": "review", "reason": "loss_watch", "urgency": "high"}


def _max_loss_watch(
    entry_kind: str,
    close_mid_net: float,
    loss: float,
    entry_value: float,
    max_loss_multiple: float | None,
) -> bool:
    if max_loss_multiple is None or max_loss_multiple <= 0 or entry_value <= 0:
        return False
    threshold = max_loss_multiple - 1e-9
    if entry_kind == "credit":
        return close_mid_net < 0 and (abs(close_mid_net) / entry_value) >= threshold
    return (loss / entry_value) >= threshold


def _short_strike_state(
    legs: list[dict[str, Any]],
    underlying_price: float | None,
    buffer_pct: float,
) -> dict[str, Any]:
    if underlying_price is None or underlying_price <= 0:
        return {"available": False}
    short_legs = [leg for leg in legs if str(leg.get("side") or "").lower() == "sell"]
    states = []
    for leg in short_legs:
        option_type = str(leg.get("option_type") or "").lower()
        try:
            strike = float(leg.get("strike") or 0.0)
        except (TypeError, ValueError):
            continue
        if strike <= 0 or option_type not in {"put", "call"}:
            continue
        if option_type == "put":
            breached = underlying_price <= strike
            within_buffer = underlying_price <= strike * (1.0 + buffer_pct / 100.0)
            distance_pct = ((underlying_price - strike) / strike) * 100.0
        else:
            breached = underlying_price >= strike
            within_buffer = underlying_price >= strike * (1.0 - buffer_pct / 100.0)
            distance_pct = ((strike - underlying_price) / strike) * 100.0
        states.append({
            "option_type": option_type,
            "strike": strike,
            "breached": breached,
            "within_buffer": within_buffer,
            "distance_pct": round(distance_pct, 2),
        })
    if not states:
        return {"available": False}
    return {
        "available": True,
        "breached": any(item["breached"] for item in states),
        "within_buffer": any(item["within_buffer"] for item in states),
        "legs": states,
    }


def _target_close_limit_net(mark: dict[str, Any]) -> float:
    target = float(mark.get("target_close_net") or 0.0)
    current = float(mark.get("close_mid_net") or 0.0)
    return max(target, current)


def _entry_net_cashflow(group: dict[str, Any]) -> float:
    candidate = group.get("candidate") or {}
    status = group.get("order_status") or {}
    if len(candidate.get("legs") or []) == 1 and status.get("averagePrice") not in (None, ""):
        price = _as_float(status.get("averagePrice"), abs(_as_float(candidate.get("net_credit"), 0.0)))
        side = str(status.get("side") or "").upper()
        return price if side == "SELL" else -price
    return _as_float(candidate.get("net_credit"), 0.0)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _optional_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _max_loss_action(value: Any) -> str:
    raw = str(value or "review").strip().lower()
    allowed = {"review", "close_when_confirmed", "close"}
    return raw if raw in allowed else "review"
