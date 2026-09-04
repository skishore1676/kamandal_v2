"""Turn exact observed opening packages into normal planner candidates.

The source ledger and planner are intentionally separate.  Every evidence
revision is retained, but only complete ``open`` packages authorized by a
source policy and one compatible existing playbook become candidates. This module never
chooses substitute expirations or strikes and never calls broker preflight.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg, Playbook, PreflightResult, utc_now
from kamandal_v2.intelligence.observed_packages import ObservedPackageBatch, ObservedPackageEvidence
from kamandal_v2.intelligence.trade_sources import (
    TradeSourceMode,
    TradeSourceOutputKind,
    TradeSourcePolicy,
)
from kamandal_v2.liquidity import candidate_liquidity_metrics
from kamandal_v2.market.interfaces import MarketDataProvider
from kamandal_v2.planner.candidate_builder import (  # shared deterministic economics; no leg construction
    _candidate_score,
    _entry_economic_bounds,
    _estimate_bpr,
    _filter_rejections,
    _risk_width,
)
from kamandal_v2.planner.shape_validators import validate_structure
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.policy import PlaybookPolicy


def persist_observed_package_batches(
    batches: Iterable[ObservedPackageBatch],
    *,
    store: LocalStore,
) -> tuple[ObservedPackageEvidence, ...]:
    """Append every package revision to the passive, non-economic ledger."""

    packages = tuple(package for batch in batches for package in batch.packages)
    inserted = 0
    for package in packages:
        inserted += int(store.record_observed_package_evidence(package.to_dict()))
    store.event(
        "observed_package_evidence_recorded",
        {
            "packages_seen": len(packages),
            "revisions_inserted": inserted,
            "idea_created": False,
            "plan_created": False,
            "ticket_created": False,
            "fill_created": False,
            "lifecycle_created": False,
            "broker_effects": False,
        },
    )
    return packages


def record_observed_packages_not_authorized(
    packages: Iterable[ObservedPackageEvidence],
    *,
    store: LocalStore,
    blocker: str = "no_matching_observed_package_policy",
) -> None:
    for package in packages:
        _receipt(store, package, status="not_authorized", blocker=blocker)


def build_observed_package_candidates(
    packages: Iterable[ObservedPackageEvidence],
    *,
    policies: tuple[PlaybookPolicy, ...],
    playbooks: list[Playbook],
    market: MarketDataProvider,
    store: LocalStore,
    config: dict[str, Any] | None = None,
    trade_source_policies: dict[tuple[str, TradeSourceOutputKind], TradeSourcePolicy] | None = None,
) -> list[Candidate]:
    """Hydrate source-exact legs and return ordinary optimizer candidates."""

    playbook_by_id = {playbook.playbook_id: playbook for playbook in playbooks}
    exact_policies = tuple(
        policy
        for policy in policies
        if "exact_package" in getattr(
            policy,
            "accepted_inputs",
            ("exact_package",) if getattr(policy, "source_mode", "") == "observed_package" else (),
        )
    )
    chain_cache: dict[str, Any] = {}
    candidates: list[Candidate] = []

    for package in packages:
        blocker = _evidence_blocker(package)
        source_policy = (trade_source_policies or {}).get(
            (package.source_profile.lower(), TradeSourceOutputKind.EXACT_PACKAGE)
        )
        if trade_source_policies is not None and (
            source_policy is None or source_policy.mode not in {TradeSourceMode.SHADOW, TradeSourceMode.LIVE}
        ):
            mode = source_policy.mode.value if source_policy is not None else "missing"
            _receipt(store, package, status="observed", blocker=f"source_mode_{mode}")
            continue
        matching = [
            policy
            for policy in exact_policies
            if policy.structure == package.structure
        ]
        if blocker:
            _receipt(store, package, status="parked", blocker=blocker)
            continue
        if not matching:
            _receipt(store, package, status="parked", blocker="unsupported")
            continue
        if len(matching) > 1:
            _receipt(store, package, status="parked", blocker="ambiguous_playbook_match")
            continue

        if package.symbol not in chain_cache:
            try:
                chain_cache[package.symbol] = market.chain_snapshot(package.symbol)
            except Exception as exc:  # noqa: BLE001 - the evidence receipt owns the precise park reason.
                chain_cache[package.symbol] = exc
        chain = chain_cache[package.symbol]
        if isinstance(chain, Exception):
            _receipt(store, package, status="parked", blocker=f"quote_unavailable:{type(chain).__name__}:{chain}")
            continue
        if not _chain_is_fresh(chain.captured_at, config):
            _receipt(store, package, status="parked", blocker="quote_snapshot_stale")
            continue

        for policy in matching:
            playbook = playbook_by_id.get(policy.playbook_id)
            if playbook is None:
                _receipt(store, package, status="parked", blocker=f"playbook_missing:{policy.playbook_id}")
                continue
            try:
                legs = _hydrate_exact_legs(package, chain.quotes)
            except ValueError as exc:
                _receipt(store, package, status="parked", blocker=str(exc), playbook_id=policy.playbook_id)
                continue
            shape = validate_structure(str(package.structure), legs, float(chain.underlying_price))
            if not shape.valid:
                _receipt(store, package, status="parked", blocker=shape.reason, playbook_id=policy.playbook_id)
                continue
            candidate = _candidate(package, playbook, legs, chain_snapshot=chain)
            rejections = _filter_rejections(candidate, playbook, config)
            package_spread = float(candidate_liquidity_metrics(candidate)["aggregate_spread_to_mid_pct"])
            if playbook.max_bid_ask_pct is not None and package_spread > playbook.max_bid_ask_pct:
                rejections.append(
                    f"package_bid_ask_pct_above_max:{package_spread:.4f}>{playbook.max_bid_ask_pct}"
                )
            quote_rejections = [reason for reason in rejections if _is_quote_actionability_rejection(reason)]
            if not quote_rejections:
                first_mark = store.first_observed_package_mark(
                    source_event_id=package.source_event_id,
                    package_signature=str(package.package_signature),
                    observed_at=str(chain.captured_at),
                    package_midpoint=candidate.net_credit,
                    payload={
                        "chain_snapshot_id": chain.chain_snapshot_id,
                        "chain_captured_at": chain.captured_at,
                        "quote_source": chain.source,
                        "broker_effects": False,
                    },
                )
                _apply_first_mark(candidate, first_mark)
            if rejections:
                candidate.rejection_reason = rejections[0]
            candidate.score = _candidate_score(candidate, thesis_fit=0.0)
            candidates.append(candidate)
            _receipt(
                store,
                package,
                status="candidate_generated" if candidate.eligible else "candidate_rejected",
                blocker=candidate.rejection_reason,
                playbook_id=policy.playbook_id,
                candidate_id=candidate.candidate_id,
                observational_entry_mark=candidate.metadata.get("observational_entry_mark"),
                attempted_package_midpoint=candidate.net_credit,
                chain_snapshot_id=chain.chain_snapshot_id,
            )
    return candidates


def _evidence_blocker(package: ObservedPackageEvidence) -> str:
    if package.action != "open":
        return f"benchmark_only_action:{package.action}"
    if not package.complete:
        return package.blocker or "observed_package_incomplete"
    if not package.structure:
        return "observed_package_structure_missing"
    if not package.package_signature:
        return "observed_package_signature_missing"
    if package.product_type == "futures_option":
        return "unsupported_product:futures_option"
    if any(leg.effect != "open" for leg in package.legs):
        return "opening_package_contains_non_open_leg"
    return ""


def _hydrate_exact_legs(package: ObservedPackageEvidence, quotes: list[Any]) -> list[OptionLeg]:
    roles = _canonical_roles(package)
    legs: list[OptionLeg] = []
    for position, observed in enumerate(package.legs, start=1):
        if None in (observed.quantity, observed.expiration, observed.strike, observed.option_type, observed.side):
            raise ValueError(f"source_leg_{position}_incomplete")
        strike = float(observed.strike)
        matches = [
            quote
            for quote in quotes
            if quote.expiration == observed.expiration
            and quote.option_type == observed.option_type
            and abs(float(quote.strike) - strike) < 1e-8
        ]
        if len(matches) != 1:
            raise ValueError(f"exact_contract_match_count:{position}:{len(matches)}")
        legs.append(
            OptionLeg.from_quote(
                matches[0],
                role=roles[position - 1],
                side=str(observed.side),
                quantity=int(observed.quantity),
            )
        )
    return legs


def _canonical_roles(package: ObservedPackageEvidence) -> list[str]:
    """Assign manager roles without changing a source-observed contract."""

    if package.structure not in {"call_calendar", "put_calendar", "call_diagonal", "put_diagonal"}:
        raise ValueError(f"unsupported_exact_structure:{package.structure}")
    if len(package.legs) != 2:
        raise ValueError("exact_calendar_or_diagonal_requires_two_legs")
    indexed = list(enumerate(package.legs))
    sold = [(index, leg) for index, leg in indexed if leg.side == "sell"]
    bought = [(index, leg) for index, leg in indexed if leg.side == "buy"]
    if len(sold) != 1 or len(bought) != 1:
        raise ValueError("exact_calendar_or_diagonal_requires_one_buy_and_one_sell")
    sold_index, sold_leg = sold[0]
    bought_index, bought_leg = bought[0]
    if not sold_leg.expiration or not bought_leg.expiration or sold_leg.expiration >= bought_leg.expiration:
        raise ValueError("exact_calendar_or_diagonal_requires_short_near_long_far")
    roles = ["", ""]
    roles[sold_index] = "short_near"
    roles[bought_index] = "long_far"
    return roles


def _candidate(
    package: ObservedPackageEvidence,
    playbook: Playbook,
    legs: list[OptionLeg],
    *,
    chain_snapshot: Any,
) -> Candidate:
    net_credit = round(sum(leg.signed_mid * leg.quantity for leg in legs), 4)
    greeks = Greeks()
    for leg in legs:
        greeks = greeks + leg.signed_greeks
    metrics = candidate_liquidity_metrics({"legs": legs, "net_credit": net_credit})
    liquidity_score = max(0.0, min(1.0, 1.0 - float(metrics["avg_bid_ask_pct"])))
    bpr = _estimate_bpr(playbook.structure, legs, net_credit)
    opportunity_id = package.opportunity_group_id or f"observed:{package.source_event_id}"
    identity = [opportunity_id, playbook.playbook_id, package.package_signature]
    candidate = Candidate(
        candidate_id="candidate_observed_" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:20],
        idea_id=opportunity_id,
        underlying=package.symbol,
        playbook_id=playbook.playbook_id,
        structure=playbook.structure,
        legs=legs,
        net_credit=net_credit,
        estimated_bpr=bpr,
        greeks=greeks,
        liquidity_score=round(liquidity_score, 4),
        score=0.0,
        execution_venue=playbook.execution_venue,
        preflight=PreflightResult(
            ok=True,
            bpr=bpr,
            message="shadow exact package uses deterministic local risk; broker preflight not consulted",
            raw={"source": "observed_package_shadow_local", "broker_effects": False},
        ),
        reasons=[
            "source_mode=observed_package",
            "construction=source_exact_legs",
            "thesis_fit=0.0",
            f"source_profile={package.source_profile}",
            f"source_event_id={package.source_event_id}",
            f"package_signature={package.package_signature}",
            f"evidence_revision_id={package.evidence_revision_id}",
            f"current_package_midpoint={net_credit}",
            f"chain_snapshot_id={chain_snapshot.chain_snapshot_id}",
            f"aggregate_spread_to_mid_pct={metrics['aggregate_spread_to_mid_pct']}",
        ],
        metadata={
            "source_mode": "observed_package",
            "source_profile": package.source_profile,
            "source_event_id": package.source_event_id,
            "source_opportunity_id": opportunity_id,
            "canonical_post_id": package.canonical_post_id,
            "media_index": package.media_index,
            "package_position": package.package_position,
            "package_signature": package.package_signature,
            "evidence_revision_id": package.evidence_revision_id,
            "displayed_price": dict(package.displayed_price) if package.displayed_price else None,
            "displayed_trade_time": package.displayed_trade_time,
            "chain_snapshot_id": chain_snapshot.chain_snapshot_id,
            "chain_captured_at": chain_snapshot.captured_at,
            "broker_effects": False,
        },
    )
    width = _risk_width(candidate)
    floor, ceiling, source = _entry_economic_bounds(
        playbook,
        structure=playbook.structure,
        width=width,
        net_credit=net_credit,
    )
    candidate.entry_credit_floor = floor
    candidate.entry_debit_ceiling = ceiling
    candidate.entry_economic_bound_source = source
    return candidate


def _apply_first_mark(candidate: Candidate, first_mark: dict[str, Any]) -> None:
    candidate.metadata.update(
        {
            "observational_entry_mark": float(first_mark["package_midpoint"]),
            "observational_mark_kind": str(first_mark["kind"]),
            "observational_mark_id": str(first_mark["observation_id"]),
            "observational_mark_at": str(first_mark["observed_at"]),
        }
    )
    candidate.reasons.append(f"observational_entry_mark={first_mark['package_midpoint']}")


def _receipt(store: LocalStore, package: ObservedPackageEvidence, *, status: str, blocker: str = "", **extra: Any) -> None:
    store.event(
        "observed_package_planner_receipt",
        {
            "source_profile": package.source_profile,
            "source_event_id": package.source_event_id,
            "canonical_post_id": package.canonical_post_id,
            "package_signature": package.package_signature,
            "evidence_revision_id": package.evidence_revision_id,
            "action": package.action,
            "structure": package.structure,
            "symbol": package.symbol,
            "status": status,
            "blocker": blocker,
            "broker_effects": False,
            **extra,
        },
    )


def _text_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _chain_is_fresh(captured_at: str, config: dict[str, Any] | None) -> bool:
    if not str(captured_at or "").strip():
        return False
    observed_at = str((((config or {}).get("runtime") or {}).get("observed_at") or utc_now()))
    maximum_minutes = int(
        float(
            (((((config or {}).get("live") or {}).get("option_submission") or {}).get("quote_max_age_minutes")) or 10)
        )
    )
    try:
        captured = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00")).astimezone(UTC)
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return False
    age_seconds = (observed - captured).total_seconds()
    return -60 <= age_seconds <= max(maximum_minutes, 1) * 60


def _is_quote_actionability_rejection(reason: str) -> bool:
    return reason.startswith(
        (
            "bad_quote_",
            "open_interest_below_min:",
            "bid_ask_pct_above_max:",
            "package_bid_ask_pct_above_max:",
        )
    )
