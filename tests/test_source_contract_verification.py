"""Live exact contracts require a second transcription of retained source media."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from kamandal_v2.intelligence.observed_packages import (
    ObservedPackageValidationError, normalize_observed_package_output,
    observed_package_batch_from_dict,
)
from kamandal_v2.intelligence.source_contract_verification import verify_live_source_contracts


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "mike_observed_packages"


class _Client:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = 0

    def chat_json(self, *_args, **_kwargs):
        self.calls += 1
        return self.response


def _case() -> tuple[dict, dict, object]:
    case = json.loads((FIXTURE_ROOT / "ground-truth.json").read_text())["fixtures"][0]
    raw = copy.deepcopy(case["expected_extraction"])
    post_id = f"x-post:{case['post_id']}"
    signal = {
        "schema": "birdclaw.correspondent_signal.v1",
        "signal_id": post_id,
        "profile_id": "mike_butler",
        "source": {
            "kind": "public_x_post", "source_id": post_id,
            "published_at": case["published_at"],
            "media": [{
                "type": "photo", "cache_status": "cached", "media_index": 1,
                "artifact_path": str(FIXTURE_ROOT / case["images"][0]["path"]),
                "sha256": case["images"][0]["sha256"],
            }],
        },
        "literal": {"text": case["post_text"]},
    }
    batch = normalize_observed_package_output(
        raw, source_profile="mike_butler", canonical_post_id=post_id,
        published_at=case["published_at"], image_sha256=(case["images"][0]["sha256"],),
        prompt_sha256="episode-interpreter-prompt",
    )
    return signal, raw, batch


def test_matching_image_transcription_marks_opening_verified_and_reuses_cache(tmp_path: Path) -> None:
    signal, raw, batch = _case()
    client = _Client(raw)
    for _ in range(2):
        verified, failures = verify_live_source_contracts(
            [batch], {"records": [signal]}, live_structures={"call_calendar"},
            client=client, cache_root=tmp_path,
        )
        assert not failures
        package = verified[0].packages[0]
        assert package.source_verified
        assert package.source_verification_ref.startswith("sv_")
        assert package.evidence_revision_id != batch.packages[0].evidence_revision_id
    assert client.calls == 1


@pytest.mark.parametrize("change,expected", [
    ("quantity", "source_contracts_disagree_with_image"),
    ("price", "source_price_disagrees_with_image"),
    ("structure", "source_structure_disagrees_with_image"),
    ("count", "source_package_count_disagrees_with_image"),
])
def test_image_disagreement_fails_closed(tmp_path: Path, change: str, expected: str) -> None:
    signal, raw, batch = _case()
    package = batch.packages[0]
    if change == "quantity":
        package = replace(package, legs=(replace(package.legs[0], quantity=2), *package.legs[1:]),
                          package_signature="altered-contracts")
    elif change == "price":
        package = replace(package, displayed_price={"amount": "2.50", "effect": "debit"})
    elif change == "structure":
        package = replace(package, structure="call_diagonal")
    elif change == "count":
        package = replace(package, source_opening_package_count=2)
    batch = replace(batch, packages=(package,))

    verified, failures = verify_live_source_contracts(
        [batch], {"records": [signal]}, live_structures={"call_calendar", "call_diagonal"},
        client=_Client(raw), cache_root=tmp_path,
    )

    assert verified[0].packages[0].source_verified is False
    assert failures[0]["reason"] == expected


def test_missing_image_or_verifier_cannot_authorize_live(tmp_path: Path) -> None:
    signal, raw, batch = _case()
    signal["source"]["media"][0]["sha256"] = "0" * 64
    verified, failures = verify_live_source_contracts(
        [batch], {"records": [signal]}, live_structures={"call_calendar"},
        client=_Client(raw), cache_root=tmp_path,
    )
    assert not verified[0].packages[0].source_verified
    assert failures[0]["reason"] == "source_verifier_failed:ValueError"

    signal, _, batch = _case()
    verified, failures = verify_live_source_contracts(
        [batch], {"records": [signal]}, live_structures={"call_calendar"},
        client=None, cache_root=tmp_path,
    )
    assert not verified[0].packages[0].source_verified
    assert failures[0]["reason"] == "source_verifier_unavailable"


def test_short_strangle_transcription_is_recognized_for_existing_live_route() -> None:
    raw = {
        "schema": "kamandal.observed_package_extraction.v1",
        "post_disposition": "packages", "post_blocker": None,
        "packages": [{
            "media_index": 1, "package_position": 1, "action": "open", "symbol": "XYZ",
            "displayed_trade_time": None,
            "displayed_price": {"amount": "1.25", "effect": "credit"},
            "complete": True, "blocker": None,
            "legs": [
                {"quantity": 1, "expiration": "2026-10-16", "strike": "90", "option_type": "put", "order_code": "STO"},
                {"quantity": 1, "expiration": "2026-10-16", "strike": "110", "option_type": "call", "order_code": "STO"},
            ],
        }],
    }
    batch = normalize_observed_package_output(
        raw, source_profile="mike_butler", canonical_post_id="x-post:strangle",
        published_at="2026-09-08T14:00:00Z", image_sha256=("0" * 64,),
        prompt_sha256="fixture",
    )
    assert batch.packages[0].structure == "short_strangle"


def test_feed_rejects_inconsistent_verified_provenance() -> None:
    _, _, batch = _case()
    payload = batch.to_dict()
    payload["packages"][0]["source_verified"] = True
    with pytest.raises(ObservedPackageValidationError, match="source verification is inconsistent"):
        observed_package_batch_from_dict(payload)
    payload["packages"][0]["source_verification_ref"] = "sv_fixture"
    payload["packages"][0]["source_verification_reason"] = "source_price_disagrees_with_image"
    with pytest.raises(ObservedPackageValidationError, match="source verification is inconsistent"):
        observed_package_batch_from_dict(payload)
