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


def live_exit_policy(config: dict[str, Any] | None) -> LiveExitPolicy:
    live_cfg = ((config or {}).get("live") or {})
    raw = live_cfg.get("exit_pricing") or {}
    if not isinstance(raw, dict):
        raw = {}
    return LiveExitPolicy(
        profit_target_trigger_pct=_as_float(raw.get("profit_target_trigger_pct"), 95.0),
        min_profit_to_trigger=_as_float(raw.get("min_profit_to_trigger"), 5.0),
        require_fresh_quotes=_as_bool(raw.get("require_fresh_quotes"), True),
    )


def mark_live_group(
    group: dict[str, Any],
    quote_map: dict[tuple[str, str, float], dict[str, Any]],
    playbook: Playbook | None,
    *,
    quote_fresh: bool,
    config: dict[str, Any],
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

    pnl_mid = entry_net + close_mid_net
    pnl_natural = entry_net + close_natural_net
    profit_target_pct = normalize_profit_target_pct(playbook.profit_target_pct if playbook else 50.0)
    target_profit = entry_value * (profit_target_pct / 100.0)
    target_close_net = target_profit - entry_net
    progress = (pnl_mid / target_profit) * 100.0 if target_profit > 0 else 0.0
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
        "profit_target_pct": profit_target_pct,
        "target_profit": round(target_profit, 2),
        "target_close_net": round(target_close_net, 2),
        "target_close_value": round(abs(target_close_net), 2),
        "target_progress_pct": round(progress, 2),
        "trigger_progress_pct": live_exit_policy(config).profit_target_trigger_pct,
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
    close_net = float(mark.get("close_mid_net") or 0.0)
    missing_quotes = bool(mark.get("missing_quotes"))
    if policy.require_fresh_quotes and not bool(mark.get("quote_fresh")):
        reason = "fresh_quotes_missing"
    elif missing_quotes:
        reason = "missing_quotes"
    else:
        exit_pre_event_days = playbook.exit_pre_event_days if playbook else None
        days_to_earnings = earnings_days(mark, earnings)
        dte = mark.get("dte") or {}
        exit_dte_min = int(playbook.exit_dte_min if playbook else 21)
        half_time_exit = bool(playbook.half_time_exit) if playbook else True
        if exit_pre_event_days is not None and days_to_earnings is not None and days_to_earnings <= exit_pre_event_days:
            action = "close"
            reason = "pre_event"
        elif _profit_target_reached(mark, policy):
            action = "close"
            reason = "profit_target"
            close_net = _target_close_limit_net(mark)
        elif dte.get("remaining") is not None and dte["remaining"] <= exit_dte_min:
            action = "close"
            reason = "dte_target"
        elif half_time_exit and dte.get("remaining") is not None and dte.get("half_time_threshold") is not None and dte["remaining"] <= dte["half_time_threshold"]:
            action = "close"
            reason = "half_time"
    return {
        "fill_id": mark.get("group_id"),
        "group_id": mark.get("group_id"),
        "underlying": mark.get("underlying"),
        "playbook_id": mark.get("playbook_id"),
        "structure": mark.get("structure"),
        "action": action,
        "reason": reason,
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


def _target_close_limit_net(mark: dict[str, Any]) -> float:
    target = float(mark.get("target_close_net") or 0.0)
    current = float(mark.get("close_mid_net") or 0.0)
    if target > 0:
        return max(target, current)
    return min(target, current)


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


def _as_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
