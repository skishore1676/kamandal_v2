from __future__ import annotations

import pytest
from dataclasses import replace

from kamandal_v2.strategy_engine.lifecycle import (
    adopt_legacy_position,
    observe_strangle_episode,
    strangle_adjustment_eligible,
    validate_strangle_replacement,
)
from kamandal_v2.strategy_lanes.models import LaneId, LegEffect, LegSide, LifecycleState, ShadowFill, StrategyTicket, TicketLeg
from kamandal_v2.strategy_lanes.shadow_execution import ShadowExecutionAdapter


NOW = "2026-08-14T15:00:00Z"


def _strangle(*, adjustment_count: int = 0) -> LifecycleState:
    return LifecycleState(
        lifecycle_id="life-strangle",
        opportunity_id="opp-strangle",
        lane=LaneId.SHORT_STRANGLE,
        version=1,
        status="open",
        active_legs=(
            {"instrument_id": "put90", "side": "sell", "effect": "open", "quantity": 1, "option_type": "put", "expiration": "2026-09-25", "strike": 90.0, "role": "short_put"},
            {"instrument_id": "call110", "side": "sell", "effect": "open", "quantity": 1, "option_type": "call", "expiration": "2026-09-25", "strike": 110.0, "role": "short_call"},
        ),
        cashflow_ledger=({"ticket_id": "open", "fill_id": "open", "amount": 2.0, "filled_at": NOW},),
        opened_at=NOW,
        updated_at=NOW,
        policy_hash="frozen-policy",
        metadata={"opening_credit": 2.0, "cumulative_cashflow": 2.0, "adjustment_count": adjustment_count},
    )


def _replacement_ticket() -> StrategyTicket:
    return StrategyTicket(
        ticket_id="replace-call",
        action_id="adjust-call",
        lifecycle_id="life-strangle",
        lifecycle_version=1,
        lane=LaneId.SHORT_STRANGLE,
        underlying="XYZ",
        order_kind="credit",
        limit_price=0.2,
        legs=(
            TicketLeg("call110", LegSide.BUY, LegEffect.CLOSE, 1, "call", "2026-09-25", 110.0, "short_call"),
            TicketLeg("call100", LegSide.SELL, LegEffect.OPEN, 1, "call", "2026-09-25", 100.0, "short_call"),
        ),
        policy_hash="frozen-policy",
        created_at=NOW,
        metadata={"action_type": "adjust", "adjustment_kind": "untested_side_same_expiry_credit_roll", "tested_side": "put", "breached_strike": 90.0},
    )


def test_strangle_replacement_is_atomic_and_preserves_opening_target() -> None:
    lifecycle = _strangle()
    lifecycle = observe_strangle_episode(lifecycle, tested_side="put", breached_strike=90.0, required_confirmations=2)
    lifecycle = observe_strangle_episode(lifecycle, tested_side="put", breached_strike=90.0, required_confirmations=2)
    assert strangle_adjustment_eligible(lifecycle)
    ticket = _replacement_ticket()
    validate_strangle_replacement(lifecycle, ticket.legs, tested_side="put", net_credit=0.2)

    filled = ShadowExecutionAdapter().adopt_fill(
        lifecycle,
        ticket,
        ShadowFill("fill-replace", ticket.ticket_id, lifecycle.lifecycle_id, "filled", 0, 0.2, 0.2, 0.2, NOW, {}),
    )

    assert filled.version == 2
    assert [(leg["role"], leg["strike"]) for leg in filled.active_legs] == [("short_put", 90.0), ("short_call", 100.0)]
    assert filled.metadata["adjustment_count"] == 1
    assert filled.metadata["cumulative_credit"] == 2.2
    assert filled.metadata["put_breakeven"] == 87.8
    assert filled.metadata["call_breakeven"] == 102.2
    assert filled.metadata["profit_target_dollars"] == 0.8
    assert filled.metadata["strangle_test_episode"]["consumed"] is True


def test_strangle_episode_requires_rearm_or_distinct_side_and_filled_limit() -> None:
    lifecycle = _strangle()
    lifecycle = observe_strangle_episode(lifecycle, tested_side="put", breached_strike=90.0, required_confirmations=2)
    lifecycle = observe_strangle_episode(lifecycle, tested_side="put", breached_strike=90.0, required_confirmations=2)
    lifecycle = observe_strangle_episode(lifecycle, tested_side="", breached_strike=None, required_confirmations=2)
    assert not strangle_adjustment_eligible(lifecycle)
    lifecycle = observe_strangle_episode(lifecycle, tested_side="", breached_strike=None, required_confirmations=2)
    assert not strangle_adjustment_eligible(lifecycle)
    switched = observe_strangle_episode(lifecycle, tested_side="call", breached_strike=110.0, required_confirmations=2)
    assert switched.metadata["strangle_test_episode"]["confirmations"] == 1
    assert switched.metadata["strangle_test_episode"]["consumed"] is False
    limited = _strangle(adjustment_count=2)
    with pytest.raises(ValueError, match="limit reached"):
        validate_strangle_replacement(limited, _replacement_ticket().legs, tested_side="put", net_credit=0.2)


def test_strangle_replacement_rejects_crossing_and_legacy_adoption_is_precise() -> None:
    lifecycle = _strangle()
    crossing = _replacement_ticket()
    crossing = replace(crossing, legs=(crossing.legs[0], TicketLeg("call88", LegSide.SELL, LegEffect.OPEN, 1, "call", "2026-09-25", 88.0, "short_call")))
    with pytest.raises(ValueError, match="strictly inward"):
        validate_strangle_replacement(lifecycle, crossing.legs, tested_side="put", net_credit=0.2)
    with pytest.raises(ValueError, match="exactly one short_put"):
        adopt_legacy_position({"lane": "short_strangle", "active_legs": []}, lifecycle_id="legacy-1", adopted_at=NOW)
