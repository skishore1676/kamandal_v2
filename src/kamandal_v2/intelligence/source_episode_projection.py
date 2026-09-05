"""Project one source-episode interpretation into Kamandal's existing seams.

This module is deliberately an adapter, not a second planner.  It turns atomic
source events into the already-supported planner-idea YAML and observed-package
feed.  Source policy, portfolio selection, lifecycle creation, and broker
effects remain downstream.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from kamandal_v2.intelligence.correspondent_signals import (
    _chart_activation,
    _load_chart_evaluations,
    _directional_structures,
)
from kamandal_v2.intelligence.observed_packages import (
    ObservedLegEvidence,
    ObservedPackageBatch,
    ObservedPackageEvidence,
    _normalize_decimal,
    _normalize_expiration,
    _package_signature,
    _product_type,
)
from kamandal_v2.intelligence.source_episode_compiler import (
    PROMPT_VERSION,
    SourceEpisodeCompilation,
)


@dataclass(frozen=True, slots=True)
class SourceEpisodeProjection:
    planner_ideas: tuple[dict[str, Any], ...]
    observed_batches: tuple[ObservedPackageBatch, ...]
    observations: tuple[dict[str, Any], ...]
    failures: tuple[dict[str, str], ...]


def project_source_episode_compilation(
    compilation: SourceEpisodeCompilation,
    packet: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    universe_symbols: Iterable[str],
    chart_evaluation_paths: Iterable[str | Path] = (),
) -> SourceEpisodeProjection:
    """Create effect-free projections from one validated compilation."""

    universe = {str(item).strip().upper() for item in universe_symbols if str(item).strip()}
    records = {
        str(item.get("signal_id") or ""): item
        for item in packet.get("records") or []
        if isinstance(item, Mapping)
    }
    charts, _chart_hashes = _load_chart_evaluations(chart_evaluation_paths)
    ideas: list[dict[str, Any]] = []
    batches: list[ObservedPackageBatch] = []
    observations: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    for episode in compilation.episodes:
        post_ref = str(episode.get("post_ref") or "")
        record = records.get(post_ref)
        if record is None:
            failures.append({"source_id": post_ref, "reason": "source_record_missing"})
            continue
        exact_packages: list[ObservedPackageEvidence] = []
        for event in episode.get("events") or []:
            if not isinstance(event, Mapping):
                continue
            dispositions = {
                str(item.get("projection") or ""): dict(item)
                for item in event.get("projection_dispositions") or []
                if isinstance(item, Mapping)
            }
            event_reasons: list[str] = [str(item) for item in event.get("blockers") or []]
            idea_disposition = dispositions.get("idea") or {}
            exact_disposition = dispositions.get("exact_package") or {}

            if idea_disposition.get("disposition") == "ready_for_source_policy":
                idea, blocker = _idea_projection(
                    event,
                    record,
                    profile,
                    universe=universe,
                    charts=charts,
                    as_of=compilation.compiled_at,
                )
                if idea is not None:
                    ideas.append(idea)
                elif blocker:
                    event_reasons.append(blocker)

            if exact_disposition.get("disposition") == "ready_for_source_policy":
                exact_age_blocker = _age_blocker(record, profile, compilation.compiled_at)
                if exact_age_blocker:
                    projected, exact_failures = [], [{
                        "source_id": str(event.get("event_id") or ""),
                        "reason": exact_age_blocker,
                    }]
                else:
                    projected, exact_failures = _exact_package_projections(
                        event,
                        record,
                        compilation=compilation,
                    )
                exact_packages.extend(projected)
                failures.extend(exact_failures)
                event_reasons.extend(item["reason"] for item in exact_failures)

            observations.append(
                {
                    "source_id": compilation.profile_id,
                    "post_ref": post_ref,
                    "event_id": str(event.get("event_id") or ""),
                    "opportunity_group_id": str(event.get("opportunity_group_id") or ""),
                    "action": str(event.get("action") or ""),
                    "symbol": str(event.get("symbol") or ""),
                    "structure": str(event.get("structure_hint") or ""),
                    "evidence_status": str(event.get("evidence_status") or ""),
                    "link_state": str(event.get("link_state") or ""),
                    "classification": ",".join(str(item) for item in event.get("projections") or ["residual"]),
                    "planner_disposition": "published" if any(
                        idea["idea_id"] == _opportunity_id(str(event.get("opportunity_group_id") or ""))
                        for idea in ideas
                    ) else "parked",
                    "reason": ",".join(dict.fromkeys(item for item in event_reasons if item)),
                    "normalized_output": dict(event),
                }
            )

        if exact_packages:
            output_sha = _sha(_stable_json([item.to_dict() for item in exact_packages]))
            exact_packages = [replace(item, output_sha256=output_sha) for item in exact_packages]
            batches.append(
                ObservedPackageBatch(
                    source_profile=compilation.profile_id,
                    canonical_post_id=post_ref,
                    post_disposition="packages",
                    post_blocker=None,
                    packages=tuple(exact_packages),
                    prompt_sha256=compilation.prompt_sha256,
                    output_sha256=output_sha,
                )
            )

    return SourceEpisodeProjection(
        planner_ideas=tuple(_dedupe(ideas, key="idea_id")),
        observed_batches=tuple(batches),
        observations=tuple(observations),
        failures=tuple(failures),
    )


def _idea_projection(
    event: Mapping[str, Any],
    record: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    universe: set[str],
    charts: Mapping[tuple[str, str], dict[str, Any]],
    as_of: str,
) -> tuple[dict[str, Any] | None, str]:
    symbol = str(event.get("symbol") or "").upper()
    structure = str(event.get("structure_hint") or "")
    direction = str(event.get("direction") or "neutral")
    if not symbol or symbol not in universe:
        return None, "outside_configured_universe"
    allowed = _allowed_structures(structure, direction, profile)
    if not allowed:
        return None, "planner_structure_unsupported"

    classification = str(((record.get("classification") or {}).get("type") or ""))
    family = (profile.get("families") or {}).get(classification) or {}
    if age_blocker := _age_blocker(record, profile, as_of):
        return None, age_blocker
    planner = dict(family.get("planner") or {})
    if not planner:
        planner = dict((((profile.get("families") or {}).get("trade_journal") or {}).get("planner") or {}))
    if family.get("mode") == "chart_watch":
        chart = charts.get((str(record.get("signal_id") or ""), symbol))
        if chart is None:
            return None, "chart_evaluation_missing"
        evaluation = chart["evaluation"]
        activation = _chart_activation(evaluation)
        if evaluation.get("evaluation_status") != "evaluated" or activation.get("status") != "triggered":
            return None, "chart_trigger_not_confirmed"
        chart_direction = str(evaluation.get("direction") or direction)
        if chart_direction in {"bullish", "bearish"}:
            direction = chart_direction
            allowed = _directional_structures(planner, direction, fallback=allowed)

    group = str(event.get("opportunity_group_id") or "")
    idea_id = _opportunity_id(group)
    literal = record.get("literal") or {}
    tags = list(
        dict.fromkeys(
            [
                *[str(item) for item in planner.get("thesis_tags") or []],
                f"correspondent:{profile['profile_id']}",
                "source_episode",
                *(
                    [f"template_number:{event['template_number']}"]
                    if event.get("template_number") is not None
                    else []
                ),
            ]
        )
    )
    horizon = int(planner.get("horizon_days") or 30)
    return (
        {
            "idea_id": idea_id,
            "source": f"correspondent:{profile['profile_id']}:{record['signal_id']}",
            "underlying": symbol,
            "direction": direction,
            "strategy_hint": "",
            "mentioned_strategy": allowed[0] if len(allowed) == 1 else "",
            "allowed_structures": allowed,
            "thesis_tags": tags,
            "horizon_days": horizon,
            "trade_horizon_days": horizon,
            "confidence": f"semantic:{float(event.get('semantic_confidence') or 0):.2f}",
            "extraction_confidence": f"semantic:{float(event.get('semantic_confidence') or 0):.2f}",
            "quote_evidence": str(literal.get("text") or "")[:2000],
            "extraction_notes": (
                f"Source episode {event.get('event_id')}; opportunity_group_id={group}; "
                f"evidence_status={event.get('evidence_status')}; source_structure={structure}; "
                f"idea_reexpression={str(structure not in allowed).lower()}"
            ),
            "operator_status": "pending",
            "notes": (f"post_ref={record['signal_id']}; action={event.get('action')}; source_episode=true; "
                      f"source_structure={structure}; idea_reexpression={str(structure not in allowed).lower()}"),
        },
        "",
    )


def _allowed_structures(structure: str, direction: str, profile: Mapping[str, Any]) -> list[str]:
    reexpression = (profile.get("idea_reexpressions") or {}).get(structure) or {}
    if reexpression.get("direction") == direction:
        return [str(value) for value in reexpression.get("allowed_structures") or [] if str(value)]
    for rule in profile.get("strategy_rules") or []:
        if str(rule.get("strategy_family") or "") != structure:
            continue
        allowed = [str(item) for item in rule.get("allowed_structures") or [] if str(item)]
        return allowed
    family = ((profile.get("families") or {}).get("trade_journal") or {}).get("planner") or {}
    return _directional_structures(dict(family), direction, fallback=[])


def _age_blocker(record: Mapping[str, Any], profile: Mapping[str, Any], as_of: str) -> str:
    classification = str(((record.get("classification") or {}).get("type") or ""))
    family = (profile.get("families") or {}).get(classification) or {}
    maximum = family.get("max_age_hours")
    if maximum is None:
        return ""
    observed = datetime.fromisoformat(str(as_of).replace("Z", "+00:00")).astimezone(UTC)
    published = datetime.fromisoformat(
        str(((record.get("source") or {}).get("published_at") or "")).replace("Z", "+00:00")
    ).astimezone(UTC)
    age_hours = (observed - published).total_seconds() / 3600
    if age_hours < -1:
        return "source_timestamp_in_future"
    if age_hours > float(maximum):
        return "source_too_old"
    return ""


def _exact_package_projections(
    event: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    compilation: SourceEpisodeCompilation,
) -> tuple[list[ObservedPackageEvidence], list[dict[str, str]]]:
    action = str(event.get("action") or "")
    if action not in {"open", "close", "roll", "adjust"}:
        return [], [{"source_id": str(event.get("event_id") or ""), "reason": f"exact_action_unsupported:{action}"}]
    source = record.get("source") or {}
    media = source.get("media") if isinstance(source.get("media"), list) else []
    media_by_index = {
        int(item.get("media_index") or 0): item
        for item in media
        if isinstance(item, Mapping)
    }
    publication_date = datetime.fromisoformat(
        str(source.get("published_at") or "").replace("Z", "+00:00")
    ).astimezone(UTC).date()
    results: list[ObservedPackageEvidence] = []
    failures: list[dict[str, str]] = []
    positions: dict[int, int] = {}
    for raw in event.get("exact_packages") or []:
        if not isinstance(raw, Mapping) or raw.get("complete") is not True:
            continue
        image_refs = [
            int(match.group(1))
            for item in raw.get("field_provenance") or []
            if (match := re.fullmatch(r"image:(\d+)", str(item).strip().lower()))
        ]
        if len(set(image_refs)) != 1:
            failures.append({
                "source_id": str(event.get("event_id") or ""),
                "reason": "exact_package_requires_one_verified_image_locator",
            })
            continue
        media_index = image_refs[0]
        descriptor = media_by_index.get(media_index)
        if not _verified_media(descriptor):
            failures.append({
                "source_id": str(event.get("event_id") or ""),
                "reason": f"media_{media_index}_not_verified_public_photo",
            })
            continue
        try:
            legs = tuple(_observed_leg(item, publication_date) for item in raw.get("legs") or [])
            if not legs:
                raise ValueError("complete exact package requires legs")
            if action == "open" and {leg.effect for leg in legs} != {"open"}:
                raise ValueError("opening package contains non-open leg")
            positions[media_index] = positions.get(media_index, 0) + 1
            signature = _package_signature(legs)
            image_sha = str(descriptor.get("sha256") or "").lower()
            event_id = str(event.get("event_id") or "")
            revision = "orev_" + _sha(
                _stable_json(
                    {
                        "source_event_id": event_id,
                        "package_signature": signature,
                        "image_sha256": image_sha,
                        "prompt_sha256": compilation.prompt_sha256,
                    }
                )
            )[:24]
            results.append(
                ObservedPackageEvidence(
                    source_event_id=event_id,
                    source_profile=compilation.profile_id,
                    canonical_post_id=str(record.get("signal_id") or ""),
                    media_index=media_index,
                    package_position=positions[media_index],
                    action=action,
                    structure=str(event.get("structure_hint") or "") or None,
                    symbol=str(event.get("symbol") or "").upper(),
                    product_type=_product_type(str(event.get("symbol") or "").upper()),
                    displayed_trade_time=None,
                    displayed_price=dict(raw["displayed_price"]) if raw.get("displayed_price") else None,
                    complete=True,
                    blocker=None,
                    legs=legs,
                    package_signature=signature,
                    evidence_revision_id=revision,
                    image_sha256=image_sha,
                    prompt_sha256=compilation.prompt_sha256,
                    output_sha256="pending",
                    opportunity_group_id=_opportunity_id(str(event.get("opportunity_group_id") or "")),
                    prompt_version=PROMPT_VERSION,
                )
            )
        except (TypeError, ValueError) as exc:
            failures.append({
                "source_id": str(event.get("event_id") or ""),
                "reason": f"exact_package_projection_failed:{type(exc).__name__}:{exc}",
            })
    return results, failures


def _observed_leg(raw: Any, publication_date: Any) -> ObservedLegEvidence:
    if not isinstance(raw, Mapping):
        raise ValueError("exact package leg must be an object")
    order_code = str(raw.get("order_code") or "").upper()
    if order_code not in {"BTO", "STO", "BTC", "STC"}:
        raise ValueError("invalid order code")
    side, effect = {
        "BTO": ("buy", "open"),
        "STO": ("sell", "open"),
        "BTC": ("buy", "close"),
        "STC": ("sell", "close"),
    }[order_code]
    quantity = raw.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("invalid quantity")
    expiration = _normalize_expiration(str(raw.get("expiration") or ""), publication_date)
    strike = _normalize_decimal(raw.get("strike"))
    option_type = str(raw.get("option_type") or "").lower()
    if strike is None or option_type not in {"call", "put"}:
        raise ValueError("invalid option contract")
    return ObservedLegEvidence(quantity, expiration, strike, option_type, order_code, side, effect)


def _verified_media(raw: Any) -> bool:
    if not isinstance(raw, Mapping) or raw.get("type") != "photo" or raw.get("cache_status") != "cached":
        return False
    path = Path(str(raw.get("artifact_path") or "")).expanduser().resolve()
    expected = str(raw.get("sha256") or "").lower()
    return path.is_file() and len(expected) == 64 and _file_sha(path) == expected


def _opportunity_id(group: str) -> str:
    return "corr_opp_" + _sha(group or "missing")[:16]


def _dedupe(items: list[dict[str, Any]], *, key: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        identity = str(item.get(key) or "")
        if identity not in seen:
            result.append(item)
            seen.add(identity)
    return result


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
