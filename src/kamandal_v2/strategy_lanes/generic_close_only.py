"""One conservative lifecycle for ordinary strategy capabilities.

Specialised managers are reserved for strategy families with genuine extra
state (strangle replacements, paired diagonals, and event-relative earnings
calendars).  All other structures share the same full-position close logic.
"""

from __future__ import annotations

from typing import Any, Mapping

from kamandal_v2.strategy_lanes.call_vertical import propose_call_vertical_actions
from kamandal_v2.strategy_lanes.models import CsaAction, LifecycleState
from kamandal_v2.strategy_lanes.policy import CsaPolicy


def propose_generic_close_only_actions(
    lifecycle: LifecycleState,
    policy: CsaPolicy,
    context: Mapping[str, Any],
    *,
    proposed_at: str,
) -> tuple[CsaAction, ...]:
    """Use the defined-risk close rules without an implicit event lifecycle."""
    return propose_call_vertical_actions(lifecycle, policy, context, proposed_at=proposed_at)
