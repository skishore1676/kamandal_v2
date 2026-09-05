"""Compile sanitized source posts into atomic, effect-free source episodes.

The compiler owns interpretation and lifecycle linkage only.  It does not
publish planner ideas, write the operator Sheet, create shadow/live state, or
call a broker.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from pydantic import TypeAdapter

from kamandal_v2.intelligence.llm_client import JsonLlmClient

EPISODE_COMPILATION_SCHEMA = "kamandal.source_episode_compilation.v1"
EPISODE_SCHEMA = "kamandal.source_episode.v1"
PROMPT_SCHEMA = "kamandal.source_episode_interpretation.v1"
PROMPT_VERSION = "source-episode-compiler-v1"

_ACTIONS = {
    "open",
    "scale_in",
    "close",
    "scale_out",
    "roll",
    "adjust",
    "hold",
    "commentary",
    "discovery",
}
_FOLLOW_UP_ACTIONS = {"close", "scale_out", "roll", "adjust", "hold"}
_ENTRY_ACTIONS = {"open", "scale_in"}
_DIRECTIONS = {"bullish", "bearish", "neutral", "unknown"}
_EVIDENCE_STATES = {"complete", "needs_media", "needs_history", "ambiguous", "unsupported"}
_PROJECTIONS = {"idea", "exact_package", "residual"}
_OPTION_TYPES = {"call", "put"}
_ORDER_CODES = {"BTO", "STO", "BTC", "STC"}
_SYMBOL = re.compile(r"^/?[A-Z][A-Z0-9./-]{0,19}$")
_SLUG = re.compile(r"[^a-z0-9]+")
_TIMESTAMP = TypeAdapter(datetime)


@dataclass(frozen=True, slots=True)
class SourceEpisodeCompilation:
    profile_id: str
    profile_version: str
    compiled_at: str
    source_packet_sha256: str
    prompt_sha256: str
    episodes: tuple[dict[str, Any], ...]
    model_receipts: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EPISODE_COMPILATION_SCHEMA,
            "compiler_version": PROMPT_VERSION,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "compiled_at": self.compiled_at,
            "source_packet_sha256": self.source_packet_sha256,
            "prompt_sha256": self.prompt_sha256,
            "episodes": list(self.episodes),
            "model_receipts": list(self.model_receipts),
            "effects": _effects(),
        }


def compile_source_episode_packet(
    packet: Mapping[str, Any],
    profile: Mapping[str, Any],
    client: JsonLlmClient | None,
    *,
    history: Iterable[Mapping[str, Any]] = (),
) -> SourceEpisodeCompilation:
    """Interpret one already-validated Birdclaw packet.

    Deterministic bundle/noise rules run before the model. Remaining posts are
    interpreted in one bounded multimodal turn, followed by at most one repair
    turn when deterministic validation rejects the response.
    """

    profile_id = _required_slug(profile.get("profile_id"), "profile_id")
    profile_version = str(profile.get("version") or "1")
    records = packet.get("records")
    if not isinstance(records, list):
        raise ValueError("source episode packet records must be an array")
    source_packet_sha256 = _sha256(_stable_json(packet))
    compiled_at = str(packet.get("generated_at") or "")
    _TIMESTAMP.validate_python(compiled_at)

    deterministic: dict[str, dict[str, Any]] = {}
    reusable = {
        str(item.get("post_ref") or ""): dict(item)
        for item in history
        if isinstance(item, Mapping)
        and item.get("schema") == EPISODE_SCHEMA
        and str(item.get("profile_version") or "") == profile_version
    }
    reused: dict[str, dict[str, Any]] = {}
    model_records: list[Mapping[str, Any]] = []
    image_map: dict[str, list[int]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("source episode record must be an object")
        signal_id = _required_text(record.get("signal_id"), "signal_id")
        prior = reusable.get(signal_id)
        if prior is not None and prior.get("source_record_sha256") == _sha256(_stable_json(record)):
            reused[signal_id] = prior
            continue
        result = _deterministic_episode(record, profile)
        if result is not None:
            deterministic[signal_id] = result
            continue
        model_records.append(record)

    history_context = _bounded_history(history, profile)
    system_prompt = _system_prompt(profile)
    prompt_material: list[str] = []
    model_receipts: list[dict[str, Any]] = []
    interpreted: dict[str, dict[str, Any]] = {}
    batch_size = max(
        1,
        min(30, int(((profile.get("episode_interpreter") or {}).get("max_records_per_turn") or 20))),
    )
    for start in range(0, len(model_records), batch_size):
        if client is None:
            raise RuntimeError("source episode interpreter client is unavailable")
        chunk = model_records[start : start + batch_size]
        chunk_images: list[str] = []
        chunk_image_map: dict[str, list[int]] = {}
        for record in chunk:
            signal_id = str(record["signal_id"])
            indexes: list[int] = []
            for path in _verified_public_images(record):
                chunk_images.append(path)
                indexes.append(len(chunk_images))
            chunk_image_map[signal_id] = indexes
            image_map[signal_id] = indexes
        user_prompt = _user_prompt(chunk, image_map=chunk_image_map, history=history_context)
        prompt_material.append(user_prompt)
        raw = client.chat_json(system_prompt, user_prompt, images=tuple(chunk_images))
        suffix = "" if len(model_records) <= batch_size else f":{start // batch_size + 1}"
        model_receipts.append(_client_receipt(client, pass_name=f"interpret{suffix}"))
        expected_ids = {str(item["signal_id"]) for item in chunk}
        try:
            normalized = _normalize_response(raw, expected_ids=expected_ids)
        except ValueError as exc:
            repair_prompt = _repair_prompt(user_prompt, raw=raw, error=str(exc))
            raw = client.chat_json(system_prompt, repair_prompt, images=tuple(chunk_images))
            model_receipts.append(_client_receipt(client, pass_name=f"repair{suffix}"))
            normalized = _normalize_response(raw, expected_ids=expected_ids)
        interpreted.update(normalized)
    prompt_sha256 = _sha256("\n\n".join([system_prompt, *prompt_material]))

    by_signal = {**deterministic, **interpreted}
    ordered_records = sorted(
        records,
        key=lambda item: (
            _timestamp_value(((item.get("source") or {}).get("published_at"))),
            str(item.get("signal_id") or ""),
        ),
    )
    active = _active_history(history_context)
    episodes: list[dict[str, Any]] = []
    for record in ordered_records:
        signal_id = str(record["signal_id"])
        if signal_id in reused:
            episodes.append(reused[signal_id])
            continue
        normalized = by_signal.get(signal_id)
        if normalized is None:
            raise ValueError(f"source episode response missing signal_id: {signal_id}")
        episode = _finalize_episode(
            record,
            normalized,
            profile_id=profile_id,
            profile_version=profile_version,
            active=active,
            media_available=bool(image_map.get(signal_id)),
            image_numbers=image_map.get(signal_id, []),
            profile=profile,
        )
        episodes.append(episode)

    return SourceEpisodeCompilation(
        profile_id=profile_id,
        profile_version=profile_version,
        compiled_at=compiled_at,
        source_packet_sha256=source_packet_sha256,
        prompt_sha256=prompt_sha256,
        episodes=tuple(episodes),
        model_receipts=tuple(model_receipts),
    )


def load_episode_history(root: str | Path, profile_id: str, *, limit: int = 40) -> tuple[dict[str, Any], ...]:
    path = Path(root).expanduser().resolve() / "latest" / profile_id
    if not path.is_dir():
        return ()
    episodes: list[dict[str, Any]] = []
    for item in sorted(path.glob("*.json"), key=lambda value: value.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("schema") == EPISODE_SCHEMA:
            episodes.append(payload)
        if len(episodes) >= limit:
            break
    return tuple(episodes)


def write_episode_compilation(compilation: SourceEpisodeCompilation, root: str | Path) -> Path:
    base = Path(root).expanduser().resolve()
    payload = compilation.to_dict()
    run_id = _sha256(_stable_json(payload))[:16]
    run_path = base / "runs" / compilation.profile_id / f"{run_id}.json"
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _write_idempotent(run_path, content)
    latest_root = base / "latest" / compilation.profile_id
    for episode in compilation.episodes:
        episode_path = latest_root / f"{episode['episode_id']}.json"
        _write_replace(episode_path, json.dumps(episode, indent=2, sort_keys=True) + "\n")
    return run_path


def _deterministic_episode(record: Mapping[str, Any], profile: Mapping[str, Any]) -> dict[str, Any] | None:
    literal = record.get("literal") or {}
    classification = record.get("classification") or {}
    text = str(literal.get("text") or "")
    signal_id = _required_text(record.get("signal_id"), "signal_id")
    family = (profile.get("families") or {}).get(str(classification.get("type") or "")) or {}

    if _obvious_noise(text, classification=str(classification.get("type") or "")):
        return {"signal_id": signal_id, "events": []}

    numbers = family.get("bundle_idea_numbers")
    if isinstance(numbers, list) and numbers:
        symbols = _record_symbols(record, profile)
        events: list[dict[str, Any]] = []
        mapping = family.get("idea_number_map") or {}
        for symbol in symbols:
            for number in numbers:
                rule = mapping.get(str(number)) or {}
                structure = str(rule.get("strategy_family") or "")
                supported = bool(rule.get("planner_supported", bool(rule.get("allowed_structures"))))
                events.append(
                    {
                        "action": "open",
                        "symbol": symbol,
                        "direction": str(rule.get("direction") or "neutral"),
                        "structure_hint": structure,
                        "thesis": f"Source template idea {number}",
                        "semantic_confidence": 1.0,
                        "evidence_status": "complete",
                        "projections": ["idea"],
                        "exact_packages": [],
                        "blockers": [] if supported else ["planner_structure_unsupported"],
                        "template_number": int(number),
                    }
                )
        return {"signal_id": signal_id, "events": events}

    confirmation_regex = str(((profile.get("episode_interpreter") or {}).get("open_confirmation_regex") or ""))
    if confirmation_regex and re.search(confirmation_regex, text, flags=re.IGNORECASE):
        symbols = _record_symbols(record, profile)
        number_match = re.search(r"(?:idea|#)\s*#?\s*(\d+)", text, flags=re.IGNORECASE)
        number = int(number_match.group(1)) if number_match else None
        structure = ""
        for configured_family in (profile.get("families") or {}).values():
            mapped = (configured_family or {}).get("idea_number_map") or {}
            if number is not None and str(number) in mapped:
                structure = str(mapped[str(number)].get("strategy_family") or "")
                break
        return {
            "signal_id": signal_id,
            "events": [
                {
                    "action": "open",
                    "symbol": symbol,
                    "direction": "neutral",
                    "structure_hint": structure,
                    "thesis": "Source confirmed taking a previously published idea",
                    "semantic_confidence": 1.0,
                    "evidence_status": "needs_history",
                    "projections": ["residual"],
                    "exact_packages": [],
                    "blockers": ["source_open_confirmation_requires_prior_opportunity"],
                    "template_number": number,
                }
                for symbol in symbols
            ],
        }
    return None


def _normalize_response(raw: Mapping[str, Any], *, expected_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping) or raw.get("schema") != PROMPT_SCHEMA:
        raise ValueError(f"source episode response schema must be {PROMPT_SCHEMA}")
    raw_episodes = raw.get("episodes")
    if not isinstance(raw_episodes, list):
        raise ValueError("source episode response episodes must be an array")
    result: dict[str, dict[str, Any]] = {}
    for item in raw_episodes:
        if not isinstance(item, Mapping):
            raise ValueError("source episode must be an object")
        signal_id = _required_text(item.get("signal_id"), "episode.signal_id")
        if signal_id in result:
            raise ValueError(f"duplicate source episode signal_id: {signal_id}")
        events_raw = item.get("events")
        if not isinstance(events_raw, list) or len(events_raw) > 20:
            raise ValueError(f"source episode events must be an array of at most 20: {signal_id}")
        result[signal_id] = {"signal_id": signal_id, "events": [_normalize_event(event) for event in events_raw]}
    if set(result) != expected_ids:
        missing = sorted(expected_ids - set(result))
        extra = sorted(set(result) - expected_ids)
        raise ValueError(f"source episode response ids mismatch; missing={missing}, extra={extra}")
    return result


def _normalize_event(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("source episode event must be an object")
    action = str(raw.get("action") or "commentary").strip().lower()
    if action not in _ACTIONS:
        raise ValueError(f"unsupported source event action: {action}")
    symbol_raw = str(raw.get("symbol") or "").strip().upper()
    symbol = symbol_raw or None
    if symbol and not _SYMBOL.fullmatch(symbol):
        raise ValueError(f"invalid source event symbol: {symbol}")
    direction = str(raw.get("direction") or "unknown").strip().lower()
    if direction not in _DIRECTIONS:
        raise ValueError(f"invalid source event direction: {direction}")
    structure = _slug(str(raw.get("structure_hint") or "")) or None
    thesis = " ".join(str(raw.get("thesis") or "").split())[:500]
    try:
        confidence = float(raw.get("semantic_confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError("semantic_confidence must be numeric") from exc
    if not 0 <= confidence <= 1:
        raise ValueError("semantic_confidence must be between zero and one")
    evidence_status = str(raw.get("evidence_status") or "ambiguous").strip().lower()
    if evidence_status not in _EVIDENCE_STATES:
        raise ValueError(f"invalid evidence_status: {evidence_status}")
    projections_raw = raw.get("projections")
    if not isinstance(projections_raw, list):
        raise ValueError("source event projections must be an array")
    projections = list(dict.fromkeys(str(item).strip().lower() for item in projections_raw))
    if any(item not in _PROJECTIONS for item in projections):
        raise ValueError("source event projections contain an unsupported value")
    exact_packages = _normalize_exact_packages(
        raw.get("exact_packages"),
        legacy_single=raw.get("exact_package"),
    )
    blockers = _string_list(raw.get("blockers"))
    template_number = raw.get("template_number")
    if template_number is not None and (
        isinstance(template_number, bool) or not isinstance(template_number, int)
    ):
        raise ValueError("template_number must be an integer or null")
    return {
        "action": action,
        "symbol": symbol,
        "direction": direction,
        "structure_hint": structure,
        "thesis": thesis,
        "semantic_confidence": confidence,
        "evidence_status": evidence_status,
        "projections": projections,
        "exact_packages": exact_packages,
        "blockers": blockers,
        "template_number": template_number,
    }


def _normalize_exact_packages(raw: Any, *, legacy_single: Any = None) -> list[dict[str, Any]]:
    if raw is not None and legacy_single is not None:
        raise ValueError("use exact_packages, not exact_package and exact_packages together")
    if raw is None:
        raw = [] if legacy_single is None else [legacy_single]
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError("exact_packages must be an array of at most 20 packages")
    return [_normalize_exact_package(item) for item in raw]


def _normalize_exact_package(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("exact package must be an object")
    complete = raw.get("complete")
    if not isinstance(complete, bool):
        raise ValueError("exact_package.complete must be boolean")
    blocker = " ".join(str(raw.get("blocker") or "").split())[:300] or None
    legs_raw = raw.get("legs")
    if not isinstance(legs_raw, list):
        raise ValueError("exact_package.legs must be an array")
    legs: list[dict[str, Any]] = []
    for leg in legs_raw:
        if not isinstance(leg, Mapping):
            raise ValueError("exact package leg must be an object")
        quantity = leg.get("quantity")
        expiration = " ".join(str(leg.get("expiration") or "").split()) or None
        strike = str(leg.get("strike") or "").strip() or None
        option_type = str(leg.get("option_type") or "").strip().lower() or None
        order_code = str(leg.get("order_code") or "").strip().upper() or None
        if quantity is not None and (
            isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0
        ):
            raise ValueError("exact package leg quantity must be a positive integer or null")
        if option_type is not None and option_type not in _OPTION_TYPES:
            raise ValueError("exact package leg option_type is invalid")
        if order_code is not None and order_code not in _ORDER_CODES:
            raise ValueError("exact package leg order_code is invalid")
        if complete and None in {quantity, expiration, strike, option_type, order_code}:
            raise ValueError("complete exact package contains an incomplete leg")
        legs.append(
            {
                "quantity": quantity,
                "expiration": expiration,
                "strike": strike,
                "option_type": option_type,
                "order_code": order_code,
            }
        )
    if complete and (not legs or blocker):
        raise ValueError("complete exact package requires legs and no blocker")
    if not complete and not blocker:
        raise ValueError("incomplete exact package requires a blocker")
    price_raw = raw.get("displayed_price")
    price = None
    if price_raw is not None:
        if not isinstance(price_raw, Mapping):
            raise ValueError("exact_package.displayed_price must be an object or null")
        amount = re.sub(r"[$,\s]", "", str(price_raw.get("amount") or ""))
        raw_effect = _slug(str(price_raw.get("effect") or ""))
        if "credit" in raw_effect or raw_effect in {"received", "receive", "proceeds"}:
            effect = "credit"
        elif "debit" in raw_effect or raw_effect in {
            "paid",
            "pay",
            "cost",
            "risk",
            "more_risk",
        }:
            effect = "debit"
        else:
            effect = raw_effect
        if re.fullmatch(r"\d+(?:\.\d+)?", amount) and effect in {
            "debit",
            "credit",
        }:
            price = {"amount": amount, "effect": effect}
    return {
        "complete": complete,
        "blocker": blocker,
        "displayed_price": price,
        "legs": legs,
        "field_provenance": _string_list(raw.get("field_provenance")),
    }


def _finalize_episode(
    record: Mapping[str, Any],
    interpreted: Mapping[str, Any],
    *,
    profile_id: str,
    profile_version: str,
    active: dict[tuple[str, str], list[str]],
    media_available: bool,
    image_numbers: list[int],
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    signal_id = str(record["signal_id"])
    source = record.get("source") or {}
    events: list[dict[str, Any]] = []
    normalized_events = [
        _canonicalize_event(_normalize_event(raw_event), record=record, profile=profile)
        for raw_event in interpreted.get("events") or []
    ]
    normalized_events = _merge_equivalent_events(normalized_events)
    for ordinal, event in enumerate(normalized_events, start=1):
        action = event["action"]
        symbol = event["symbol"]
        structure = event["structure_hint"] or "unknown"
        blockers = list(event["blockers"])
        projections = list(event["projections"])

        exact_packages = [
            _localize_package_image_refs(item, image_numbers)
            for item in event["exact_packages"]
        ]
        incomplete_packages = [item for item in exact_packages if not item["complete"]]
        complete_packages = [item for item in exact_packages if item["complete"]]
        if incomplete_packages:
            blockers.append("exact_package_incomplete")
            if not complete_packages:
                projections = [item for item in projections if item != "exact_package"]
            if not complete_packages and not any(
                item in projections for item in {"idea", "residual"}
            ):
                event["evidence_status"] = "needs_media" if not media_available else "ambiguous"
        if "exact_package" in projections and not complete_packages:
            projections = [item for item in projections if item != "exact_package"]
            blockers.append("exact_package_missing")
        # Some provider responses replace a visible equity cashtag with a
        # blockchain smarttag. Never turn that opaque identifier into a guessed
        # security unless this post supplies independent literal/image evidence.
        literal_symbols = {str(item.get("symbol") or "").upper()
                           for item in (record.get("literal") or {}).get("symbols", [])}
        image_symbol_evidence = media_available and any(
            any(str(ref).startswith("image:") for ref in package.get("field_provenance", []))
            for package in complete_packages
        )
        if (_has_opaque_identifier(record) and symbol not in literal_symbols
                and not image_symbol_evidence and action in _ENTRY_ACTIONS):
            blockers.append("source_identifier_unresolved")
            event["evidence_status"] = "ambiguous"

        confirmation_requires_prior = "source_open_confirmation_requires_prior_opportunity" in blockers
        if action in _FOLLOW_UP_ACTIONS or confirmation_requires_prior:
            projections = [item for item in projections if item != "idea"]
            if "residual" not in projections:
                projections.append("residual")

        link_state = "not_needed"
        links_to: list[str] = []
        key = (symbol or "", structure)
        candidates = active.get(key, []) if symbol else []
        if action in _FOLLOW_UP_ACTIONS or confirmation_requires_prior:
            if len(candidates) == 1:
                link_state = "linked"
                links_to = list(candidates)
                if confirmation_requires_prior:
                    blockers = [
                        item
                        for item in blockers
                        if item != "source_open_confirmation_requires_prior_opportunity"
                    ]
                    event["evidence_status"] = "complete"
            elif len(candidates) > 1:
                link_state = "ambiguous"
                blockers.append("ambiguous_lifecycle_link")
                event["evidence_status"] = "ambiguous"
            else:
                link_state = "needs_history"
                blockers.append("lifecycle_link_missing")
                event["evidence_status"] = "needs_history"

        event_id = "sevt_" + _sha256(f"{profile_id}|{profile_version}|{signal_id}|{ordinal}")[:24]
        opportunity_group_id = "sopp_" + _sha256(f"{profile_id}|{signal_id}|{ordinal}")[:24]
        projection_dispositions = _projection_dispositions(
            action=action,
            projections=projections,
            exact_packages=complete_packages,
            evidence_status=event["evidence_status"],
            blockers=blockers,
            symbol=symbol,
            structure=structure,
            direction=event["direction"],
        )
        planner_new_entry = any(
            item["projection"] in {"idea", "exact_package"}
            and item["disposition"] == "ready_for_source_policy"
            for item in projection_dispositions
        )
        if action in _ENTRY_ACTIONS and symbol and not confirmation_requires_prior:
            active.setdefault(key, [])
            if event_id not in active[key]:
                active[key].append(event_id)
        if action == "close" and link_state == "linked":
            active.pop(key, None)

        events.append(
            {
                **event,
                "exact_packages": exact_packages,
                "event_id": event_id,
                "opportunity_group_id": opportunity_group_id,
                "projections": projections or ["residual"],
                "projection_dispositions": projection_dispositions,
                "link_state": link_state,
                "links_to": links_to,
                "planner_new_entry": planner_new_entry,
                "blockers": list(dict.fromkeys(blockers)),
            }
        )
    episode_id = "sepi_" + _sha256(f"{profile_id}|{profile_version}|{signal_id}")[:24]
    return {
        "schema": EPISODE_SCHEMA,
        "episode_id": episode_id,
        "source_id": profile_id,
        "post_ref": signal_id,
        "published_at": str(source.get("published_at") or ""),
        "profile_version": profile_version,
        "source_record_sha256": _sha256(_stable_json(record)),
        "events": events,
        "effects": _effects(),
    }


def _localize_package_image_refs(package: Mapping[str, Any], image_numbers: list[int]) -> dict[str, Any]:
    """Translate prompt-global image numbers back to this post's media indexes."""

    localized = dict(package)
    global_to_local = {number: index for index, number in enumerate(image_numbers, start=1)}
    provenance: list[str] = []
    invalid_image_reference = False
    for item in package.get("field_provenance") or []:
        text = str(item)
        match = re.fullmatch(r"image:(\d+)", text.strip().lower())
        if match:
            global_number = int(match.group(1))
            if global_number in global_to_local:
                text = f"image:{global_to_local[global_number]}"
            else:
                invalid_image_reference = True
        provenance.append(text)
    localized["field_provenance"] = provenance
    if invalid_image_reference:
        # Prompt image numbers are shared by the whole model batch. Never let a
        # locator from another post be reinterpreted as this post's local image.
        localized["complete"] = False
        localized["blocker"] = "image_reference_outside_source_post"
    return localized


def _projection_dispositions(
    *,
    action: str,
    projections: list[str],
    exact_packages: list[Mapping[str, Any]],
    evidence_status: str,
    blockers: list[str],
    symbol: str | None,
    structure: str,
    direction: str,
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    idea_evidence_blocked = evidence_status in {"needs_history", "ambiguous"}
    exact_evidence_blocked = evidence_status in {"needs_media", "needs_history", "ambiguous"}
    for projection in projections or ["residual"]:
        disposition = "retained"
        reason = "non_entry_evidence"
        if projection == "idea":
            if action not in _ENTRY_ACTIONS:
                disposition, reason = "benchmark_only", "follow_up_is_not_a_new_entry"
            elif "planner_structure_unsupported" in blockers:
                disposition, reason = "parked", "planner_structure_unsupported"
            elif not symbol or structure == "unknown" or direction == "unknown":
                disposition, reason = "parked", "idea_semantics_incomplete"
            elif idea_evidence_blocked:
                disposition, reason = "parked", evidence_status
            else:
                disposition, reason = "ready_for_source_policy", "idea_evidence_complete"
        elif projection == "exact_package":
            if action not in _ENTRY_ACTIONS:
                disposition, reason = "benchmark_only", "follow_up_is_not_a_new_entry"
            elif not exact_packages:
                disposition, reason = "parked", "exact_package_incomplete"
            elif exact_evidence_blocked:
                disposition, reason = "parked", evidence_status
            else:
                disposition, reason = "ready_for_source_policy", "exact_package_complete"
        results.append(
            {
                "projection": projection,
                "disposition": disposition,
                "reason": reason,
            }
        )
    return results


def _canonicalize_event(
    event: dict[str, Any],
    *,
    record: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(event)
    config = profile.get("episode_interpreter") or {}
    text = str((record.get("literal") or {}).get("text") or "")
    for rule in config.get("action_overrides") or []:
        if re.search(str(rule["regex"]), text, flags=re.IGNORECASE):
            result["action"] = str(rule["action"])
            break

    aliases = config.get("structure_aliases") or {}
    structure = str(result.get("structure_hint") or "")
    structure = str(aliases.get(structure, structure))
    literal_symbols = (record.get("literal") or {}).get("symbols") or []
    evidence_text = str(result.get("thesis") or "")
    if len(literal_symbols) <= 1:
        evidence_text = f"{evidence_text} {text}"
    matching_rules = [
        rule for rule in profile.get("strategy_rules") or []
        if re.search(str(rule["regex"]), evidence_text, flags=re.IGNORECASE)
    ]
    if matching_rules:
        # A roll can mention both the old calendar and the resulting diagonal.
        # Preserve the model's structure when it is itself supported by literal
        # evidence; profile list order must not change it back to the old shape.
        rule = next(
            (rule for rule in matching_rules if rule.get("strategy_family") == structure),
            matching_rules[0],
        )
        structure = str(rule.get("strategy_family") or "")
        if result.get("direction") == "unknown" and rule.get("direction"):
            result["direction"] = str(rule["direction"])
    for rule in config.get("composite_structure_rules") or []:
        components = {str(item) for item in rule.get("component_structures") or []}
        if structure in components and re.search(
            str(rule["regex"]), text, flags=re.IGNORECASE
        ):
            structure = str(rule["structure_hint"])
            break
    result["structure_hint"] = structure or None
    if result["action"] == "scale_in" and config.get("scale_in_creates_idea") is True:
        result["projections"] = list(
            dict.fromkeys([*result.get("projections", []), "idea"])
        )
        if (
            result.get("symbol")
            and result.get("structure_hint")
            and result.get("direction") != "unknown"
        ):
            result["evidence_status"] = "complete"
    return result


def _merge_equivalent_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str], int] = {}
    for event in events:
        structure = str(event.get("structure_hint") or "")
        key = (str(event.get("action") or ""), str(event.get("symbol") or ""), structure)
        if key not in by_key:
            by_key[key] = len(merged)
            merged.append(event)
            continue
        target = merged[by_key[key]]
        target["projections"] = list(
            dict.fromkeys([*target.get("projections", []), *event.get("projections", [])])
        )
        target["blockers"] = list(
            dict.fromkeys([*target.get("blockers", []), *event.get("blockers", [])])
        )
        target["thesis"] = "; ".join(
            dict.fromkeys(
                item
                for item in [str(target.get("thesis") or ""), str(event.get("thesis") or "")]
                if item
            )
        )[:500]
        target["semantic_confidence"] = max(
            float(target.get("semantic_confidence") or 0),
            float(event.get("semantic_confidence") or 0),
        )
        target["exact_packages"] = _merge_exact_packages(
            target.get("exact_packages") or [], event.get("exact_packages") or []
        )
        if target.get("direction") == "unknown" and event.get("direction") != "unknown":
            target["direction"] = event["direction"]
    return merged


def _merge_exact_packages(
    first: Iterable[Mapping[str, Any]],
    second: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for package in [*first, *second]:
        normalized = dict(package)
        key = _stable_json(normalized)
        if key not in seen:
            packages.append(normalized)
            seen.add(key)
    return packages


def _system_prompt(profile: Mapping[str, Any]) -> str:
    config = profile.get("episode_interpreter") or {}
    guidance = str(
        config.get("source_guidance")
        or "Interpret the author's public trading language literally."
    ).strip()
    return f"""\
You are a source-specific trading-post interpreter inside Kamandal.
Decompose each public post into zero or more atomic source events. A post may
contain several symbols and several different actions. Do not judge portfolio
fit, choose risk, approve an order, or invent missing image/history facts.

Source: {profile.get('profile_id')}
Source guidance:
{guidance}

Use idea only for a new thesis Kamandal could reconstruct. Use exact_package
only when every displayed leg is observable from supplied text/image or an
unambiguous declared source grammar. A close, roll, adjustment, scale-out, or
hold is never a new idea. Missing required media or prior-trade context must be
marked needs_media or needs_history and retained as residual. If the text fully
supports an idea but the image legs are unavailable, keep the idea complete and
mark only exact_package incomplete with its blocker. One event may contain
several exact_packages when the post shows variants of the same action, symbol,
direction, and structure; do not repeat the idea projection for each variant.

Opaque blockchain identifiers in provider text are not equity symbols. Do not
infer their ticker from familiarity. Resolve a security only from independent
literal evidence or this post's attached trade image; otherwise mark ambiguous.

Return JSON only:
{{"schema":"{PROMPT_SCHEMA}","episodes":[{{"signal_id":"x-post:1","events":[{{"action":"open|scale_in|close|scale_out|roll|adjust|hold|commentary|discovery","symbol":"AAPL or null","direction":"bullish|bearish|neutral|unknown","structure_hint":"call_diagonal or null","thesis":"short literal thesis","semantic_confidence":0.0,"evidence_status":"complete|needs_media|needs_history|ambiguous|unsupported","projections":["idea|exact_package|residual"],"exact_packages":[],"blockers":[],"template_number":null}}]}}]}}

Each item in exact_packages has:
{{"complete":true,"blocker":null,"displayed_price":{{"amount":"2.95","effect":"debit"}},"legs":[{{"quantity":1,"expiration":"Sep 18 2026","strike":"115","option_type":"put","order_code":"BTO"}}],"field_provenance":["text","image:1"]}}
quantity is always a positive integer magnitude, never a signed position count.
Encode buy/sell and open/close only in order_code (BTO, STO, BTC, STC).
Return exactly one episode for every signal_id. Do not add fields outside this schema.
"""


def _has_opaque_identifier(record: Mapping[str, Any]) -> bool:
    text = str((record.get("literal") or {}).get("text") or "")
    return bool(re.search(r"\b(?:solana|ethereum|base):[A-Za-z0-9]{20,}\b", text, re.IGNORECASE))


def _user_prompt(
    records: Iterable[Mapping[str, Any]],
    *,
    image_map: Mapping[str, list[int]],
    history: Iterable[Mapping[str, Any]],
) -> str:
    posts = []
    for record in records:
        source = record.get("source") or {}
        literal = record.get("literal") or {}
        signal_id = str(record.get("signal_id") or "")
        media = source.get("media") if isinstance(source.get("media"), list) else []
        posts.append(
            {
                "signal_id": signal_id,
                "published_at": source.get("published_at"),
                "post_family": (record.get("classification") or {}).get("type"),
                "text": str(literal.get("text") or "")[:2000],
                "literal_symbols": literal.get("symbols") or [],
                "attached_image_numbers": image_map.get(signal_id, []),
                "media_expected_but_unavailable": (bool(media) or any(
                    re.search(r"/photo/\d+", str(url)) for url in source.get("expanded_urls", [])
                )) and not bool(image_map.get(signal_id)),
                "opaque_identifier_requires_independent_symbol_evidence": _has_opaque_identifier(record),
            }
        )
    context = [
        {
            "post_ref": item.get("post_ref"),
            "published_at": item.get("published_at"),
            "events": item.get("events") or [],
        }
        for item in history
    ]
    return json.dumps({"posts": posts, "recent_source_history": context}, indent=2, sort_keys=True)


def _repair_prompt(original: str, *, raw: Mapping[str, Any], error: str) -> str:
    return "\n\n".join(
        [
            original,
            "Your prior output failed deterministic validation.",
            f"Validation error: {error[:600]}",
            "Prior output:",
            json.dumps(raw, indent=2, sort_keys=True)[:12000],
            "Return one corrected JSON object. Do not explain.",
        ]
    )


def _record_symbols(record: Mapping[str, Any], profile: Mapping[str, Any]) -> list[str]:
    literal = record.get("literal") or {}
    symbols = [
        str(item.get("symbol") or "").strip().upper()
        for item in (literal.get("symbols") or [])
        if isinstance(item, Mapping) and _SYMBOL.fullmatch(str(item.get("symbol") or "").strip().upper())
    ]
    text = str(literal.get("text") or "")
    aliases = (profile.get("episode_interpreter") or {}).get("symbol_aliases") or {}
    for name, symbol in aliases.items():
        normalized = str(symbol).strip().upper()
        if re.search(rf"\b{re.escape(str(name))}\b", text, flags=re.IGNORECASE) and _SYMBOL.fullmatch(normalized):
            symbols.append(normalized)
    return list(dict.fromkeys(symbols))


def _obvious_noise(text: str, *, classification: str) -> bool:
    lowered = text.strip().lower()
    trade_language = re.compile(
        r"\b(?:bought|sold|opened|closed|rolled|added|calendar|diagonal|spread|strangle|butterfly|fly)\b"
    )
    if classification == "irrelevant" and not trade_language.search(lowered):
        return True
    reply_trade_language = re.compile(
        r"\$[A-Za-z]{1,8}|\b(?:bought|sold|opened|closed|rolled|added)\b"
    )
    if lowered.startswith("@") and not reply_trade_language.search(text):
        return True
    return False


def _verified_public_images(record: Mapping[str, Any]) -> tuple[str, ...]:
    source = record.get("source") or {}
    media = source.get("media")
    if not isinstance(media, list):
        return ()
    paths: list[str] = []
    for item in sorted(media, key=lambda value: int((value or {}).get("media_index") or 0)):
        if (
            not isinstance(item, Mapping)
            or item.get("type") != "photo"
            or item.get("cache_status") != "cached"
        ):
            return ()
        path = Path(str(item.get("artifact_path") or "")).expanduser().resolve()
        if not path.is_file() or _file_sha256(path) != str(item.get("sha256") or "").lower():
            return ()
        paths.append(str(path))
    return tuple(paths)


def _bounded_history(
    history: Iterable[Mapping[str, Any]], profile: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    raw_limit = (profile.get("episode_interpreter") or {}).get("max_history_episodes", 12)
    limit = max(0, min(40, int(raw_limit)))
    items = [
        dict(item)
        for item in history
        if isinstance(item, Mapping) and item.get("schema") == EPISODE_SCHEMA
    ]
    items.sort(
        key=lambda item: (
            _timestamp_value(item.get("published_at")),
            str(item.get("post_ref") or ""),
        ),
        reverse=True,
    )
    return tuple(items[:limit])


def _active_history(history: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], list[str]]:
    active: dict[tuple[str, str], list[str]] = {}
    ordered = sorted(
        history,
        key=lambda item: (
            _timestamp_value(item.get("published_at")),
            str(item.get("post_ref") or ""),
        ),
    )
    for episode in ordered:
        for event in episode.get("events") or []:
            if not isinstance(event, Mapping):
                continue
            symbol = str(event.get("symbol") or "")
            structure = str(event.get("structure_hint") or "unknown")
            event_id = str(event.get("event_id") or "")
            key = (symbol, structure)
            if event.get("action") in _ENTRY_ACTIONS and symbol and event_id:
                active.setdefault(key, []).append(event_id)
            elif event.get("action") == "close":
                active.pop(key, None)
    return active


def _client_receipt(client: JsonLlmClient, *, pass_name: str) -> dict[str, Any]:
    summary = getattr(client, "last_receipt_summary", None)
    payload = summary if isinstance(summary, dict) else {}
    return {"pass": pass_name, **payload}


def _write_idempotent(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") != content:
        raise ValueError(f"source episode artifact collision: {path}")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _write_replace(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _required_text(value: Any, label: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _required_slug(value: Any, label: str) -> str:
    text = _required_text(value, label)
    if _slug(text) != text:
        raise ValueError(f"{label} is invalid")
    return text


def _slug(value: str) -> str:
    return _SLUG.sub("_", value.strip().lower()).strip("_")[:80]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            " ".join(str(item).split())[:200] for item in value if str(item).strip()
        )
    )


def _timestamp_value(value: Any) -> float:
    try:
        return _TIMESTAMP.validate_python(value).timestamp()
    except Exception:  # noqa: BLE001 - invalid history sorts first and is still validated elsewhere.
        return 0.0


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _effects() -> dict[str, bool]:
    return {
        "sheet_write": False,
        "active_idea_publication": False,
        "planner_run": False,
        "shadow_admission": False,
        "live_admission": False,
        "broker_effects": False,
        "order_effects": False,
        "external_send": False,
    }
