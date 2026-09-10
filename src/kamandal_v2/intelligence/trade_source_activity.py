"""Project canonical trade-source events to the bounded operator activity tab."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from kamandal_v2.schemas import TRADE_SOURCE_ACTIVITY_HEADER
from kamandal_v2.sheets import write_translation_review
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
        current["_event_id"] = event.get("_event_id", current.get("_event_id", 0))

    records = sorted(
        by_output.values(),
        key=lambda item: (str(item.get("observed_at") or item.get("_created_at") or ""), str(item.get("output_id") or "")),
        reverse=True,
    )
    lifecycles = store.source_activity_lifecycles(
        idea_ids={str(item.get("planner_idea_id") or "") for item in records},
        revision_ids={str(item.get("output_id") or "") for item in records if item.get("classification") == "exact_package"},
    )
    rows: list[list[Any]] = []
    for item in records:
        raw = item.get("normalized_output") or {}
        raw = raw if isinstance(raw, dict) else {}
        # Retained pre-episode receipts nested the interpretation under record.
        # Render that evidence faithfully without reinterpreting old decisions.
        if isinstance(raw.get("record"), dict):
            raw = raw["record"]
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
            "structure": item.get("structure") or raw.get("structure") or raw.get("structure_hint") or raw.get("strategy_family") or "",
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
    return _current_trade_rows(rows, event_orders={str(item["output_id"]): int(item.get("_event_id") or 0) for item in records})[:max(int(limit), 1)]


def _current_trade_rows(rows: list[list[Any]], *, event_orders: dict[str, int] | None = None) -> list[list[Any]]:
    """Fold receipt revisions for display only; retain every execution identity."""
    event_orders = event_orders or {}
    records = [dict(zip(TRADE_SOURCE_ACTIVITY_HEADER, row)) for row in rows]
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    parents: dict[tuple[str, str, str], list[tuple[str, ...]]] = {}
    pending = []
    for row in records:
        try:
            raw = json.loads(row["normalized_output"])
        except (ValueError, TypeError):
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        row["_raw"] = raw
        parent = str(raw.get("source_event_id") or "")
        signature = str(raw.get("package_signature") or "")
        if row["classification"] == "exact_package" and parent and signature:
            identity = (row["source_id"], row["post_ref"], parent)
            key = (*identity, signature)
            groups.setdefault(key, []).append(row)
            if key not in parents.setdefault(identity, []):
                parents[identity].append(key)
        else:
            pending.append(row)
    for row in pending:
        raw = row["_raw"]
        parent = str(raw.get("source_id") or "") if row["classification"] == "residual" else str(row["output_id"])
        keys = parents.get((row["source_id"], row["post_ref"], parent), [])
        if keys:
            for key in keys:
                groups[key].append(row)
        else:
            groups[("output", str(row["output_id"]))] = [row]

    result = []
    for members in groups.values():
        members.sort(key=lambda row: (_timestamp(str(row["last_update"])), event_orders.get(str(row["output_id"]), 0)))
        current = dict(members[-1])
        if len(members) > 1:
            exact = [row for row in members if row["classification"] == "exact_package"]
            ideas = [row for row in members if "idea" in str(row["classification"]).split(",")]
            latest_exact = exact[-1] if exact else None
            latest_idea = ideas[-1] if ideas else None
            exact_decisions = [row for row in members if row["classification"] in {"exact_package", "residual"}]
            exact_decision = exact_decisions[-1] if exact_decisions else latest_exact
            if latest_exact:
                current["output_id"] = latest_exact["output_id"]
                for field in ("symbol", "structure", "action", "evidence_status", "capability_support",
                              "acquisition_status", "link_status", "interpretation_confidence"):
                    current[field] = (latest_idea or {}).get(field) or latest_exact[field]
                current["interpretation"] = "; ".join(dict.fromkeys(
                    str(row["interpretation"]) for row in [latest_idea, latest_exact]
                    if row and row["interpretation"]))
            if latest_exact and latest_idea:
                for field in ("planner_disposition", "effective_mode", "reason"):
                    current[field] = " | ".join(f"{label}: {row[field]}" for label, row in
                                              [("Idea", latest_idea), ("Exact", exact_decision)] if row[field])
            current["classification"] = ",".join(sorted({kind for row in members
                                                         for kind in str(row["classification"]).split(",")}))
            for field in ("lifecycle_ids", "lifecycle_status"):
                values = {part for row in members for part in str(row[field]).split("; ")
                          if part and part != "no_linked_lifecycle"}
                current[field] = "; ".join(sorted(values)) or ("no_linked_lifecycle" if field == "lifecycle_status" else "")
            raw = dict((latest_exact or current)["_raw"])
            raw["activity_history"] = [{field: row[field] for field in
                ("output_id", "classification", "last_update", "planner_disposition", "effective_mode", "reason", "lifecycle_ids")}
                for row in members]
            current["normalized_output"] = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        result.append(current)
    result.sort(key=lambda row: (_timestamp(str(row["last_update"])), str(row["output_id"])), reverse=True)
    return [[row[column] for column in TRADE_SOURCE_ACTIVITY_HEADER] for row in result]


def project_trade_source_activity(
    config: dict[str, Any],
    store: LocalStore,
    *,
    limit: int = 500,
) -> int:
    since = str(((config.get("source_intelligence") or {}).get("translation_review") or {}).get("since") or "")
    return write_translation_review(config, translation_review_rows(store, since=since, limit=limit))


def translation_review_rows(store: LocalStore, *, since: str = "", limit: int = 500) -> list[list[Any]]:
    """One source post and its semantic interpretation, without planner receipts."""
    if since:
        _timestamp(since)  # Invalid reset boundaries fail visibly, never silently reset.
    posts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    latest_batches: dict[tuple[str, str], str] = {}
    for item in store.source_translation_observations(since=since, limit=max(limit * 20, 2000)):
        raw = item.get("normalized_output") or {}
        if not isinstance(raw, dict) or not raw.get("event_id"):
            continue  # Exact revisions and planner failures are audit evidence.
        source_id, post_ref = str(item.get("source_id") or ""), str(item.get("post_ref") or "")
        if not post_ref.removeprefix("x-post:").isdigit():
            continue
        key = (source_id, post_ref)
        batch = str(item.get("translation_batch") or "")
        latest_batches.setdefault(key, batch)
        if latest_batches[key] and batch != latest_batches[key]:
            continue  # A later interpretation may remove events from this post.
        posts.setdefault(key, []).append(raw)
    rows = []
    for (source_id, post_ref), events in list(posts.items())[:max(limit, 1)]:
        symbols, interpretations, details, uncertainties = [], [], [], []
        for event in sorted(events, key=lambda event: str(event.get("event_id"))):
            symbol = str(event.get("symbol") or "")
            if symbol:
                symbols.append(symbol)
            thesis = str(event.get("thesis") or "")
            action = str(event.get("action") or "").replace("_", " ")
            structure = str(event.get("structure_hint") or "").replace("_", " ")
            summary = " — ".join(part for part in (symbol, action, structure, thesis) if part)
            if summary:
                interpretations.append(summary)
            for package in event.get("exact_packages") or []:
                if package.get("legs"):
                    terms = _interpretation(package)
                    price = package.get("displayed_price") or {}
                    if price:
                        terms += f"; source price: {price.get('amount', '')} {price.get('effect', '')}".rstrip()
                    details.append(f"{symbol}: {terms}".lstrip(": "))
                if package.get("blocker"):
                    uncertainties.append(str(package["blocker"]))
            uncertainties.extend(_review_uncertainties(event))
        unique = lambda values: "\n".join(dict.fromkeys(value for value in values if value))
        rows.append([source_id.replace("_", " ").title(),
                     "https://x.com/i/status/" + post_ref.removeprefix("x-post:"),
                     ", ".join(dict.fromkeys(symbols)), unique(interpretations), unique(details),
                     unique(uncertainties), ""])
    return rows


def _review_uncertainties(event: dict[str, Any]) -> list[str]:
    labels = {"needs_media": "Source image or linked content is missing.",
              "needs_history": "Earlier source context is missing.",
              "lifecycle_link_missing": "Earlier source position could not be linked.",
              "exact_package_incomplete": "Exact contract details are incomplete."}
    ignored = {"planner_structure_unsupported", "outside_configured_universe", "source_too_old"}
    result = []
    status = str(event.get("evidence_status") or "")
    if status in labels:
        result.append(labels[status])
    for blocker in event.get("blockers") or []:
        text = str(blocker)
        if text in ignored:
            continue
        result.append(labels.get(text, text.replace("needs_media:", "Missing source image:").replace("needs_history:", "Missing earlier context:")))
    return result


def _interpretation(raw: dict[str, Any]) -> str:
    thesis = str(raw.get("thesis") or raw.get("summary") or raw.get("reason") or (raw.get("source_intent") or {}).get("reason") or raw.get("source_text") or "")
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
