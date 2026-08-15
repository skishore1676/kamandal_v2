"""Deterministic event-session timing for paired earnings calendars."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from kamandal_v2.ops.market_calendar import is_non_trading_day


CENTRAL = ZoneInfo("America/Chicago")
MARKET_OPEN = time(8, 30)


def final_pre_event_session(event_date: date, time_of_day: str) -> date:
    """Return the final regular session in which a new paired entry is allowed."""
    timing = _timing(time_of_day)
    if timing == "amc":
        return _previous_or_same_session(event_date)
    return _previous_session(event_date)


def event_exit_due(*, event_date: date, time_of_day: str, observed_at: str) -> bool:
    """True only in the first eligible post-announcement trading session."""
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).astimezone(CENTRAL)
    if is_non_trading_day(observed.date()):
        return False
    timing = _timing(time_of_day)
    if timing == "bmo":
        return observed.date() >= _previous_or_same_session(event_date) and (
            observed.date() > event_date or (observed.date() == event_date and observed.timetz().replace(tzinfo=None) >= MARKET_OPEN)
        )
    return observed.date() >= _next_session(event_date) and observed.timetz().replace(tzinfo=None) >= MARKET_OPEN


def entry_session_due(*, event_date: date, time_of_day: str, observed_at: str) -> bool:
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).astimezone(CENTRAL)
    return (
        not is_non_trading_day(observed.date())
        and observed.date() == final_pre_event_session(event_date, time_of_day)
        and observed.timetz().replace(tzinfo=None) >= MARKET_OPEN
    )


def _timing(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"bmo", "before_market_open", "before open"}:
        return "bmo"
    if normalized in {"amc", "after_market_close", "after close"}:
        return "amc"
    raise ValueError("earnings event timing must be confirmed BMO or AMC")


def _previous_or_same_session(value: date) -> date:
    cursor = value
    while is_non_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _previous_session(value: date) -> date:
    return _previous_or_same_session(value - timedelta(days=1))


def _next_session(value: date) -> date:
    cursor = value + timedelta(days=1)
    while is_non_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor
