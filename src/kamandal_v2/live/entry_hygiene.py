"""Self-healing helpers for live entry approval lifecycle state."""

from __future__ import annotations

import os
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from kamandal_v2.stores.sqlite import LocalStore


RETIRED_STALE_ENTRY_APPROVAL_STATUS = "retired_stale_entry_approval"
DEFAULT_STALE_ENTRY_APPROVAL_MINUTES = 120


def retire_stale_entry_approvals(
    config: dict[str, Any],
    store: LocalStore,
    *,
    active_hashes: set[str] | None = None,
    now: datetime | None = None,
    source: str = "health_self_heal",
) -> list[dict[str, Any]]:
    """Retire stale local entry approvals that are no longer actionable.

    With ``active_hashes`` supplied, the caller is reconciling against a current
    daily-plan surface and any stale ticket absent from that surface is retired.
    Without ``active_hashes``, this stays conservative and only retires tickets
    created before the current market day. That lets health/status self-heal
    yesterday's leftovers without guessing about fresh same-day approvals.
    """

    retired = stale_entry_approvals(
        config,
        store,
        active_hashes=active_hashes,
        now=now,
        source=source,
    )
    for item in retired:
        ticket_hash = str(item["ticket_hash"])
        store.update_live_order_intent_status_with_payload(
            ticket_hash,
            RETIRED_STALE_ENTRY_APPROVAL_STATUS,
            {
                "order_reconciliation": {
                    "status": RETIRED_STALE_ENTRY_APPROVAL_STATUS,
                    "prior_status": "pending_approval",
                    "reason": item["reason"],
                    "age_minutes": item["age_minutes"],
                    "stale_after_minutes": stale_entry_approval_minutes(config),
                    "reconciled_at": now_utc(),
                    "source": source,
                }
            },
        )
    return retired


def stale_entry_approvals(
    config: dict[str, Any],
    store: LocalStore,
    *,
    active_hashes: set[str] | None = None,
    now: datetime | None = None,
    source: str = "health_self_heal",
) -> list[dict[str, Any]]:
    """Describe stale approvals without changing their ledger state."""

    active_hashes = active_hashes or set()
    now = now or datetime.now(UTC)
    stale_minutes = stale_entry_approval_minutes(config)
    market_start = market_day_start(config, now=now)
    stale = []
    for ticket in store.live_order_intents_by_type("open", statuses={"pending_approval"}):
        ticket_hash = str(ticket.get("ticket_hash") or "")
        if ticket_hash in active_hashes:
            continue
        age_minutes = ticket_age_minutes(ticket, now=now)
        if age_minutes is None or age_minutes <= stale_minutes:
            continue
        created_at = parse_ledger_timestamp(str(ticket.get("_ledger_created_at") or ticket.get("created_at") or ""))
        before_today = bool(created_at and created_at < market_start)
        if not active_hashes and not before_today:
            continue
        reason = "stale_entry_approval_not_in_current_daily_plan" if active_hashes else "stale_entry_approval_from_prior_market_day"
        item = {
            "ticket_hash": ticket_hash,
            "order_id": ticket.get("order_id"),
            "underlying": ticket.get("underlying"),
            "structure": ticket.get("structure"),
            "age_minutes": round(age_minutes, 2),
            "status": RETIRED_STALE_ENTRY_APPROVAL_STATUS,
            "reason": reason,
            "source": source,
        }
        stale.append(item)
    return stale


def stale_entry_approval_minutes(config: dict[str, Any]) -> int:
    recon = ((config.get("live") or {}).get("reconciliation") or {})
    return int(recon.get("stale_entry_approval_minutes") or DEFAULT_STALE_ENTRY_APPROVAL_MINUTES)


def market_today(config: dict[str, Any], *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    market_tz = str((config.get("runtime") or {}).get("market_timezone") or os.environ.get("KAMANDAL_MARKET_TZ") or "America/Chicago")
    return now.astimezone(ZoneInfo(market_tz)).date().isoformat()


def market_day_start(config: dict[str, Any], *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    market_tz = ZoneInfo(str((config.get("runtime") or {}).get("market_timezone") or os.environ.get("KAMANDAL_MARKET_TZ") or "America/Chicago"))
    today = now.astimezone(market_tz).date()
    return datetime.combine(today, time.min, tzinfo=market_tz).astimezone(UTC)


def ticket_age_minutes(ticket: dict[str, Any], *, now: datetime | None = None) -> float | None:
    now = now or datetime.now(UTC)
    parsed = parse_ledger_timestamp(str(ticket.get("_ledger_updated_at") or ticket.get("_ledger_created_at") or ticket.get("created_at") or ""))
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def parse_ledger_timestamp(raw: str) -> datetime | None:
    value = raw.strip()
    if not value:
        return None
    for candidate in (value, value.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            continue
    return None


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
