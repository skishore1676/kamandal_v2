"""Fail-closed option submission windows.

Schedules decide when Kamandal wakes up. This module is the authoritative
last-mile guard immediately before a broker submission.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from kamandal_v2.ops.market_calendar import is_non_trading_day


DEFAULT_REGULAR_CLOSE = time(15, 0)
DEFAULT_EXTENDED_CLOSE = time(15, 15)
DEFAULT_EXTENDED_SYMBOLS = frozenset({"SPY"})
DEFAULT_ENTRY_BUFFER_MINUTES = 30
DEFAULT_CLOSE_BUFFER_MINUTES = 5
DEFAULT_MARKET_OPEN = time(8, 30)
DEFAULT_ENTRY_NOT_BEFORE = time(9, 0)


def submission_window(
    config: dict[str, Any],
    ticket: dict[str, Any],
    *,
    close: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the current submission decision and its evidence."""
    policy = ((config.get("live") or {}).get("option_submission") or {})
    timezone_name = str(
        (config.get("runtime") or {}).get("market_timezone")
        or policy.get("market_timezone")
        or "America/Chicago"
    )
    market_tz = ZoneInfo(timezone_name)
    current = now or datetime.now(market_tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=market_tz)
    else:
        current = current.astimezone(market_tz)

    action_type = str(ticket.get("csa_action_type") or ticket.get("intent_type") or ("close" if close else "open")).strip().lower()
    is_close_action = action_type == "close"
    reason_class = str(
        ticket.get("csa_action_reason_class")
        or ticket.get("exit_reason_class")
        or ((ticket.get("csa_strategy_ticket") or {}).get("metadata") or {}).get("action_reason_class")
        or ""
    ).strip().lower()
    adverse_price_exit = is_close_action and reason_class == "adverse_price_loss"
    intent_type = "close" if is_close_action else action_type
    underlying = str(ticket.get("underlying") or ticket.get("symbol") or "").upper()
    entry_buffer = int(policy.get("entry_buffer_minutes", DEFAULT_ENTRY_BUFFER_MINUTES))
    close_buffer = int(policy.get("close_buffer_minutes", DEFAULT_CLOSE_BUFFER_MINUTES))
    buffer_minutes = entry_buffer if adverse_price_exit else (close_buffer if is_close_action else entry_buffer)

    extended_symbols = {
        str(symbol).upper()
        for symbol in policy.get("extended_close_symbols", DEFAULT_EXTENDED_SYMBOLS)
        if str(symbol).strip()
    }
    uses_extended_session = underlying in extended_symbols
    close_key = "extended_close_time" if uses_extended_session else "regular_close_time"
    default_close = DEFAULT_EXTENDED_CLOSE if uses_extended_session else DEFAULT_REGULAR_CLOSE
    session_close = _parse_time(policy.get(close_key), default_close)

    early_close = (policy.get("early_close_dates") or {}).get(current.date().isoformat()) or {}
    if early_close:
        session_close = _parse_time(early_close.get(close_key), session_close)

    close_at = datetime.combine(current.date(), session_close, market_tz)
    cutoff_at = close_at - timedelta(minutes=buffer_minutes)
    market_open = datetime.combine(current.date(), _parse_time(policy.get("market_open_time"), DEFAULT_MARKET_OPEN), market_tz)
    entry_not_before = datetime.combine(current.date(), _parse_time(policy.get("entry_not_before_time"), DEFAULT_ENTRY_NOT_BEFORE), market_tz)
    enabled = _as_bool(policy.get("enabled"), True)
    non_trading_day = is_non_trading_day(current.date())
    requires_entry_window = adverse_price_exit or (not is_close_action and action_type in {"open", "adjust", "duration_roll"})
    allowed = enabled and not non_trading_day and current >= market_open and current < cutoff_at and (not requires_entry_window or current >= entry_not_before)

    if not enabled:
        reason = "option_submission_disabled"
    elif non_trading_day:
        reason = "market_closed_non_trading_day"
    elif current < market_open:
        reason = "market_not_open"
    elif requires_entry_window and current < entry_not_before:
        reason = "adverse_exit_opening_buffer" if adverse_price_exit else "entry_not_open"
    elif current >= cutoff_at:
        reason = "adverse_exit_closing_buffer" if adverse_price_exit else ("close_cutoff_reached" if is_close_action else "entry_cutoff_reached")
    else:
        reason = "within_submission_window"

    return {
        "allowed": allowed,
        "reason": reason,
        "intent_type": intent_type,
        "underlying": underlying,
        "market_timezone": timezone_name,
        "uses_extended_session": uses_extended_session,
        "session_close_at": close_at.isoformat(),
        "market_open_at": market_open.isoformat(),
        "entry_not_before_at": entry_not_before.isoformat(),
        "submission_cutoff_at": cutoff_at.isoformat(),
        "evaluated_at": current.isoformat(),
        "buffer_minutes": buffer_minutes,
        "action_reason_class": reason_class,
        "retryable_current_session": reason in {"entry_not_open", "adverse_exit_opening_buffer"},
        "retryable_next_session": is_close_action and reason in {"market_closed_non_trading_day", "close_cutoff_reached", "adverse_exit_closing_buffer"},
    }


def _parse_time(value: Any, default: time) -> time:
    raw = str(value or "").strip()
    if not raw:
        return default
    try:
        return time.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid option session time: {raw!r}") from exc


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
