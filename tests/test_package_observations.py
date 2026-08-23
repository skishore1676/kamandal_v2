from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from kamandal_v2.domain.models import OptionLeg
from kamandal_v2.live.execution import _repriced_close_limit_price
from kamandal_v2.live.option_sessions import submission_window
from kamandal_v2.live.orders import build_csa_live_ticket
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.lane_common import propose_action
from kamandal_v2.strategy_lanes.call_vertical import propose_call_vertical_actions
from kamandal_v2.strategy_lanes.action_arbiter import arbitrate_actions
from kamandal_v2.strategy_lanes.management_runtime import _apply_management_safety
from kamandal_v2.strategy_lanes.models import ActionType, CsaStage, LaneId, LifecycleState, SourceMode
from kamandal_v2.strategy_lanes.observations import observe_package
from kamandal_v2.strategy_lanes.policy import CsaPolicy
from kamandal_v2.strategy_lanes.tickets import mixed_ticket


def _policy(*, max_spread: float = 0.20) -> CsaPolicy:
    return CsaPolicy(
        playbook_id="short_strangle_csa",
        lane=LaneId.SHORT_STRANGLE,
        stage=CsaStage.SHADOW,
        source_mode=SourceMode.MARKET_SCAN,
        management={"lifecycle": {"fill": {"max_attempts": 4, "price_increment": 0.05}}},
        resolved_fields={"max_bid_ask_pct": max_spread, "profit_target_pct": 50},
        policy_hash="policy",
        source="test",
        read_at="2026-08-18T13:30:00Z",
    )


def _lifecycle() -> LifecycleState:
    return LifecycleState(
        lifecycle_id="lifecycle-tlt",
        opportunity_id="opportunity-tlt",
        lane=LaneId.SHORT_STRANGLE,
        version=1,
        status="open",
        active_legs=(
            {"role": "short_put", "side": "sell", "quantity": 1, "option_type": "put", "strike": 79.0, "expiration": "2026-09-18"},
            {"role": "short_call", "side": "sell", "quantity": 1, "option_type": "call", "strike": 85.0, "expiration": "2026-09-18"},
        ),
        cashflow_ledger=({"amount": 0.56, "filled_at": "2026-08-17T18:45:00Z"},),
        opened_at="2026-08-17T18:45:00Z",
        updated_at="2026-08-18T13:30:00Z",
        policy_hash="policy",
        metadata={"execution_mode": "shadow", "underlying": "TLT", "cumulative_cashflow": 0.56},
    )


def _leg(role: str, option_type: str, strike: float, bid: float, ask: float) -> OptionLeg:
    return OptionLeg(
        role=role,
        side="sell",
        option_type=option_type,
        strike=strike,
        expiration="2026-09-18",
        quantity=1,
        mid=(bid + ask) / 2,
        bid=bid,
        ask=ask,
        delta=0.2,
        gamma=0,
        theta=0,
        vega=0,
        open_interest=100,
    )


def _snapshot(captured_at: str) -> SimpleNamespace:
    return SimpleNamespace(
        underlying="TLT",
        chain_snapshot_id="snapshot-tlt",
        captured_at=captured_at,
    )


def test_tlt_wide_natural_quote_is_evidence_not_a_loss_decision() -> None:
    observed_at = "2026-08-18T13:30:00Z"
    observation = observe_package(
        _lifecycle(),
        _policy(),
        (
            _leg("short_put", "put", 79, 0.15, 2.70),
            _leg("short_call", "call", 85, 0.00, 0.26),
        ),
        _snapshot(observed_at),
        observed_at=observed_at,
    )

    assert observation.midpoint_liquidation == pytest.approx(-1.555)
    assert observation.natural_liquidation == pytest.approx(-2.96)
    assert observation.loss_multiple == pytest.approx(1.555 / 0.56)
    assert observation.quote_actionable is False
    assert "spread_exceeds_frozen_policy" in observation.quote_blockers


def test_tight_package_uses_midpoint_for_decision_and_natural_for_boundary() -> None:
    observed_at = "2026-08-18T19:35:00Z"
    observation = observe_package(
        _lifecycle(),
        _policy(max_spread=0.30),
        (
            _leg("short_put", "put", 79, 0.20, 0.24),
            _leg("short_call", "call", 85, 0.12, 0.14),
        ),
        _snapshot(observed_at),
        observed_at=observed_at,
    )

    assert observation.quote_actionable is True
    assert observation.midpoint_liquidation == pytest.approx(-0.35)
    assert observation.natural_liquidation == pytest.approx(-0.38)
    assert observation.midpoint_pnl == pytest.approx(0.21)


def test_adverse_loss_is_observe_only_at_market_open(tmp_path) -> None:
    observed_at = "2026-08-21T13:30:00Z"  # 08:30 CT
    lifecycle = _lifecycle()
    raw = propose_action(
        lifecycle,
        ActionType.CLOSE,
        "loss_stage_close",
        arbiter_class="adverse_price_loss",
        proposed_at=observed_at,
    )
    observation = observe_package(
        lifecycle,
        _policy(max_spread=0.30),
        (_leg("short_put", "put", 79, 0.20, 0.24), _leg("short_call", "call", 85, 0.12, 0.14)),
        _snapshot(observed_at),
        observed_at=observed_at,
    )

    selected, protected = _apply_management_safety(
        raw,
        lifecycle,
        observation,
        config={},
        store=LocalStore(tmp_path / "kamandal.db"),
        proposed_at=observed_at,
    )

    assert selected.action_type is ActionType.HOLD
    assert selected.reason_codes == ("adverse_loss_session_buffer",)
    assert protected.loss_window_allowed is False
    assert protected.loss_confirmation_count == 0


def test_reason_aware_window_keeps_profit_exit_open_but_blocks_loss_exit() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 8, 21, 8, 30, tzinfo=ZoneInfo("America/Chicago"))
    profit = submission_window(
        {},
        {"underlying": "NVDA", "intent_type": "close", "csa_action_type": "close", "csa_action_reason_class": "executable_profit"},
        close=True,
        now=now,
    )
    loss = submission_window(
        {},
        {"underlying": "NVDA", "intent_type": "close", "csa_action_type": "close", "csa_action_reason_class": "adverse_price_loss"},
        close=True,
        now=now,
    )

    assert profit["allowed"] is True
    assert loss["allowed"] is False
    assert loss["reason"] == "adverse_exit_opening_buffer"


def test_scheduled_exit_wins_when_loss_and_dte_are_both_due() -> None:
    lifecycle = replace(_lifecycle(), lane=LaneId.CALL_VERTICAL)
    policy = replace(
        _policy(),
        lane=LaneId.CALL_VERTICAL,
        resolved_fields={
            **_policy().resolved_fields,
            "max_loss_multiple": 1.5,
            "profit_target_pct": 50,
            "exit_dte_min": 21,
        },
    )
    proposals = propose_call_vertical_actions(
        lifecycle,
        policy,
        {
            "working_order_conflict": False,
            "ownership_clear": True,
            "hard_emergency": False,
            "event_exit_due": False,
            "profit_pct": -50,
            "loss_multiple": 3,
            "dte": 10,
            "half_time_exit_due": True,
        },
        proposed_at="2026-08-21T13:30:00Z",
    )

    selected = arbitrate_actions(proposals).selected

    assert selected.action_type is ActionType.CLOSE
    assert selected.reason_codes == ("time_exit",)
    assert selected.payload["arbiter_class"] == "time_decision"


def test_typed_close_retains_reason_and_midpoint_to_natural_envelope() -> None:
    lifecycle = _lifecycle()
    action = propose_action(
        lifecycle,
        ActionType.CLOSE,
        "defined_risk_loss_exit",
        arbiter_class="adverse_price_loss",
        proposed_at="2026-08-21T15:00:00Z",
    )
    ticket = mixed_ticket(
        action,
        _policy(),
        underlying="TLT",
        close_legs=(_leg("short_put", "put", 79, 0.20, 0.24),),
        open_legs=(),
        created_at="2026-08-21T15:00:00Z",
        limit_price=-0.22,
    )
    ticket = replace(
        ticket,
        metadata={
            **ticket.metadata,
            "decision_observation_id": "observation-1",
            "exit_reason": "defined_risk_loss_exit",
            "exit_reason_class": "adverse_price_loss",
            "exit_midpoint_net": -22.0,
            "exit_natural_net": -24.0,
        },
    )
    live = build_csa_live_ticket(ticket)

    assert live["csa_action_reason_class"] == "adverse_price_loss"
    assert live["decision_observation_id"] == "observation-1"
    assert live["exit_natural_net"] == -24.0
    assert _repriced_close_limit_price(live, {}) == "0.24"
