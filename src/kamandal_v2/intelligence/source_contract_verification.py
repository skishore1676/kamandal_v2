"""Independent image transcription gate for source-exact live openings.

The source-episode interpreter proposes an opening. A separate, image-focused
extractor must agree on its displayed contracts before the live planner sees
that package as source verified. Disagreement leaves the original evidence
available for shadow/review and never authorizes an entry.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from kamandal_v2.intelligence.llm_client import JsonLlmClient
from kamandal_v2.intelligence.observed_packages import (
    PROMPT_VERSION,
    ObservedPackageBatch,
    ObservedPackageEvidence,
    observed_package_batch_from_dict,
    extract_observed_packages_from_correspondent_signal,
)


def verify_live_source_contracts(
    batches: Iterable[ObservedPackageBatch],
    packet: Mapping[str, Any],
    *,
    live_structures: Iterable[str],
    client: JsonLlmClient | None,
    cache_root: str | Path,
) -> tuple[tuple[ObservedPackageBatch, ...], tuple[dict[str, str], ...]]:
    """Verify only live-permitted exact shapes, using cached public evidence."""

    permitted = set(live_structures)
    records = {
        str(item.get("signal_id") or ""): item
        for item in packet.get("records") or [] if isinstance(item, Mapping)
    }
    root = Path(cache_root)
    verified_batches: list[ObservedPackageBatch] = []
    failures: list[dict[str, str]] = []
    for batch in batches:
        targets = [package for package in batch.packages if package.action == "open" and package.structure in permitted]
        if not targets:
            verified_batches.append(batch)
            continue
        record = records.get(batch.canonical_post_id)
        independent: ObservedPackageBatch | None = None
        error = ""
        if record is None:
            error = "source_record_missing_for_verification"
        elif client is None:
            error = "source_verifier_unavailable"
        else:
            try:
                independent = _cached_extraction(record, client, root)
            except Exception as exc:  # noqa: BLE001 - exact entry fails closed; idea output remains usable.
                error = f"source_verifier_failed:{type(exc).__name__}"
        updated: list[ObservedPackageEvidence] = []
        for package in batch.packages:
            if package not in targets:
                updated.append(package)
                continue
            reason = error or _disagreement(package, independent)
            reference = _verification_reference(independent) if independent is not None else ""
            revision = "orev_" + _sha("|".join((package.evidence_revision_id, reference, reason)))[:24]
            revised = replace(
                package,
                source_verified=not reason,
                source_verification_ref=reference or None,
                source_verification_reason=reason or None,
                evidence_revision_id=revision,
            )
            updated.append(revised)
            if reason:
                failures.append({"source_id": package.source_event_id,
                                 "post_ref": batch.canonical_post_id, "reason": reason})
        output_sha = _sha(_stable_json([item.to_dict() for item in updated]))
        updated = [replace(item, output_sha256=output_sha) for item in updated]
        verified_batches.append(replace(batch, packages=tuple(updated), output_sha256=output_sha))
    return tuple(verified_batches), tuple(failures)


def _cached_extraction(
    record: Mapping[str, Any], client: JsonLlmClient, root: Path
) -> ObservedPackageBatch:
    source = record.get("source") or {}
    media = source.get("media") or []
    if not isinstance(media, list) or not media:
        raise ValueError("source media unavailable for verification")
    for item in media:
        if not isinstance(item, Mapping):
            raise ValueError("source media descriptor invalid")
        artifact = Path(str(item.get("artifact_path") or ""))
        expected = str(item.get("sha256") or "").lower()
        if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
            raise ValueError("source media hash mismatch")
    key = _sha(_stable_json({
        "post_ref": record.get("signal_id"),
        "published_at": source.get("published_at"),
        "text": (record.get("literal") or {}).get("text"),
        "media_sha256": [item.get("sha256") for item in media if isinstance(item, Mapping)],
        "extractor_version": PROMPT_VERSION,
        "extractor_code_sha256": hashlib.sha256(
            Path(__file__).with_name("observed_packages.py").read_bytes()
        ).hexdigest(),
    }))
    profile_key = _sha(str(record.get("profile_id") or "unknown"))[:16]
    path = root / profile_key / f"{key}.json"
    if path.is_file():
        result = observed_package_batch_from_dict(json.loads(path.read_text(encoding="utf-8")))
        if (result.source_profile != record.get("profile_id")
                or result.canonical_post_id != source.get("source_id")):
            raise ValueError("source verification cache identity mismatch")
        return result
    result = extract_observed_packages_from_correspondent_signal(client, record)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return result


def _disagreement(package: ObservedPackageEvidence, independent: ObservedPackageBatch | None) -> str:
    if independent is None:
        return "source_verifier_unavailable"
    openings = [item for item in independent.packages
                if item.action == "open" and item.symbol == package.symbol]
    if len(openings) != package.source_opening_package_count:
        return "source_package_count_disagrees_with_image"
    matches = [item for item in openings
               if item.complete and item.package_signature == package.package_signature
               and item.media_index == package.media_index and item.image_sha256 == package.image_sha256]
    if len(matches) != 1:
        return "source_contracts_disagree_with_image"
    match = matches[0]
    if match.structure != package.structure:
        return "source_structure_disagrees_with_image"
    if not _same_price(match.displayed_price, package.displayed_price):
        return "source_price_disagrees_with_image"
    return ""


def _same_price(first: Mapping[str, str] | None, second: Mapping[str, str] | None) -> bool:
    if first is None or second is None:
        return first is None and second is None
    try:
        return (first.get("effect") == second.get("effect")
                and Decimal(str(first.get("amount"))) == Decimal(str(second.get("amount"))))
    except (InvalidOperation, ValueError):
        return False


def _verification_reference(batch: ObservedPackageBatch | None) -> str:
    return "sv_" + _sha(_stable_json(batch.to_dict()))[:24] if batch is not None else ""


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
