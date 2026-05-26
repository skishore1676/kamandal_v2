from __future__ import annotations

from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg, PortfolioState
from kamandal_v2.planner.plan_generator import generate_plans


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
