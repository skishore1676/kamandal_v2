from __future__ import annotations

import hashlib
import json
from datetime import date
from copy import deepcopy
from pathlib import Path

import pytest

from kamandal_v2.intelligence.observed_packages import (
    ObservedPackageValidationError,
    extract_observed_packages,
    extract_observed_packages_from_correspondent_signal,
    load_observed_package_feed,
    normalize_observed_package_output,
    _normalize_expiration,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "mike_observed_packages"
GROUND_TRUTH = FIXTURE_ROOT / "ground-truth.json"


@pytest.mark.parametrize("value", ["Aug 28 2026", "Aug 28, 2026", "2026-08-28"])
def test_explicit_expiration_year_is_preserved_even_for_historical_package(value):
    assert _normalize_expiration(value, date(2026, 9, 5)) == "2026-08-28"


def test_yearless_expiration_keeps_existing_rollover_and_incomplete_date_rejects():
    assert _normalize_expiration("Jan 15", date(2026, 12, 5)) == "2027-01-15"
    with pytest.raises(ValueError):
        _normalize_expiration("Sep 2026", date(2026, 9, 5))


def _manifest() -> dict:
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))


def _normalize(fixture: dict, raw: dict | None = None):
    return normalize_observed_package_output(
        raw or fixture["expected_extraction"],
        source_profile="mike_butler",
        canonical_post_id=fixture["post_id"],
        published_at=fixture["published_at"],
        image_sha256=tuple(image["sha256"] for image in fixture["images"]),
        prompt_sha256="1" * 64,
    )


def test_browser_ground_truth_images_and_packages_are_complete() -> None:
    manifest = _manifest()
    assert manifest["schema"] == "kamandal.observed_package_calibration.v1"
    assert len(manifest["fixtures"]) == 6

    actions: set[str] = set()
    symbols: set[str] = set()
    package_count = 0
    source_event_ids: set[str] = set()
    for fixture in manifest["fixtures"]:
        for image in fixture["images"]:
            image_path = FIXTURE_ROOT / image["path"]
            assert hashlib.sha256(image_path.read_bytes()).hexdigest() == image["sha256"]
        batch = _normalize(fixture)
        package_count += len(batch.packages)
        for package in batch.packages:
            assert package.complete is True
            assert package.package_signature
            assert package.source_event_id not in source_event_ids
            source_event_ids.add(package.source_event_id)
            actions.add(package.action)
            symbols.add(package.symbol)

    assert package_count == 13
    assert actions == {"open", "close", "roll"}
    assert {"ADSK", "CRWD", "UPS", "SPX", "/NGU6", "WDC", "MSTR"} <= symbols


def test_extract_routes_every_image_and_stops_at_evidence() -> None:
    fixture = _manifest()["fixtures"][4]

    class FakeClient:
        def __init__(self) -> None:
            self.images: tuple[str, ...] = ()

        def chat_json(self, _system: str, _user: str, *, images: tuple[str, ...] = ()) -> dict:
            self.images = images
            return fixture["expected_extraction"]

    client = FakeClient()
    image_paths = tuple(FIXTURE_ROOT / image["path"] for image in fixture["images"])
    batch = extract_observed_packages(
        client,
        source_profile="mike_butler",
        canonical_post_id=fixture["post_id"],
        published_at=fixture["published_at"],
        post_text=fixture["post_text"],
        image_paths=image_paths,
    )

    assert client.images == tuple(str(path.resolve()) for path in image_paths)
    assert len(batch.packages) == 6
    assert batch.to_dict()["effects"] == {
        "idea_created": False,
        "plan_created": False,
        "ticket_created": False,
        "fill_created": False,
        "lifecycle_created": False,
        "broker_effects": False,
    }


def test_birdclaw_sanitized_signal_reaches_the_same_evidence_contract() -> None:
    fixture = _manifest()["fixtures"][0]
    image = fixture["images"][0]
    image_path = (FIXTURE_ROOT / image["path"]).resolve()

    class FakeClient:
        def chat_json(self, _system: str, _user: str, *, images: tuple[str, ...] = ()) -> dict:
            assert images == (str(image_path),)
            return fixture["expected_extraction"]

    signal = {
        "schema": "birdclaw.correspondent_signal.v1",
        "signal_id": f"x-post:{fixture['post_id']}",
        "profile_id": "mike_butler",
        "source": {
            "kind": "public_x_post",
            "source_id": f"x-post:{fixture['post_id']}",
            "published_at": fixture["published_at"],
            "media": [{
                "media_index": 1,
                "type": "photo",
                "cache_status": "cached",
                "sha256": image["sha256"],
                "artifact_path": str(image_path),
            }],
        },
        "literal": {"text": fixture["post_text"]},
    }

    batch = extract_observed_packages_from_correspondent_signal(FakeClient(), signal)

    assert batch.source_profile == "mike_butler"
    assert batch.canonical_post_id == f"x-post:{fixture['post_id']}"
    assert len(batch.packages) == 1
    assert batch.packages[0].structure == "call_calendar"


def test_birdclaw_media_hash_mismatch_parks_before_model_call() -> None:
    fixture = _manifest()["fixtures"][0]
    image = fixture["images"][0]
    signal = {
        "schema": "birdclaw.correspondent_signal.v1",
        "profile_id": "mike_butler",
        "source": {
            "kind": "public_x_post",
            "source_id": f"x-post:{fixture['post_id']}",
            "published_at": fixture["published_at"],
            "media": [{
                "media_index": 1,
                "type": "photo",
                "cache_status": "cached",
                "sha256": "0" * 64,
                "artifact_path": str((FIXTURE_ROOT / image["path"]).resolve()),
            }],
        },
        "literal": {"text": fixture["post_text"]},
    }

    with pytest.raises(ObservedPackageValidationError, match="hash mismatch"):
        extract_observed_packages_from_correspondent_signal(object(), signal)


def test_source_event_survives_extraction_correction_but_revision_changes() -> None:
    fixture = _manifest()["fixtures"][0]
    original = _normalize(fixture)
    corrected_raw = deepcopy(fixture["expected_extraction"])
    corrected_raw["packages"][0]["displayed_trade_time"] = "11:13a today"
    corrected = _normalize(fixture, corrected_raw)

    assert original.packages[0].source_event_id == corrected.packages[0].source_event_id
    assert original.packages[0].evidence_revision_id != corrected.packages[0].evidence_revision_id


def test_activation_feed_round_trips_with_checksum(tmp_path: Path) -> None:
    batch = _normalize(_manifest()["fixtures"][0])
    batches = [batch.to_dict()]
    canonical = json.dumps(batches, sort_keys=True, separators=(",", ":"))
    path = tmp_path / "latest.json"
    path.write_text(
        json.dumps(
            {
                "schema": "kamandal.observed_package_feed.v1",
                "generated_at": "2026-08-28T20:00:00Z",
                "batches_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                "batches": batches,
            }
        ),
        encoding="utf-8",
    )

    loaded = load_observed_package_feed(path)

    assert loaded == (batch,)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["batches"][0]["canonical_post_id"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ObservedPackageValidationError, match="checksum mismatch"):
        load_observed_package_feed(path)


def test_package_signature_is_independent_of_model_leg_order() -> None:
    fixture = _manifest()["fixtures"][0]
    original = _normalize(fixture)
    reordered_raw = deepcopy(fixture["expected_extraction"])
    reordered_raw["packages"][0]["legs"].reverse()
    reordered = _normalize(fixture, reordered_raw)

    assert original.packages[0].package_signature == reordered.packages[0].package_signature


def test_ambiguous_package_can_only_park() -> None:
    fixture = _manifest()["fixtures"][0]
    raw = deepcopy(fixture["expected_extraction"])
    package = raw["packages"][0]
    package["complete"] = False
    package["blocker"] = "short-leg strike unreadable"
    package["legs"][0]["strike"] = None
    batch = _normalize(fixture, raw)

    assert batch.packages[0].complete is False
    assert batch.packages[0].package_signature is None
    assert batch.packages[0].blocker == "short-leg strike unreadable"


def test_complete_open_package_rejects_closing_leg() -> None:
    fixture = _manifest()["fixtures"][0]
    raw = deepcopy(fixture["expected_extraction"])
    raw["packages"][0]["legs"][0]["order_code"] = "BTC"

    with pytest.raises(ObservedPackageValidationError, match="opening legs"):
        _normalize(fixture, raw)


def test_wrong_double_calendar_pairing_is_rejected() -> None:
    fixture = _manifest()["fixtures"][1]
    raw = deepcopy(fixture["expected_extraction"])
    raw["packages"][0]["legs"][1]["order_code"] = "BTO"
    raw["packages"][0]["legs"][2]["order_code"] = "STO"

    with pytest.raises(ObservedPackageValidationError, match="corpus-proven structure"):
        _normalize(fixture, raw)


def test_unknown_model_fields_fail_closed() -> None:
    fixture = _manifest()["fixtures"][0]
    raw = deepcopy(fixture["expected_extraction"])
    raw["packages"][0]["confidence"] = 0.99

    with pytest.raises(ObservedPackageValidationError, match=r"extra=\['confidence'\]"):
        _normalize(fixture, raw)
