"""Versioned, read-only lifecycle history projection for downstream research."""

from __future__ import annotations

import json
from typing import Any

from kamandal_v2.strategy_lanes.models import LifecycleState
from kamandal_v2.strategy_lanes.store import CsaStore


HISTORY_SCHEMA_VERSION = "kamandal.lifecycle-history.v1"


def lifecycle_history(store: CsaStore, *, lifecycle_id: str | None = None) -> list[dict[str, Any]]:
    """Return deterministic lifecycle records without reading Sheets or brokers."""
    rows = store.rows("csa_lifecycles")
    records: list[dict[str, Any]] = []
    for row in rows:
        lifecycle = _lifecycle_from_row(row)
        if lifecycle_id and lifecycle.lifecycle_id != lifecycle_id:
            continue
        records.append(
            history_record(
                lifecycle,
                actions=_payloads_for_lifecycle(store.rows("csa_actions"), lifecycle.lifecycle_id),
                tickets=_payloads_for_lifecycle(store.rows("csa_shadow_order_intents"), lifecycle.lifecycle_id),
                fills=_payloads_for_lifecycle(store.rows("csa_shadow_fills"), lifecycle.lifecycle_id),
            )
        )
    return sorted(records, key=lambda record: (record["opened_at"], record["lifecycle_id"]))


def history_record(
    lifecycle: LifecycleState,
    *,
    actions: list[dict[str, Any]] | None = None,
    tickets: list[dict[str, Any]] | None = None,
    fills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Serialize one lifecycle with explicit economics and evidence quality."""
    metadata = dict(lifecycle.metadata)
    ledger = [dict(item) for item in lifecycle.cashflow_ledger]
    cashflow = round(sum(float(item.get("amount") or 0.0) for item in ledger), 6)
    is_closed = lifecycle.status == "closed"
    realized = metadata.get("realized_pnl_price") if is_closed else None
    mark = metadata.get("mark_pnl_price") if not is_closed else None
    missing: list[str] = []
    if not metadata.get("compiled_management_policy") and not metadata.get("policy"):
        missing.append("compiled_policy")
    if not lifecycle.opportunity_id:
        missing.append("source_identity")
    if not ledger:
        missing.append("cashflow_ledger")
    if not is_closed and mark is None:
        missing.append("open_mark")
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "lifecycle_id": lifecycle.lifecycle_id,
        "status": lifecycle.status,
        "lane": lifecycle.lane.value,
        "mode": str(metadata.get("execution_mode") or "unknown"),
        "opened_at": lifecycle.opened_at,
        "updated_at": lifecycle.updated_at,
        "source": {
            "opportunity_id": lifecycle.opportunity_id,
            "underlying": str(metadata.get("underlying") or ""),
            "playbook_id": str(metadata.get("playbook_id") or ""),
            "candidate_id": str(metadata.get("candidate_id") or ""),
            "legacy_source_id": str(metadata.get("legacy_source_id") or ""),
        },
        "policy": {
            "hash": lifecycle.policy_hash,
            "at_adoption": bool(metadata.get("policy_at_adoption")),
            "compiled": metadata.get("compiled_management_policy") or metadata.get("policy") or {},
        },
        "active_legs": [dict(item) for item in lifecycle.active_legs],
        "cashflow_ledger": ledger,
        "economics": {
            "cashflow_total": cashflow,
            "state": "realized" if is_closed else "open_mark",
            "realized_pnl_price": realized,
            "mark_pnl_price": mark,
            "mark_source": metadata.get("mark_source") if not is_closed else None,
            "opening_credit": metadata.get("opening_credit"),
            "cumulative_credit": metadata.get("cumulative_credit"),
            "profit_target_dollars": metadata.get("profit_target_dollars"),
            "adjustment_count": int(metadata.get("adjustment_count") or 0),
        },
        "actions": _ordered(actions or []),
        "tickets": _ordered(tickets or []),
        "fills": _ordered(fills or []),
        "evidence_quality": "complete" if not missing else "incomplete",
        "evidence_limitations": missing,
    }


def _lifecycle_from_row(row: dict[str, Any]) -> LifecycleState:
    from kamandal_v2.strategy_lanes.store import _lifecycle_from_payload

    return _lifecycle_from_payload(json.loads(str(row["payload"])))


def _payloads_for_lifecycle(rows: list[dict[str, Any]], lifecycle_id: str) -> list[dict[str, Any]]:
    payloads = []
    for row in rows:
        if str(row.get("lifecycle_id") or "") != lifecycle_id:
            continue
        payloads.append(json.loads(str(row["payload"])))
    return _ordered(payloads)


def _ordered(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(item) for item in items), key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), default=str))
