"""Explicit, fail-closed CSA lane dispatch."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from kamandal_v2.strategy_lanes.models import LaneId


class UnknownLaneError(LookupError):
    """Raised when a CSA lane has not been explicitly registered."""


class LaneRegistry:
    def __init__(self) -> None:
        self._handlers: dict[LaneId, Callable[..., Any]] = {}

    def register(self, lane: LaneId, handler: Callable[..., Any]) -> None:
        if lane in self._handlers:
            raise ValueError(f"CSA lane already registered: {lane.value}")
        self._handlers[lane] = handler

    def resolve(self, lane: LaneId | str) -> Callable[..., Any]:
        try:
            normalized = lane if isinstance(lane, LaneId) else LaneId(str(lane))
        except ValueError as exc:
            raise UnknownLaneError(f"Unknown CSA lane: {lane}") from exc
        try:
            return self._handlers[normalized]
        except KeyError as exc:
            raise UnknownLaneError(f"Unregistered CSA lane: {normalized.value}") from exc

    def registered(self) -> tuple[LaneId, ...]:
        return tuple(sorted(self._handlers, key=lambda item: item.value))

    def require_all(self, lanes: Iterable[LaneId]) -> None:
        missing = sorted((lane.value for lane in lanes if lane not in self._handlers))
        if missing:
            raise UnknownLaneError(f"Unregistered CSA lanes: {', '.join(missing)}")


def lifecycle_registry() -> LaneRegistry:
    from kamandal_v2.strategy_lanes.call_vertical import propose_call_vertical_actions
    from kamandal_v2.strategy_lanes.diagonal import propose_diagonal_actions
    from kamandal_v2.strategy_lanes.earnings_calendar import propose_earnings_calendar_actions
    from kamandal_v2.strategy_lanes.generic_close_only import propose_generic_close_only_actions
    from kamandal_v2.strategy_lanes.strangle import propose_strangle_actions

    registry = LaneRegistry()
    registry.register(LaneId.SHORT_STRANGLE, propose_strangle_actions)
    registry.register(LaneId.CALL_VERTICAL, propose_call_vertical_actions)
    registry.register(LaneId.DIRECTIONAL_DIAGONAL, propose_diagonal_actions)
    registry.register(LaneId.GENERIC_CLOSE_ONLY, propose_generic_close_only_actions)
    registry.register(LaneId.EARNINGS_CALENDAR, propose_earnings_calendar_actions)
    registry.require_all(LaneId)
    return registry
