"""Extract source-observed option packages without creating trading intent.

This module stops at evidence.  It does not create an ``Idea``, planner source
mode, candidate, plan, ticket, fill, or lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from kamandal_v2.intelligence.llm_client import JsonLlmClient

EXTRACTION_SCHEMA = "kamandal.observed_package_extraction.v1"
EVIDENCE_SCHEMA = "kamandal.observed_package_evidence.v1"
PROMPT_VERSION = "observed-package-extractor-v1"

_POST_DISPOSITIONS = {"packages", "commentary", "unreadable"}
_PACKAGE_ACTIONS = {"open", "close", "roll", "adjust"}
_ORDER_CODES = {
    "BTO": ("buy", "open"),
    "STO": ("sell", "open"),
    "BTC": ("buy", "close"),
    "STC": ("sell", "close"),
}
_OPTION_TYPES = {"call", "put"}
_PRICE_EFFECTS = {"debit", "credit"}
_SYMBOL = re.compile(r"^[A-Z0-9/.-]{1,20}$")
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


class ObservedPackageValidationError(ValueError):
    """The model output cannot be retained as deterministic evidence."""

    def __init__(self, message: str, *, raw_output: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.raw_output = dict(raw_output) if raw_output is not None else None


@dataclass(frozen=True, slots=True)
class ObservedLegEvidence:
    quantity: int | None
    expiration: str | None
    strike: str | None
    option_type: str | None
    order_code: str | None
    side: str | None
    effect: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "quantity": self.quantity,
            "expiration": self.expiration,
            "strike": self.strike,
            "option_type": self.option_type,
            "order_code": self.order_code,
            "side": self.side,
            "effect": self.effect,
        }


@dataclass(frozen=True, slots=True)
class ObservedPackageEvidence:
    source_event_id: str
    source_profile: str
    canonical_post_id: str
    media_index: int
    package_position: int
    action: str
    structure: str | None
    symbol: str
    product_type: str
    displayed_trade_time: str | None
    displayed_price: Mapping[str, str] | None
    complete: bool
    blocker: str | None
    legs: tuple[ObservedLegEvidence, ...]
    package_signature: str | None
    evidence_revision_id: str
    image_sha256: str
    prompt_sha256: str
    output_sha256: str
    opportunity_group_id: str | None = None
    prompt_version: str = PROMPT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EVIDENCE_SCHEMA,
            "source_event_id": self.source_event_id,
            "source_profile": self.source_profile,
            "canonical_post_id": self.canonical_post_id,
            "source_locator": {
                "media_index": self.media_index,
                "package_position": self.package_position,
            },
            "action": self.action,
            "structure": self.structure,
            "symbol": self.symbol,
            "product_type": self.product_type,
            "displayed_trade_time": self.displayed_trade_time,
            "displayed_price": dict(self.displayed_price) if self.displayed_price else None,
            "complete": self.complete,
            "blocker": self.blocker,
            "legs": [leg.to_dict() for leg in self.legs],
            "package_signature": self.package_signature,
            "evidence_revision_id": self.evidence_revision_id,
            "opportunity_group_id": self.opportunity_group_id,
            "provenance": {
                "image_sha256": self.image_sha256,
                "prompt_sha256": self.prompt_sha256,
                "output_sha256": self.output_sha256,
                "prompt_version": self.prompt_version,
            },
        }


@dataclass(frozen=True, slots=True)
class ObservedPackageBatch:
    source_profile: str
    canonical_post_id: str
    post_disposition: str
    post_blocker: str | None
    packages: tuple[ObservedPackageEvidence, ...]
    prompt_sha256: str
    output_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "kamandal.observed_package_batch.v1",
            "source_profile": self.source_profile,
            "canonical_post_id": self.canonical_post_id,
            "post_disposition": self.post_disposition,
            "post_blocker": self.post_blocker,
            "packages": [package.to_dict() for package in self.packages],
            "prompt_sha256": self.prompt_sha256,
            "output_sha256": self.output_sha256,
            "effects": {
                "idea_created": False,
                "plan_created": False,
                "ticket_created": False,
                "fill_created": False,
                "lifecycle_created": False,
                "broker_effects": False,
            },
        }


def extract_observed_packages(
    client: JsonLlmClient,
    *,
    source_profile: str,
    canonical_post_id: str,
    published_at: str,
    post_text: str,
    image_paths: tuple[str | Path, ...],
) -> ObservedPackageBatch:
    """Ask the shared model labor for transcription, then validate it locally."""

    resolved_images = tuple(Path(path).expanduser().resolve() for path in image_paths)
    if not resolved_images:
        raise ObservedPackageValidationError("at least one public evidence image is required")
    for image in resolved_images:
        if not image.is_file():
            raise ObservedPackageValidationError(f"image does not exist: {image}")

    system_prompt = _system_prompt()
    user_prompt = _user_prompt(
        source_profile=source_profile,
        canonical_post_id=canonical_post_id,
        published_at=published_at,
        post_text=post_text,
        image_count=len(resolved_images),
    )
    raw = client.chat_json(
        system_prompt,
        user_prompt,
        images=tuple(str(path) for path in resolved_images),
    )
    try:
        return normalize_observed_package_output(
            raw,
            source_profile=source_profile,
            canonical_post_id=canonical_post_id,
            published_at=published_at,
            image_sha256=tuple(_file_sha256(path) for path in resolved_images),
            prompt_sha256=_sha256_text(f"{system_prompt}\n\n{user_prompt}"),
        )
    except ObservedPackageValidationError as exc:
        raise ObservedPackageValidationError(str(exc), raw_output=raw) from exc


def extract_observed_packages_from_correspondent_signal(
    client: JsonLlmClient,
    signal: Mapping[str, Any],
) -> ObservedPackageBatch:
    """Consume Birdclaw's sanitized correspondent seam, never its raw store."""

    if signal.get("schema") != "birdclaw.correspondent_signal.v1":
        raise ObservedPackageValidationError("unexpected correspondent signal schema")
    source_profile = _required_text(signal.get("profile_id"), "profile_id")
    source = signal.get("source")
    literal = signal.get("literal")
    if not isinstance(source, Mapping) or not isinstance(literal, Mapping):
        raise ObservedPackageValidationError("correspondent signal requires source and literal objects")
    if source.get("kind") != "public_x_post":
        raise ObservedPackageValidationError("observed package source must be a public X post")
    canonical_post_id = _required_text(source.get("source_id"), "source.source_id")
    published_at = _required_text(source.get("published_at"), "source.published_at")
    post_text = _required_text(literal.get("text"), "literal.text")
    media = source.get("media")
    if not isinstance(media, list) or not media:
        raise ObservedPackageValidationError("correspondent signal has no public media")

    ordered_media = sorted(media, key=lambda item: int(item.get("media_index") or 0) if isinstance(item, Mapping) else 0)
    image_paths: list[Path] = []
    for expected_index, item in enumerate(ordered_media, start=1):
        if not isinstance(item, Mapping):
            raise ObservedPackageValidationError("correspondent media descriptor must be an object")
        if item.get("type") != "photo" or item.get("cache_status") != "cached":
            raise ObservedPackageValidationError(f"media {expected_index} is not a cached public photo")
        if int(item.get("media_index") or 0) != expected_index:
            raise ObservedPackageValidationError("correspondent media indexes must be contiguous from one")
        artifact = Path(_required_text(item.get("artifact_path"), f"media[{expected_index}].artifact_path")).resolve()
        if not artifact.is_file():
            raise ObservedPackageValidationError(f"cached media artifact does not exist: {artifact}")
        expected_sha = _required_text(item.get("sha256"), f"media[{expected_index}].sha256").lower()
        if _file_sha256(artifact) != expected_sha:
            raise ObservedPackageValidationError(f"cached media hash mismatch at index {expected_index}")
        image_paths.append(artifact)

    return extract_observed_packages(
        client,
        source_profile=source_profile,
        canonical_post_id=canonical_post_id,
        published_at=published_at,
        post_text=post_text,
        image_paths=tuple(image_paths),
    )


def observed_package_batch_from_dict(raw: Mapping[str, Any]) -> ObservedPackageBatch:
    """Rehydrate one locally persisted, already-normalized evidence batch.

    The activation job writes this contract and the later planning job reads it.
    Re-validating the shape here keeps that file seam typed instead of allowing
    arbitrary JSON to enter candidate construction.
    """

    if raw.get("schema") != "kamandal.observed_package_batch.v1":
        raise ObservedPackageValidationError("unexpected observed package batch schema")
    source_profile = _required_text(raw.get("source_profile"), "source_profile")
    canonical_post_id = _required_text(raw.get("canonical_post_id"), "canonical_post_id")
    disposition = _choice(raw.get("post_disposition"), _POST_DISPOSITIONS, "post_disposition")
    post_blocker = _optional_text(raw.get("post_blocker"))
    packages_raw = raw.get("packages")
    if not isinstance(packages_raw, list):
        raise ObservedPackageValidationError("batch packages must be a list")
    packages: list[ObservedPackageEvidence] = []
    for index, item in enumerate(packages_raw, start=1):
        if not isinstance(item, Mapping) or item.get("schema") != EVIDENCE_SCHEMA:
            raise ObservedPackageValidationError(f"batch package {index} has an invalid schema")
        locator = item.get("source_locator")
        provenance = item.get("provenance")
        legs_raw = item.get("legs")
        if not isinstance(locator, Mapping) or not isinstance(provenance, Mapping) or not isinstance(legs_raw, list):
            raise ObservedPackageValidationError(f"batch package {index} is incomplete")
        legs: list[ObservedLegEvidence] = []
        for leg_index, leg in enumerate(legs_raw, start=1):
            if not isinstance(leg, Mapping):
                raise ObservedPackageValidationError(f"batch package {index} leg {leg_index} is invalid")
            quantity = leg.get("quantity")
            if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0):
                raise ObservedPackageValidationError(f"batch package {index} leg {leg_index} quantity is invalid")
            legs.append(
                ObservedLegEvidence(
                    quantity=quantity,
                    expiration=_optional_text(leg.get("expiration")),
                    strike=_optional_text(leg.get("strike")),
                    option_type=_optional_text(leg.get("option_type")),
                    order_code=_optional_text(leg.get("order_code")),
                    side=_optional_text(leg.get("side")),
                    effect=_optional_text(leg.get("effect")),
                )
            )
        displayed_price = item.get("displayed_price")
        if displayed_price is not None and not isinstance(displayed_price, Mapping):
            raise ObservedPackageValidationError(f"batch package {index} displayed_price is invalid")
        package = ObservedPackageEvidence(
            source_event_id=_required_text(item.get("source_event_id"), f"packages[{index}].source_event_id"),
            source_profile=source_profile,
            canonical_post_id=canonical_post_id,
            media_index=int(locator.get("media_index") or 0),
            package_position=int(locator.get("package_position") or 0),
            action=_required_text(item.get("action"), f"packages[{index}].action"),
            structure=_optional_text(item.get("structure")),
            symbol=_required_text(item.get("symbol"), f"packages[{index}].symbol"),
            product_type=_required_text(item.get("product_type"), f"packages[{index}].product_type"),
            displayed_trade_time=_optional_text(item.get("displayed_trade_time")),
            displayed_price=dict(displayed_price) if displayed_price is not None else None,
            complete=bool(item.get("complete")),
            blocker=_optional_text(item.get("blocker")),
            legs=tuple(legs),
            package_signature=_optional_text(item.get("package_signature")),
            evidence_revision_id=_required_text(item.get("evidence_revision_id"), f"packages[{index}].evidence_revision_id"),
            image_sha256=_required_text(provenance.get("image_sha256"), f"packages[{index}].provenance.image_sha256"),
            prompt_sha256=_required_text(provenance.get("prompt_sha256"), f"packages[{index}].provenance.prompt_sha256"),
            output_sha256=_required_text(provenance.get("output_sha256"), f"packages[{index}].provenance.output_sha256"),
            opportunity_group_id=_optional_text(item.get("opportunity_group_id")),
            prompt_version=_optional_text(provenance.get("prompt_version")) or PROMPT_VERSION,
        )
        if package.action not in _PACKAGE_ACTIONS or package.media_index <= 0 or package.package_position <= 0:
            raise ObservedPackageValidationError(f"batch package {index} has invalid identity fields")
        packages.append(package)
    return ObservedPackageBatch(
        source_profile=source_profile,
        canonical_post_id=canonical_post_id,
        post_disposition=disposition,
        post_blocker=post_blocker,
        packages=tuple(packages),
        prompt_sha256=_required_text(raw.get("prompt_sha256"), "prompt_sha256"),
        output_sha256=_required_text(raw.get("output_sha256"), "output_sha256"),
    )


def load_observed_package_feed(path: str | Path) -> tuple[ObservedPackageBatch, ...]:
    """Load the activation-to-planner feed and verify its declared checksum."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema") != "kamandal.observed_package_feed.v1":
        raise ObservedPackageValidationError("unexpected observed package feed schema")
    batches_raw = payload.get("batches")
    if not isinstance(batches_raw, list):
        raise ObservedPackageValidationError("observed package feed batches must be a list")
    declared_sha = _required_text(payload.get("batches_sha256"), "batches_sha256")
    if declared_sha != _sha256_text(_stable_json(batches_raw)):
        raise ObservedPackageValidationError("observed package feed checksum mismatch")
    return tuple(observed_package_batch_from_dict(item) for item in batches_raw if isinstance(item, Mapping))


def normalize_observed_package_output(
    raw: Mapping[str, Any],
    *,
    source_profile: str,
    canonical_post_id: str,
    published_at: str,
    image_sha256: tuple[str, ...],
    prompt_sha256: str,
) -> ObservedPackageBatch:
    """Turn one raw extraction into immutable, revision-safe evidence."""

    _require_exact_keys(raw, {"schema", "post_disposition", "post_blocker", "packages"}, "output")
    if raw.get("schema") != EXTRACTION_SCHEMA:
        raise ObservedPackageValidationError(f"unexpected extraction schema: {raw.get('schema')!r}")
    disposition = _choice(raw.get("post_disposition"), _POST_DISPOSITIONS, "post_disposition")
    post_blocker = _optional_text(raw.get("post_blocker"))
    packages_raw = raw.get("packages")
    if not isinstance(packages_raw, list):
        raise ObservedPackageValidationError("packages must be a list")
    if disposition == "packages" and not packages_raw:
        raise ObservedPackageValidationError("packages disposition requires at least one package")
    if disposition != "packages" and packages_raw:
        raise ObservedPackageValidationError("commentary/unreadable output cannot contain packages")
    if disposition == "unreadable" and not post_blocker:
        raise ObservedPackageValidationError("unreadable output requires post_blocker")

    publication_date = _parse_timestamp(published_at).date()
    output_sha256 = _sha256_text(_stable_json(raw))
    seen_locators: set[tuple[int, int]] = set()
    packages: list[ObservedPackageEvidence] = []
    for package_raw in packages_raw:
        package = _normalize_package(
            package_raw,
            source_profile=source_profile,
            canonical_post_id=canonical_post_id,
            publication_date=publication_date,
            image_sha256=image_sha256,
            prompt_sha256=prompt_sha256,
            output_sha256=output_sha256,
        )
        locator = (package.media_index, package.package_position)
        if locator in seen_locators:
            raise ObservedPackageValidationError(f"duplicate source locator: {locator}")
        seen_locators.add(locator)
        packages.append(package)

    return ObservedPackageBatch(
        source_profile=_required_text(source_profile, "source_profile"),
        canonical_post_id=_required_text(canonical_post_id, "canonical_post_id"),
        post_disposition=disposition,
        post_blocker=post_blocker,
        packages=tuple(packages),
        prompt_sha256=prompt_sha256,
        output_sha256=output_sha256,
    )


def _normalize_package(
    raw: Any,
    *,
    source_profile: str,
    canonical_post_id: str,
    publication_date: date,
    image_sha256: tuple[str, ...],
    prompt_sha256: str,
    output_sha256: str,
) -> ObservedPackageEvidence:
    if not isinstance(raw, Mapping):
        raise ObservedPackageValidationError("each package must be an object")
    _require_exact_keys(
        raw,
        {
            "media_index",
            "package_position",
            "action",
            "symbol",
            "displayed_trade_time",
            "displayed_price",
            "complete",
            "blocker",
            "legs",
        },
        "package",
    )
    media_index = _positive_int(raw.get("media_index"), "media_index")
    package_position = _positive_int(raw.get("package_position"), "package_position")
    if media_index > len(image_sha256):
        raise ObservedPackageValidationError(f"media_index {media_index} exceeds supplied image count")
    action = _choice(raw.get("action"), _PACKAGE_ACTIONS, "action")
    symbol = _required_text(raw.get("symbol"), "symbol").upper()
    if not _SYMBOL.fullmatch(symbol):
        raise ObservedPackageValidationError(f"invalid displayed symbol: {symbol!r}")
    displayed_trade_time = _optional_text(raw.get("displayed_trade_time"))
    displayed_price = _normalize_price(raw.get("displayed_price"))
    if not isinstance(raw.get("complete"), bool):
        raise ObservedPackageValidationError("complete must be a boolean")
    complete = bool(raw["complete"])
    blocker = _optional_text(raw.get("blocker"))
    if complete and blocker:
        raise ObservedPackageValidationError("complete package cannot have a blocker")
    if not complete and not blocker:
        raise ObservedPackageValidationError("incomplete package requires a blocker")

    legs_raw = raw.get("legs")
    if not isinstance(legs_raw, list):
        raise ObservedPackageValidationError("legs must be a list")
    legs = tuple(_normalize_leg(leg, publication_date=publication_date, complete=complete) for leg in legs_raw)
    if complete and not legs:
        raise ObservedPackageValidationError("complete package requires at least one leg")
    if complete:
        effects = {leg.effect for leg in legs}
        if action == "open" and effects != {"open"}:
            raise ObservedPackageValidationError("open package must contain only opening legs")
        if action == "close" and effects != {"close"}:
            raise ObservedPackageValidationError("close package must contain only closing legs")
        if action in {"roll", "adjust"} and not {"open", "close"}.issubset(effects):
            raise ObservedPackageValidationError(f"{action} package requires opening and closing legs")

    structure = _infer_corpus_structure(legs, action=action) if complete else None
    locator_text = f"{source_profile}|{canonical_post_id}|media:{media_index}|package:{package_position}"
    source_event_id = f"ose_{_sha256_text(locator_text)[:24]}"
    package_signature = _package_signature(legs) if complete else None
    revision_payload = {
        "source_event_id": source_event_id,
        "image_sha256": image_sha256[media_index - 1],
        "schema": EVIDENCE_SCHEMA,
        "prompt_sha256": prompt_sha256,
        "output_sha256": output_sha256,
    }
    evidence_revision_id = f"orev_{_sha256_text(_stable_json(revision_payload))[:24]}"
    return ObservedPackageEvidence(
        source_event_id=source_event_id,
        source_profile=source_profile,
        canonical_post_id=canonical_post_id,
        media_index=media_index,
        package_position=package_position,
        action=action,
        structure=structure,
        symbol=symbol,
        product_type=_product_type(symbol),
        displayed_trade_time=displayed_trade_time,
        displayed_price=displayed_price,
        complete=complete,
        blocker=blocker,
        legs=legs,
        package_signature=package_signature,
        evidence_revision_id=evidence_revision_id,
        image_sha256=image_sha256[media_index - 1],
        prompt_sha256=prompt_sha256,
        output_sha256=output_sha256,
    )


def _normalize_leg(raw: Any, *, publication_date: date, complete: bool) -> ObservedLegEvidence:
    if not isinstance(raw, Mapping):
        raise ObservedPackageValidationError("each leg must be an object")
    _require_exact_keys(raw, {"quantity", "expiration", "strike", "option_type", "order_code"}, "leg")
    quantity = _optional_positive_int(raw.get("quantity"), "quantity")
    expiration_raw = _optional_text(raw.get("expiration"))
    expiration = _normalize_expiration(expiration_raw, publication_date) if expiration_raw else None
    strike = _normalize_decimal(raw.get("strike"))
    option_type_raw = _optional_text(raw.get("option_type"))
    option_type = option_type_raw.lower() if option_type_raw else None
    if option_type is not None and option_type not in _OPTION_TYPES:
        raise ObservedPackageValidationError(f"invalid option_type: {option_type!r}")
    order_code_raw = _optional_text(raw.get("order_code"))
    order_code = order_code_raw.upper() if order_code_raw else None
    if order_code is not None and order_code not in _ORDER_CODES:
        raise ObservedPackageValidationError(f"invalid order_code: {order_code!r}")
    if complete and None in {quantity, expiration, strike, option_type, order_code}:
        raise ObservedPackageValidationError("complete package has an incomplete leg")
    side, effect = _ORDER_CODES[order_code] if order_code else (None, None)
    return ObservedLegEvidence(
        quantity=quantity,
        expiration=expiration,
        strike=strike,
        option_type=option_type,
        order_code=order_code,
        side=side,
        effect=effect,
    )


def _normalize_price(raw: Any) -> Mapping[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ObservedPackageValidationError("displayed_price must be null or an object")
    _require_exact_keys(raw, {"amount", "effect"}, "displayed_price")
    amount = _normalize_decimal(raw.get("amount"))
    effect = _choice(raw.get("effect"), _PRICE_EFFECTS, "displayed_price.effect")
    if amount is None:
        raise ObservedPackageValidationError("displayed_price.amount is required")
    return {"amount": amount, "effect": effect}


def _normalize_expiration(value: str, publication_date: date) -> str:
    text = value.strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    match = re.fullmatch(r"([A-Za-z]{3})\s+(\d{1,2})", text)
    if not match or match.group(1).lower() not in _MONTHS:
        raise ObservedPackageValidationError(f"invalid expiration: {value!r}")
    month = _MONTHS[match.group(1).lower()]
    day = int(match.group(2))
    year = publication_date.year
    candidate = date(year, month, day)
    if candidate < publication_date:
        candidate = date(year + 1, month, day)
    return candidate.isoformat()


def _package_signature(legs: tuple[ObservedLegEvidence, ...]) -> str:
    canonical = sorted(
        (leg.to_dict() for leg in legs),
        key=lambda leg: (
            str(leg["expiration"]),
            str(leg["option_type"]),
            Decimal(str(leg["strike"])),
            str(leg["order_code"]),
            int(leg["quantity"]),
        ),
    )
    return f"opkg_{_sha256_text(_stable_json(canonical))[:24]}"


def _product_type(symbol: str) -> str:
    if symbol.startswith("/"):
        return "futures_option"
    if symbol in {"SPX", "NDX", "RUT", "VIX"}:
        return "index_option"
    return "equity_or_etf_option"


def _infer_corpus_structure(legs: tuple[ObservedLegEvidence, ...], *, action: str) -> str | None:
    """Recognize only structures proven by the browser-grounded corpus."""

    if action in {"roll", "adjust"}:
        return None
    opening_legs = tuple(_as_opening_leg(leg) for leg in legs)
    if len(opening_legs) == 2:
        first, second = opening_legs
        if (
            first.expiration == second.expiration
            and first.strike == second.strike
            and {first.option_type, second.option_type} == {"call", "put"}
            and {first.order_code, second.order_code} in ({"STO"}, {"BTO"})
        ):
            prefix = "short" if first.order_code == "STO" else "long"
            return f"{prefix}_straddle"
        if first.option_type == second.option_type and first.expiration != second.expiration:
            near, far = sorted(opening_legs, key=lambda leg: str(leg.expiration))
            if near.order_code == "STO" and far.order_code == "BTO":
                kind = "calendar" if near.strike == far.strike else "diagonal"
                return f"{first.option_type}_{kind}"
    if len(opening_legs) == 3 and _is_butterfly(opening_legs):
        return f"{opening_legs[0].option_type}_butterfly"
    if len(opening_legs) == 4:
        calendar_type = _double_calendar_type(opening_legs)
        if calendar_type:
            return calendar_type
        super_type = _super_spread_type(opening_legs)
        if super_type:
            return super_type
    raise ObservedPackageValidationError("complete package does not match a corpus-proven structure")


def _as_opening_leg(leg: ObservedLegEvidence) -> ObservedLegEvidence:
    if leg.order_code in {"BTO", "STO"}:
        return leg
    opening_code = {"BTC": "STO", "STC": "BTO"}.get(str(leg.order_code))
    side, effect = _ORDER_CODES[opening_code] if opening_code else (None, None)
    return ObservedLegEvidence(
        quantity=leg.quantity,
        expiration=leg.expiration,
        strike=leg.strike,
        option_type=leg.option_type,
        order_code=opening_code,
        side=side,
        effect=effect,
    )


def _is_butterfly(legs: tuple[ObservedLegEvidence, ...]) -> bool:
    if len({leg.expiration for leg in legs}) != 1 or len({leg.option_type for leg in legs}) != 1:
        return False
    ordered = sorted(legs, key=lambda leg: Decimal(str(leg.strike)))
    return (
        [leg.quantity for leg in ordered] == [1, 2, 1]
        and [leg.order_code for leg in ordered] == ["BTO", "STO", "BTO"]
    )


def _double_calendar_type(legs: tuple[ObservedLegEvidence, ...]) -> str | None:
    if len({leg.option_type for leg in legs}) != 1:
        return None
    expirations = sorted({str(leg.expiration) for leg in legs})
    strikes = sorted({str(leg.strike) for leg in legs}, key=Decimal)
    if len(expirations) != 2 or len(strikes) != 2:
        return None
    near, far = expirations
    for strike in strikes:
        pair = {(str(leg.expiration), leg.order_code) for leg in legs if leg.strike == strike}
        if pair != {(near, "STO"), (far, "BTO")}:
            return None
    return f"double_{legs[0].option_type}_calendar"


def _super_spread_type(legs: tuple[ObservedLegEvidence, ...]) -> str | None:
    if len({leg.expiration for leg in legs}) != 1:
        return None
    by_type = {kind: sorted((leg for leg in legs if leg.option_type == kind), key=lambda leg: Decimal(str(leg.strike))) for kind in ("put", "call")}
    if any(len(group) != 2 for group in by_type.values()):
        return None
    codes = tuple(tuple(leg.order_code for leg in by_type[kind]) for kind in ("put", "call"))
    if codes == (("BTO", "STO"), ("BTO", "STO")):
        return "super_bull"
    if codes == (("STO", "BTO"), ("STO", "BTO")):
        return "super_bear"
    return None


def _system_prompt() -> str:
    return f"""\
You transcribe visible option-package facts from public trading-post images.
Do not recommend a trade, choose or repair a leg, infer an unreadable value, score risk, or decide execution.
Read packages top-to-bottom within each supplied image. media_index is the 1-based image order; package_position is the 1-based visual package position within that image.
Within each package, transcribe leg rows top-to-bottom exactly as displayed. Do not regroup, alternate, pair, or reorder calendar legs.
Use exact displayed order codes BTO, STO, BTC, or STC. Use expiration text exactly as displayed (for example Aug 28). Use positive quantities.
Set complete=false with one concise blocker whenever any required leg field or package grouping is unreadable or ambiguous.
Return JSON only with exactly this shape:
{{
  "schema": "{EXTRACTION_SCHEMA}",
  "post_disposition": "packages|commentary|unreadable",
  "post_blocker": null,
  "packages": [
    {{
      "media_index": 1,
      "package_position": 1,
      "action": "open|close|roll|adjust",
      "symbol": "ADSK",
      "displayed_trade_time": "11:14a today",
      "displayed_price": {{"amount": "1.50", "effect": "debit"}},
      "complete": true,
      "blocker": null,
      "legs": [
        {{"quantity": 1, "expiration": "Aug 28", "strike": "290", "option_type": "call", "order_code": "STO"}}
      ]
    }}
  ]
}}
For a missing displayed package price use null. For commentary return an empty packages list. For an unreadable post return an empty packages list and a post_blocker.
"""


def _user_prompt(
    *,
    source_profile: str,
    canonical_post_id: str,
    published_at: str,
    post_text: str,
    image_count: int,
) -> str:
    return "\n".join(
        [
            f"source_profile: {source_profile}",
            f"canonical_post_id: {canonical_post_id}",
            f"published_at: {published_at}",
            f"image_count: {image_count}",
            "post_text:",
            post_text.strip(),
            "Transcribe only the observable package facts.",
        ]
    )


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservedPackageValidationError(f"published_at must be ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ObservedPackageValidationError("published_at must include a timezone")
    return parsed


def _require_exact_keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    keys = set(raw)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise ObservedPackageValidationError(f"{label} keys mismatch; missing={missing}, extra={extra}")


def _choice(value: Any, allowed: set[str], label: str) -> str:
    text = _required_text(value, label).lower()
    if text not in allowed:
        raise ObservedPackageValidationError(f"invalid {label}: {text!r}")
    return text


def _required_text(value: Any, label: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ObservedPackageValidationError(f"{label} is required")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_int(value: Any, label: str) -> int:
    result = _optional_positive_int(value, label)
    if result is None:
        raise ObservedPackageValidationError(f"{label} is required")
    return result


def _optional_positive_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ObservedPackageValidationError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ObservedPackageValidationError(f"{label} must be a positive integer") from exc
    if result <= 0 or str(result) != str(value).strip():
        raise ObservedPackageValidationError(f"{label} must be a positive integer")
    return result


def _normalize_decimal(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ObservedPackageValidationError(f"invalid decimal: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise ObservedPackageValidationError(f"invalid non-negative decimal: {value!r}")
    text = format(number.normalize(), "f")
    return "0" if text == "-0" else text


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
