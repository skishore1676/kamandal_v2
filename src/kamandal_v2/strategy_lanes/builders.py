"""CSA adapters over existing deterministic strategy builders."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

from kamandal_v2.domain.models import Candidate, ChainSnapshot, Idea, Playbook
from kamandal_v2.planner.candidate_builder import (
    _build_for_playbook,
    _call_calendar_candidates,
    _call_diagonal_candidates,
    _call_spread_candidates,
    _put_calendar_candidates,
    _put_diagonal_candidates,
    _short_strangle_candidates,
)
from kamandal_v2.strategy_lanes.models import LaneId, StrategyOpportunity
from kamandal_v2.strategy_lanes.policy import CsaPolicy


def build_lane_candidates(
    opportunity: StrategyOpportunity,
    policy: CsaPolicy,
    snapshot: ChainSnapshot,
) -> tuple[Candidate, ...]:
    if opportunity.policy_hash != policy.policy_hash:
        raise ValueError("opportunity policy hash does not match builder policy")
    if opportunity.underlying != snapshot.underlying:
        raise ValueError("opportunity underlying does not match chain snapshot")
    resolved_fields = dict(policy.resolved_fields)
    playbook = Playbook.from_row(resolved_fields)
    idea = _source_idea(opportunity)
    quotes = snapshot.quotes
    if policy.lane is LaneId.SHORT_STRANGLE:
        candidates = _short_strangle_candidates(idea, playbook, quotes)
    elif policy.lane is LaneId.CALL_VERTICAL:
        candidates = _call_spread_candidates(idea, playbook, quotes, config=_builder_config(policy))
    elif policy.lane is LaneId.DIRECTIONAL_DIAGONAL:
        if playbook.structure == "call_diagonal":
            candidates = _call_diagonal_candidates(idea, playbook, quotes, config=_builder_config(policy))
        elif playbook.structure == "put_diagonal":
            candidates = _put_diagonal_candidates(idea, playbook, quotes, config=_builder_config(policy))
        else:
            raise ValueError(f"unsupported diagonal structure: {playbook.structure}")
    elif policy.lane is LaneId.GENERIC_CLOSE_ONLY:
        candidates = _build_for_playbook(
            idea,
            playbook,
            snapshot.underlying_price,
            quotes,
            config=_builder_config(policy),
        )
    elif policy.lane is LaneId.EARNINGS_CALENDAR:
        if opportunity.event_context.get("state") not in {"known", "confirmed"}:
            return ()
        playbook = _event_relative_playbook(playbook, opportunity, policy)
        if playbook.structure == "call_calendar":
            candidates = _call_calendar_candidates(idea, playbook, quotes, config=_builder_config(policy))
        elif playbook.structure == "put_calendar":
            candidates = _put_calendar_candidates(idea, playbook, quotes, config=_builder_config(policy))
        else:
            raise ValueError(f"unsupported earnings calendar structure: {playbook.structure}")
        candidates = _event_relative_calendars(candidates, opportunity, policy)
    else:
        raise ValueError(f"unsupported CSA lane: {policy.lane.value}")
    for candidate in candidates:
        candidate.reasons.extend(
            [
                f"csa_lane={policy.lane.value}",
                f"csa_policy_hash={policy.policy_hash}",
                f"csa_source_mode={policy.source_mode.value}",
            ]
        )
        if policy.lane is LaneId.DIRECTIONAL_DIAGONAL:
            candidate.reasons.extend(
                [
                    "csa_width_source=independent_sheet_leg_targets",
                    f"csa_actual_width={abs(candidate.legs[0].strike - candidate.legs[1].strike):g}",
                ]
            )
    return tuple(candidates)


def _event_relative_playbook(
    playbook: Playbook,
    opportunity: StrategyOpportunity,
    policy: CsaPolicy,
) -> Playbook:
    event_date = date.fromisoformat(str(opportunity.event_context["event_date"]))
    observed_date = date.fromisoformat(opportunity.observed_at[:10])
    event_dte = max((event_date - observed_date).days, 0)
    expiration_policy = (policy.management.get("lifecycle") or {}).get("event_expiration") or {}
    near_after_days = int(float(expiration_policy["near_before_days"]))
    far_after_days = int(float(expiration_policy["far_after_days"]))
    near_min = event_dte
    near_max = event_dte + near_after_days
    far_min = near_min + 1
    far_max = event_dte + far_after_days
    if far_max < far_min:
        raise ValueError(f"{policy.playbook_id}: event expiration window has no far expiration")
    return replace(
        playbook,
        dte_min=near_min,
        dte_max=near_max,
        long_dte_min=far_min,
        long_dte_max=far_max,
    )


def _event_relative_calendars(
    candidates: list[Candidate],
    opportunity: StrategyOpportunity,
    policy: CsaPolicy,
) -> list[Candidate]:
    raw_event_date = opportunity.event_context.get("event_date")
    if not raw_event_date:
        return []
    try:
        event_date = date.fromisoformat(str(raw_event_date))
    except ValueError:
        return []
    lifecycle = policy.management.get("lifecycle") or {}
    expiration_policy = lifecycle.get("event_expiration") or {}
    near_before_days = int(float(expiration_policy["near_before_days"]))
    far_after_days = int(float(expiration_policy["far_after_days"]))
    accepted: list[Candidate] = []
    for candidate in candidates:
        near = min(date.fromisoformat(leg.expiration) for leg in candidate.legs)
        far = max(date.fromisoformat(leg.expiration) for leg in candidate.legs)
        near_gap = (near - event_date).days
        far_gap = (far - event_date).days
        if 0 <= near_gap <= near_before_days and 0 <= far_gap <= far_after_days:
            candidate.reasons.extend(
                [
                    f"event_date={event_date.isoformat()}",
                    f"event_before_near_expiration_days={near_gap}",
                    f"event_far_after_days={far_gap}",
                ]
            )
            accepted.append(candidate)
    return accepted


def _source_idea(opportunity: StrategyOpportunity) -> Idea:
    payload = opportunity.evidence.get("idea")
    if isinstance(payload, dict):
        return Idea.from_dict(payload)
    direction = "bearish" if opportunity.lane is LaneId.CALL_VERTICAL else "neutral"
    return Idea(
        idea_id=opportunity.source_id,
        source=opportunity.source_mode.value,
        underlying=opportunity.underlying,
        direction=direction,
        operator_status="approved",
    )


def _builder_config(policy: CsaPolicy) -> dict[str, Any]:
    lifecycle = policy.management.get("lifecycle") or {}
    raw_fallback = lifecycle.get("dte_fallback")
    if raw_fallback is None:
        fallback = {"enabled": False}
    elif isinstance(raw_fallback, dict):
        fallback = dict(raw_fallback)
    else:
        raise ValueError(f"{policy.playbook_id}: lifecycle.dte_fallback must be an object")
    raw_width_search = lifecycle.get("vertical_width_search")
    if raw_width_search is None:
        width_search = {"enabled": False}
    elif isinstance(raw_width_search, dict):
        width_search = dict(raw_width_search)
    else:
        raise ValueError(f"{policy.playbook_id}: lifecycle.vertical_width_search must be an object")
    return {
        "planner": {
            "expiry": {"diagonal_calendar_dte_fallback": fallback},
            "vertical_width_search": width_search,
        }
    }
