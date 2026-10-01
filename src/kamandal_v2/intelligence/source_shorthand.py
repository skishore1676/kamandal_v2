"""Resolve declared source shorthand into review evidence, never entry authority."""

from __future__ import annotations

import copy
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from kamandal_v2.ops.market_calendar import MARKET_HOLIDAYS

_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}
_CALL_SPREAD = re.compile(
    r"(?:also\s+)?(?P<verb>added|bought)\s+(?:some\s+)?"
    r"\$(?P<symbol>[A-Z][A-Z0-9.]{0,19})\s+"
    r"(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"(?:\s+(?P<year>20\d{2}))?\s+"
    r"(?P<long>\d+(?:\.\d+)?)\s*/\s*(?P<short>\d+(?:\.\d+)?)"
    r"\s+call\s+spreads?[.!]?", re.IGNORECASE,
)


def resolve_declared_text_contracts(
    record: Mapping[str, Any], episode: dict[str, Any], profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Repair only an unambiguous standalone, profile-declared monthly vertical.

    Retain the original timestamp and identities. Normalized unit ratios do not
    claim source size. The normal provenance, contract and execution gates apply.
    """
    convention = (profile.get("episode_interpreter") or {}).get("text_contract_convention")
    if convention != "equity_monthly_v1":
        return episode
    literal = record.get("literal") or {}
    match = _CALL_SPREAD.fullmatch(" ".join(str(literal.get("text") or "").split()))
    if not match:
        return episode
    symbol = match["symbol"].upper()
    if symbol in {"SPX", "SPXW", "NDX", "NDXP", "RUT", "VIX", "XSP"}:
        return episode  # Index settlement conventions are not equity shorthand.
    if symbol not in {str(item.get("symbol") or "").upper() for item in literal.get("symbols", [])}:
        return episode
    source = record.get("source") or {}
    try:
        published = datetime.fromisoformat(str(source.get("published_at") or "").replace("Z", "+00:00")).date()
        expiry = _monthly_expiration(match["month"], match["year"], published)
    except ValueError:
        return episode
    long, short = Decimal(match["long"]), Decimal(match["short"])
    if expiry is None or not 0 < long < short:
        return episode
    events = episode.get("events") or []
    if len(events) != 1:
        return episode
    event = events[0]
    if (event.get("symbol") != symbol or event.get("structure_hint") not in {"call_spread", "long_call"}
            or event.get("direction") != "bullish" or event.get("evidence_status") != "complete"
            or event.get("action") not in {"open", "scale_in"}
            or event.get("template_number") is not None or event.get("links_to")
            or set(event.get("blockers") or []) - {"exact_package_incomplete", "exact_package_missing",
                                                    "exact_package_requires_verified_image"}):
        return episode
    packages = event.get("exact_packages") or []
    if len(packages) != 1:
        return episode
    package = packages[0]
    # Do not replace displayed contracts, conflicting terms, or other blockers.
    if package.get("complete") or package.get("legs") or "expir" not in str(package.get("blocker") or "").lower():
        return episode
    updated = copy.deepcopy(episode)
    event = updated["events"][0]
    # Older profile rules matched singular "spread" only; a cached plural
    # vertical may therefore have been labeled as a lone long call.
    event["structure_hint"] = "call_spread"
    event["exact_packages"] = [{
        "complete": True, "blocker": None, "displayed_price": None,
        "legs": [
            {"quantity": 1, "expiration": expiry.isoformat(), "strike": str(strike),
             "option_type": "call", "order_code": code}
            for strike, code in ((long, "BTO"), (short, "STO"))
        ],
        "field_provenance": ["text", "source_grammar:equity_monthly_v1"],
    }]
    event["blockers"] = []
    event["projections"] = ["exact_package"]
    event["projection_dispositions"] = [{"projection": "exact_package",
        "disposition": "ready_for_source_policy", "reason": "declared_text_contracts_complete"}]
    event["planner_new_entry"] = True
    return updated


def _monthly_expiration(month: str, explicit_year: str | None, published: date) -> date | None:
    number = _MONTHS[month[:3].lower()]
    year = int(explicit_year) if explicit_year else published.year + int(number < published.month)
    # The maintained owner calendar currently covers these years. Fail closed
    # rather than silently assume a future holiday calendar or roll an expired
    # same-month contract to next year.
    if year not in {2026, 2027}:
        return None
    first = date(year, number, 1)
    expiry = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
    while expiry.weekday() >= 5 or expiry.isoformat() in MARKET_HOLIDAYS:
        expiry -= timedelta(days=1)
    return expiry if expiry >= published else None
