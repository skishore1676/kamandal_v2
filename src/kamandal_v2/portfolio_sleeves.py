"""Sheet-owned entry ceilings and conservative live sleeve accounting.

These controls admit new entries only. Existing positions and their exits keep
their frozen management policy when an operator lowers a ceiling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from kamandal_v2.domain.models import Candidate, PortfolioState
from kamandal_v2.stores.sqlite import LocalStore, _live_group_bpr


CURRENT_IDEA = "current_idea"
GURU_EXACT = "guru_exact"
PORTFOLIO_TOTAL = "portfolio_total"
REQUIRED_ROWS = (CURRENT_IDEA, GURU_EXACT, PORTFOLIO_TOTAL)
PENDING_ENTRY_STATUSES = {
    "pending_approval", "stage_approved_pending_submit", "waiting_entry_window",
    "submitted", "partially_filled", "submit_uncertain", "replace_cancel_pending",
    "replace_waiting_cancel",
}


@dataclass(frozen=True, slots=True)
class SleevePolicy:
    current_idea_pct: float
    guru_exact_pct: float
    portfolio_total_pct: float

    def limit(self, lane: str) -> float:
        return {
            CURRENT_IDEA: self.current_idea_pct,
            GURU_EXACT: self.guru_exact_pct,
            PORTFOLIO_TOTAL: self.portfolio_total_pct,
        }[lane]

    def to_dict(self) -> dict[str, float]:
        return {lane: self.limit(lane) for lane in REQUIRED_ROWS}


@dataclass(frozen=True, slots=True)
class SleeveUsage:
    account_size: float
    current_idea_bpr: float
    guru_exact_bpr: float
    portfolio_total_bpr: float
    broker_bpr: float
    ledger_open_bpr: float
    pending_bpr: float
    unmatched_bpr: float

    def used(self, lane: str) -> float:
        return {
            CURRENT_IDEA: self.current_idea_bpr,
            GURU_EXACT: self.guru_exact_bpr,
            PORTFOLIO_TOTAL: self.portfolio_total_bpr,
        }[lane]

    def to_dict(self) -> dict[str, float]:
        return {
            "account_size": self.account_size,
            "current_idea_bpr": self.current_idea_bpr,
            "guru_exact_bpr": self.guru_exact_bpr,
            "portfolio_total_bpr": self.portfolio_total_bpr,
            "broker_bpr": self.broker_bpr,
            "ledger_open_bpr": self.ledger_open_bpr,
            "pending_bpr": self.pending_bpr,
            "unmatched_bpr": self.unmatched_bpr,
        }


def compile_sleeve_policy(rows: Iterable[dict[str, Any]]) -> SleevePolicy:
    values: dict[str, float] = {}
    for number, row in enumerate(rows, start=2):
        if not any(str(value or "").strip() for value in row.values()):
            continue
        lane = str(row.get("lane") or "").strip().lower()
        if lane not in REQUIRED_ROWS:
            raise ValueError(f"portfolio_sleeves row {number}: unknown lane {lane!r}")
        if lane in values:
            raise ValueError(f"portfolio_sleeves row {number}: duplicate lane {lane}")
        try:
            value = float(str(row.get("max_bpr_pct") or "").strip().rstrip("%"))
        except ValueError as exc:
            raise ValueError(f"portfolio_sleeves row {number}: invalid max_bpr_pct") from exc
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError(f"portfolio_sleeves row {number}: max_bpr_pct must be 0..100")
        values[lane] = value
    missing = set(REQUIRED_ROWS) - values.keys()
    if missing:
        raise ValueError("portfolio_sleeves: missing " + ", ".join(sorted(missing)))
    if values[CURRENT_IDEA] > values[PORTFOLIO_TOTAL] or values[GURU_EXACT] > values[PORTFOLIO_TOTAL]:
        raise ValueError("portfolio_sleeves: lane ceiling exceeds portfolio_total")
    return SleevePolicy(values[CURRENT_IDEA], values[GURU_EXACT], values[PORTFOLIO_TOTAL])


def candidate_lane(candidate: Candidate | dict[str, Any]) -> str:
    metadata = (candidate.get("metadata") or {}) if isinstance(candidate, dict) else candidate.metadata
    explicit = str(metadata.get("sleeve_id") or "")
    if explicit in {CURRENT_IDEA, GURU_EXACT}:
        return explicit
    if str(metadata.get("input_kind") or "") == "exact_package" and metadata.get("source_profile"):
        return GURU_EXACT
    return CURRENT_IDEA


def ticket_lane(ticket: dict[str, Any]) -> str:
    lane = str(ticket.get("sleeve_id") or "")
    return lane if lane in {CURRENT_IDEA, GURU_EXACT} else CURRENT_IDEA


def occupied_source_opportunities(
    store: LocalStore, *, exclude_ticket_hash: str = "",
) -> set[tuple[str, str]]:
    """A source opening is one opportunity across its idea and exact routes."""
    occupied: set[tuple[str, str]] = set()
    for group in store.open_live_position_groups():
        candidate = group.get("candidate") or {}
        metadata = candidate.get("metadata") or {}
        source = str(group.get("source_id") or metadata.get("source_profile") or "").lower()
        opportunity = str(group.get("source_opportunity_id") or metadata.get("source_opportunity_id") or "")
        if source and opportunity:
            occupied.add((source, opportunity))
    for ticket in store.live_order_intents_by_type("open", PENDING_ENTRY_STATUSES):
        if str(ticket.get("ticket_hash") or "") == exclude_ticket_hash:
            continue
        source = str(ticket.get("source_id") or "").lower()
        opportunity = str(ticket.get("source_opportunity_id") or "")
        if source and opportunity:
            occupied.add((source, opportunity))
    return occupied


def live_sleeve_usage(
    store: LocalStore,
    account: PortfolioState,
    *,
    exclude_ticket_hash: str = "",
) -> SleeveUsage:
    if not math.isfinite(account.account_size) or account.account_size <= 0:
        raise ValueError("portfolio_sleeves: account size unavailable")
    if not math.isfinite(account.bpr_used) or account.bpr_used < 0:
        raise ValueError("portfolio_sleeves: broker BPR unavailable")
    open_bpr = {CURRENT_IDEA: 0.0, GURU_EXACT: 0.0}
    for group in store.open_live_position_groups():
        lane = str(group.get("sleeve_id") or "")
        if lane not in open_bpr:
            lane = CURRENT_IDEA  # Existing untagged positions consume the current lane.
        open_bpr[lane] += max(_live_group_bpr(group), 0.0)
    local_open = sum(open_bpr.values())
    unmatched = max(account.bpr_used - local_open, 0.0)
    open_bpr[CURRENT_IDEA] += unmatched  # Conservative attribution for legacy/residual BPR.

    # Reprices are one commitment. The full reserved BPR wins over a partial
    # fill's smaller projection; a broker reservation may double count here,
    # which reduces capacity safely until reconciliation catches up.
    pending: dict[tuple[str, str], tuple[str, float]] = {}
    for ticket in store.live_order_intents_by_type("open", PENDING_ENTRY_STATUSES):
        if str(ticket.get("ticket_hash") or "") == exclude_ticket_hash:
            continue
        key = (str(ticket.get("plan_id") or ""), str(ticket.get("candidate_id") or ticket.get("ticket_hash") or ""))
        try:
            amount = float(ticket.get("entry_risk_budget") or (ticket.get("preflight") or {}).get("bpr") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("portfolio_sleeves: pending entry BPR unavailable")
        if key not in pending or amount > pending[key][1]:
            pending[key] = (ticket_lane(ticket), amount)
    pending_bpr = sum(value for _, value in pending.values())
    for lane, value in pending.values():
        open_bpr[lane] += value
    return SleeveUsage(
        account_size=account.account_size,
        current_idea_bpr=round(open_bpr[CURRENT_IDEA], 2),
        guru_exact_bpr=round(open_bpr[GURU_EXACT], 2),
        portfolio_total_bpr=round(max(account.bpr_used, local_open) + pending_bpr, 2),
        broker_bpr=round(account.bpr_used, 2),
        ledger_open_bpr=round(local_open, 2),
        pending_bpr=round(pending_bpr, 2),
        unmatched_bpr=round(unmatched, 2),
    )


def sleeve_entry_blocker(
    policy: SleevePolicy,
    usage: SleeveUsage,
    additions: Iterable[tuple[str, float]],
) -> str:
    added = {CURRENT_IDEA: 0.0, GURU_EXACT: 0.0}
    for lane, bpr in additions:
        if lane not in added or not math.isfinite(bpr) or bpr <= 0:
            return "sleeve_entry_invalid"
        added[lane] += bpr
    for lane in (CURRENT_IDEA, GURU_EXACT):
        if added[lane] and (usage.used(lane) + added[lane]) / usage.account_size * 100 > policy.limit(lane) + 1e-8:
            return f"sleeve_bpr_cap:{lane}"
    if sum(added.values()) and (usage.portfolio_total_bpr + sum(added.values())) / usage.account_size * 100 > policy.portfolio_total_pct + 1e-8:
        return "sleeve_bpr_cap:portfolio_total"
    return ""
