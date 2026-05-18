"""Broker adapter selection."""

from __future__ import annotations

from typing import Any

from kamandal_v2.market.public import PublicAdapter
from kamandal_v2.market.tastytrade import TastytradeAdapter


def broker_adapter(config: dict[str, Any]) -> Any:
    active = str((config.get("broker") or {}).get("active") or "public").strip().lower()
    if active == "public":
        return PublicAdapter(config)
    if active in {"tastytrade", "tasty"}:
        return TastytradeAdapter(config)
    raise RuntimeError(f"Unsupported broker.active={active!r}")
