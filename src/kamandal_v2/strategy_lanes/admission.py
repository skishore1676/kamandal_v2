"""Shared five-stage CSA admission with complete rejection evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kamandal_v2.strategy_lanes.compatibility import source_structure_compatible
from kamandal_v2.strategy_lanes.models import AdmissionDecision, AdmissionStageResult, CsaStage, LaneId, StrategyOpportunity, stable_csa_id
from kamandal_v2.strategy_lanes.policy import CsaPolicy
from kamandal_v2.strategy_lanes.scoring import ScoreResult


@dataclass(frozen=True, slots=True)
class AdmissionContext:
    market_data_fresh: bool
    quote_valid: bool
    structure_valid: bool
    liquidity_valid: bool
    bpr: float | None
    bpr_source: str
    broker_state_clear: bool
    portfolio_allowed: bool
    buying_power_available: bool
    ownership_clear: bool = True
    working_order_conflict: bool = False
    event_state: str = "not_applicable"
    evidence: dict[str, Any] = field(default_factory=dict)


def evaluate_admission(
    opportunity: StrategyOpportunity,
    policy: CsaPolicy,
    context: AdmissionContext,
    *,
    decided_at: str,
    score: ScoreResult | None = None,
) -> AdmissionDecision:
    if opportunity.policy_hash != policy.policy_hash:
        raise ValueError("opportunity policy hash does not match admission policy")
    stages = (
        _source_stage(opportunity, policy),
        _market_stage(opportunity, policy, context),
        _structure_stage(policy, context),
        _broker_stage(policy, context),
        _portfolio_stage(context),
    )
    all_reasons = [reason for stage in stages for reason in stage.reasons if not stage.passed]
    primary_blocker = all_reasons[0] if all_reasons else ""
    admitted = all(stage.passed for stage in stages)
    decision_id = stable_csa_id(
        "admission",
        [opportunity.opportunity_id, policy.policy_hash, [stage.to_dict() for stage in stages]],
    )
    return AdmissionDecision(
        decision_id=decision_id,
        opportunity_id=opportunity.opportunity_id,
        admitted=admitted,
        primary_blocker=primary_blocker,
        stages=stages,
        policy_hash=policy.policy_hash,
        decided_at=decided_at,
        score=score.score if score else None,
        score_components=score.components if score else {},
    )


def _source_stage(opportunity: StrategyOpportunity, policy: CsaPolicy) -> AdmissionStageResult:
    compatible, reasons = source_structure_compatible(policy, opportunity.evidence)
    return AdmissionStageResult(
        stage="source",
        passed=compatible,
        reasons=reasons or ("source_ok",),
        evidence={"source_id": opportunity.source_id, "source_mode": opportunity.source_mode.value},
    )


def _market_stage(opportunity: StrategyOpportunity, policy: CsaPolicy, context: AdmissionContext) -> AdmissionStageResult:
    reasons: list[str] = []
    if not context.market_data_fresh:
        reasons.append("market_data_stale")
    if not context.quote_valid:
        reasons.append("market_quote_invalid")
    if policy.lane is LaneId.SHORT_STRANGLE and _as_bool(policy.resolved_fields.get("universe_expansion_enabled")):
        price = opportunity.market_context.get("underlying_price")
        iv_rank = opportunity.market_context.get("iv_rank")
        if not _within(price, policy.resolved_fields.get("underlying_price_min"), policy.resolved_fields.get("underlying_price_max")):
            reasons.append("market_underlying_price_outside_sheet_range")
        if not _within(iv_rank, policy.resolved_fields.get("iv_rank_min"), policy.resolved_fields.get("iv_rank_max")):
            reasons.append("market_iv_rank_outside_sheet_range")
    if policy.lane is LaneId.EARNINGS_CALENDAR and context.event_state not in {"known", "confirmed"}:
        reasons.append(f"event_state_not_admissible:{context.event_state}")
    return AdmissionStageResult(
        stage="market",
        passed=not reasons,
        reasons=tuple(reasons or ["market_ok"]),
        evidence={"event_state": context.event_state, **opportunity.market_context},
    )


def _structure_stage(policy: CsaPolicy, context: AdmissionContext) -> AdmissionStageResult:
    reasons: list[str] = []
    if not context.structure_valid:
        reasons.append("structure_invalid")
    if not context.liquidity_valid:
        reasons.append("structure_liquidity_invalid")
    return AdmissionStageResult(
        stage="structure",
        passed=not reasons,
        reasons=tuple(reasons or ["structure_ok"]),
        evidence={"lane": policy.lane.value},
    )


def _broker_stage(policy: CsaPolicy, context: AdmissionContext) -> AdmissionStageResult:
    reasons: list[str] = []
    evidence: dict[str, Any] = {"bpr": context.bpr, "bpr_source": context.bpr_source}
    if not context.broker_state_clear:
        reasons.append("broker_state_ambiguous")
    if context.bpr is None or context.bpr <= 0:
        reasons.append("broker_bpr_unknown")
    elif policy.lane is LaneId.SHORT_STRANGLE and context.bpr_source != "broker_preflight":
        if policy.stage is CsaStage.SHADOW and context.bpr_source == "local_fallback":
            evidence["shadow_only_warning"] = "broker_bpr_fallback"
        else:
            reasons.append("broker_bpr_not_authoritative")
    return AdmissionStageResult(
        stage="broker",
        passed=not reasons,
        reasons=tuple(reasons or ["broker_ok"]),
        evidence=evidence,
    )


def _portfolio_stage(context: AdmissionContext) -> AdmissionStageResult:
    reasons: list[str] = []
    if not context.portfolio_allowed:
        reasons.append("portfolio_policy_blocked")
    if not context.buying_power_available:
        reasons.append("portfolio_buying_power_unavailable")
    if not context.ownership_clear:
        reasons.append("portfolio_ownership_ambiguous")
    if context.working_order_conflict:
        reasons.append("portfolio_working_order_conflict")
    return AdmissionStageResult(
        stage="portfolio",
        passed=not reasons,
        reasons=tuple(reasons or ["portfolio_ok"]),
        evidence=context.evidence,
    )


def _within(value: Any, low: Any, high: Any) -> bool:
    if value in (None, "") or low in (None, "") or high in (None, ""):
        return False
    try:
        return float(low) <= float(value) <= float(high)
    except (TypeError, ValueError):
        return False


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}
