"""Project canonical trade-source events to the bounded operator activity tab."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from kamandal_v2.schemas import TRADE_SOURCE_ACTIVITY_HEADER
from kamandal_v2.sheets import write_trade_source_activity
from kamandal_v2.stores.sqlite import LocalStore


def activity_rows(store: LocalStore, *, limit: int = 500) -> list[list[Any]]:
    events = store.recent_events(
        (
            "trade_source_output_observed",
            "trade_source_planner_disposition",
            "observed_package_planner_receipt",
        ),
        limit=max(int(limit) * 4, 500),
    )
    by_output: dict[str, dict[str, Any]] = {}
    idea_to_output: dict[str, str] = {}
    for event in events:
        event_type = str(event.get("_event_type") or "")
        if event_type == "trade_source_output_observed":
            output_id = str(event.get("output_id") or "")
            if not output_id:
                continue
            by_output[output_id] = dict(event)
            planner_idea_id = str(event.get("planner_idea_id") or "")
            if planner_idea_id:
                idea_to_output[planner_idea_id] = output_id
            continue
        if event_type == "trade_source_planner_disposition":
            output_id = idea_to_output.get(str(event.get("idea_id") or ""), "")
        else:
            output_id = str(event.get("evidence_revision_id") or "")
        if not output_id or output_id not in by_output:
            continue
        current = by_output[output_id]
        current["planner_disposition"] = str(event.get("status") or current.get("planner_disposition") or "")
        current["reason"] = str(event.get("blocker") or event.get("reason") or current.get("reason") or "")
        if event.get("playbook_id"):
            current["capability_support"] = "supported"
        elif current["reason"] == "unsupported":
            current["capability_support"] = "unsupported"
        elif current["reason"] == "ambiguous_playbook_match":
            current["capability_support"] = "ambiguous"
        if event.get("mode"):
            current["effective_mode"] = str(event["mode"])
        current["_created_at"] = str(event.get("_created_at") or current.get("_created_at") or "")

    records = sorted(
        by_output.values(),
        key=lambda item: (str(item.get("observed_at") or item.get("_created_at") or ""), str(item.get("output_id") or "")),
        reverse=True,
    )[: max(int(limit), 1)]
    lifecycles = store.source_activity_lifecycles(
        idea_ids={str(item.get("planner_idea_id") or "") for item in records},
        revision_ids={str(item.get("output_id") or "") for item in records if item.get("classification") == "exact_package"},
    )
    rows: list[list[Any]] = []
    for item in records:
        raw = item.get("normalized_output") or {}
        raw = raw if isinstance(raw, dict) else {}
        matched = []
        for lifecycle in lifecycles:
            identity = (lifecycle.get("metadata") or {}).get("source_identity") or {}
            if ((item.get("planner_idea_id") and identity.get("idea_id") == item["planner_idea_id"])
                or (item.get("classification") == "exact_package" and identity.get("evidence_revision_id") == item.get("output_id"))):
                matched.append(lifecycle)
        matched.sort(key=lambda value: (str(value.get("updated_at") or ""), str(value.get("lifecycle_id") or "")))
        post_id = str(item.get("post_ref") or "").removeprefix("x-post:")
        normalized = item.get("normalized_output")
        if not isinstance(normalized, str):
            normalized = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
        row = {
            "observed_at": item.get("observed_at") or item.get("_created_at") or "",
            "source_id": item.get("source_id") or item.get("source_profile") or "",
            "post_ref": item.get("post_ref") or item.get("canonical_post_id") or "",
            "output_id": item.get("output_id") or "",
            "acquisition_status": item.get("acquisition_status") or "missing",
            "classification": item.get("classification") or "residual",
            "normalized_output": normalized,
            "action": item.get("action") or raw.get("action") or "",
            "symbol": item.get("symbol") or raw.get("symbol") or raw.get("underlying") or "",
            "structure": item.get("structure") or raw.get("structure") or raw.get("structure_hint") or "",
            "link_status": item.get("link_state") or item.get("link_status") or "",
            "evidence_status": item.get("evidence_status") or "",
            "interpretation_confidence": (
                (item.get("normalized_output") or {}).get("semantic_confidence", "")
                if isinstance(item.get("normalized_output"), dict)
                else ""
            ),
            "capability_support": item.get("capability_support") or "unknown",
            "planner_disposition": item.get("planner_disposition") or "observed",
            "effective_mode": item.get("effective_mode") or "observe",
            "reason": item.get("reason") or "",
            "source_url": f"https://x.com/i/status/{post_id}" if post_id.isdigit() else "",
            "interpretation": _interpretation(raw),
            "lifecycle_status": "; ".join(
                f"{(life.get('metadata') or {}).get('execution_mode', 'unknown')}:{life.get('status', 'unknown')}"
                for life in matched
            ) or "no_linked_lifecycle",
            "lifecycle_ids": "; ".join(str(life.get("lifecycle_id") or "") for life in matched),
            "last_update": max([str(item.get("_created_at") or ""), *[str(life.get("updated_at") or "") for life in matched]], key=_timestamp),
        }
        rows.append([row[column] for column in TRADE_SOURCE_ACTIVITY_HEADER])
    return rows


def project_trade_source_activity(
    config: dict[str, Any],
    store: LocalStore,
    *,
    limit: int = 500,
) -> int:
    return write_trade_source_activity(
        config,
        activity_rows(store, limit=limit),
        TRADE_SOURCE_ACTIVITY_HEADER,
    )


def _interpretation(raw: dict[str, Any]) -> str:
    thesis = str(raw.get("thesis") or raw.get("summary") or raw.get("reason") or "")
    legs = raw.get("legs") or []
    if legs:
        terms = []
        for leg in legs:
            code = leg.get("order_code") or f"{leg.get('side', '')} {leg.get('effect', '')}".strip()
            terms.append(f"{code} {leg.get('quantity', '')} {leg.get('expiration', '')} {leg.get('strike', '')} {leg.get('option_type', '')}".strip())
        return "; ".join(filter(None, [thesis, *terms]))
    return thesis


def _timestamp(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
