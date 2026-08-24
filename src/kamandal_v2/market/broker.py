"""Broker adapter selection."""

from __future__ import annotations

import os
from typing import Any

from kamandal_v2.market.public import PublicAdapter
from kamandal_v2.market.tastytrade import TastytradeAdapter

DEFAULT_EXECUTION_VENUES = {
    "public_primary": "public",
    "tasty_primary": "tastytrade",
}


def execution_venue_registry(config: dict[str, Any]) -> dict[str, str]:
    broker = config.get("broker") or {}
    configured = broker.get("execution_venues") or {}
    registry = dict(DEFAULT_EXECUTION_VENUES)
    if isinstance(configured, dict):
        for alias, payload in configured.items():
            value = payload.get("broker") if isinstance(payload, dict) else payload
            normalized = str(value or "").strip().lower()
            if normalized:
                registry[str(alias).strip().lower()] = normalized
    return registry


def default_execution_venue(config: dict[str, Any]) -> str:
    broker = config.get("broker") or {}
    explicit = str(broker.get("default_execution_venue") or "").strip().lower()
    if explicit:
        return explicit
    active = str(broker.get("active") or "public").strip().lower()
    return "tasty_primary" if active in {"tasty", "tastytrade"} else "public_primary"


def ticket_execution_venue(config: dict[str, Any], ticket: dict[str, Any]) -> str:
    candidate = ticket.get("candidate") or {}
    nested = ticket.get("csa_strategy_ticket") or {}
    metadata = nested.get("metadata") or {}
    return str(
        ticket.get("execution_venue")
        or candidate.get("execution_venue")
        or metadata.get("execution_venue")
        or default_execution_venue(config)
    ).strip().lower()


def broker_adapter(config: dict[str, Any], *, execution_venue: str | None = None) -> Any:
    worker_venue = str(os.environ.get("KAMANDAL_EXECUTION_VENUE") or "").strip().lower()
    venue = str(execution_venue or worker_venue or "").strip().lower()
    if venue:
        registry = execution_venue_registry(config)
        if venue not in registry:
            raise RuntimeError(f"Unsupported execution_venue={venue!r}")
        active = registry[venue]
    else:
        active = str((config.get("broker") or {}).get("active") or "public").strip().lower()
    if active == "public":
        return PublicAdapter(config)
    if active in {"tastytrade", "tasty"}:
        return TastytradeAdapter(config)
    raise RuntimeError(f"Unsupported broker.active={active!r}")


def broker_adapter_for_ticket(config: dict[str, Any], ticket: dict[str, Any]) -> Any:
    return broker_adapter(config, execution_venue=ticket_execution_venue(config, ticket))
