"""Generate and score portfolio plan bundles."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from kamandal_v2.domain.models import Candidate, Greeks, Plan, PortfolioState
from kamandal_v2.liquidity import candidate_liquidity_metrics


@dataclass(frozen=True, slots=True)
class _BasketPolicy:
    target_new_bpr_pct: float | None = None
    hard_new_bpr_pct: float | None = None
    min_marginal_score: float | None = None
    max_new_positions_per_plan: int | None = None


@dataclass(frozen=True, slots=True)
class _RankContext:
    best_singleton_by_underlying: dict[str, float]
    best_singleton_by_idea: dict[str, float]


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
    basket_policy = _basket_policy(control, max_new_positions=max_new_positions)
    max_positions = _max_positions(control)
    remaining_positions = max(max_positions - portfolio.positions_count, 0)
    if basket_policy.max_new_positions_per_plan is not None:
        remaining_positions = min(remaining_positions, basket_policy.max_new_positions_per_plan)
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
                violation = _constraint_violation(
                    next_plan,
                    portfolio,
                    max_bpr_pct,
                    max_underlying_pct,
                    basket_policy.hard_new_bpr_pct,
                    control,
                )
                if violation:
                    continue
                marginal_score = _score(next_plan, portfolio, control) - _score(partial, portfolio, control)
                if basket_policy.min_marginal_score is not None and marginal_score < basket_policy.min_marginal_score:
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
        unique.setdefault(key, plan)
    rank_context = _rank_context(list(unique.values()), portfolio, control)
    ranked_plans = sorted(
        unique.values(),
        key=lambda plan: _rank_score(plan, portfolio, control, rank_context),
        reverse=True,
    )[:top_n]
    return [
        _materialize(plan, rank=index + 1, portfolio=portfolio, control=control, rank_context=rank_context)
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


def _constraint_violation(
    plan: list[Candidate],
    portfolio: PortfolioState,
    max_bpr_pct: float,
    max_underlying_pct: float,
    hard_new_bpr_pct: float | None,
    control: dict,
) -> str:
    total_bpr = sum(candidate.estimated_bpr for candidate in plan)
    if hard_new_bpr_pct is not None and (total_bpr / max(portfolio.account_size, 1.0)) * 100 > hard_new_bpr_pct:
        return "new_bpr_cap"
    if ((portfolio.bpr_used + total_bpr) / max(portfolio.account_size, 1.0)) * 100 > max_bpr_pct:
        return "portfolio_bpr_cap"
    per_underlying: dict[str, float] = dict(portfolio.per_underlying_bpr)
    for candidate in plan:
        per_underlying[candidate.underlying] = per_underlying.get(candidate.underlying, 0.0) + candidate.estimated_bpr
    for value in per_underlying.values():
        if (value / max(portfolio.account_size, 1.0)) * 100 > max_underlying_pct:
            return "underlying_bpr_cap"
    if violation := _delta_guard_violation(plan, portfolio, control):
        return violation
    return ""


def _score(plan: list[Candidate], portfolio: PortfolioState, control: dict) -> float:
    return round(sum(_score_components(plan, portfolio, control).values()), 4)


def _rank_context(plans: list[list[Candidate]], portfolio: PortfolioState, control: dict) -> _RankContext:
    best_singleton_by_underlying: dict[str, float] = {}
    best_singleton_by_idea: dict[str, float] = {}
    for plan in plans:
        if len(plan) != 1:
            continue
        candidate = plan[0]
        score = _score(plan, portfolio, control)
        best_singleton_by_underlying[candidate.underlying] = max(
            best_singleton_by_underlying.get(candidate.underlying, float("-inf")),
            score,
        )
        best_singleton_by_idea[candidate.idea_id] = max(
            best_singleton_by_idea.get(candidate.idea_id, float("-inf")),
            score,
        )
    return _RankContext(best_singleton_by_underlying, best_singleton_by_idea)


def _rank_score(plan: list[Candidate], portfolio: PortfolioState, control: dict, rank_context: _RankContext) -> float:
    return round(_score(plan, portfolio, control) + _rank_adjustment(plan, portfolio, control, rank_context), 4)


def _rank_adjustment(plan: list[Candidate], portfolio: PortfolioState, control: dict, rank_context: _RankContext) -> float:
    adjustment = _basket_rank_bonus(plan, portfolio, control)
    if len(plan) == 1 and _is_singleton_variant(plan[0], _score(plan, portfolio, control), rank_context):
        adjustment -= 4.0
    return round(adjustment, 4)


def _basket_rank_bonus(plan: list[Candidate], portfolio: PortfolioState, control: dict) -> float:
    if len(plan) < 2:
        return 0.0
    policy = _basket_policy(control)
    marginal_scores = _marginal_scores(plan, portfolio, control)
    added_scores = marginal_scores[1:]
    if not added_scores:
        return 0.0
    if policy.min_marginal_score is not None and min(added_scores) < policy.min_marginal_score:
        return 0.0
    marginal_floor = max(policy.min_marginal_score or 2.0, 2.0)
    marginal_quality = max(0.0, min(1.0, min(added_scores) / max(marginal_floor * 2.0, 1.0)))
    return round(min(8.0, 5.0 * (len(plan) - 1)) * marginal_quality, 4)


def _is_singleton_variant(candidate: Candidate, score: float, rank_context: _RankContext) -> bool:
    best_underlying = rank_context.best_singleton_by_underlying.get(candidate.underlying, score)
    best_idea = rank_context.best_singleton_by_idea.get(candidate.idea_id, score)
    return score < best_underlying or score < best_idea


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
    components = {
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
    target_score = _new_bpr_target_score(total_bpr, portfolio, control)
    if target_score is not None:
        components["new_bpr_target_fit"] = target_score
    return components


def _new_bpr_target_score(total_bpr: float, portfolio: PortfolioState, control: dict) -> float | None:
    target_new_bpr_pct = _basket_policy(control).target_new_bpr_pct
    if target_new_bpr_pct is None or target_new_bpr_pct <= 0:
        return None
    new_bpr_pct = (total_bpr / max(portfolio.account_size, 1.0)) * 100.0
    if new_bpr_pct <= target_new_bpr_pct:
        return min(10.0, 10.0 * new_bpr_pct / target_new_bpr_pct)
    excess_pct = new_bpr_pct - target_new_bpr_pct
    return max(0.0, 10.0 - 10.0 * excess_pct / max(target_new_bpr_pct, 1.0))


def _basket_policy(control: dict, *, max_new_positions: int | None = None) -> _BasketPolicy:
    mode = str((control.get("runtime") or {}).get("mode") or "shadow").lower()
    planner_cfg = control.get("planner") or {}
    basket_cfg = dict(planner_cfg.get("basket") or {})
    mode_cfg = control.get(mode) or {}
    basket_cfg.update(mode_cfg.get("basket") or {})

    configured_limits = [
        _optional_int(basket_cfg.get("max_new_positions_per_plan")),
        max_new_positions,
    ]
    if mode == "live":
        configured_limits.append(_optional_int((control.get("live") or {}).get("max_new_positions_per_plan")))
    elif mode == "shadow":
        configured_limits.append(_optional_int((control.get("shadow") or {}).get("max_new_positions_per_plan")))
    limits = [limit for limit in configured_limits if limit is not None]

    return _BasketPolicy(
        target_new_bpr_pct=_optional_float(basket_cfg.get("target_new_bpr_pct", planner_cfg.get("target_new_bpr_pct"))),
        hard_new_bpr_pct=_optional_float(basket_cfg.get("hard_new_bpr_pct", planner_cfg.get("hard_new_bpr_pct"))),
        min_marginal_score=_optional_float(basket_cfg.get("min_marginal_score", planner_cfg.get("min_marginal_score"))),
        max_new_positions_per_plan=min(limits) if limits else None,
    )


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    parsed = _optional_float(value)
    return None if parsed is None else int(parsed)


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


def _delta_guard_violation(plan: list[Candidate], portfolio: PortfolioState, control: dict) -> str:
    guard = _delta_guard_settings(control)
    if not isinstance(guard, dict) or not _as_bool(guard.get("enabled"), default=False):
        return ""
    mode = str((control.get("runtime") or {}).get("mode") or "shadow").lower()
    if mode not in _guard_modes(guard.get("apply_modes", "live")):
        return ""

    before_delta = float(portfolio.greeks.delta or 0.0)
    after_delta = before_delta + _plan_greeks(plan).delta
    min_delta = _optional_float(guard.get("min_delta", guard.get("min")))
    max_delta = _optional_float(guard.get("max_delta", guard.get("max")))
    allow_improvement = _as_bool(guard.get("allow_improvement_when_outside"), default=True)
    epsilon = 1e-9

    if max_delta is not None and after_delta > max_delta + epsilon:
        if allow_improvement and before_delta > max_delta + epsilon and after_delta <= before_delta + epsilon:
            return ""
        return "portfolio_delta_above_max"
    if min_delta is not None and after_delta < min_delta - epsilon:
        if allow_improvement and before_delta < min_delta - epsilon and after_delta >= before_delta - epsilon:
            return ""
        return "portfolio_delta_below_min"
    return ""


def _delta_guard_settings(control: dict) -> dict[str, object]:
    guard = dict(((control.get("portfolio") or {}).get("delta_guard") or {}))
    mode = str(guard.get("mode") or "").strip().lower()
    modes = guard.get("modes") or {}
    if mode and isinstance(modes, dict):
        mode_values = modes.get(mode) or {}
        if isinstance(mode_values, dict):
            merged = dict(mode_values)
            for key, value in guard.items():
                if key == "modes" or value in (None, ""):
                    continue
                merged[key] = value
            guard = merged
    return guard


def _guard_modes(raw: object) -> set[str]:
    if raw in (None, ""):
        return {"live"}
    if isinstance(raw, str):
        values = raw.replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _as_bool(value: object, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


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


def _materialize(plan: list[Candidate], *, rank: int, portfolio: PortfolioState, control: dict, rank_context: _RankContext) -> Plan:
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
        reasons=_reasons(
            plan,
            greeks,
            total_bpr,
            _score_components(plan, portfolio, control),
            _basket_policy(control),
            _marginal_scores(plan, portfolio, control),
            _rank_adjustment(plan, portfolio, control, rank_context),
            _rank_score(plan, portfolio, control, rank_context),
        ),
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


def _marginal_scores(plan: list[Candidate], portfolio: PortfolioState, control: dict) -> list[float]:
    scores: list[float] = []
    partial: list[Candidate] = []
    prior_score = _score(partial, portfolio, control)
    for candidate in plan:
        next_plan = partial + [candidate]
        next_score = _score(next_plan, portfolio, control)
        scores.append(round(next_score - prior_score, 4))
        partial = next_plan
        prior_score = next_score
    return scores


def _reasons(
    plan: list[Candidate],
    greeks: Greeks,
    total_bpr: float,
    score_components: dict[str, float],
    basket_policy: _BasketPolicy,
    marginal_scores: list[float],
    rank_adjustment: float,
    rank_score: float,
) -> list[str]:
    last_marginal_score = marginal_scores[-1] if marginal_scores else 0.0
    return [
        f"{len(plan)} trades",
        f"bpr={total_bpr:.2f}",
        f"delta_change={greeks.delta:.2f}",
        f"theta_change={greeks.theta:.2f}",
        f"vega_change={greeks.vega:.2f}",
        "score_components=" + ",".join(f"{key}:{value:.2f}" for key, value in score_components.items()),
        "basket_controls="
        + ",".join(
            [
                f"target_new_bpr_pct:{_reason_value(basket_policy.target_new_bpr_pct)}",
                f"hard_new_bpr_pct:{_reason_value(basket_policy.hard_new_bpr_pct)}",
                f"min_marginal_score:{_reason_value(basket_policy.min_marginal_score)}",
                f"max_new_positions_per_plan:{basket_policy.max_new_positions_per_plan or 'none'}",
            ]
        ),
        f"marginal_score={last_marginal_score:.4f}",
        "marginal_scores=" + ",".join(f"{value:.4f}" for value in marginal_scores),
        f"rank_objective=adjustment:{rank_adjustment:.4f},rank_score:{rank_score:.4f}",
        f"underlyings={','.join(candidate.underlying for candidate in plan)}",
    ]


def _reason_value(value: float | None) -> str:
    return "none" if value is None else f"{value:.2f}"
