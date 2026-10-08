"""Source-exact FIFO admission, retaining evidence and broker gates.

Suman authorized the two-pathway separation and deployment on 2026-10-01;
see docs/TWO_ENTRY_PATHWAYS.md. This decision does not submit orders.
"""
from __future__ import annotations

from datetime import datetime, UTC
from math import isfinite
from typing import Callable

from kamandal_v2.domain.models import Candidate, PortfolioState
from kamandal_v2.portfolio_sleeves import candidate_lane


def admit_guru_openings(
    candidates: list[Candidate], account: PortfolioState, control: dict, *,
    limit: int | None, capital_check: Callable[[list[Candidate]], str],
) -> list[Candidate]:
    admitted: list[Candidate] = []
    opportunities: set[tuple[str, str]] = set()
    contracts: set[tuple] = set()
    live = str((control.get("runtime") or {}).get("mode") or "").lower() == "live"
    for candidate in sorted((c for c in candidates if candidate_lane(c) == "guru_exact"), key=_order):
        metadata = candidate.metadata
        identities = {(str(metadata.get("source_profile") or "").lower(), str(value))
                      for value in {metadata.get("source_opportunity_id") or candidate.idea_id,
                                    *metadata.get("source_opportunity_ids", [])} if value}
        keys = {(candidate.underlying, leg.expiration, leg.option_type, leg.strike) for leg in candidate.legs}
        blocker = candidate.rejection_reason
        if not blocker and (not isfinite(candidate.estimated_bpr) or candidate.estimated_bpr <= 0):
            blocker = "invalid_candidate_bpr"
        if not blocker and live:
            if (metadata.get("source_verified") is not True
                    or not metadata.get("source_verification_ref")
                    or metadata.get("source_verification_reason")):
                blocker = "source_verification_required"
            elif (candidate.preflight is None or not candidate.preflight.ok
                  or candidate.preflight.raw.get("broker_bpr_provided") is not True):
                blocker = "live_preflight_bpr_required"
        if not blocker and identities & opportunities:
            blocker = "source_opportunity_already_admitted"
        if not blocker and keys & contracts:
            blocker = "exact_contract_already_admitted"
        if not blocker and limit is not None and len(admitted) >= limit:
            blocker = "entry_submission_capacity"
        if not blocker:
            blocker = capital_check(admitted + [candidate])
        receipt = {
            "pathway": "guru_exact", "status": "blocked" if blocker else "admitted",
            "rule": blocker or "guru_fifo_capital_admission",
            "source_id": metadata.get("source_profile"),
            "source_opportunity_id": metadata.get("source_opportunity_id") or candidate.idea_id,
            "source_published_at": metadata.get("source_published_at"),
            "candidate_bpr": candidate.estimated_bpr,
            "account_size": account.account_size, "buying_power": account.buying_power,
            "reserved_bpr": sum(c.estimated_bpr for c in admitted), "submission_limit": limit,
        }
        policy, usage = control.get("_live_sleeve_policy"), control.get("_live_sleeve_usage")
        if policy is not None and usage is not None:
            receipt.update(sleeve_used_bpr=usage.guru_exact_bpr,
                           sleeve_limit_bpr=usage.account_size * policy.guru_exact_pct / 100,
                           total_used_bpr=usage.portfolio_total_bpr,
                           total_limit_bpr=usage.account_size * policy.portfolio_total_pct / 100)
        metadata["entry_decision"] = receipt
        if blocker:
            candidate.rejection_reason = blocker
            continue
        admitted.append(candidate)
        opportunities.update(identities)
        contracts.update(keys)
    return admitted


def _order(candidate: Candidate) -> tuple:
    raw = str(candidate.metadata.get("source_published_at") or "")
    try:
        published = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC).timestamp()
    except ValueError:
        published = float("inf")
    return (published, str(candidate.metadata.get("source_profile") or ""),
            str(candidate.metadata.get("source_opportunity_id") or candidate.idea_id), candidate.candidate_id)
