"""Canonical lifecycle ownership repairs shared by planning and reconciliation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from kamandal_v2.domain.models import utc_now
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.migrations import csa_schema_ready
from kamandal_v2.strategy_lanes.store import CsaStore


ACTIVE_OPEN_INTENT_STATUSES = {
    "pending_approval",
    "stage_approved_pending_submit",
    "waiting_entry_window",
    "submitted",
    "submit_uncertain",
    "working",
    "repriced",
    "partially_filled",
    "replace_pending_cancel",
    "replace_cancel_pending",
    "replace_waiting_cancel",
    "cancel_pending",
}


def retire_orphaned_pending_live_lifecycles(
    store: LocalStore,
    *,
    dry_run: bool = False,
    retire_unmatched: bool = True,
) -> list[dict[str, Any]]:
    """Terminalize pre-entry lifecycles after their guarded order lineage ends."""

    if not csa_schema_ready(store.sqlite_path):
        return []
    typed = CsaStore(store.sqlite_path)
    open_intents = store.live_order_intents_by_type("open")
    repairs: list[dict[str, Any]] = []
    for row in typed.rows("csa_lifecycles"):
        if str(row.get("status") or "") != "pending_live_submission":
            continue
        lifecycle = typed.lifecycle(str(row.get("id") or ""))
        if lifecycle is None or str(lifecycle.metadata.get("execution_mode") or "") != "live":
            continue
        plan_id = str(lifecycle.metadata.get("unified_plan_id") or "")
        candidate_id = str(lifecycle.metadata.get("candidate_id") or "")
        matching = [
            ticket
            for ticket in open_intents
            if str(ticket.get("csa_lifecycle_id") or "") == lifecycle.lifecycle_id
            or (
                str(ticket.get("plan_id") or "") == plan_id
                and str(ticket.get("candidate_id") or "") == candidate_id
            )
        ]
        if any(
            str(ticket.get("_ledger_status") or ticket.get("status") or "")
            in ACTIVE_OPEN_INTENT_STATUSES
            for ticket in matching
        ):
            continue
        if not matching and not retire_unmatched:
            continue
        repair = {
            "status": "dry_run" if dry_run else "applied",
            "reason": "guarded_open_intent_lineage_terminal",
            "lifecycle_id": lifecycle.lifecycle_id,
            "terminal_ticket_hashes": sorted(
                str(ticket.get("ticket_hash") or "")
                for ticket in matching
                if str(ticket.get("ticket_hash") or "")
            ),
            "terminal_ticket_statuses": sorted(
                {
                    str(ticket.get("_ledger_status") or ticket.get("status") or "")
                    for ticket in matching
                }
            ),
        }
        if not dry_run:
            observed_at = utc_now()
            typed.save_lifecycle(
                replace(
                    lifecycle,
                    status="entry_missed",
                    updated_at=observed_at,
                    metadata={
                        **lifecycle.metadata,
                        "entry_retirement_reason": repair["reason"],
                        "entry_retired_at": observed_at,
                        "terminal_ticket_hashes": repair["terminal_ticket_hashes"],
                        "terminal_ticket_statuses": repair["terminal_ticket_statuses"],
                    },
                )
            )
        repairs.append(repair)
    if repairs and not dry_run:
        store.event(
            "orphaned_pending_live_lifecycles_retired",
            {
                "count": len(repairs),
                "lifecycle_ids": [repair["lifecycle_id"] for repair in repairs],
            },
        )
    return repairs
