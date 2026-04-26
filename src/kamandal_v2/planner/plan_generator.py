"""Generate and score portfolio plan bundles."""

from __future__ import annotations

import hashlib

from kamandal_v2.domain.models import Candidate, Greeks, Plan, PortfolioState


def generate_plans(
    candidates: list[Candidate],
    portfolio: PortfolioState,
    control: dict,
    *,
    beam_width: int = 20,
    top_n: int = 5,
) -> list[Plan]:
    eligible = [candidate for candidate in candidates if candidate.eligible]
    max_positions = int(((control.get("portfolio") or {}).get("max_positions") or 5))
    max_bpr_pct = float(((control.get("portfolio") or {}).get("hard_max_bpr_utilization_pct") or 90))
    max_underlying_pct = float(((control.get("portfolio") or {}).get("max_bpr_per_underlying_pct") or 25))
    partials: list[list[Candidate]] = [[]]
    completed: list[list[Candidate]] = []

    for _depth in range(1, max_positions + 1):
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
    total_bpr = sum(candidate.estimated_bpr for candidate in plan)
    greeks = _plan_greeks(plan)
    after_delta = portfolio.greeks.delta + greeks.delta
    bpr_pct = (portfolio.bpr_used + total_bpr) / max(portfolio.account_size, 1.0) * 100
    target_bpr = float(((control.get("portfolio") or {}).get("target_max_bpr_utilization_pct") or 90))
    bpr_fit = max(0.0, 1.0 - abs(target_bpr - bpr_pct) / max(target_bpr, 1.0))
    delta_fit = max(0.0, 1.0 - abs(after_delta + 5.0) / 50.0)
    theta_gain = max(greeks.theta, 0.0)
    gamma_penalty = abs(greeks.gamma)
    diversification = len({candidate.underlying for candidate in plan}) / max(len(plan), 1)
    liquidity = sum(candidate.liquidity_score for candidate in plan) / max(len(plan), 1)
    candidate_quality = sum(candidate.score for candidate in plan) / max(len(plan), 1)
    return round(candidate_quality + bpr_fit * 30 + delta_fit * 20 + theta_gain * 5 + diversification * 10 + liquidity * 10 - gamma_penalty * 10, 4)


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
        reasons=_reasons(plan, greeks, total_bpr),
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


def _reasons(plan: list[Candidate], greeks: Greeks, total_bpr: float) -> list[str]:
    return [
        f"{len(plan)} trades",
        f"bpr={total_bpr:.2f}",
        f"delta_change={greeks.delta:.2f}",
        f"theta_change={greeks.theta:.2f}",
        f"underlyings={','.join(candidate.underlying for candidate in plan)}",
    ]

