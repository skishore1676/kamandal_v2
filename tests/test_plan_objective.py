from __future__ import annotations

from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg, PortfolioState
from kamandal_v2.planner.engine import PlanRunResult
from kamandal_v2.planner.plan_generator import generate_plans
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.planning import _record_short_strangle_source_comparison
from kamandal_v2.strategy_engine.policy import ExecutionMode


def _portfolio() -> PortfolioState:
    return PortfolioState(
        account_size=10_000,
        buying_power=10_000,
        bpr_used=0,
        positions_count=0,
        greeks=Greeks(),
    )


def _control() -> dict:
    return {
        "runtime": {"mode": "live"},
        "portfolio": {
            "max_positions": 5,
            "hard_max_bpr_utilization_pct": 90,
            "max_bpr_per_underlying_pct": 90,
            "delta_bias": "slightly_negative",
        },
        "execution": {"approval_mode": "live_plan_only"},
    }


def _basket_control(**basket: float | int) -> dict:
    control = _control()
    control["runtime"]["mode"] = "shadow"
    control["shadow"] = {"basket": basket}
    return control


def _delta_guard_control() -> dict:
    control = _control()
    control["portfolio"]["delta_guard"] = {
        "enabled": True,
        "apply_modes": ["live"],
        "min_delta": -2.0,
        "max_delta": 0.0,
        "allow_improvement_when_outside": True,
    }
    return control


def _candidate(
    candidate_id: str,
    *,
    underlying: str,
    structure: str,
    bpr: float,
    delta: float,
    gamma: float,
    theta: float,
    vega: float,
    net_credit: float,
    liquidity: float = 0.95,
    score: float = 30.0,
    iv_pct: float = 50.0,
    iv_rank: float = 50.0,
) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        idea_id=f"idea_{candidate_id}",
        underlying=underlying,
        playbook_id=f"{structure}_default",
        structure=structure,
        legs=[
            OptionLeg(
                role="test",
                side="sell" if net_credit > 0 else "buy",
                option_type="call",
                strike=100.0,
                expiration="2026-06-19",
                quantity=1,
                mid=abs(net_credit) or 1.0,
                bid=max(abs(net_credit) - 0.05, 0.01),
                ask=abs(net_credit) + 0.05,
                delta=abs(delta),
                gamma=abs(gamma),
                theta=-abs(theta) if net_credit > 0 else theta,
                vega=abs(vega),
                open_interest=1000,
            )
        ],
        net_credit=net_credit,
        estimated_bpr=bpr,
        greeks=Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega),
        liquidity_score=liquidity,
        score=score,
        reasons=[f"iv_pct={iv_pct}", f"iv_rank={iv_rank}"],
    )


def _source_priority_control() -> dict:
    control = _control()
    control["planner"] = {
        "source_priority": {
            "short_strangle_high_iv": {
                "mike_butler": 2,
                "greg_harmon": 1,
                "market_scan": 0,
            }
        }
    }
    return control


def _source_strangle(candidate_id: str, *, underlying: str, source: str) -> Candidate:
    candidate = _candidate(
        candidate_id,
        underlying=underlying,
        structure="short_strangle",
        bpr=1_800,
        delta=-0.05,
        gamma=-0.01,
        theta=0.08,
        vega=-0.25,
        net_credit=2.0,
        score=50.0,
        iv_pct=65.0,
        iv_rank=65.0,
    )
    candidate.playbook_id = "short_strangle_high_iv"
    candidate.metadata.update(
        {
            "ranking_source": source,
            "input_kind": "market_scan" if source == "market_scan" else "exact_package",
            "candidate_score_components": {"structure_thesis_fit": 18.0},
        }
    )
    return candidate


def test_short_strangle_source_priority_orders_same_underlying_mike_greg_direct_iv() -> None:
    candidates = [
        _source_strangle("direct", underlying="SPY", source="market_scan"),
        _source_strangle("greg", underlying="SPY", source="greg_harmon"),
        _source_strangle("mike", underlying="SPY", source="mike_butler"),
    ]

    plans = generate_plans(candidates, _portfolio(), _source_priority_control(), max_new_positions=1)

    assert [plan.candidates[0].candidate_id for plan in plans[:3]] == ["mike", "greg", "direct"]


def test_short_strangle_source_priority_orders_different_underlyings_mike_greg_direct_iv() -> None:
    candidates = [
        _source_strangle("direct", underlying="IWM", source="market_scan"),
        _source_strangle("greg", underlying="QQQ", source="greg_harmon"),
        _source_strangle("mike", underlying="SPY", source="mike_butler"),
    ]

    plans = generate_plans(candidates, _portfolio(), _source_priority_control(), max_new_positions=1)

    assert [plan.candidates[0].candidate_id for plan in plans[:3]] == ["mike", "greg", "direct"]
    components = next(reason for reason in plans[0].reasons if reason.startswith("score_components="))
    assert "source_priority:2.00" in components


def test_short_strangle_source_comparison_receipt_explains_each_alternative(tmp_path) -> None:
    blocked = _source_strangle("blocked", underlying="IWM", source="market_scan")
    blocked.rejection_reason = "event_status_blocked"
    candidates = [
        _source_strangle("direct", underlying="SPY", source="market_scan"),
        _source_strangle("greg", underlying="SPY", source="greg_harmon"),
        _source_strangle("mike", underlying="SPY", source="mike_butler"),
        blocked,
    ]
    control = _source_priority_control()
    plans = generate_plans(candidates, _portfolio(), control, max_new_positions=1)
    result = PlanRunResult("source-comparison", [], candidates, plans, [], {}, [], [])
    store = LocalStore(tmp_path / "state.db")

    _record_short_strangle_source_comparison(result, store=store, mode=ExecutionMode.LIVE, control=control)

    receipt = store.latest_event("short_strangle_source_comparison")
    assert receipt["comparison_basis"] == "eligible_singleton_plan_score"
    assert receipt["winner"]["source"] == "mike_butler"
    assert receipt["winner"]["singleton_plan_score_components"]["source_priority"] == 2
    by_id = {item["alternative_candidate_id"]: item for item in receipt["comparisons"]}
    assert by_id["greg"]["alternative_source"] == "greg_harmon"
    assert by_id["direct"]["alternative_source"] == "direct_iv"
    assert by_id["greg"]["score_delta_to_winner"] == 1
    assert by_id["direct"]["score_delta_to_winner"] == 2
    assert by_id["greg"]["component_deltas_to_winner"]["source_priority"] == 1
    assert by_id["direct"]["component_deltas_to_winner"]["source_priority"] == 2
    assert "positive_components=source_priority" in by_id["greg"]["why_winner_beat_alternative"]
    assert "positive_components=source_priority" in by_id["direct"]["why_winner_beat_alternative"]
    assert by_id["blocked"]["alternative_rejection_reason"] == "event_status_blocked"
    assert by_id["blocked"]["why_winner_beat_alternative"] == "alternative_rejected:event_status_blocked"
    assert receipt["broker_effects"] is False


def test_plan_objective_prefers_theta_and_vol_capture_over_bpr_spend() -> None:
    long_call = _candidate(
        "long_call",
        underlying="AMZN",
        structure="long_call",
        bpr=2_000,
        delta=0.50,
        gamma=0.01,
        theta=-0.10,
        vega=0.50,
        net_credit=-20.0,
        score=90.0,
        iv_pct=20.0,
        iv_rank=20.0,
    )
    put_spread = _candidate(
        "put_spread",
        underlying="MSFT",
        structure="put_spread",
        bpr=500,
        delta=-0.12,
        gamma=-0.005,
        theta=0.05,
        vega=-0.10,
        net_credit=1.0,
        score=35.0,
        iv_pct=70.0,
        iv_rank=70.0,
    )

    plans = generate_plans([long_call, put_spread], _portfolio(), _control(), max_new_positions=1)

    assert plans[0].candidates[0].candidate_id == "put_spread"


def test_live_delta_guard_blocks_positive_delta_when_book_is_already_positive() -> None:
    portfolio = PortfolioState(
        account_size=10_000,
        buying_power=10_000,
        bpr_used=0,
        positions_count=0,
        greeks=Greeks(delta=0.08),
    )
    positive_put_spread = _candidate(
        "positive_put_spread",
        underlying="MSFT",
        structure="put_spread",
        bpr=500,
        delta=0.03,
        gamma=-0.005,
        theta=0.05,
        vega=-0.10,
        net_credit=1.0,
        score=70.0,
        iv_pct=70.0,
        iv_rank=70.0,
    )

    plans = generate_plans([positive_put_spread], portfolio, _delta_guard_control(), max_new_positions=1)

    assert plans == []


def test_live_delta_guard_mode_can_start_relaxed() -> None:
    control = _control()
    control["portfolio"]["delta_guard"] = {
        "enabled": True,
        "mode": "relaxed",
        "apply_modes": ["live"],
        "allow_improvement_when_outside": True,
        "modes": {
            "relaxed": {"min_delta": -2.5, "max_delta": 0.5},
            "balanced": {"min_delta": -2.0, "max_delta": 0.0},
        },
    }
    portfolio = PortfolioState(
        account_size=10_000,
        buying_power=10_000,
        bpr_used=0,
        positions_count=0,
        greeks=Greeks(delta=0.08),
    )
    positive_put_spread = _candidate(
        "positive_put_spread",
        underlying="MSFT",
        structure="put_spread",
        bpr=500,
        delta=0.03,
        gamma=-0.005,
        theta=0.05,
        vega=-0.10,
        net_credit=1.0,
        score=70.0,
        iv_pct=70.0,
        iv_rank=70.0,
    )

    plans = generate_plans([positive_put_spread], portfolio, control, max_new_positions=1)

    assert plans
    assert plans[0].portfolio_after.greeks.delta == 0.11


def test_live_delta_guard_allows_delta_improvement_when_book_is_outside_band() -> None:
    portfolio = PortfolioState(
        account_size=10_000,
        buying_power=10_000,
        bpr_used=0,
        positions_count=0,
        greeks=Greeks(delta=0.08),
    )
    positive_put_spread = _candidate(
        "positive_put_spread",
        underlying="MSFT",
        structure="put_spread",
        bpr=500,
        delta=0.03,
        gamma=-0.005,
        theta=0.05,
        vega=-0.10,
        net_credit=1.0,
        score=90.0,
        iv_pct=70.0,
        iv_rank=70.0,
    )
    bearish_call_spread = _candidate(
        "bearish_call_spread",
        underlying="GOOGL",
        structure="call_spread",
        bpr=500,
        delta=-0.05,
        gamma=-0.005,
        theta=0.04,
        vega=-0.08,
        net_credit=1.0,
        score=45.0,
        iv_pct=70.0,
        iv_rank=70.0,
    )

    plans = generate_plans(
        [positive_put_spread, bearish_call_spread],
        portfolio,
        _delta_guard_control(),
        max_new_positions=1,
    )

    assert plans
    assert plans[0].candidates[0].candidate_id == "bearish_call_spread"


def test_plan_objective_uses_theta_efficiency_not_raw_bpr_fit() -> None:
    small_bpr = _candidate(
        "small_bpr",
        underlying="IWM",
        structure="put_spread",
        bpr=500,
        delta=-0.15,
        gamma=-0.005,
        theta=0.04,
        vega=-0.08,
        net_credit=0.8,
        iv_pct=65.0,
        iv_rank=65.0,
    )
    large_bpr = _candidate(
        "large_bpr",
        underlying="QQQ",
        structure="put_spread",
        bpr=2_000,
        delta=-0.15,
        gamma=-0.005,
        theta=0.04,
        vega=-0.08,
        net_credit=0.8,
        iv_pct=65.0,
        iv_rank=65.0,
    )

    plans = generate_plans([large_bpr, small_bpr], _portfolio(), _control(), max_new_positions=1)

    assert plans[0].candidates[0].candidate_id == "small_bpr"


def test_plan_reasons_include_optimizer_components() -> None:
    candidate = _candidate(
        "put_spread",
        underlying="SPY",
        structure="put_spread",
        bpr=500,
        delta=-0.10,
        gamma=-0.005,
        theta=0.05,
        vega=-0.10,
        net_credit=1.0,
        iv_pct=75.0,
        iv_rank=75.0,
    )

    plan = generate_plans([candidate], _portfolio(), _control(), max_new_positions=1)[0]

    reasons = " ".join(plan.reasons)
    assert "score_components=" in reasons
    assert "delta_fit:" in reasons
    assert "theta_capture:" in reasons
    assert "volatility_capture:" in reasons


def test_shadow_basket_is_ranked_without_bpr_target_scoring() -> None:
    candidates = [
        _candidate(
            f"put_spread_{index}",
            underlying=underlying,
            structure="put_spread",
            bpr=500,
            delta=-0.08,
            gamma=-0.004,
            theta=0.05,
            vega=-0.10,
            net_credit=1.0,
            iv_pct=70.0,
            iv_rank=70.0,
        )
        for index, underlying in enumerate(["MSFT", "AAPL", "NVDA"], start=1)
    ]

    plans = generate_plans(
        candidates,
        _portfolio(),
        _basket_control(
            target_new_bpr_pct=15,
            hard_new_bpr_pct=20,
            min_marginal_score=0.01,
            max_new_positions_per_plan=3,
        ),
    )

    assert len(plans[0].candidates) == 3
    reasons = " ".join(plans[0].reasons)
    assert "new_bpr_target_fit:" not in reasons
    assert "bpr_capacity_mode=observe_only" in reasons
    assert "basket_controls=" in reasons
    assert "target_new_bpr_pct:none" in reasons
    assert "max_new_positions_per_plan:3" in reasons
    assert "marginal_score=" in reasons


def test_shadow_ignores_synthetic_hard_new_bpr_cap() -> None:
    candidates = [
        _candidate(
            f"candidate_{index}",
            underlying=underlying,
            structure="put_spread",
            bpr=600,
            delta=-0.06,
            gamma=-0.004,
            theta=0.05,
            vega=-0.10,
            net_credit=1.0,
            iv_pct=70.0,
            iv_rank=70.0,
        )
        for index, underlying in enumerate(["MSFT", "AAPL", "NVDA"], start=1)
    ]

    plans = generate_plans(
        candidates,
        _portfolio(),
        _basket_control(
            target_new_bpr_pct=18,
            hard_new_bpr_pct=12,
            min_marginal_score=0.01,
            max_new_positions_per_plan=4,
        ),
    )

    assert len(plans[0].candidates) == 3
    assert plans[0].total_bpr == 1800


def test_live_still_enforces_hard_new_bpr_cap() -> None:
    candidates = [
        _candidate(
            f"candidate_{index}",
            underlying=underlying,
            structure="put_spread",
            bpr=600,
            delta=-0.06,
            gamma=-0.004,
            theta=0.05,
            vega=-0.10,
            net_credit=1.0,
            iv_pct=70.0,
            iv_rank=70.0,
        )
        for index, underlying in enumerate(["MSFT", "AAPL", "NVDA"], start=1)
    ]
    control = _control()
    control["planner"] = {
        "basket": {
            "target_new_bpr_pct": 18,
            "hard_new_bpr_pct": 12,
            "min_marginal_score": 0.01,
            "max_new_positions_per_plan": 4,
        }
    }

    plans = generate_plans(candidates, _portfolio(), control)

    assert len(plans[0].candidates) == 2
    assert plans[0].total_bpr == 1200
    assert "bpr_capacity_mode=enforced" in " ".join(plans[0].reasons)


def test_min_marginal_score_stops_weak_basket_additions() -> None:
    strong_first = _candidate(
        "strong_first",
        underlying="MSFT",
        structure="put_spread",
        bpr=500,
        delta=-0.08,
        gamma=-0.004,
        theta=0.06,
        vega=-0.10,
        net_credit=1.0,
        iv_pct=75.0,
        iv_rank=75.0,
    )
    strong_second = _candidate(
        "strong_second",
        underlying="AAPL",
        structure="put_spread",
        bpr=500,
        delta=-0.08,
        gamma=-0.004,
        theta=0.06,
        vega=-0.10,
        net_credit=1.0,
        iv_pct=75.0,
        iv_rank=75.0,
    )
    weak_drag = _candidate(
        "weak_drag",
        underlying="NVDA",
        structure="long_call",
        bpr=500,
        delta=2.0,
        gamma=0.06,
        theta=-0.60,
        vega=0.50,
        net_credit=-5.0,
        liquidity=0.20,
        score=0.0,
        iv_pct=95.0,
        iv_rank=95.0,
    )

    plans = generate_plans(
        [strong_first, strong_second, weak_drag],
        _portfolio(),
        _basket_control(
            target_new_bpr_pct=15,
            hard_new_bpr_pct=20,
            min_marginal_score=5,
            max_new_positions_per_plan=3,
        ),
    )

    candidate_ids = {candidate.candidate_id for candidate in plans[0].candidates}
    assert candidate_ids == {"strong_first", "strong_second"}


def test_basket_rank_objective_prefers_good_marginal_basket_over_singleton_variants() -> None:
    alpha_primary = _candidate(
        "alpha_primary",
        underlying="MSFT",
        structure="put_spread",
        bpr=1500,
        delta=-0.10,
        gamma=-0.004,
        theta=0.12,
        vega=-0.10,
        net_credit=1.0,
        score=90.0,
        iv_pct=75.0,
        iv_rank=75.0,
    )
    alpha_variant = _candidate(
        "alpha_variant",
        underlying="MSFT",
        structure="put_spread",
        bpr=1450,
        delta=-0.10,
        gamma=-0.004,
        theta=0.118,
        vega=-0.10,
        net_credit=1.0,
        score=89.0,
        iv_pct=75.0,
        iv_rank=75.0,
    )
    beta = _candidate(
        "beta",
        underlying="AAPL",
        structure="put_spread",
        bpr=350,
        delta=-0.05,
        gamma=-0.004,
        theta=0.04,
        vega=-0.10,
        net_credit=1.0,
        score=35.0,
        iv_pct=75.0,
        iv_rank=75.0,
    )
    gamma = _candidate(
        "gamma",
        underlying="NVDA",
        structure="put_spread",
        bpr=300,
        delta=-0.04,
        gamma=-0.004,
        theta=0.04,
        vega=-0.10,
        net_credit=1.0,
        score=35.0,
        iv_pct=75.0,
        iv_rank=75.0,
    )

    plans = generate_plans(
        [alpha_primary, alpha_variant, beta, gamma],
        _portfolio(),
        _basket_control(
            target_new_bpr_pct=15,
            hard_new_bpr_pct=30,
            min_marginal_score=2,
            max_new_positions_per_plan=3,
        ),
    )

    top_ids = {candidate.candidate_id for candidate in plans[0].candidates}
    assert len(top_ids) >= 2
    assert top_ids <= {"alpha_primary", "alpha_variant", "beta", "gamma"}
    assert {"beta", "gamma"} & top_ids
    assert not (
        len(plans) > 1
        and len(plans[0].candidates) == 1
        and len(plans[1].candidates) == 1
        and plans[0].candidates[0].underlying == plans[1].candidates[0].underlying
    )
    assert "rank_objective=" in " ".join(plans[0].reasons)


def test_basket_rank_objective_does_not_include_weak_marginal_addon() -> None:
    alpha_primary = _candidate(
        "alpha_primary",
        underlying="MSFT",
        structure="put_spread",
        bpr=1500,
        delta=-0.10,
        gamma=-0.004,
        theta=0.12,
        vega=-0.10,
        net_credit=1.0,
        score=90.0,
        iv_pct=75.0,
        iv_rank=75.0,
    )
    beta = _candidate(
        "beta",
        underlying="AAPL",
        structure="put_spread",
        bpr=350,
        delta=-0.05,
        gamma=-0.004,
        theta=0.04,
        vega=-0.10,
        net_credit=1.0,
        score=35.0,
        iv_pct=75.0,
        iv_rank=75.0,
    )
    weak_drag = _candidate(
        "weak_drag",
        underlying="NVDA",
        structure="long_call",
        bpr=300,
        delta=2.0,
        gamma=0.06,
        theta=-0.60,
        vega=0.50,
        net_credit=-5.0,
        liquidity=0.20,
        score=0.0,
        iv_pct=95.0,
        iv_rank=95.0,
    )

    plans = generate_plans(
        [alpha_primary, beta, weak_drag],
        _portfolio(),
        _basket_control(
            target_new_bpr_pct=15,
            hard_new_bpr_pct=30,
            min_marginal_score=5,
            max_new_positions_per_plan=3,
        ),
    )

    top_ids = {candidate.candidate_id for candidate in plans[0].candidates}
    assert top_ids == {"alpha_primary", "beta"}
    assert "weak_drag" not in top_ids
