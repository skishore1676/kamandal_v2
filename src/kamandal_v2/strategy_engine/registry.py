"""Explicit in-process strategy capability registry.

Capabilities are selected by ``strategy_family``.  ``structure`` is checked as
an order-shape constraint only; it never selects a capability by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Capability:
    key: str
    allowed_structures: frozenset[str]
    actions: frozenset[str]


class CapabilityRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        key = capability.key.strip().lower()
        if not key:
            raise ValueError("strategy capability key is required")
        if key in self._items:
            raise ValueError(f"strategy capability already registered: {key}")
        self._items[key] = capability

    def resolve(self, key: str) -> Capability:
        normalized = str(key or "").strip().lower()
        try:
            return self._items[normalized]
        except KeyError as exc:
            raise LookupError(f"Unknown strategy capability: {key}") from exc

    def registered(self) -> tuple[Capability, ...]:
        return tuple(self._items[key] for key in sorted(self._items))

    def require_all(self, keys: Iterable[str]) -> None:
        missing = sorted({str(key) for key in keys if str(key).strip().lower() not in self._items})
        if missing:
            raise LookupError(f"Unregistered strategy capabilities: {', '.join(missing)}")


def capability_registry() -> CapabilityRegistry:
    """Return the one built-in registry for currently supported Sheet families."""
    registry = CapabilityRegistry()
    close_only = frozenset({"open", "hold", "close"})
    for key, structures in (
        ("short_put", {"short_put"}),
        ("put_spread", {"put_spread"}),
        ("call_spread", {"call_spread"}),
        ("iron_condor", {"iron_condor"}),
        ("jade_lizard", {"jade_lizard"}),
        ("call_calendar", {"call_calendar"}),
        ("put_calendar", {"put_calendar"}),
        ("put_diagonal", {"put_diagonal"}),
        ("call_diagonal", {"call_diagonal"}),
        ("narrative_ignition", {"call_diagonal", "put_diagonal"}),
        ("long_call", {"long_call"}),
        ("long_put", {"long_put"}),
    ):
        registry.register(Capability(key, frozenset(structures), close_only))
    registry.register(
        Capability(
            "short_strangle",
            frozenset({"short_strangle", "strangle"}),
            frozenset({"open", "hold", "adjust", "duration_roll", "close"}),
        )
    )
    registry.register(
        Capability(
            "earnings_calendar",
            frozenset({"call_calendar", "put_calendar"}),
            close_only,
        )
    )
    return registry
