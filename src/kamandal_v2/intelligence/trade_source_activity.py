"""Project canonical trade-source events to the bounded operator activity tab."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from kamandal_v2.domain.models import PortfolioState
from kamandal_v2.intelligence.trade_sources import compile_trade_source_policies
from kamandal_v2.intelligence.source_episode_projection import _opportunity_id
from kamandal_v2.portfolio_sleeves import compile_sleeve_policy, live_sleeve_usage
from kamandal_v2.schemas import TRADE_SOURCE_ACTIVITY_HEADER
from kamandal_v2.sheets import pull_portfolio_sleeves, pull_trade_sources, write_trade_source_brief
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
    source_rows = pull_trade_sources(config)
    source_compilation = compile_trade_source_policies(source_rows)
    sleeves = compile_sleeve_policy(pull_portfolio_sleeves(config))
    summary, decisions = chief_of_staff_rows(
        store,
        source_modes={
            (str(row.get("source_id") or ""), str(row.get("output_kind") or "")): str(row.get("mode") or "")
            for row in source_rows if row.get("source_id") and row.get("output_kind")
        },
        sleeve_policy=sleeves,
        policy_errors=source_compilation.errors,
        limit=limit,
    )
    return write_trade_source_brief(config, summary, decisions)


def chief_of_staff_rows(
    store: LocalStore,
    *,
    source_modes: dict[tuple[str, str], str],
    sleeve_policy: Any,
    policy_errors: tuple[str, ...] = (),
    limit: int = 500,
    now: datetime | None = None,
) -> tuple[list[list[Any]], list[list[Any]]]:
    """Answer operator questions from receipts, without rendering the audit payload."""
    observed = now or datetime.now(UTC)
    cutoff = observed - timedelta(days=35)
    records = [dict(zip(TRADE_SOURCE_ACTIVITY_HEADER, row)) for row in activity_rows(store, limit=max(limit, 500))]
    groups = [*store.open_live_position_groups(), *store.closed_live_position_groups(limit=1000)]
    intents = store.live_order_intents_by_type("open")
    decisions: list[list[Any]] = []
    confirmed = templates = complete_packages = entered = held = 0
    issues: Counter[str] = Counter()
    opening_groups: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for item in records:
        if str(item.get("action") or "") != "open":
            continue
        published = _post_published_at(str(item.get("post_ref") or item.get("source_url") or ""))
        if published is not None and published < cutoff:
            continue
        try:
            raw = json.loads(str(item.get("normalized_output") or "{}"))
        except ValueError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if raw.get("template_number"):
            templates += 1
            continue  # A proposed menu item is not a confirmed guru opening.
        source_id = str(item.get("source_id") or "")
        raw_group = str(raw.get("opportunity_group_id") or "")
        opportunity = ((raw_group if raw_group.startswith("corr_opp_") else _opportunity_id(raw_group)) if raw_group else str(
            raw.get("source_opportunity_id") or raw.get("event_id") or raw.get("source_event_id") or item.get("output_id") or ""
        ))
        opening_groups.setdefault((source_id, opportunity), []).append((item, raw))

    for (source_id, opportunity), members in opening_groups.items():
        item, raw = max(members, key=lambda pair: (_timestamp(str(pair[0].get("last_update") or "")), str(pair[0].get("output_id") or "")))
        confirmed += 1
        matched_groups = [
            group for group in groups
            if opportunity and str(group.get("source_opportunity_id") or group.get("idea_id") or "") == opportunity
            and (not group.get("source_id") or group.get("source_id") == source_id)
        ]
        matched_intents = [
            ticket for ticket in intents
            if opportunity and str(ticket.get("source_opportunity_id") or ticket.get("idea_id") or "") == opportunity
            and (not ticket.get("source_id") or ticket.get("source_id") == source_id)
        ]
        has_complete_package = any(
            (evidence.get("legs") and evidence.get("complete") is True and not evidence.get("blocker"))
            or any(package.get("legs") and package.get("complete") is True and not package.get("blocker")
                   for package in evidence.get("exact_packages") or [] if isinstance(package, dict))
            for member, evidence in members
        )
        complete_packages += int(has_complete_package)
        reason = str(item.get("reason") or "")
        signatures = {str(evidence.get("package_signature") or "") for _member, evidence in members if evidence.get("package_signature")}
        if len(signatures) > 1 and not matched_groups:
            reason = "multi_package_opening_requires_atomic_group"
        mode = str(item.get("effective_mode") or "observe")
        if matched_groups:
            lane = str(matched_groups[0].get("source_output_kind") or "idea")
            decision = "Entered exact" if lane == "exact_package" else "Entered idea"
            reason = "Kamandal manages the position"
            entered += 1
        elif any(str(ticket.get("_ledger_status") or "") in {"submitted", "partially_filled", "submit_uncertain"} for ticket in matched_intents):
            decision = "Submitted; awaiting fill"
            reason = "Broker status pending"
        elif any(str(ticket.get("_ledger_status") or "") in {"pending_approval", "stage_approved_pending_submit", "waiting_entry_window"} for ticket in matched_intents):
            decision = "Queued"
            reason = "Entry has not been submitted"
        elif any(token in reason for token in ("duplicate", "already_open", "superseded")):
            decision = "Duplicate"
        elif any(token in reason for token in ("risk", "bpr_cap", "health_gate")):
            decision = "Blocked by risk"
        elif "unsupported" in reason:
            decision = "Unsupported"
        elif "incomplete" in reason or str(item.get("evidence_status") or "") in {"needs_media", "needs_history", "ambiguous"}:
            decision = "Needs evidence"
        elif "stale" in reason or "source_too_old" in reason:
            decision = "Stale"
        elif "shadow" in mode.lower():
            decision = "Shadow"
        elif "off" in mode.lower() or "observe" in mode.lower():
            decision = "Observed only"
        elif "selected" in str(item.get("planner_disposition") or ""):
            decision = "Selected"
        else:
            decision = "Held"
        if decision in {"Unsupported", "Needs evidence", "Stale", "Held", "Blocked by risk", "Duplicate"}:
            held += 1
            if decision != "Stale":
                issues[_plain_reason(reason or str(item.get("evidence_status") or "unresolved"))[:80]] += 1
        package_terms = []
        for _member, evidence in members:
            packages = evidence.get("exact_packages") or []
            if evidence.get("legs"):
                packages = [evidence, *packages]
            for package in packages:
                if isinstance(package, dict) and package.get("legs"):
                    detail = _interpretation(package)
                    price = package.get("displayed_price") or {}
                    if isinstance(price, dict) and price:
                        detail += f"; source price {price.get('amount', '')} {price.get('effect', '')}".rstrip()
                    if detail and detail not in package_terms:
                        package_terms.append(detail)
        opening = " ".join(part for part in (
            str(item.get("symbol") or ""),
            str(item.get("structure") or "").replace("_", " "),
            "; ".join(package_terms) if package_terms else str(item.get("interpretation") or "") if has_complete_package else "",
        ) if part)
        source_url = str(item.get("source_url") or "")
        key = "|".join((source_id, opportunity))
        decisions.append([
            source_id.replace("_", " ").title(), source_url, opening[:700], decision,
            _plain_reason(reason)[:130], mode[:90], "", key,
        ])
    decisions.sort(key=lambda row: (_post_published_at(str(row[1])) or datetime.min.replace(tzinfo=UTC), str(row[7])), reverse=True)
    decisions = decisions[:max(min(int(limit), 150), 1)]
    snapshot = store.latest_account_snapshot(mode="live")
    capacity = "Latest account snapshot unavailable"
    snapshot_id = ""
    if snapshot:
        snapshot_id = str(snapshot.get("_snapshot_id") or "")
        try:
            account = PortfolioState(
                account_size=float(snapshot["account_size"]),
                buying_power=float(snapshot["buying_power"]),
                bpr_used=float(snapshot["bpr_used"]),
                positions_count=int(snapshot.get("positions_count") or 0),
            )
            usage = live_sleeve_usage(store, account)
            total_room = max(sleeve_policy.portfolio_total_pct - usage.portfolio_total_bpr / usage.account_size * 100, 0.0)
            capacity = "; ".join(
                f"{label} {usage.used(lane) / usage.account_size * 100:.1f}/{sleeve_policy.limit(lane):.1f}%"
                f" ({min(max(sleeve_policy.limit(lane) - usage.used(lane) / usage.account_size * 100, 0.0), total_room):.1f}% room)"
                for label, lane in (("Current", "current_idea"), ("Guru", "guru_exact"), ("Total", "portfolio_total"))
            )
            if usage.unmatched_bpr / usage.account_size >= 0.01:
                capacity += f"; Broker residual {usage.unmatched_bpr / usage.account_size * 100:.1f}% charged to Current"
            elif (usage.ledger_open_bpr - usage.broker_bpr) / usage.account_size >= 0.01:
                capacity += f"; Review ledger BPR above broker by {(usage.ledger_open_bpr - usage.broker_bpr) / usage.account_size * 100:.1f}%"
        except (KeyError, TypeError, ValueError) as exc:
            capacity = f"BPR accounting unavailable ({type(exc).__name__})"
    modes = "; ".join(
        f"{source.replace('_', ' ').title()} {kind.replace('_', ' ')}: {mode}"
        for (source, kind), mode in sorted(source_modes.items())
    )
    attention_items = [f"Source switch invalid: {_plain_reason(error)}" for error in policy_errors]
    attention_items.extend(f"{reason} ({count})" for reason, count in issues.most_common(3))
    attention = "; ".join(attention_items[:3]) or "No decision exception in this window"
    summary = [
        ["Guru trade brief", observed.isoformat(timespec="seconds"), "Window", "Last 35 calendar days"],
        ["Route switches", modes],
        ["BPR used / ceiling", capacity, "Account receipt", snapshot_id],
        ["Opening decisions", f"{confirmed} non-template openings; {templates} templates; {complete_packages} model-complete; {entered} entered; {held} exceptions"],
        ["Needs attention", attention],
    ]
    return summary, decisions


def _post_published_at(value: str) -> datetime | None:
    """An X post id carries its creation time; polling time is not trade time."""
    post_id = value.rsplit("/", 1)[-1].removeprefix("x-post:")
    if not post_id.isdigit() or len(post_id) < 16:
        return None
    milliseconds = (int(post_id) >> 22) + 1288834974657
    try:
        return datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _plain_reason(value: str) -> str:
    aliases = {
        "planner_structure_unsupported": "Structure understood; no executable playbook",
        "source_too_old": "Source opening is stale",
        "outside_configured_universe": "Symbol outside configured universe",
        "unsupported_live_exact_structure": "Exact structure lacks live execution support",
    }
    return aliases.get(value, value.replace("_", " ").strip())


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
