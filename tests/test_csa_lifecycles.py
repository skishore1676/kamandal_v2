from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from kamandal_v2.domain.models import ChainSnapshot, Idea, OptionLeg, OptionQuote
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.action_arbiter import arbitrate_actions
from kamandal_v2.strategy_lanes.builders import build_lane_candidates
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.strategy_lanes.models import (
    ActionDisposition,
    ActionType,
    CsaAction,
    LaneId,
    LegEffect,
    LegSide,
    LifecycleState,
    ShadowFill,
    SourceMode,
    StrategyOpportunity,
    StrategyTicket,
    TicketLeg,
)
from kamandal_v2.strategy_lanes.policy import compile_csa_policy
from kamandal_v2.strategy_lanes.registry import lifecycle_registry
from kamandal_v2.strategy_lanes.shadow_execution import ShadowExecutionAdapter
from kamandal_v2.strategy_lanes.store import CsaStore
from kamandal_v2.strategy_lanes.strangle import build_strangle_adjustment_ticket
from kamandal_v2.strategy_lanes.tickets import mixed_ticket, open_ticket_from_candidate


TODAY = date.today()
NOW = f"{TODAY.isoformat()}T12:00:00Z"


def _row(structure: str):  # noqa: ANN202
    common = {
        "playbook_id": f"{structure}_csa",
        "enabled": "TRUE",
        "strategy_family": structure,
        "structure": structure,
        "csa_stage": "shadow",
        "sizing_method": "fixed_contracts",
        "sizing_value": 1,
        "max_contracts": 1,
        "score_weight_credit": 1,
        "score_weight_pop": 1,
        "score_weight_liquidity": 1,
        "score_weight_spread": 1,
        "max_bid_ask_pct": 1,
        "min_option_oi": 1,
        "profit_target_pct": 50,
        "live_max_bpr_per_order": 2500,
    }
    if structure == "short_strangle":
        fields = {
            "source_mode": "market_scan",
            "dte_min": 30,
            "dte_max": 60,
            "short_delta_min": 0.1,
            "short_delta_max": 0.2,
            "iv_rank_min": 35,
            "iv_rank_max": 100,
            "exit_dte_min": 21,
            "universe_expansion_enabled": "TRUE",
            "underlying_price_min": 50,
            "underlying_price_max": 250,
            "lifecycle": {
                "tested_side_confirmation": 2,
                "roll": {"min_credit": 0.1, "duration_trigger_dte": 21},
                "adjustment_limit": 2,
                "inversion": {"allowed": True, "max_width": 5},
                "cooldown": {"minutes": 30},
                "loss_stages": {"watch_multiple": 1.5, "close_multiple": 3},
                "fill": {"max_attempts": 2, "price_increment": 0.05},
            },
        }
    elif structure == "call_spread":
        fields = {
            "source_mode": "portfolio_hedge",
            "dte_min": 30,
            "dte_max": 60,
            "short_delta_min": 0.2,
            "short_delta_max": 0.35,
            "spread_width": 5,
            "max_loss_multiple": 2,
            "exit_dte_min": 21,
            "lifecycle": {"close_only": True, "portfolio_delta_trigger": 25, "hedge_underlyings": ["SPY"], "fill": {"max_attempts": 2, "price_increment": 0.05}},
        }
    elif structure == "call_diagonal":
        fields = {
            "source_mode": "idea",
            "dte_min": 20,
            "dte_max": 40,
            "long_dte_min": 60,
            "long_dte_max": 100,
            "short_delta_min": 0.2,
            "short_delta_max": 0.35,
            "long_delta_min": 0.5,
            "long_delta_max": 0.8,
            "spread_width": 5,
            "max_loss_multiple": 2,
            "exit_dte_min": 10,
            "lifecycle": {"short_leg": {"roll": True, "roll_dte": 7}, "long_only": {"requires_approval": True}, "fill": {"max_attempts": 2, "price_increment": 0.05}},
        }
    elif structure == "call_calendar":
        common["strategy_family"] = "earnings_calendar"
        fields = {
            "source_mode": "idea",
            "dte_min": 5,
            "dte_max": 20,
            "long_dte_min": 25,
            "long_dte_max": 50,
            "long_delta_min": 0.4,
            "long_delta_max": 0.6,
            "exit_pre_event_days": "",
            "event_timing": "confirmed_bmo_or_amc_final_pre_event_session",
            "event_near_expiry_after_days": 1,
            "paired_order_required": "TRUE",
            "post_event_exit": "first_eligible_post_event_session",
            "lifecycle": {"event_expiration": {"near_before_days": 7, "far_after_days": 21}, "close_only": True, "fill": {"max_attempts": 2, "price_increment": 0.05}},
        }
    else:
        raise AssertionError(structure)
    common.update(fields)
    lifecycle = common.pop("lifecycle")
    common["management_policy_json"] = json.dumps({"lifecycle": lifecycle})
    return common


def _policy(structure: str):  # noqa: ANN202
    result = compile_csa_policy(_row(structure), source="google_sheet", read_at=NOW)
    assert result is not None
    return result


def test_earnings_policy_requires_post_event_contract_not_legacy_pre_event_exit() -> None:
    row = _row("call_calendar")
    row["management_policy_json"] = json.dumps(
        {"lifecycle": {"close_only": True, "fill": {"max_attempts": 4, "price_increment": 0.05}}}
    )

    policy = compile_csa_policy(row, source="google_sheet", read_at=NOW)

    assert policy is not None
    assert "exit_pre_event_days" not in policy.resolved_fields
    assert policy.resolved_fields["post_event_exit"] == "first_eligible_post_event_session"


def _opportunity(policy, *, event_state: str = "not_applicable"):  # noqa: ANN001, ANN202
    source_mode = policy.source_mode
    evidence = {"source_approved": True, "source_fresh": True}
    if source_mode is SourceMode.IDEA:
        evidence["idea"] = Idea("idea-1", "manual", "XYZ", "bullish").to_dict()
    return StrategyOpportunity(
        opportunity_id=f"opp-{policy.lane.value}",
        lane=policy.lane,
        source_mode=source_mode,
        playbook_id=policy.playbook_id,
        underlying="XYZ",
        observed_at=NOW,
        source_id="source-1",
        policy_hash=policy.policy_hash,
        evidence=evidence,
        event_context={
            "state": event_state,
            "event_date": (TODAY + timedelta(days=17)).isoformat() if event_state in {"known", "confirmed"} else "",
        },
    )


def _quote(option_type: str, strike: float, dte: int, delta: float, bid: float, ask: float):  # noqa: ANN202
    return OptionQuote(
        underlying="XYZ",
        expiration=(TODAY + timedelta(days=dte)).isoformat(),
        option_type=option_type,
        strike=strike,
        bid=bid,
        ask=ask,
        delta=delta,
        gamma=0.01,
        theta=-0.02,
        vega=0.05,
        iv=0.4,
        open_interest=1000,
    )


def _chain(quotes):  # noqa: ANN001, ANN202
    return ChainSnapshot("chain-1", "XYZ", NOW, 100, list(quotes), "fixture")


def _lifecycle(lane: LaneId, *, version: int = 1):
    return LifecycleState(
        lifecycle_id=f"life-{lane.value}",
        opportunity_id=f"opp-{lane.value}",
        lane=lane,
        version=version,
        status="open",
        active_legs=(),
        cashflow_ledger=(),
        opened_at=NOW,
        updated_at=NOW,
        policy_hash="policy",
    )


def test_existing_builders_create_all_four_csa_lane_entries() -> None:
    strangle = _policy("short_strangle")
    strangle_candidates = build_lane_candidates(
        _opportunity(strangle),
        strangle,
        _chain([_quote("put", 90, 45, -0.15, 1.0, 1.1), _quote("call", 110, 45, 0.15, 1.0, 1.1)]),
    )
    vertical = _policy("call_spread")
    vertical_candidates = build_lane_candidates(
        _opportunity(vertical),
        vertical,
        _chain([_quote("call", 105, 45, 0.25, 2.0, 2.1), _quote("call", 110, 45, 0.1, 0.8, 0.9)]),
    )
    diagonal = _policy("call_diagonal")
    diagonal_candidates = build_lane_candidates(
        _opportunity(diagonal),
        diagonal,
        _chain([_quote("call", 105, 30, 0.25, 1.5, 1.6), _quote("call", 100, 75, 0.6, 8.0, 8.2)]),
    )
    calendar = _policy("call_calendar")
    calendar_candidates = build_lane_candidates(
        _opportunity(calendar, event_state="known"),
        calendar,
        _chain([_quote("call", 100, 20, 0.5, 2.0, 2.1), _quote("call", 100, 35, 0.5, 4.0, 4.2)]),
    )

    assert {item.structure for item in strangle_candidates} == {"short_strangle"}
    assert {item.structure for item in vertical_candidates} == {"call_spread"}
    assert {item.structure for item in diagonal_candidates} == {"call_diagonal"}
    assert {item.structure for item in calendar_candidates} == {"call_calendar"}
    assert all(any(reason.startswith("csa_policy_hash=") for reason in item.reasons) for item in (*strangle_candidates, *vertical_candidates, *diagonal_candidates, *calendar_candidates))


def test_earnings_calendar_uses_event_relative_expirations_for_tomorrow_event() -> None:
    policy = _policy("call_calendar")
    today = TODAY
    opportunity = replace(
        _opportunity(policy, event_state="confirmed"),
        observed_at=f"{today.isoformat()}T12:00:00Z",
        event_context={"state": "confirmed", "event_date": (today + timedelta(days=1)).isoformat()},
    )
    candidates = build_lane_candidates(
        opportunity,
        policy,
        _chain([_quote("call", 100, 4, 0.5, 2.0, 2.1), _quote("call", 100, 18, 0.5, 4.0, 4.2)]),
    )

    assert len(candidates) == 1
    assert [leg.expiration for leg in candidates[0].legs] == [
        (today + timedelta(days=4)).isoformat(),
        (today + timedelta(days=18)).isoformat(),
    ]


def test_diagonal_blank_sheet_width_is_derived_from_the_quote_grid() -> None:
    row = _row("call_diagonal")
    row["spread_width"] = ""
    policy = compile_csa_policy(row, source="google_sheet", read_at=NOW)
    assert policy is not None
    candidates = build_lane_candidates(
        _opportunity(policy),
        policy,
        _chain(
            [
                _quote("call", 103, 30, 0.25, 1.5, 1.6),
                _quote("call", 100, 75, 0.6, 8.0, 8.2),
                _quote("call", 97, 75, 0.7, 10.0, 10.2),
            ]
        ),
    )

    assert candidates
    assert any(reason == "csa_width_source=strike_grid" for reason in candidates[0].reasons)
    assert any(reason == "csa_actual_width=3" for reason in candidates[0].reasons)


def test_earnings_builder_fails_closed_without_known_event() -> None:
    policy = _policy("call_calendar")
    candidates = build_lane_candidates(
        _opportunity(policy, event_state="unknown"),
        policy,
        _chain([_quote("call", 100, 20, 0.5, 2.0, 2.1), _quote("call", 100, 35, 0.5, 4.0, 4.2)]),
    )
    assert candidates == ()


def test_earnings_builder_requires_event_between_sheet_bounded_expirations() -> None:
    policy = _policy("call_calendar")
    event_date = TODAY + timedelta(days=17)
    opportunity = _opportunity(policy, event_state="confirmed")
    accepted = build_lane_candidates(
        opportunity,
        policy,
        _chain([_quote("call", 100, 20, 0.5, 2.0, 2.1), _quote("call", 100, 35, 0.5, 4.0, 4.2)]),
    )
    rejected = build_lane_candidates(
        opportunity,
        policy,
        _chain([_quote("call", 100, 20, 0.5, 2.0, 2.1), _quote("call", 100, 45, 0.5, 4.0, 4.2)]),
    )

    assert accepted
    assert any(reason == f"event_date={event_date.isoformat()}" for reason in accepted[0].reasons)
    assert rejected == ()


def test_action_arbiter_selects_one_highest_precedence_action_deterministically() -> None:
    policy = _policy("short_strangle")
    lifecycle = _lifecycle(policy.lane)
    handler = lifecycle_registry().resolve(policy.lane)
    context = {
        "working_order_conflict": True,
        "ownership_clear": True,
        "hard_emergency": False,
        "event_exit_due": False,
        "profit_pct": 80,
        "dte": 10,
        "loss_multiple": 0,
        "duration_roll_due": False,
        "tested_side": True,
        "cooldown_elapsed": True,
        "tested_side_confirmations": 2,
        "adjustment_count": 0,
        "same_expiry_roll_credit": 0.2,
        "inversion_possible": True,
    }
    first = arbitrate_actions(handler(lifecycle, policy, context, proposed_at=NOW))
    second = arbitrate_actions(handler(lifecycle, policy, context, proposed_at=NOW))

    assert first == second
    assert first.selected.action_type is ActionType.BLOCK
    assert first.selected.reason_codes == ("working_order_conflict",)
    assert sum(action.disposition is ActionDisposition.SELECTED for action in first.actions) == 1


def test_lane_lifecycle_scenarios_cover_adjust_close_and_approval_block() -> None:
    registry = lifecycle_registry()
    strangle = _policy("short_strangle")
    strangle_result = arbitrate_actions(
        registry.resolve(strangle.lane)(
            _lifecycle(strangle.lane),
            strangle,
            {
                "working_order_conflict": False,
                "ownership_clear": True,
                "hard_emergency": False,
                "event_exit_due": False,
                "profit_pct": 0,
                "dte": 45,
                "loss_multiple": 0,
                "duration_roll_due": False,
                "tested_side": True,
                "cooldown_elapsed": True,
                "tested_side_confirmations": 2,
                "adjustment_count": 0,
                "same_expiry_roll_credit": 0.2,
                "inversion_possible": False,
            },
            proposed_at=NOW,
        )
    )
    assert strangle_result.selected.action_type is ActionType.ADJUST

    vertical = _policy("call_spread")
    vertical_result = arbitrate_actions(
        registry.resolve(vertical.lane)(
            _lifecycle(vertical.lane),
            vertical,
            {"working_order_conflict": False, "ownership_clear": True, "hard_emergency": False, "loss_multiple": 0, "event_exit_due": False, "profit_pct": 60, "dte": 40},
            proposed_at=NOW,
        )
    )
    assert vertical_result.selected.reason_codes == ("profit_target",)

    diagonal = _policy("call_diagonal")
    diagonal_result = arbitrate_actions(
        registry.resolve(diagonal.lane)(
            _lifecycle(diagonal.lane),
            diagonal,
            {
                "working_order_conflict": False,
                "ownership_clear": True,
                "hard_emergency": False,
                "loss_multiple": 0,
                "event_exit_due": False,
                "profit_pct": 0,
                "far_dte": 70,
                "short_leg_present": False,
                "long_only_approved": False,
            },
            proposed_at=NOW,
        )
    )
    assert diagonal_result.selected.reason_codes == ("diagonal_pair_reconciliation_required",)

    earnings = _policy("call_calendar")
    earnings_result = arbitrate_actions(
        registry.resolve(earnings.lane)(
            _lifecycle(earnings.lane),
            earnings,
            {"working_order_conflict": False, "ownership_clear": True, "event_state": "confirmed", "hard_emergency": False, "event_exit_due": True, "profit_pct": 0, "near_leg_expired": False},
            proposed_at=NOW,
        )
    )
    assert earnings_result.selected.reason_codes == ("earnings_event_exit",)


def test_all_lanes_have_deterministic_hold_and_earnings_has_expiry_close() -> None:
    registry = lifecycle_registry()
    cases = [
        (
            _policy("short_strangle"),
            {
                "working_order_conflict": False,
                "ownership_clear": True,
                "hard_emergency": False,
                "event_exit_due": False,
                "profit_pct": 0,
                "dte": 45,
                "loss_multiple": 0,
                "duration_roll_due": False,
                "tested_side": False,
                "cooldown_elapsed": True,
                "tested_side_confirmations": 0,
                "adjustment_count": 0,
                "same_expiry_roll_credit": 0,
                "inversion_possible": False,
            },
        ),
        (
            _policy("call_spread"),
            {"working_order_conflict": False, "ownership_clear": True, "hard_emergency": False, "loss_multiple": 0, "event_exit_due": False, "profit_pct": 0, "dte": 45},
        ),
        (
            _policy("call_diagonal"),
            {
                "working_order_conflict": False,
                "ownership_clear": True,
                "hard_emergency": False,
                "loss_multiple": 0,
                "event_exit_due": False,
                "profit_pct": 0,
                "far_dte": 70,
                "short_leg_present": True,
                "short_leg_roll_due": False,
            },
        ),
        (
            _policy("call_calendar"),
            {"working_order_conflict": False, "ownership_clear": True, "event_state": "confirmed", "hard_emergency": False, "event_exit_due": False, "profit_pct": 0, "near_leg_expired": False},
        ),
    ]
    for policy, context in cases:
        result = arbitrate_actions(
            registry.resolve(policy.lane)(_lifecycle(policy.lane), policy, context, proposed_at=NOW)
        )
        assert result.selected.action_type is ActionType.HOLD

    earnings, context = cases[-1]
    expiry_context = dict(context)
    expiry_context["near_leg_expired"] = True
    expiry = arbitrate_actions(
        registry.resolve(earnings.lane)(_lifecycle(earnings.lane), earnings, expiry_context, proposed_at=NOW)
    )
    assert expiry.selected.reason_codes == ("near_leg_expiry_close",)


@pytest.mark.parametrize(
    ("structure", "context"),
    [
        (
            "short_strangle",
            {
                "working_order_conflict": False,
                "ownership_clear": True,
                "hard_emergency": False,
                "event_exit_due": False,
                "half_time_exit_due": True,
                "profit_pct": 0,
                "loss_multiple": 0,
                "dte": 29,
                "tested_side": "",
                "cooldown_elapsed": True,
                "tested_side_confirmations": 0,
                "adjustment_count": 0,
                "same_expiry_roll_credit": 0,
            },
        ),
        (
            "call_spread",
            {
                "working_order_conflict": False,
                "ownership_clear": True,
                "hard_emergency": False,
                "event_exit_due": False,
                "half_time_exit_due": True,
                "profit_pct": 0,
                "loss_multiple": 0,
                "dte": 29,
            },
        ),
        (
            "call_diagonal",
            {
                "working_order_conflict": False,
                "ownership_clear": True,
                "hard_emergency": False,
                "event_exit_due": False,
                "half_time_exit_due": True,
                "profit_pct": 0,
                "loss_multiple": 0,
                "far_dte": 59,
                "short_leg_present": True,
                "paired_position_complete": True,
            },
        ),
    ],
)
def test_half_time_exit_closes_the_complete_strategy_package(structure, context) -> None:  # noqa: ANN001
    policy = _policy(structure)
    selected = arbitrate_actions(
        lifecycle_registry().resolve(policy.lane)(
            _lifecycle(policy.lane),
            policy,
            context,
            proposed_at=NOW,
        )
    ).selected

    assert selected.action_type is ActionType.CLOSE
    assert selected.reason_codes == ("half_time_exit",)


def test_normal_pre_event_exit_has_priority_over_half_time() -> None:
    policy = _policy("call_spread")
    selected = arbitrate_actions(
        lifecycle_registry().resolve(policy.lane)(
            _lifecycle(policy.lane),
            policy,
            {
                "working_order_conflict": False,
                "ownership_clear": True,
                "hard_emergency": False,
                "event_exit_due": True,
                "half_time_exit_due": True,
                "profit_pct": 0,
                "loss_multiple": 0,
                "dte": 29,
            },
            proposed_at=NOW,
        )
    ).selected

    assert selected.action_type is ActionType.CLOSE
    assert selected.reason_codes == ("mandatory_event_exit",)


def test_mixed_tickets_reverse_close_sides_and_shadow_restart_is_safe(tmp_path) -> None:
    policy = _policy("short_strangle")
    candidate = build_lane_candidates(
        _opportunity(policy),
        policy,
        _chain([_quote("put", 90, 45, -0.15, 1.0, 1.1), _quote("call", 110, 45, 0.15, 1.0, 1.1)]),
    )[0]
    lifecycle = _lifecycle(policy.lane)
    action = CsaAction("open-action", lifecycle.lifecycle_id, lifecycle.version, ActionType.OPEN, ActionDisposition.SELECTED, ("admitted",), NOW, 1)
    ticket = open_ticket_from_candidate(candidate, action, policy, created_at=NOW, limit_price=1.5)
    quotes = {leg.instrument_id: {"bid": 1.0, "ask": 1.1, "fresh": True} for leg in ticket.legs}
    adapter = ShadowExecutionAdapter()
    fill = adapter.simulate_fill(ticket, quotes, {"max_attempts": 2, "price_increment": 0.05}, observed_at=NOW, attempt=0)
    opened = adapter.adopt_fill(lifecycle, ticket, fill)

    database = tmp_path / "kamandal.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    store = CsaStore(database)
    store.save_lifecycle(opened)
    store.save_action(action)
    store.save_shadow_order_intent(ticket)
    store.save_shadow_fill(fill)
    restarted = CsaStore(database).lifecycle(lifecycle.lifecycle_id)

    assert fill.status == "filled"
    assert restarted == opened
    assert len(opened.active_legs) == 2
    assert len(opened.cashflow_ledger) == 1

    close_action = CsaAction("close-action", opened.lifecycle_id, opened.version, ActionType.CLOSE, ActionDisposition.SELECTED, ("profit_target",), NOW, 1)
    close_ticket = mixed_ticket(
        close_action,
        policy,
        underlying="XYZ",
        close_legs=candidate.legs,
        open_legs=(),
        created_at=NOW,
        limit_price=-2.2,
    )
    assert all(leg.effect.value == "close" for leg in close_ticket.legs)
    assert {leg.side.value for leg in close_ticket.legs} == {"buy"}


def test_close_fill_matches_adopted_leg_without_instrument_id_and_is_idempotent() -> None:
    lifecycle = replace(
        _lifecycle(LaneId.EARNINGS_CALENDAR, version=2),
        active_legs=(
            {
                "side": "buy",
                "effect": "open",
                "quantity": 1,
                "option_type": "call",
                "expiration": "2026-10-16",
                "strike": 290.0,
                "role": "long_call",
            },
        ),
    )
    close_ticket = StrategyTicket(
        ticket_id="close-adopted-calendar",
        action_id="close-action",
        lifecycle_id=lifecycle.lifecycle_id,
        lifecycle_version=lifecycle.version,
        lane=lifecycle.lane,
        underlying="AMZN",
        order_kind="credit",
        limit_price=3.30,
        legs=(
            TicketLeg(
                instrument_id="AMZN  261016C00290000",
                side=LegSide.SELL,
                effect=LegEffect.CLOSE,
                quantity=1,
                option_type="call",
                expiration="2026-10-16",
                strike=290.0,
                role="long_call",
            ),
        ),
        policy_hash="policy",
        created_at=NOW,
        metadata={"action_type": "close"},
    )
    fill = ShadowFill(
        fill_id="broker-fill-1",
        ticket_id=close_ticket.ticket_id,
        lifecycle_id=lifecycle.lifecycle_id,
        status="filled",
        attempt=0,
        natural_price=3.31,
        working_price=3.30,
        filled_price=3.31,
        filled_at=NOW,
        quote_evidence={"source": "broker_order_status"},
    )

    closed = ShadowExecutionAdapter().adopt_fill(lifecycle, close_ticket, fill)
    replayed = ShadowExecutionAdapter().adopt_fill(
        replace(closed, version=close_ticket.lifecycle_version),
        close_ticket,
        fill,
    )

    assert closed.status == "closed"
    assert closed.active_legs == ()
    assert len(closed.cashflow_ledger) == 1
    assert len(replayed.cashflow_ledger) == 1


def test_diagonal_partial_state_blocks_and_never_emits_a_short_leg_roll() -> None:
    policy = _policy("call_diagonal")
    lifecycle = _lifecycle(policy.lane)
    result = arbitrate_actions(
        lifecycle_registry().resolve(policy.lane)(
            lifecycle,
            policy,
            {
                "working_order_conflict": False,
                "ownership_clear": True,
                "hard_emergency": False,
                "loss_multiple": 0,
                "event_exit_due": False,
                "profit_pct": 0,
                "far_dte": 70,
                "short_leg_present": False,
                "paired_position_complete": False,
            },
            proposed_at=NOW,
        )
    )

    assert result.selected.action_type is ActionType.BLOCK
    assert result.selected.reason_codes == ("diagonal_pair_reconciliation_required",)


def test_strangle_adjustment_cannot_increase_short_contract_count() -> None:
    policy = _policy("short_strangle")
    candidate = build_lane_candidates(
        _opportunity(policy),
        policy,
        _chain([_quote("put", 90, 45, -0.15, 1.0, 1.1), _quote("call", 110, 45, 0.15, 1.0, 1.1)]),
    )[0]
    lifecycle = _lifecycle(policy.lane)
    open_action = CsaAction("open-strangle", lifecycle.lifecycle_id, 1, ActionType.OPEN, ActionDisposition.SELECTED, ("admitted",), NOW, 1)
    open_ticket = open_ticket_from_candidate(candidate, open_action, policy, created_at=NOW, limit_price=1.5)
    quotes = {leg.instrument_id: {"bid": 1.0, "ask": 1.1, "fresh": True} for leg in open_ticket.legs}
    adapter = ShadowExecutionAdapter()
    opened = adapter.adopt_fill(
        lifecycle,
        open_ticket,
        adapter.simulate_fill(open_ticket, quotes, {"max_attempts": 2, "price_increment": 0.05}, observed_at=NOW, attempt=0),
    )
    adjust_action = CsaAction(
        "bad-adjustment",
        opened.lifecycle_id,
        opened.version,
        ActionType.ADJUST,
        ActionDisposition.SELECTED,
        ("tested_side_confirmed",),
        NOW,
        1,
        {"adjustment_kind": "untested_side_same_expiry_credit_roll"},
    )

    with pytest.raises(ValueError, match="increase short contracts"):
        build_strangle_adjustment_ticket(
            opened,
            adjust_action,
            policy,
            underlying="XYZ",
            close_legs=(candidate.legs[0],),
            open_legs=tuple(candidate.legs),
            created_at=NOW,
            limit_price=0.1,
        )
