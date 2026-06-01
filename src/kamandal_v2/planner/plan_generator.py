"""Generate and score portfolio plan bundles."""

from __future__ import annotations

import hashlib

from kamandal_v2.domain.models import Candidate, Greeks, Plan, PortfolioState
from kamandal_v2.liquidity import candidate_liquidity_metrics


def generate_plans(
    candidates: list[Candidate],
    portfolio: PortfolioState,
    control: dict,
    *,
    beam_width: int = 20,
    top_n: int = 5,
    max_new_positions: int | None = None,
) -> list[Plan]:
    eligible = [candidate for candidate in candidates if candidate.eligible]
    max_positions = _max_positions(control)
    remaining_positions = max(max_positions - portfolio.positions_count, 0)
    if max_new_positions is not None:
        remaining_positions = min(remaining_positions, max_new_positions)
    if remaining_positions <= 0:
        return []
    max_bpr_pct = float(((control.get("portfolio") or {}).get("hard_max_bpr_utilization_pct") or 90))
    max_underlying_pct = float(((control.get("portfolio") or {}).get("max_bpr_per_underlying_pct") or 25))
    partials: list[list[Candidate]] = [[]]
    completed: list[list[Candidate]] = []

    for _depth in range(1, remaining_positions + 1):
        expanded: list[list[Candidate]] = []
        for partial in partials:
            used_ids = {candidate.candidate_id for candidate in partial}
            used_underlyings = {candidate.underlying for candidate in partial}
            used_ideas = {candidate.idea_id for candidate in partial}
            for candidate in eligible:
                if candidate.candidate_id in used_ids or candidate.underlying in used_underlyings or candidate.idea_id in used_ideas:
                    continue
                next_plan = partial + [candidate]
                violation = _constraint_violation(next_plan, portfolio, max_bpr_pct, max_underlying_pct)
                if violation:
                    continue
                expanded.append(next_plan)
        if not expanded:
            break
        ranked = sorted(expanded, key=lambda plan: _score(plan, portfolio, control), reverse=True)
        completed.extend(ranked[:beam_width])
        partials = ranked[:beam_width]

    unique: dict[str, list[Candidate]] = {}
    for plan in completed:
        key = "|".join(sorted(candidate.candidate_id for candidate in plan))
        unique[key] = plan
    ranked_plans = sorted(unique.values(), key=lambda plan: _score(plan, portfolio, control), reverse=True)[:top_n]
    return [
        _materialize(plan, rank=index + 1, portfolio=portfolio, control=control)
        for index, plan in enumerate(ranked_plans)
    ]


def _max_positions(control: dict) -> int:
    portfolio_value = (control.get("portfolio") or {}).get("max_positions")
    max_positions = int(5 if portfolio_value in (None, "") else portfolio_value)
    mode = str((control.get("runtime") or {}).get("mode") or "shadow").lower()
    shadow_override = (control.get("shadow") or {}).get("max_positions_override")
    if mode == "shadow" and shadow_override not in (None, ""):
        return int(shadow_override)
    return max_positions


def _constraint_violation(plan: list[Candidate], portfolio: PortfolioState, max_bpr_pct: float, max_underlying_pct: float) -> str:
    total_bpr = sum(candidate.estimated_bpr for candidate in plan)
    if ((portfolio.bpr_used + total_bpr) / max(portfolio.account_size, 1.0)) * 100 > max_bpr_pct:
        return "portfolio_bpr_cap"
    per_underlying: dict[str, float] = dict(portfolio.per_underlying_bpr)
    for candidate in plan:
        per_underlying[candidate.underlying] = per_underlying.get(candidate.underlying, 0.0) + candidate.estimated_bpr
    for value in per_underlying.values():
        if (value / max(portfolio.account_size, 1.0)) * 100 > max_underlying_pct:
            return "underlying_bpr_cap"
    return ""


def _score(plan: list[Candidate], portfolio: PortfolioState, control: dict) -> float:
    return round(sum(_score_components(plan, portfolio, control).values()), 4)


def _score_components(plan: list[Candidate], portfolio: PortfolioState, control: dict) -> dict[str, float]:
    total_bpr = sum(candidate.estimated_bpr for candidate in plan)
    greeks = _plan_greeks(plan)
    after_delta = portfolio.greeks.delta + greeks.delta
    delta_fit = _delta_fit_score(after_delta, portfolio, control)
    theta_capture = _theta_capture_score(greeks.theta, total_bpr)
    volatility_capture = sum(_volatility_capture_score(candidate) for candidate in plan) / max(len(plan), 1)
    diversification = len({candidate.underlying for candidate in plan}) / max(len(plan), 1)
    liquidity = sum(candidate.liquidity_score for candidate in plan) / max(len(plan), 1)
    thesis_quality = sum(candidate.score for candidate in plan) / max(len(plan), 1)
    gamma_penalty = _gamma_stress_penalty(greeks.gamma)
    concentration_penalty = _concentration_penalty(plan, portfolio)
    slippage_penalty = _slippage_penalty(plan)
    return {
        "delta_fit": delta_fit,
        "theta_capture": theta_capture,
        "volatility_capture": volatility_capture,
        "liquidity": liquidity * 12.0,
        "diversification": diversification * 4.0,
        "thesis_quality": thesis_quality * 0.35,
        "gamma_stress_penalty": -gamma_penalty,
        "concentration_penalty": -concentration_penalty,
        "slippage_penalty": -slippage_penalty,
    }


def _delta_fit_score(after_delta: float, portfolio: PortfolioState, control: dict) -> float:
    portfolio_cfg = control.get("portfolio") or {}
    account_units = max(portfolio.account_size, 1.0) / 1000.0
    raw_target = portfolio_cfg.get("target_delta")
    if raw_target not in (None, ""):
        target_delta = float(raw_target)
    else:
        bias = str(portfolio_cfg.get("delta_bias") or "slightly_negative").lower()
        if bias in {"slightly_negative", "negative"}:
            target_delta = -0.5 * account_units
        elif bias in {"slightly_positive", "positive"}:
            target_delta = 0.5 * account_units
        else:
            target_delta = 0.0
    raw_band = portfolio_cfg.get("delta_band")
    band = float(raw_band) if raw_band not in (None, "") else max(5.0, 2.5 * account_units)
    return max(0.0, 25.0 * (1.0 - abs(after_delta - target_delta) / max(band, 1.0)))


def _theta_capture_score(theta: float, total_bpr: float) -> float:
    theta_dollars_per_day = theta * 100.0
    theta_per_1k_bpr = theta_dollars_per_day / max(total_bpr / 1000.0, 0.001)
    if theta_per_1k_bpr >= 0:
        return min(theta_per_1k_bpr * 4.0, 35.0)
    return max(theta_per_1k_bpr * 8.0, -45.0)


def _volatility_capture_score(candidate: Candidate) -> float:
    iv_context = _candidate_iv_context(candidate)
    if iv_context is None:
        iv_context = 50.0
    vega = candidate.greeks.vega
    credit_yield = max(candidate.net_credit, 0.0) * 100.0 / max(candidate.estimated_bpr, 1.0)
    short_vol_structures = {"short_put", "put_spread", "call_spread", "iron_condor", "short_strangle", "jade_lizard"}
    long_vol_structures = {"call_calendar", "put_calendar", "put_diagonal", "call_diagonal", "long_call", "long_put"}
    if candidate.structure in short_vol_structures or vega < 0:
        return min((iv_context / 100.0) * 24.0 + min(credit_yield * 8.0, 8.0), 32.0)
    if candidate.structure in long_vol_structures or vega > 0:
        return min(((100.0 - iv_context) / 100.0) * 12.0, 12.0)
    return 4.0


def _candidate_iv_context(candidate: Candidate) -> float | None:
    values = [
        _reason_float(candidate, "iv_pct="),
        _reason_float(candidate, "iv_rank="),
    ]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return max(0.0, min(100.0, max(values)))


def _reason_float(candidate: Candidate, prefix: str) -> float | None:
    for reason in candidate.reasons:
        if reason.startswith(prefix):
            try:
                return float(reason[len(prefix):])
            except ValueError:
                return None
    return None


def _gamma_stress_penalty(gamma: float) -> float:
    return min(abs(gamma) * 500.0, 30.0)


def _concentration_penalty(plan: list[Candidate], portfolio: PortfolioState) -> float:
    total_after = portfolio.bpr_used + sum(candidate.estimated_bpr for candidate in plan)
    if total_after <= 0:
        return 0.0
    by_underlying: dict[str, float] = dict(portfolio.per_underlying_bpr)
    for candidate in plan:
        by_underlying[candidate.underlying] = by_underlying.get(candidate.underlying, 0.0) + candidate.estimated_bpr
    max_share = max(by_underlying.values(), default=0.0) / total_after
    return max(0.0, (max_share - 0.35) * 30.0)


def _slippage_penalty(plan: list[Candidate]) -> float:
    penalties: list[float] = []
    for candidate in plan:
        metrics = candidate_liquidity_metrics(candidate)
        max_leg = float(metrics["max_bid_ask_pct"])
        aggregate = float(metrics["aggregate_spread_to_mid_pct"])
        pressure = max(max_leg, aggregate)
        # Slippage is convex: slightly wide markets are a nuisance, but very
        # wide markets should quickly lose to cleaner alternatives.
        penalties.append((pressure ** 1.35) * 18.0)
    avg_penalty = sum(penalties) / max(len(penalties), 1)
    return min(avg_penalty, 35.0)


def _materialize(plan: list[Candidate], *, rank: int, portfolio: PortfolioState, control: dict) -> Plan:
    total_bpr = round(sum(candidate.estimated_bpr for candidate in plan), 2)
    greeks = _plan_greeks(plan)
    after = PortfolioState(
        account_size=portfolio.account_size,
        buying_power=round(portfolio.buying_power - total_bpr, 2),
        bpr_used=round(portfolio.bpr_used + total_bpr, 2),
        positions_count=portfolio.positions_count + len(plan),
        greeks=portfolio.greeks + greeks,
        per_underlying_bpr=_after_underlying_bpr(portfolio, plan),
    )
    score = _score(plan, portfolio, control)
    plan_id = "plan_" + hashlib.sha256("|".join(candidate.candidate_id for candidate in plan).encode("utf-8")).hexdigest()[:12]
    approval_mode = str((control.get("execution") or {}).get("approval_mode") or "")
    operator_action = "approve" if approval_mode == "shadow_auto_top_plan" and rank == 1 else ""
    return Plan(
        plan_id=plan_id,
        plan_rank=rank,
        status="eligible",
        candidates=plan,
        score=score,
        total_bpr=total_bpr,
        bpr_utilization_pct=round(after.bpr_used_pct, 2),
        buying_power_after=after.buying_power,
        portfolio_before=portfolio,
        portfolio_after=after,
        reasons=_reasons(plan, greeks, total_bpr, _score_components(plan, portfolio, control)),
        blocked_by=[],
        operator_action=operator_action,
    )


def _plan_greeks(plan: list[Candidate]) -> Greeks:
    total = Greeks()
    for candidate in plan:
        total = total + candidate.greeks
    return total


def _after_underlying_bpr(portfolio: PortfolioState, plan: list[Candidate]) -> dict[str, float]:
    result = dict(portfolio.per_underlying_bpr)
    for candidate in plan:
        result[candidate.underlying] = round(result.get(candidate.underlying, 0.0) + candidate.estimated_bpr, 2)
    return result


def _reasons(plan: list[Candidate], greeks: Greeks, total_bpr: float, score_components: dict[str, float]) -> list[str]:
    return [
        f"{len(plan)} trades",
        f"bpr={total_bpr:.2f}",
        f"delta_change={greeks.delta:.2f}",
        f"theta_change={greeks.theta:.2f}",
        f"vega_change={greeks.vega:.2f}",
        "score_components=" + ",".join(f"{key}:{value:.2f}" for key, value in score_components.items()),
        f"underlyings={','.join(candidate.underlying for candidate in plan)}",
    ]
