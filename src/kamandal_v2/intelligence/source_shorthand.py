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
_CALL_CALENDAR = re.compile(
    r"(?:added|bought)\s+(?:some\s+)?\$(?P<symbol>[A-Z][A-Z0-9.]{0,19})\s+"
    r"(?P<near>[A-Za-z]+)\s*/\s*(?P<far>[A-Za-z]+)\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s+call\s+calendars?[.!]?", re.IGNORECASE,
)


def resolve_declared_text_contracts(
    record: Mapping[str, Any], episode: dict[str, Any], profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve unambiguous profile-declared monthly vertical/calendar shorthand.

    Retain the original timestamp and identities. Normalized unit ratios do not
    claim source size. The normal provenance, contract and execution gates apply.
    """
    convention = (profile.get("episode_interpreter") or {}).get("text_contract_convention")
    if convention != "equity_monthly_v1":
        return episode
    literal = record.get("literal") or {}
    match = _CALL_SPREAD.fullmatch(" ".join(str(literal.get("text") or "").split()))
    if not match:
        return _resolve_split_call_fly(record, _resolve_monthly_call_calendar(record, episode))
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


def _resolve_monthly_call_calendar(record: Mapping[str, Any], episode: dict[str, Any]) -> dict[str, Any]:
    """Resolve dates on a matching interpreted calendar, without inventing legs."""
    literal = record.get("literal") or {}
    match = _CALL_CALENDAR.fullmatch(" ".join(str(literal.get("text") or "").split()))
    if not match:
        return episode
    month_name = r'Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?'
    if any(not re.fullmatch(month_name, match[name], re.IGNORECASE) for name in ('near', 'far')):
        return episode
    symbol = match['symbol'].upper()
    if symbol in {'SPX', 'SPXW', 'NDX', 'NDXP', 'RUT', 'VIX', 'XSP'}:
        return episode
    if symbol not in {str(s.get('symbol') or '').upper() for s in literal.get('symbols', [])}:
        return episode
    try:
        published = datetime.fromisoformat(str((record.get('source') or {}).get('published_at') or '').replace('Z', '+00:00')).date()
        near = _monthly_expiration(match['near'], None, published)
        far = _monthly_expiration(match['far'], None, near or published)
    except (ValueError, KeyError):
        return episode
    if near is None or far is None or near >= far or Decimal(match['strike']) <= 0:
        return episode
    events = episode.get('events') or []
    if len(events) != 1:
        return episode
    event = events[0]
    allowed_blockers = {'exact_package_incomplete', 'exact_package_missing',
                        'exact calendar expiration dates are unresolved.'}
    if (event.get('symbol') != symbol or event.get('structure_hint') != 'call_calendar'
            or event.get('action') not in {'open', 'scale_in'} or event.get('evidence_status') != 'complete'
            or event.get('template_number') is not None or event.get('links_to')
            or {str(b).lower() for b in event.get('blockers') or []} - allowed_blockers):
        return episode
    packages = event.get('exact_packages') or []
    if len(packages) != 1:
        return episode
    package = packages[0]
    if package.get('complete') or 'expir' not in str(package.get('blocker') or '').lower():
        return episode
    if set(package.get('field_provenance') or []) != {'text'} or package.get('displayed_price') is not None:
        return episode
    legs = package.get('legs') or []
    if len(legs) != 2 or {leg.get('order_code') for leg in legs} != {'STO', 'BTO'}:
        return episode
    dates = {'STO': near, 'BTO': far}
    months = {'STO': match['near'], 'BTO': match['far']}
    for leg in legs:
        if (str(leg.get('strike')) != match['strike'] or leg.get('quantity') != 1
                or leg.get('option_type') != 'call'):
            return episode
        code = leg['order_code']
        expiry = str(leg.get('expiration') or '')
        # Permit only this month's explicit standard-monthly label, or the
        # already matching ISO date. Never overwrite a weekly/conflicting date.
        label = re.fullmatch(r'([A-Za-z]+)\s+(20\d{2})\s+standard monthly', expiry, re.IGNORECASE)
        if expiry != dates[code].isoformat() and not (label
                and label[1].lower() == months[code].lower()
                and int(label[2]) == dates[code].year):
            return episode
    updated = copy.deepcopy(episode)
    event = updated['events'][0]
    package = event['exact_packages'][0]
    for leg in package['legs']:
        leg['expiration'] = dates[leg['order_code']].isoformat()
    package.update(complete=True, blocker=None, field_provenance=['text', 'source_grammar:equity_monthly_v1'])
    event.update(blockers=[], projections=['exact_package'], planner_new_entry=True,
                 projection_dispositions=[{'projection': 'exact_package', 'disposition': 'ready_for_source_policy',
                                           'reason': 'declared_text_contracts_complete'}])
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


def _resolve_split_call_fly(record: Mapping[str, Any], episode: dict[str, Any]) -> dict[str, Any]:
    """Repair only a matching retained four-leg transcription, never conflicting terms."""
    from kamandal_v2.intelligence.observed_packages import _normalize_expiration
    text = " ".join(str((record.get("literal") or {}).get("text") or "").split())
    match = re.fullmatch(
        r"(?:added|bought) (?:some )?\$(?P<symbol>[A-Z][A-Z0-9.]*) "
        r"(?P<month>[A-Za-z]+) (?P<day>\d{1,2})(?: (?P<year>20\d{2}))? Exp "
        r"(?P<a>\d+(?:\.\d+)?)/(?P<b>\d+(?:\.\d+)?)-(?P<c>\d+(?:\.\d+)?)/(?P<d>\d+(?:\.\d+)?) "
        r"split[- ]wing butterfly call spreads?[.!]?", text, re.IGNORECASE)
    if not match or len(episode.get("events") or []) != 1:
        return episode
    event = episode["events"][0]
    if (event.get("symbol") != match["symbol"].upper()
            or event.get("action") not in {"open", "scale_in"}
            or event.get("structure_hint") not in {"call_spread", "butterfly", "split_call_fly"}
            or event.get("template_number") is not None or event.get("links_to")
            or set(event.get("blockers") or []) - {"exact_package_incomplete", "exact_package_missing"}):
        return episode
    packages = event.get("exact_packages") or []
    if len(packages) != 1:
        return episode
    package = packages[0]
    if (package.get("blocker") != "structure_leg_count_mismatch"
            or set(package.get("field_provenance") or []) != {"text"}
            or len(package.get("legs") or []) != 4):
        return episode
    try:
        published = datetime.fromisoformat(str((record.get("source") or {}).get("published_at")).replace("Z", "+00:00")).date()
        expiry = date(int(match["year"] or published.year), _MONTHS[match["month"][:3].lower()], int(match["day"]))
        strikes = [Decimal(match[name]) for name in ("a", "b", "c", "d")]
        if not 0 < strikes[0] < strikes[1] < strikes[2] < strikes[3] or expiry < published:
            return episode
        legs = sorted(package["legs"], key=lambda leg: Decimal(str(leg.get("strike"))))
        for leg, strike, code in zip(legs, strikes, ("BTO", "STO", "STO", "BTO"), strict=True):
            if (Decimal(str(leg.get("strike"))) != strike or leg.get("quantity") != 1
                    or leg.get("option_type") != "call" or leg.get("order_code") != code
                    or _normalize_expiration(leg.get("expiration"), published) != expiry.isoformat()):
                return episode
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return episode
    updated = copy.deepcopy(episode)
    event = updated["events"][0]
    event["exact_packages"][0].update(complete=True, blocker=None)
    event.update(structure_hint="split_call_fly", evidence_status="complete", blockers=[],
                 projections=["exact_package"], planner_new_entry=True,
                 projection_dispositions=[{"projection": "exact_package", "disposition": "ready_for_source_policy",
                                           "reason": "split_call_fly_contract_geometry_verified"}])
    return updated
