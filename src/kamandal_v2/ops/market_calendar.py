"""Shared market-calendar facts for scheduled jobs and order-session guards."""

from __future__ import annotations

import os
from datetime import date


MARKET_HOLIDAYS = {
    "2026-01-01",
    "2026-01-19",
    "2026-02-16",
    "2026-04-03",
    "2026-05-25",
    "2026-06-19",
    "2026-07-03",
    "2026-09-07",
    "2026-11-26",
    "2026-12-25",
    "2027-01-01",
    "2027-01-18",
    "2027-02-15",
    "2027-03-26",
    "2027-05-31",
    "2027-06-18",
    "2027-07-05",
    "2027-09-06",
    "2027-11-25",
    "2027-12-24",
}


def is_non_trading_day(day: date) -> bool:
    """Return whether normal US market jobs should be idle on ``day``."""
    if day.weekday() >= 5:
        return True
    if os.getenv("KAMANDAL_MARKET_HOLIDAY_CALENDAR", "nyse").lower() == "off":
        return False
    return day.isoformat() in MARKET_HOLIDAYS
