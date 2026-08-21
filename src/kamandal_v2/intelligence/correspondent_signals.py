"""Profile-driven correspondent signal translation and planner handoff."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import TypeAdapter, ValidationError

from kamandal_v2.intelligence.chart_seeds import validate_chart_seed_evaluation
from kamandal_v2.intelligence.llm_client import JsonLlmClient
from kamandal_v2.intelligence.market_questions import (
    QUESTION_RESPONSE_SCHEMA,
    validate_market_question_response,
)
from kamandal_v2.paths import resolve_path
from kamandal_v2.stores.sqlite import LocalStore

SOURCE_SCHEMA = "birdclaw.correspondent_signals.v1"
SOURCE_RECORD_SCHEMA = "birdclaw.correspondent_signal.v1"
PROFILE_SCHEMA = "kamandal.correspondent_profile.v1"
TRANSLATION_SCHEMA = "kamandal.correspondent_signal_translation.v1"
RECORD_SCHEMA = "kamandal.correspondent_signal_record.v1"
PLANNER_SCHEMA = "kamandal.correspondent_planner_ideas.v1"
RECEIPT_SCHEMA = "kamandal.correspondent_signal_receipt.v1"
LIFECYCLE_SCHEMA = "kamandal.correspondent_lifecycle_index.v1"
ACQUISITION_REFERENCE_SCHEMA = "birdclaw.correspondent_acquisition_reference.v1"
TRANSLATOR_VERSION = "correspondent-translator-v3"

SOURCE_INTENT_ACTIONS = {"enter", "update", "exit", "ignore"}
SOURCE_INTENT_POSTURES = {"explicit_only", "inference_allowed"}
SOURCE_INTENT_DIRECTIONS = {"bullish", "bearish", "neutral"}

_TIMESTAMP = TypeAdapter(datetime)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


@dataclass(frozen=True, slots=True)
class CorrespondentImportResult:
    batch_id: str
    profile_id: str
    translation_path: Path
    review_path: Path
    receipt_path: Path
    planner_ideas_path: Path
    lifecycle_path: Path
    record_count: int
    planner_idea_count: int
    created: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RECEIPT_SCHEMA,
            "status": "succeeded",
            "batch_id": self.batch_id,
            "profile_id": self.profile_id,
            "translation_path": str(self.translation_path),
            "review_path": str(self.review_path),
            "receipt_path": str(self.receipt_path),
            "planner_ideas_path": str(self.planner_ideas_path),
            "lifecycle_path": str(self.lifecycle_path),
            "record_count": self.record_count,
            "planner_idea_count": self.planner_idea_count,
            "created": self.created,
            "effects": _effects(),
        }


def import_correspondent_signals(
    input_path: str | Path,
    *,
    profile_path: str | Path,
    universe_symbols: Iterable[str],
    chart_evaluation_paths: Iterable[str | Path] = (),
    output_dir: str | Path = "data/research/correspondent_signals",
    store: LocalStore | None = None,
    intent_client: JsonLlmClient | None = None,
) -> CorrespondentImportResult:
    source_path = resolve_path(input_path)
    source_text = source_path.read_text(encoding="utf-8")
    packet = validate_correspondent_packet(json.loads(source_text))
    profile, profile_text = load_correspondent_profile(profile_path)
    if packet["profile"]["profile_id"] != profile["source_profile_id"]:
        raise ValueError("Birdclaw packet profile does not match Kamandal profile")

    charts, chart_hashes = _load_chart_evaluations(chart_evaluation_paths)
    universe = {str(symbol).strip().upper() for symbol in universe_symbols if str(symbol).strip()}
    source_intents = _interpret_source_intents(packet, profile, intent_client)
    translated = _translate_packet(packet, profile, universe=universe, charts=charts, source_intents=source_intents)
    identity = {
        "translator_version": TRANSLATOR_VERSION,
        "source_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
        "profile_sha256": hashlib.sha256(profile_text.encode()).hexdigest(),
        "chart_sha256": chart_hashes,
        "universe": sorted(universe),
        "records": translated,
    }
    batch_id = hashlib.sha256(_stable_json(identity).encode()).hexdigest()[:16]
    for record in translated:
        record["record_id"] = _record_id(record, profile_text=profile_text)
    if store is not None:
        for record in translated:
            if "outside_configured_universe" not in (record.get("planner_blockers") or []):
                continue
            symbol = str(record.get("symbol") or "").strip().upper()
            if symbol:
                store.record_discovery_evidence(
                    symbol=symbol,
                    source_profile=str(profile["profile_id"]),
                    source_record_id=str(record["record_id"]),
                    exclusion_reason="outside_enabled_universe",
                    evidence_ref=f"correspondent:{profile['profile_id']}:{record['record_id']}",
                    observed_at=str(record.get("observed_at") or packet["generated_at"]),
                )

    planner_ideas = [_planner_idea(record, profile) for record in translated if record["planner_eligible"]]
    root = resolve_path(output_dir)
    run_dir = root / "runs" / profile["profile_id"] / batch_id
    records_dir = root / "records"
    translation_path = run_dir / "translation.json"
    review_path = run_dir / "review.md"
    receipt_path = run_dir / "receipt.json"
    planner_path = run_dir / "planner-ideas.yaml"
    lifecycle_path = root / "lifecycle-index.json"

    translation = {
        "schema": TRANSLATION_SCHEMA,
        "batch_id": batch_id,
        "translator_version": TRANSLATOR_VERSION,
        "as_of": packet["generated_at"],
        "profile": {
            "profile_id": profile["profile_id"],
            "version": str(profile.get("version") or "1"),
            "source_profile_id": profile["source_profile_id"],
            "profile_sha256": identity["profile_sha256"],
        },
        "source_packet_sha256": identity["source_sha256"],
        "source_acquisition": packet.get("acquisition") or {"status": "missing"},
        "chart_sha256": chart_hashes,
        "records": translated,
        "planner_idea_count": len(planner_ideas),
        "effects": _effects(),
    }
    planner_payload = {
        "schema": PLANNER_SCHEMA,
        "batch_id": batch_id,
        "profile_id": profile["profile_id"],
        "ideas": planner_ideas,
    }
    record_artifacts = [
        (records_dir / f"{record['record_id']}.json", json.dumps(record, indent=2, sort_keys=True) + "\n")
        for record in translated
    ]
    latest_artifacts = [
        (
            root / "latest" / profile["profile_id"] / f"{_logical_record_id(record)}.json",
            json.dumps(record, indent=2, sort_keys=True) + "\n",
        )
        for record in translated
    ]
    lifecycle = _build_lifecycle_index(root, translated)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "succeeded",
        "batch_id": batch_id,
        "profile_id": profile["profile_id"],
        "source_packet_sha256": identity["source_sha256"],
        "source_acquisition": packet.get("acquisition") or {"status": "missing"},
        "translation_path": str(translation_path),
        "review_path": str(review_path),
        "planner_ideas_path": str(planner_path),
        "lifecycle_path": str(lifecycle_path),
        "record_count": len(translated),
        "planner_idea_count": len(planner_ideas),
        "planner_run_performed": False,
        "effects": _effects(),
    }
    artifacts = [
        (translation_path, json.dumps(translation, indent=2, sort_keys=True) + "\n"),
        (review_path, _render_review(translation)),
        (planner_path, yaml.safe_dump(planner_payload, sort_keys=False)),
        (receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n"),
        *record_artifacts,
    ]
    for path, content in artifacts:
        _assert_idempotent(path, content)
    created = False
    for path, content in artifacts:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            created = True
    for path, content in latest_artifacts:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle_path.write_text(json.dumps(lifecycle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return CorrespondentImportResult(
        batch_id=batch_id,
        profile_id=profile["profile_id"],
        translation_path=translation_path,
        review_path=review_path,
        receipt_path=receipt_path,
        planner_ideas_path=planner_path,
        lifecycle_path=lifecycle_path,
        record_count=len(translated),
        planner_idea_count=len(planner_ideas),
        created=created,
    )


def load_correspondent_profile(path: str | Path) -> tuple[dict[str, Any], str]:
    profile_path = resolve_path(path)
    text = profile_path.read_text(encoding="utf-8")
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict) or payload.get("schema") != PROFILE_SCHEMA:
        raise ValueError(f"correspondent profile schema must be {PROFILE_SCHEMA}")
    for key in ("profile_id", "source_profile_id"):
        value = _text(payload.get(key), key)
        if not _ID.fullmatch(value):
            raise ValueError(f"{key} is invalid")
    families = payload.get("families")
    if not isinstance(families, dict) or not families:
        raise ValueError("correspondent profile requires families")
    for family, config in families.items():
        if not _ID.fullmatch(str(family)) or not isinstance(config, dict):
            raise ValueError("correspondent family is invalid")
        if config.get("mode") not in {"chart_watch", "numbered_template", "trade_journal", "ignore"}:
            raise ValueError(f"unsupported correspondent family mode: {config.get('mode')}")
    for rule in payload.get("strategy_rules") or []:
        _validate_regex_rule(rule, "strategy")
        for direction_rule in rule.get("direction_rules") or []:
            _validate_regex_rule(direction_rule, "strategy direction")
    for rule in payload.get("journal_actions") or []:
        _validate_regex_rule(rule, "journal action")
    posture = str(payload.get("interpretation_posture") or "").strip()
    if posture not in SOURCE_INTENT_POSTURES:
        raise ValueError("interpretation_posture must be explicit_only or inference_allowed")
    return payload, text


def validate_correspondent_packet(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != SOURCE_SCHEMA:
        raise ValueError(f"correspondent packet schema must be {SOURCE_SCHEMA}")
    _timestamp(payload.get("generated_at"), "generated_at")
    profile = payload.get("profile")
    if not isinstance(profile, dict) or not _ID.fullmatch(_text(profile.get("profile_id"), "profile.profile_id")):
        raise ValueError("packet profile is invalid")
    safety = payload.get("safety")
    if not isinstance(safety, dict):
        raise ValueError("packet safety must be an object")
    if safety.get("visibility") != "public" or safety.get("sanitization") != "sanitized":
        raise ValueError("correspondent packet must contain sanitized public evidence")
    required_false = {
        "network_call_performed",
        "x_mutation_performed",
        "raw_payload_returned",
        "database_handle_exposed",
    }
    if safety.get("read_only") is not True or any(safety.get(key) is not False for key in required_false):
        raise ValueError("correspondent packet safety boundary is not read-only")
    acquisition = payload.get("acquisition")
    if acquisition is not None:
        allowed_acquisition_keys = {"schema", "status", "receipt_generated_at", "receipt_run_id", "attempts"}
        if not isinstance(acquisition, dict) or acquisition.get("schema") != ACQUISITION_REFERENCE_SCHEMA:
            raise ValueError("correspondent packet acquisition reference is invalid")
        if not set(acquisition).issubset(allowed_acquisition_keys):
            raise ValueError("correspondent packet acquisition reference contains unsupported fields")
        if acquisition.get("status") not in {"succeeded", "incomplete", "failed", "missing"}:
            raise ValueError("correspondent packet acquisition status is invalid")
        if not isinstance(acquisition.get("attempts"), list):
            raise ValueError("correspondent packet acquisition attempts must be an array")
        allowed_attempt_keys = {
            "key", "profile_id", "profile_version", "profile_sha256", "author_handle", "source_lane",
            "command", "query", "requested_limit", "returned_count", "accepted_count", "earliest_post_id",
            "earliest_published_at", "latest_post_id", "latest_published_at", "expected_poll_interval_hours",
            "started_at", "finished_at", "status", "error_stage", "error", "continuity", "limit_reached",
            "coverage_status", "last_successful_at",
        }
        for attempt in acquisition["attempts"]:
            if not isinstance(attempt, dict) or not set(attempt).issubset(allowed_attempt_keys):
                raise ValueError("correspondent packet acquisition attempt contains unsupported fields")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("correspondent packet records must be an array")
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("schema") != SOURCE_RECORD_SCHEMA:
            raise ValueError(f"records[{index}] schema is invalid")
        signal_id = _text(record.get("signal_id"), f"records[{index}].signal_id")
        if signal_id in seen:
            raise ValueError(f"duplicate signal_id: {signal_id}")
        seen.add(signal_id)
        if record.get("profile_id") != profile["profile_id"]:
            raise ValueError(f"records[{index}] profile identity mismatch")
        source = record.get("source")
        if not isinstance(source, dict) or source.get("source_id") != signal_id:
            raise ValueError(f"records[{index}] source identity mismatch")
        _timestamp(source.get("published_at"), f"records[{index}].source.published_at")
        classification = record.get("classification")
        if not isinstance(classification, dict) or not _ID.fullmatch(
            _text(classification.get("type"), f"records[{index}].classification.type")
        ):
            raise ValueError(f"records[{index}] classification is invalid")
        literal = record.get("literal")
        if not isinstance(literal, dict) or not isinstance(literal.get("text"), str):
            raise ValueError(f"records[{index}] literal evidence is invalid")
        symbols = literal.get("symbols")
        if not isinstance(symbols, list):
            raise ValueError(f"records[{index}] literal.symbols must be an array")
        symbol_seen: set[str] = set()
        for item in symbols:
            symbol = str((item or {}).get("symbol") or "").upper() if isinstance(item, dict) else ""
            if not _SYMBOL.fullmatch(symbol) or symbol in symbol_seen:
                raise ValueError(f"records[{index}] contains an invalid or duplicate symbol")
            symbol_seen.add(symbol)
    return payload


def _translate_packet(
    packet: dict[str, Any],
    profile: dict[str, Any],
    *,
    universe: set[str],
    charts: dict[tuple[str, str], dict[str, Any]],
    source_intents: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    as_of = _parse_timestamp(packet["generated_at"])
    records: list[dict[str, Any]] = []
    for source_record in packet["records"]:
        symbols = source_record["literal"]["symbols"] or [None]
        for symbol_item in symbols:
            symbol = str(symbol_item["symbol"]).upper() if symbol_item else None
            translated = _translate_record(
                source_record,
                profile,
                symbol=symbol,
                symbol_origin=str(symbol_item.get("origin") or "") if symbol_item else "",
                as_of=as_of,
                universe=universe,
                chart=charts.get((source_record["source"]["source_id"], symbol or "")),
                source_intent=source_intents[source_record["signal_id"]],
            )
            records.append(translated)
    return records


def _translate_record(
    source_record: dict[str, Any],
    profile: dict[str, Any],
    *,
    symbol: str | None,
    symbol_origin: str,
    as_of: datetime,
    universe: set[str],
    chart: dict[str, Any] | None,
    source_intent: dict[str, str],
) -> dict[str, Any]:
    tweet_type = str(source_record["classification"]["type"])
    family = (profile.get("families") or {}).get(tweet_type)
    text = str(source_record["literal"]["text"])
    blockers: list[str] = []
    strategy_family = ""
    direction = "neutral"
    allowed_structures: list[str] = []
    planner_supported = False
    planner = dict((family or {}).get("planner") or {})
    intent_action = source_intent["action"]
    lifecycle_action = _lifecycle_action(intent_action)
    activation: dict[str, Any] = {"kind": "none", "status": "not_applicable"}
    construction_mode = "none"
    chart_run_id = None

    if family is None:
        blockers.append("classified_irrelevant" if tweet_type == "irrelevant" else "interpretation_required")
    else:
        mode = family["mode"]
        direction = str(family.get("direction") or "neutral")
        if mode == "ignore":
            blockers.append("profile_ignored")
        elif mode == "chart_watch":
            strategy_family = "directional_chart_trigger"
            allowed_structures = _string_list(planner.get("allowed_structures"))
            planner_supported = bool(planner.get("supported", True))
            construction_mode = "planner_selects_from_allowed_structures"
            activation = {"kind": "chart_trigger", "status": "awaiting_evaluation"}
            if chart is None:
                blockers.append("chart_evaluation_missing")
            else:
                chart_run_id = chart["run_id"]
                activation = _chart_activation(chart["evaluation"])
                chart_direction = str(
                    chart["evaluation"].get("direction")
                    or chart["evaluation"].get("bias")
                    or direction
                )
                if chart_direction in {"bullish", "bearish"}:
                    direction = chart_direction
                    allowed_structures = _directional_structures(
                        planner, direction, fallback=allowed_structures
                    )
                if chart["evaluation"].get("evaluation_status") != "evaluated":
                    reason = str((chart["evaluation"].get("reasons") or ["insufficient_evidence"])[0])
                    blockers.append(f"chart_{_slug(reason)}")
                elif chart["evaluation"].get("observed_setup_family") in {
                    "no_actionable_boundary",
                    "insufficient_evidence",
                }:
                    blockers.append("chart_no_actionable_boundary")
                elif activation["status"] != "triggered":
                    blockers.append("chart_trigger_not_confirmed")
                elif direction not in {"bullish", "bearish"}:
                    blockers.append("chart_direction_unresolved")
        elif mode == "numbered_template":
            explicit = _strategy_match(text, profile)
            interpreted = _strategy_from_intent(source_intent, profile)
            idea_number = source_record["literal"].get("idea_number")
            mapped = (family.get("idea_number_map") or {}).get(str(idea_number))
            default_number = family.get("default_idea_number")
            default = (family.get("idea_number_map") or {}).get(str(default_number))
            selected = explicit or interpreted or mapped or default
            lifecycle_action = _lifecycle_action(intent_action)
            activation = {"kind": "source_activation", "status": intent_action}
            construction_mode = "reconstruct_fresh"
            if not isinstance(selected, dict):
                blockers.append("strategy_unresolved")
            else:
                strategy_family = str(selected.get("strategy_family") or "")
                direction = str(selected.get("direction") or direction)
                allowed_structures = _string_list(selected.get("allowed_structures"))
                planner_supported = bool(selected.get("planner_supported", bool(allowed_structures)))
                if not planner_supported:
                    blockers.append("planner_structure_unsupported")
        elif mode == "trade_journal":
            selected = _strategy_match(text, profile) or _strategy_from_intent(source_intent, profile)
            lifecycle_action = _lifecycle_action(intent_action)
            activation = {"kind": "journal_event", "status": intent_action}
            construction_mode = "reconstruct_fresh_from_observed_structure"
            if not selected:
                blockers.append("strategy_unresolved")
            else:
                strategy_family = str(selected.get("strategy_family") or "")
                direction = str(selected.get("direction") or direction)
                allowed_structures = _string_list(selected.get("allowed_structures"))
                if planner.get("map_directional_to_planner") is True and direction in {
                    "bullish",
                    "bearish",
                }:
                    strategy_family = "directional_source_idea"
                    allowed_structures = _directional_structures(
                        planner, direction, fallback=allowed_structures
                    )
                    construction_mode = "planner_selects_from_directional_structures"
                planner_supported = bool(planner.get("supported", True)) and bool(allowed_structures)

    if family is not None and family.get("mode") != "ignore" and intent_action != "enter":
        blockers.append(f"source_{intent_action}_is_not_new_entry")

    if symbol is None and tweet_type != "irrelevant":
        blockers.append("symbol_missing")
    elif symbol not in universe:
        blockers.append("outside_configured_universe")
    if family is not None and family.get("max_age_hours") is not None:
        age = (as_of - _parse_timestamp(source_record["source"]["published_at"])).total_seconds() / 3600
        if age < -1:
            blockers.append("source_timestamp_in_future")
        elif age > float(family["max_age_hours"]):
            blockers.append("source_too_old")
    directional_config = planner.get("allowed_structures_by_direction")
    has_directional_config = isinstance(directional_config, dict) and any(
        _string_list(value) for value in directional_config.values()
    )
    if not allowed_structures and planner_supported and not has_directional_config:
        blockers.append("allowed_structures_missing")

    blockers = list(dict.fromkeys(blockers))
    planner_eligible = not blockers and planner_supported
    status = "actionable" if planner_eligible else (
        "ignored" if tweet_type == "irrelevant" else "needs_review" if tweet_type == "unknown" else "parked"
    )
    lifecycle_key = f"{profile['profile_id']}:{symbol}:{strategy_family}" if symbol and strategy_family else None
    return {
        "schema": RECORD_SCHEMA,
        "record_id": "pending",
        "profile_id": profile["profile_id"],
        "signal_id": source_record["signal_id"],
        "source": dict(source_record["source"]),
        "tweet_type": tweet_type,
        "classification": dict(source_record["classification"]),
        "source_intent": dict(source_intent),
        "symbol": symbol,
        "symbol_origin": symbol_origin,
        "strategy_family": strategy_family,
        "direction": direction,
        "construction_mode": construction_mode,
        "allowed_structures": allowed_structures,
        "activation": activation,
        "chart_run_id": chart_run_id,
        "lifecycle": {"key": lifecycle_key, "action": lifecycle_action},
        "planner_eligible": planner_eligible,
        "planner_blockers": blockers,
        "status": status,
        "source_text": text,
        "planner": {
            "thesis_tags": _string_list(planner.get("thesis_tags")),
            "horizon_days": int(planner.get("horizon_days") or 30),
        },
    }


def _planner_idea(record: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    if not record["planner_eligible"] or not record["symbol"] or not record["allowed_structures"]:
        raise ValueError("ineligible correspondent record cannot become a planner idea")
    idea_id = "corr_" + hashlib.sha256(
        f"{profile['profile_id']}|{record['signal_id']}|{record['symbol']}|{record['record_id']}".encode()
    ).hexdigest()[:16]
    one_structure = record["allowed_structures"][0] if len(record["allowed_structures"]) == 1 else ""
    tags = list(dict.fromkeys([
        *record["planner"]["thesis_tags"],
        f"correspondent:{profile['profile_id']}",
        f"signal_type:{record['tweet_type']}",
    ]))
    return {
        "idea_id": idea_id,
        "source": f"correspondent:{profile['profile_id']}:{record['signal_id']}",
        "underlying": record["symbol"],
        "direction": record["direction"],
        "strategy_hint": "",
        "mentioned_strategy": one_structure,
        "allowed_structures": list(record["allowed_structures"]),
        "thesis_tags": tags,
        "horizon_days": record["planner"]["horizon_days"],
        "trade_horizon_days": record["planner"]["horizon_days"],
        "confidence": "profile_deterministic",
        "extraction_confidence": "profile_deterministic",
        "quote_evidence": record["source_text"],
        "extraction_notes": (
            f"Translated by {profile['profile_id']} profile; activation={record['activation']['status']}; "
            f"record_id={record['record_id']}"
        ),
        "operator_status": "pending",
        "notes": (
            f"source_id={record['signal_id']}; tweet_type={record['tweet_type']}; "
            f"construction_mode={record['construction_mode']}"
        ),
    }


def _load_chart_evaluations(
    paths: Iterable[str | Path],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, str]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    for raw in paths:
        path = resolve_path(raw)
        text = path.read_text(encoding="utf-8")
        raw_payload = json.loads(text)
        if raw_payload.get("schema") == QUESTION_RESPONSE_SCHEMA:
            payload = validate_market_question_response(raw_payload)
            hashes[f"questions:{payload['run_id']}"] = hashlib.sha256(text.encode()).hexdigest()
            for answer in payload["answers"]:
                source_id = str(answer["source_context"]["source_id"])
                evaluation = _question_answer_as_evaluation(answer)
                key = (source_id, str(answer["symbol"]).upper())
                if key in result:
                    raise ValueError(f"duplicate chart evaluation for {source_id} {key[1]}")
                result[key] = {"run_id": payload["run_id"], "evaluation": evaluation}
            continue
        payload = validate_chart_seed_evaluation(raw_payload)
        source_id = str(payload["source"]["source_id"])
        hashes[f"{source_id}:{payload['run_id']}"] = hashlib.sha256(text.encode()).hexdigest()
        for evaluation in payload["evaluations"]:
            key = (source_id, str(evaluation["symbol"]).upper())
            if key in result:
                raise ValueError(f"duplicate chart evaluation for {source_id} {key[1]}")
            result[key] = {"run_id": payload["run_id"], "evaluation": evaluation}
    return result, hashes


def _question_answer_as_evaluation(answer: dict[str, Any]) -> dict[str, Any]:
    evaluated = answer.get("answer_status") == "evaluated"
    return {
        "symbol": answer["symbol"],
        "direction": answer["direction"],
        "evaluation_status": "evaluated" if evaluated else "insufficient_evidence",
        "observed_setup_family": "directional_setup" if evaluated else "insufficient_evidence",
        "source_alignment": answer.get("source_alignment"),
        "signal_state": answer.get("setup_state"),
        "confirmation_trigger": answer.get("trigger"),
        "failure_condition": answer.get("invalidation"),
        "reasons": list(answer.get("reasons") or []),
        "evidence_refs": list(answer.get("evidence_refs") or []),
    }


def _chart_activation(evaluation: dict[str, Any]) -> dict[str, Any]:
    trigger = evaluation.get("confirmation_trigger") or {}
    return {
        "kind": "chart_trigger",
        "status": str(trigger.get("status") or "not_available"),
        "rule": trigger.get("rule"),
        "price": trigger.get("price"),
        "failure_condition": evaluation.get("failure_condition"),
        "observed_setup_family": evaluation.get("observed_setup_family"),
        "source_alignment": evaluation.get("source_alignment"),
        "direction": evaluation.get("direction"),
    }


def _directional_structures(
    planner: dict[str, Any], direction: str, *, fallback: list[str]
) -> list[str]:
    configured = planner.get("allowed_structures_by_direction") or {}
    if isinstance(configured, dict):
        selected = _string_list(configured.get(direction))
        if selected:
            return selected
    return list(fallback)


def _strategy_match(text: str, profile: dict[str, Any]) -> dict[str, Any] | None:
    for rule in profile.get("strategy_rules") or []:
        if re.search(str(rule["regex"]), text, flags=re.IGNORECASE):
            selected = dict(rule)
            for direction_rule in rule.get("direction_rules") or []:
                if re.search(str(direction_rule["regex"]), text, flags=re.IGNORECASE):
                    selected["direction"] = str(direction_rule["direction"])
                    selected["direction_rule_id"] = str(direction_rule.get("id") or "profile_direction")
                    break
            return selected
    return None


def _strategy_from_intent(intent: dict[str, str], profile: dict[str, Any]) -> dict[str, Any] | None:
    hint = str(intent.get("strategy_hint") or "")
    if not hint:
        return None
    for rule in profile.get("strategy_rules") or []:
        if hint not in {str(rule.get("id") or ""), str(rule.get("strategy_family") or "")}:
            continue
        selected = dict(rule)
        if intent.get("direction") in SOURCE_INTENT_DIRECTIONS and intent["direction"] != "neutral":
            selected["direction"] = intent["direction"]
        return selected
    return None


def _journal_action(text: str, profile: dict[str, Any]) -> str:
    for rule in profile.get("journal_actions") or []:
        if re.search(str(rule["regex"]), text, flags=re.IGNORECASE):
            return str(rule.get("action") or "observe")
    return "observe"


def _interpret_source_intents(
    packet: dict[str, Any],
    profile: dict[str, Any],
    client: JsonLlmClient | None,
) -> dict[str, dict[str, str]]:
    if client is None:
        return {
            record["signal_id"]: _deterministic_source_intent(record, profile)
            for record in packet["records"]
        }

    interpreted: dict[str, dict[str, str]] = {}
    records = list(packet["records"])
    for start in range(0, len(records), 30):
        chunk = records[start : start + 30]
        response = client.chat_json(
            _intent_system_prompt(profile),
            _intent_user_prompt(chunk),
        )
        raw_items = response.get("results") or []
        if not isinstance(raw_items, list):
            raise ValueError("correspondent intent response results must be an array")
        by_signal = {
            str(item.get("signal_id") or ""): item
            for item in raw_items
            if isinstance(item, dict)
        }
        for record in chunk:
            signal_id = record["signal_id"]
            if signal_id not in by_signal:
                raise ValueError(f"correspondent intent response missing signal_id: {signal_id}")
            interpreted[signal_id] = _normalize_source_intent(by_signal[signal_id])
    return interpreted


def _deterministic_source_intent(record: dict[str, Any], profile: dict[str, Any]) -> dict[str, str]:
    """Effect-free fallback for fixtures and historical replay."""

    tweet_type = str(record["classification"]["type"])
    family = (profile.get("families") or {}).get(tweet_type) or {}
    mode = family.get("mode")
    text = str(record["literal"]["text"])
    if mode == "trade_journal":
        legacy = _journal_action(text, profile)
        action = {"open": "enter", "close": "exit", "adjust": "update"}.get(legacy, "ignore")
    elif mode in {"numbered_template", "chart_watch"}:
        action = "enter"
    else:
        action = "ignore"
    symbols = record["literal"].get("symbols") or []
    symbol = str((symbols[0] or {}).get("symbol") or "").upper() if symbols else ""
    strategy = _strategy_match(text, profile) or {}
    return {
        "action": action,
        "symbol": symbol,
        "direction": str(strategy.get("direction") or family.get("direction") or "neutral"),
        "strategy_hint": str(strategy.get("strategy_family") or ""),
        "reason": "deterministic fixture or replay interpretation",
    }


def _normalize_source_intent(raw: dict[str, Any]) -> dict[str, str]:
    action = str(raw.get("action") or "ignore").strip().lower()
    if action not in SOURCE_INTENT_ACTIONS:
        action = "ignore"
    symbol = str(raw.get("symbol") or "").strip().upper()
    if symbol and not _SYMBOL.fullmatch(symbol):
        symbol = ""
    direction = str(raw.get("direction") or "neutral").strip().lower()
    if direction not in SOURCE_INTENT_DIRECTIONS:
        direction = "neutral"
    raw_strategy_hint = str(raw.get("strategy_hint") or "").strip()
    strategy_hint = _slug(raw_strategy_hint)[:64] if raw_strategy_hint else ""
    reason = " ".join(str(raw.get("reason") or "").split())[:200]
    return {
        "action": action,
        "symbol": symbol,
        "direction": direction,
        "strategy_hint": strategy_hint,
        "reason": reason,
    }


def _lifecycle_action(action: str) -> str:
    return {"enter": "open", "update": "adjust", "exit": "close"}.get(action, "observe")


def _intent_system_prompt(profile: dict[str, Any]) -> str:
    posture = str(profile["interpretation_posture"])
    posture_rule = (
        "Classify enter only when the author explicitly recommends or reports opening a position now."
        if posture == "explicit_only"
        else "You may classify a current actionable market thesis as enter even when the author does not state a formal order."
    )
    return f"""\
You interpret public posts from one configured market correspondent.

    Answer one question: does each post introduce a new opportunity that Kamandal
    should investigate now or retain as a conditional watch, update a prior source
    thesis, close a prior source thesis, or do none of those? `enter` means new
    opportunity, not an instruction to place a broker order. Do not judge whether
    the trade is good, select option legs, or approve portfolio risk.

Interpretation posture: {posture}
{posture_rule}
Retrospective performance, "looks to expire", holding, rolling, trimming,
closing, and status language are not new entries. When uncertain, use ignore.

Return JSON only:
{{"results":[{{"signal_id":"source id","action":"enter|update|exit|ignore","symbol":"AAPL or empty","direction":"bullish|bearish|neutral","strategy_hint":"short_strangle or empty","reason":"one short sentence"}}]}}
Return exactly one result for every supplied signal_id and no additional fields.
"""


def _intent_user_prompt(records: list[dict[str, Any]]) -> str:
    compact = [
        {
            "signal_id": record["signal_id"],
            "post_family": record["classification"]["type"],
            "published_at": record["source"]["published_at"],
            "text": str(record["literal"]["text"])[:1200],
        }
        for record in records
    ]
    return json.dumps({"posts": compact}, indent=2, sort_keys=True)


def _build_lifecycle_index(root: Path, current: list[dict[str, Any]]) -> dict[str, Any]:
    by_logical_id: dict[str, dict[str, Any]] = {}
    latest_dir = root / "latest"
    if latest_dir.is_dir():
        for path in sorted(latest_dir.glob("*/*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("record_id"):
                by_logical_id[_logical_record_id(record)] = record
    by_logical_id.update({_logical_record_id(record): record for record in current})
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in by_logical_id.values():
        key = ((record.get("lifecycle") or {}).get("key"))
        if key:
            groups.setdefault(str(key), []).append(record)
    lifecycles = []
    for key, items in sorted(groups.items()):
        ordered = sorted(items, key=lambda item: (item["source"]["published_at"], item["signal_id"], item["record_id"]))
        active: str | None = None
        events = []
        for item in ordered:
            action = item["lifecycle"]["action"]
            previous = active
            linked = action == "open" or previous is not None
            if action == "open":
                active = item["record_id"]
            elif action == "close" and previous is not None:
                active = None
            events.append({
                "record_id": item["record_id"],
                "signal_id": item["signal_id"],
                "published_at": item["source"]["published_at"],
                "action": action,
                "previous_record_id": previous,
                "linked": linked,
            })
        lifecycles.append({"key": key, "active_record_id": active, "events": events})
    return {"schema": LIFECYCLE_SCHEMA, "lifecycles": lifecycles}


def _render_review(translation: dict[str, Any]) -> str:
    lines = [
        "# Correspondent Signal Review",
        "",
        f"- Profile: `{translation['profile']['profile_id']}`",
        f"- Batch: `{translation['batch_id']}`",
        f"- Planner ideas: `{translation['planner_idea_count']}`",
        f"- Birdclaw acquisition: `{translation['source_acquisition']['status']}`",
        "- Planner run performed: `false`",
        "",
        "| Source | Type | Symbol | Strategy | Activation | Status | Planner | Blockers |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in translation["records"]:
        lines.append(
            "| {source} | {tweet_type} | {symbol} | {strategy} | {activation} | {status} | {planner} | {blockers} |".format(
                source=record["signal_id"],
                tweet_type=record["tweet_type"],
                symbol=record["symbol"] or "-",
                strategy=record["strategy_family"] or "-",
                activation=record["activation"]["status"],
                status=record["status"],
                planner=str(record["planner_eligible"]).lower(),
                blockers=", ".join(record["planner_blockers"]) or "-",
            )
        )
    lines.extend([
        "",
        "Planner ideas are structured local inputs only. This import did not run the planner,",
        "write a Sheet, create a shadow fill, contact a broker, or submit an order.",
        "",
    ])
    return "\n".join(lines)


def _record_id(record: dict[str, Any], *, profile_text: str) -> str:
    identity = {
        "translator_version": TRANSLATOR_VERSION,
        "profile_sha256": hashlib.sha256(profile_text.encode()).hexdigest(),
        "record": {key: value for key, value in record.items() if key != "record_id"},
    }
    return "corrsig_" + hashlib.sha256(_stable_json(identity).encode()).hexdigest()[:20]


def _logical_record_id(record: dict[str, Any]) -> str:
    identity = f"{record['profile_id']}|{record['signal_id']}|{record.get('symbol') or '-'}"
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def _validate_regex_rule(rule: object, label: str) -> None:
    if not isinstance(rule, dict) or not rule.get("regex"):
        raise ValueError(f"{label} rule requires regex")
    try:
        re.compile(str(rule["regex"]), flags=re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid {label} regex: {exc}") from exc


def _assert_idempotent(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") != content:
        raise RuntimeError(f"idempotent artifact collision: {path}")


def _timestamp(value: object, label: str) -> None:
    _parse_timestamp(_text(value, label), label=label)


def _parse_timestamp(value: object, *, label: str = "timestamp") -> datetime:
    try:
        parsed = _TIMESTAMP.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"{label} must be a valid timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _text(value: object, label: str) -> str:
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError(f"{label} is required")
    return " ".join(str(value).split())


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        raise ValueError("profile list value must be an array")
    return [str(item).strip() for item in value if str(item).strip()]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "insufficient_evidence"


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _effects() -> dict[str, bool]:
    return {
        "planner_run": False,
        "shadow_admission": False,
        "live_admission": False,
        "sheet_write": False,
        "broker": False,
        "orders": False,
        "external_send": False,
    }
