"""Canonical identity helpers for live entry order replacement lineages.

Tickets are immutable price/order versions.  A live position, however, belongs
to the complete entry lineage that starts at the first submitted ticket.  This
module keeps that distinction deterministic without making any broker calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kamandal_v2.stores.sqlite import LocalStore


NON_CANONICAL_STATUSES = {
    "blocked_preflight_failed",
    "blocked_preflight_stale",
    "cancelled",
    "canceled",
    "deferred_entry_cutoff",
    "deferred_market_closed",
    "expired",
    "expired_eod",
    "failed",
    "rejected",
    "replace_aborted_parent_filled",
    "retired_stale_entry_approval",
    "submit_failed",
}


@dataclass(frozen=True)
class EntryLineage:
    lineage_id: str
    root_ticket_hash: str
    canonical_ticket: dict[str, Any] | None
    members: tuple[dict[str, Any], ...]
    ambiguous: bool = False
    reason: str = ""

    @property
    def group_id(self) -> str:
        return f"live_group_{self.lineage_id}"


def resolve_entry_lineage(
    store: LocalStore,
    ticket: dict[str, Any],
    *,
    broker_order_id: str = "",
) -> EntryLineage:
    """Resolve one entry lineage and its unique terminal ticket version.

    Atomic broker replacements may preserve ``order_id`` while changing the
    local ticket hash.  The deepest viable member for the observed broker order
    is canonical.  Equal-depth siblings are deliberately ambiguous and are not
    auto-projected.
    """

    ticket_hash = str(ticket.get("ticket_hash") or "")
    if not ticket_hash:
        return EntryLineage("", "", None, (), True, "missing_ticket_hash")

    tickets = {
        str(item.get("ticket_hash") or ""): item
        for item in store.live_order_intents_by_type("open")
        if str(item.get("ticket_hash") or "")
    }
    tickets.setdefault(ticket_hash, ticket)

    root_hash, root_error = _root_hash(ticket_hash, tickets)
    if root_error:
        return EntryLineage(ticket_hash, ticket_hash, None, (ticket,), True, root_error)

    members = tuple(
        item
        for item in tickets.values()
        if _belongs_to_root(str(item.get("ticket_hash") or ""), root_hash, tickets)
    )
    target_order_id = str(broker_order_id or ticket.get("order_id") or "")
    candidates = [
        item
        for item in members
        if (not target_order_id or str(item.get("order_id") or "") == target_order_id)
        and str(item.get("_ledger_status") or item.get("status") or "") not in NON_CANONICAL_STATUSES
    ]
    if not candidates:
        return EntryLineage(root_hash, root_hash, None, members, True, "no_viable_ticket_for_broker_order")

    ranked = sorted(
        ((_depth(str(item.get("ticket_hash") or ""), tickets), str(item.get("created_at") or ""), item) for item in candidates),
        key=lambda row: (row[0], row[1], str(row[2].get("ticket_hash") or "")),
        reverse=True,
    )
    max_depth = ranked[0][0]
    deepest = [item for depth, _, item in ranked if depth == max_depth]
    if len(deepest) != 1:
        return EntryLineage(root_hash, root_hash, None, members, True, "multiple_terminal_replacement_tickets")
    return EntryLineage(root_hash, root_hash, deepest[0], members)


def source_ticket_hash(group: dict[str, Any]) -> str:
    snapshot = group.get("entry_snapshot") or {}
    lineage = group.get("execution_lineage") or {}
    explicit = str(
        snapshot.get("source_ticket_hash")
        or lineage.get("canonical_ticket_hash")
        or group.get("source_ticket_hash")
        or ""
    )
    if explicit:
        return explicit
    group_id = str(group.get("group_id") or "")
    return group_id.removeprefix("live_group_") if group_id.startswith("live_group_") else ""


def _root_hash(ticket_hash: str, tickets: dict[str, dict[str, Any]]) -> tuple[str, str]:
    current = ticket_hash
    seen: set[str] = set()
    while current:
        if current in seen:
            return ticket_hash, "replacement_lineage_cycle"
        seen.add(current)
        ticket = tickets.get(current)
        if not ticket:
            return ticket_hash, "replacement_parent_missing"
        parent = str(ticket.get("parent_ticket_hash") or "")
        if not parent:
            return current, ""
        current = parent
    return ticket_hash, "replacement_root_missing"


def _belongs_to_root(ticket_hash: str, root_hash: str, tickets: dict[str, dict[str, Any]]) -> bool:
    resolved, error = _root_hash(ticket_hash, tickets)
    return not error and resolved == root_hash


def _depth(ticket_hash: str, tickets: dict[str, dict[str, Any]]) -> int:
    depth = 0
    current = ticket_hash
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        ticket = tickets.get(current) or {}
        parent = str(ticket.get("parent_ticket_hash") or "")
        if not parent:
            break
        depth += 1
        current = parent
    return depth
