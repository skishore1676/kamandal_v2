"""Deterministic opportunity adapters for CSA lanes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from typing import Any

from kamandal_v2.domain.models import Idea, UniverseEntry
from kamandal_v2.strategy_lanes.models import LaneId, SourceMode, StrategyOpportunity, stable_csa_id
from kamandal_v2.strategy_lanes.policy import CsaPolicy


def idea_opportunity(
    idea: Idea,
    policy: CsaPolicy,
    *,
    observed_at: str,
    event_context: Mapping[str, Any] | None = None,
) -> StrategyOpportunity:
    if policy.source_mode is not SourceMode.IDEA:
        raise ValueError(f"{policy.playbook_id}: policy is not idea-sourced")
    identity = [policy.playbook_id, idea.idea_id, idea.underlying, observed_at]
    return StrategyOpportunity(
        opportunity_id=stable_csa_id("opportunity", identity),
        lane=policy.lane,
        source_mode=policy.source_mode,
        playbook_id=policy.playbook_id,
        underlying=idea.underlying,
        observed_at=observed_at,
        source_id=idea.idea_id,
        policy_hash=policy.policy_hash,
        evidence={
            "idea": idea.to_dict(),
            "source_approved": idea.operator_status == "approved",
            "source_fresh": True,
        },
        event_context=dict(event_context or {}),
        confidence=_confidence_number(idea.confidence or idea.extraction_confidence),
    )


def market_scan_opportunities(
    universe: Iterable[UniverseEntry],
    policies: Iterable[CsaPolicy],
    observations: Mapping[str, Mapping[str, Any]],
    *,
    observed_at: str,
) -> tuple[StrategyOpportunity, ...]:
    scan_policies = [policy for policy in policies if policy.source_mode is SourceMode.MARKET_SCAN]
    opportunities: list[StrategyOpportunity] = []
    for entry in sorted(universe, key=lambda item: item.symbol):
        if not entry.enabled:
            continue
        observation = dict(observations.get(entry.symbol) or {})
        for policy in sorted(scan_policies, key=lambda item: item.playbook_id):
            expansion_enabled = str(policy.resolved_fields.get("universe_expansion_enabled") or "").strip().lower() in {
                "1", "true", "yes", "y", "on"
            }
            source_allowed = expansion_enabled or not entry.allowed_playbooks or (
                policy.playbook_id in entry.allowed_playbooks
                or str(policy.resolved_fields.get("structure") or "") in entry.allowed_playbooks
            )
            identity = [policy.playbook_id, entry.symbol, observed_at]
            opportunities.append(
                StrategyOpportunity(
                    opportunity_id=stable_csa_id("opportunity", identity),
                    lane=policy.lane,
                    source_mode=policy.source_mode,
                    playbook_id=policy.playbook_id,
                    underlying=entry.symbol,
                    observed_at=observed_at,
                    source_id=f"universe:{entry.symbol}",
                    policy_hash=policy.policy_hash,
                    evidence={
                        "source_approved": source_allowed,
                        "source_fresh": bool(observation.get("source_fresh", False)),
                        "universe": asdict(entry),
                    },
                    market_context=observation,
                )
            )
    return tuple(opportunities)


def portfolio_hedge_opportunities(
    portfolio_context: Mapping[str, Any],
    policies: Iterable[CsaPolicy],
    observations: Mapping[str, Mapping[str, Any]],
    *,
    observed_at: str,
) -> tuple[StrategyOpportunity, ...]:
    opportunities: list[StrategyOpportunity] = []
    portfolio_delta = _required_number(portfolio_context, "delta")
    for policy in sorted(policies, key=lambda item: item.playbook_id):
        if policy.source_mode is not SourceMode.PORTFOLIO_HEDGE:
            continue
        lifecycle = policy.management.get("lifecycle") or {}
        trigger = _required_number(lifecycle, "portfolio_delta_trigger")
        underlyings = lifecycle.get("hedge_underlyings")
        if not isinstance(underlyings, list) or not underlyings:
            raise ValueError(f"{policy.playbook_id}: lifecycle.hedge_underlyings must be a non-empty list")
        if portfolio_delta <= trigger:
            continue
        for raw_symbol in underlyings:
            symbol = str(raw_symbol).strip().upper()
            if not symbol:
                continue
            observation = dict(observations.get(symbol) or {})
            identity = [policy.playbook_id, symbol, observed_at, portfolio_delta]
            opportunities.append(
                StrategyOpportunity(
                    opportunity_id=stable_csa_id("opportunity", identity),
                    lane=policy.lane,
                    source_mode=policy.source_mode,
                    playbook_id=policy.playbook_id,
                    underlying=symbol,
                    observed_at=observed_at,
                    source_id=f"portfolio:{observed_at}",
                    policy_hash=policy.policy_hash,
                    evidence={
                        "source_approved": True,
                        "source_fresh": bool(portfolio_context.get("source_fresh", False)),
                        "trigger_source": "management_policy_json.lifecycle.portfolio_delta_trigger",
                    },
                    market_context=observation,
                    portfolio_context=dict(portfolio_context),
                )
            )
    return tuple(opportunities)


def _required_number(values: Mapping[str, Any], key: str) -> float:
    raw = values.get(key)
    if isinstance(raw, bool) or raw in (None, ""):
        raise ValueError(f"missing numeric {key}")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric {key}={raw!r}") from exc


def _confidence_number(value: str) -> float | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None
