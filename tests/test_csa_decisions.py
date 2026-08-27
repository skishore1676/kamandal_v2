from __future__ import annotations

import json

import pytest

from kamandal_v2.domain.models import Idea, UniverseEntry
from kamandal_v2.strategy_lanes.admission import AdmissionContext, evaluate_admission
from kamandal_v2.strategy_lanes.models import CsaStage, LaneId, LifecycleState, SourceMode
from kamandal_v2.strategy_lanes.policy import compile_csa_policy
from kamandal_v2.strategy_lanes.registry import lifecycle_registry
from kamandal_v2.strategy_lanes.scoring import score_opportunity
from kamandal_v2.strategy_lanes.sources import idea_opportunity, market_scan_opportunities, portfolio_hedge_opportunities


def _policy_row(structure: str = "short_strangle", **overrides):  # noqa: ANN003, ANN202
    lane_values = {
        "short_strangle": {
            "strategy_family": "short_strangle",
            "source_mode": "market_scan",
            "dte_min": 30,
            "dte_max": 60,
            "short_delta_min": 0.1,
            "short_delta_max": 0.2,
            "iv_rank_min": 35,
            "iv_rank_max": 100,
            "profit_target_pct": 50,
            "max_loss_multiple": 2,
            "exit_dte_min": 21,
            "spread_width": 5,
            "live_max_bpr_per_order": 2500,
            "universe_expansion_enabled": "TRUE",
            "underlying_price_min": 50,
            "underlying_price_max": 250,
            "lifecycle": {
                "tested_side_confirmation": 2,
                "roll": {"min_credit": 0.1, "duration_trigger_dte": 21},
                "adjustment_limit": 2,
                "inversion": {"allowed": True, "max_width": 5},
                "cooldown": {"minutes": 30},
                "loss_stages": {"watch_multiple": 2, "close_multiple": 3},
                "fill": {"max_attempts": 2, "price_increment": 0.05},
                "live_management_mode": "close_only",
            },
        },
        "call_spread": {
            "strategy_family": "call_vertical",
            "source_mode": "portfolio_hedge",
            "dte_min": 30,
            "dte_max": 60,
            "short_delta_min": 0.2,
            "short_delta_max": 0.35,
            "profit_target_pct": 50,
            "max_loss_multiple": 2,
            "exit_dte_min": 21,
            "spread_width": 5,
            "live_max_bpr_per_order": 500,
            "lifecycle": {
                "close_only": True,
                "portfolio_delta_trigger": 25,
                "hedge_underlyings": ["SPY"],
                "fill": {"max_attempts": 2, "price_increment": 0.05},
            },
        },
        "call_diagonal": {
            "strategy_family": "directional_diagonal",
            "source_mode": "idea",
            "dte_min": 20,
            "dte_max": 40,
            "long_dte_min": 60,
            "long_dte_max": 100,
            "short_delta_min": 0.2,
            "short_delta_max": 0.35,
            "long_delta_min": 0.5,
            "long_delta_max": 0.8,
            "profit_target_pct": 50,
            "max_loss_multiple": 0.5,
            "exit_dte_min": 10,
            "spread_width": "",
            "live_max_bpr_per_order": 1500,
            "lifecycle": {"short_leg": {"roll": True, "roll_dte": 7}, "long_only": {"requires_approval": True}, "fill": {"max_attempts": 2, "price_increment": 0.05}},
        },
    }
    values = lane_values[structure]
    lifecycle = values.pop("lifecycle")
    row = {
        "playbook_id": f"{structure}_csa",
        "enabled": "TRUE",
        "structure": structure,
        "csa_stage": "shadow",
        "management_policy_json": json.dumps(
            {
                "penalty_weights": {"event": 0.1},
                "lifecycle": lifecycle,
            }
        ),
        "sizing_method": "fixed_contracts",
        "sizing_value": 1,
        "max_contracts": 1,
        "score_weight_credit": 2,
        "score_weight_pop": 1,
        "score_weight_liquidity": 1,
        "score_weight_spread": 1,
        "max_bid_ask_pct": 1,
        "min_option_oi": 1,
        **values,
    }
    row.update(overrides)
    return row


def _policy(structure: str = "short_strangle", **overrides):  # noqa: ANN003, ANN202
    policy = compile_csa_policy(
        _policy_row(structure, **overrides),
        source="google_sheet",
        read_at="2026-08-08T12:00:00Z",
    )
    assert policy is not None
    return policy


def _admission_context(**overrides):  # noqa: ANN003, ANN202
    values = {
        "market_data_fresh": True,
        "quote_valid": True,
        "structure_valid": True,
        "liquidity_valid": True,
        "bpr": 1200.0,
        "bpr_source": "broker_preflight",
        "broker_state_clear": True,
        "portfolio_allowed": True,
        "buying_power_available": True,
    }
    values.update(overrides)
    return AdmissionContext(**values)


def test_market_scan_source_is_deterministic_and_sheet_driven() -> None:
    policy = _policy()
    universe = [UniverseEntry(symbol="XYZ", enabled=True, profile="large_cap")]
    observations = {"XYZ": {"source_fresh": True, "underlying_price": 100, "iv_rank": 55}}

    first = market_scan_opportunities(universe, [policy], observations, observed_at="2026-08-08T12:00:00Z")
    second = market_scan_opportunities(universe, [policy], observations, observed_at="2026-08-08T12:00:00Z")

    assert first == second
    assert len(first) == 1
    assert first[0].source_mode is SourceMode.MARKET_SCAN
    assert first[0].market_context["underlying_price"] == 100


def test_market_scan_expansion_adds_sheet_ranged_symbol_despite_normal_allowlist() -> None:
    policy = _policy()
    universe = [
        UniverseEntry(
            symbol="XYZ",
            enabled=True,
            profile="large_cap",
            allowed_playbooks=["put_spread_only"],
        )
    ]
    opportunity = market_scan_opportunities(
        universe,
        [policy],
        {"XYZ": {"source_fresh": True, "underlying_price": 100, "iv_rank": 55}},
        observed_at="2026-08-08T12:00:00Z",
    )[0]

    assert opportunity.evidence["source_approved"] is True


def test_portfolio_hedge_source_uses_sheet_trigger_and_underlyings() -> None:
    policy = _policy("call_spread")
    below = portfolio_hedge_opportunities(
        {"delta": 20, "source_fresh": True},
        [policy],
        {"SPY": {"source_fresh": True}},
        observed_at="2026-08-08T12:00:00Z",
    )
    above = portfolio_hedge_opportunities(
        {"delta": 30, "source_fresh": True},
        [policy],
        {"SPY": {"source_fresh": True}},
        observed_at="2026-08-08T12:00:00Z",
    )

    assert below == ()
    assert len(above) == 1
    assert above[0].underlying == "SPY"
    assert above[0].source_mode is SourceMode.PORTFOLIO_HEDGE


def test_ordinary_calendar_uses_generic_close_only_lane_not_earnings() -> None:
    row = _policy_row("call_spread")
    row.update(
        {
            "playbook_id": "ordinary_call_calendar",
            "strategy_family": "call_calendar",
            "structure": "call_calendar",
            "source_mode": "idea",
            "management_policy_json": json.dumps(
                {"lifecycle": {"close_only": True, "fill": {"max_attempts": 2, "price_increment": 0.05}}}
            ),
        }
    )
    policy = compile_csa_policy(row, source="google_sheet", read_at="2026-08-15T12:00:00Z")

    assert policy is not None
    assert policy.lane is LaneId.GENERIC_CLOSE_ONLY
    assert lifecycle_registry().resolve(policy.lane)(
        LifecycleState(
            lifecycle_id="ordinary-calendar",
            opportunity_id="opp",
            lane=policy.lane,
            version=1,
            status="open",
            active_legs=(),
            cashflow_ledger=(),
            opened_at="2026-08-15T12:00:00Z",
            updated_at="2026-08-15T12:00:00Z",
            policy_hash=policy.policy_hash,
        ),
        policy,
        {"ownership_clear": True, "working_order_conflict": False, "hard_emergency": False, "event_exit_due": False, "profit_pct": 0, "loss_multiple": 0, "dte": 30},
        proposed_at="2026-08-15T12:00:00Z",
    )[0].reason_codes == ("close_oriented_hold",)


def test_idea_source_normalizes_without_calling_external_effects() -> None:
    policy = _policy("call_diagonal")
    idea = Idea("idea-1", "manual", "XYZ", "bullish", confidence="high")

    opportunity = idea_opportunity(idea, policy, observed_at="2026-08-08T12:00:00Z")

    assert opportunity.source_id == "idea-1"
    assert opportunity.lane is LaneId.DIRECTIONAL_DIAGONAL
    assert opportunity.confidence is None


def test_pending_automated_idea_remains_source_eligible() -> None:
    policy = _policy("call_diagonal")
    pending = Idea("idea-pending", "llm_transcript:x_timeline.txt", "XYZ", "bullish", operator_status="pending")
    rejected = Idea("idea-rejected", "llm_transcript:x_timeline.txt", "XYZ", "bullish", operator_status="rejected")

    assert idea_opportunity(pending, policy, observed_at="2026-08-08T12:00:00Z").evidence["source_approved"] is True
    assert idea_opportunity(rejected, policy, observed_at="2026-08-08T12:00:00Z").evidence["source_approved"] is False


def test_sheet_weighted_score_is_transparent_and_deterministic() -> None:
    policy = _policy()
    components = {"credit": 80, "pop": 50, "liquidity": 50, "spread": 50}
    first = score_opportunity(policy, components, penalties={"event": 20})
    second = score_opportunity(policy, dict(reversed(list(components.items()))), penalties={"event": 20})

    assert first == second
    assert first.score == pytest.approx(60.0)
    assert first.weights == {"credit": 2.0, "liquidity": 1.0, "pop": 1.0, "spread": 1.0}
    assert "sheet_weighted" in first.formula


def test_score_fails_when_code_supplies_component_not_defined_by_sheet() -> None:
    with pytest.raises(ValueError, match="unexpected=extra"):
        score_opportunity(_policy(), {"credit": 80, "liquidity": 50, "spread": 70, "pop": 70, "extra": 1}, penalties={"event": 0})


def test_admission_preserves_all_rejections_and_primary_blocker() -> None:
    policy = _policy()
    opportunity = market_scan_opportunities(
        [UniverseEntry(symbol="XYZ", enabled=True, profile="large_cap")],
        [policy],
        {"XYZ": {"source_fresh": False, "underlying_price": 300, "iv_rank": 10}},
        observed_at="2026-08-08T12:00:00Z",
    )[0]
    decision = evaluate_admission(
        opportunity,
        policy,
        _admission_context(
            market_data_fresh=False,
            quote_valid=False,
            structure_valid=False,
            bpr=None,
            bpr_source="",
            portfolio_allowed=False,
            ownership_clear=False,
        ),
        decided_at="2026-08-08T12:01:00Z",
    )

    reasons = [reason for stage in decision.stages for reason in stage.reasons]
    assert not decision.admitted
    assert decision.primary_blocker == "source_stale"
    assert "market_underlying_price_outside_sheet_range" in reasons
    assert "market_iv_rank_outside_sheet_range" in reasons
    assert "broker_bpr_unknown" in reasons
    assert "portfolio_ownership_ambiguous" in reasons


def test_shadow_strangle_fallback_is_labeled_but_nonshadow_requires_broker_bpr() -> None:
    shadow_policy = _policy()
    opportunity = market_scan_opportunities(
        [UniverseEntry(symbol="XYZ", enabled=True, profile="large_cap")],
        [shadow_policy],
        {"XYZ": {"source_fresh": True, "underlying_price": 100, "iv_rank": 55}},
        observed_at="2026-08-08T12:00:00Z",
    )[0]
    shadow = evaluate_admission(
        opportunity,
        shadow_policy,
        _admission_context(bpr_source="local_fallback"),
        decided_at="2026-08-08T12:01:00Z",
    )
    assert shadow.admitted
    assert shadow.stages[3].evidence["shadow_only_warning"] == "broker_bpr_fallback"

    live_policy = _policy(csa_stage="pilot_live")
    live_opportunity = market_scan_opportunities(
        [UniverseEntry(symbol="XYZ", enabled=True, profile="large_cap")],
        [live_policy],
        {"XYZ": {"source_fresh": True, "underlying_price": 100, "iv_rank": 55}},
        observed_at="2026-08-08T12:00:00Z",
    )[0]
    live = evaluate_admission(
        live_opportunity,
        live_policy,
        _admission_context(bpr_source="local_fallback"),
        decided_at="2026-08-08T12:01:00Z",
    )
    assert live_policy.stage is CsaStage.PILOT_LIVE
    assert not live.admitted
    assert live.stages[3].reasons == ("broker_bpr_not_authoritative",)


def test_admitted_decision_includes_sheet_score_components() -> None:
    policy = _policy()
    opportunity = market_scan_opportunities(
        [UniverseEntry(symbol="XYZ", enabled=True, profile="large_cap")],
        [policy],
        {"XYZ": {"source_fresh": True, "underlying_price": 100, "iv_rank": 55}},
        observed_at="2026-08-08T12:00:00Z",
    )[0]
    score = score_opportunity(policy, {"credit": 80, "pop": 50, "liquidity": 50, "spread": 50}, penalties={"event": 0})
    decision = evaluate_admission(
        opportunity,
        policy,
        _admission_context(),
        decided_at="2026-08-08T12:01:00Z",
        score=score,
    )

    assert decision.admitted
    assert decision.primary_blocker == ""
    assert decision.score == score.score
    assert decision.score_components == score.components


def test_noncalendar_policy_honors_sheet_event_avoidance() -> None:
    policy = _policy(avoid_earnings="TRUE")
    opportunity = market_scan_opportunities(
        [UniverseEntry(symbol="XYZ", enabled=True, profile="large_cap")],
        [policy],
        {
            "XYZ": {
                "source_fresh": True,
                "underlying_price": 100,
                "iv_rank": 55,
                "event_status": "earnings_soon",
            }
        },
        observed_at="2026-08-08T12:00:00Z",
    )[0]

    decision = evaluate_admission(
        opportunity,
        policy,
        _admission_context(),
        decided_at="2026-08-08T12:01:00Z",
    )

    assert not decision.admitted
    assert "market_event_blocked:earnings_soon" in decision.stages[1].reasons
