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


@pytest.mark.parametrize("legs,expected", [
    ([
        (2, "2026-11-20", "105", "call", "STO"),
        (1, "2026-11-20", "115", "call", "BTO"),
        (1, "2026-12-18", "95", "call", "BTO"),
    ], "call_crab"),
    ([
        (1, "2026-10-16", "670", "put", "BTO"),
        (2, "2026-10-16", "685", "put", "STO"),
        (1, "2026-10-16", "700", "put", "BTO"),
    ], "put_butterfly"),
    ([
        (1, "2026-10-16", "670", "put", "BTO"),
        (2, "2026-10-16", "685", "put", "STO"),
        (1, "2026-10-16", "700", "put", "BTO"),
        (1, "2026-10-16", "820", "call", "STO"),
        (1, "2026-10-16", "830", "call", "BTO"),
    ], "put_butterfly_with_call_vertical"),
    ([(1, "2026-10-16", "100", "call", "BTO")], "long_call"),
    ([(1, "2026-10-16", "100", "call", "BTO"),
      (1, "2026-10-16", "110", "call", "STO")], "call_spread"),
])
def test_retained_guru_shapes_are_classified_without_live_authority(legs, expected) -> None:
    raw = {
        "schema": "kamandal.observed_package_extraction.v1",
        "post_disposition": "packages", "post_blocker": None,
        "packages": [{
            "media_index": 1, "package_position": 1, "action": "open", "symbol": "XYZ",
            "displayed_trade_time": None, "displayed_price": None,
            "complete": True, "blocker": None,
            "legs": [dict(zip(("quantity", "expiration", "strike", "option_type", "order_code"), leg))
                     for leg in legs],
        }],
    }
    batch = normalize_observed_package_output(
        raw, source_profile="mike_butler", canonical_post_id="x-post:shape",
        published_at="2026-09-08T14:00:00Z", image_sha256=("0" * 64,),
        prompt_sha256="fixture",
    )
    assert batch.packages[0].structure == expected
    assert not batch.packages[0].source_verified


def test_unknown_ratio_package_does_not_hide_supported_sibling() -> None:
    def leg(quantity, expiry, strike, kind, code):
        return {"quantity": quantity, "expiration": expiry, "strike": strike,
                "option_type": kind, "order_code": code}

    raw = {
        "schema": "kamandal.observed_package_extraction.v1",
        "post_disposition": "packages", "post_blocker": None,
        "packages": [
            {"media_index": 1, "package_position": 1, "action": "open", "symbol": "XYZ",
             "displayed_trade_time": None, "displayed_price": None,
             "complete": True, "blocker": None,
             "legs": [leg(2, "2026-10-16", "100", "call", "STO"),
                      leg(1, "2026-11-20", "100", "call", "BTO")]},
            {"media_index": 1, "package_position": 2, "action": "open", "symbol": "ABC",
             "displayed_trade_time": None, "displayed_price": None,
             "complete": True, "blocker": None,
             "legs": [leg(1, "2026-10-16", "90", "put", "STO"),
                      leg(1, "2026-10-16", "110", "call", "STO")]},
        ],
    }
    batch = normalize_observed_package_output(
        raw, source_profile="mike_butler", canonical_post_id="x-post:mixed",
        published_at="2026-09-08T14:00:00Z", image_sha256=("0" * 64,),
        prompt_sha256="fixture",
    )
    assert [package.structure for package in batch.packages] == [None, "short_strangle"]


def _condor_case():
    signal, raw, _ = _case()
    raw['packages'] = [dict(raw['packages'][0], symbol='SPX',
        displayed_price={'amount':'2.00','effect':'credit'},
        legs=[{'quantity':1,'expiration':'2026-09-29','strike':str(strike),'option_type':kind,'order_code':code}
              for kind,strike,code in [('put',7650,'BTO'),('put',7655,'STO'),('call',7700,'STO'),('call',7705,'BTO')]])]
    independent=normalize_observed_package_output(raw,source_profile='mike_butler',canonical_post_id=signal['signal_id'],
        published_at=signal['source']['published_at'],image_sha256=(signal['source']['media'][0]['sha256'],),prompt_sha256='fixture')
    assert independent.packages[0].structure is None  # Legacy extractor label, not unreadable contracts.
    proposal=replace(independent,packages=(replace(independent.packages[0],structure='iron_condor'),))
    return signal,raw,proposal


def test_legacy_condor_transcription_verifies_without_cache_rewrite_or_second_call(tmp_path):
    signal,raw,proposal=_condor_case()
    client=_Client(raw)
    saved=None
    for _ in range(2):
        verified,failures=verify_live_source_contracts([proposal],{'records':[signal]},live_structures={'iron_condor'},client=client,cache_root=tmp_path)
        assert not failures
        assert verified[0].packages[0].source_verified
        paths=list(tmp_path.glob('*/*.json'))
        assert len(paths)==1
        content=paths[0].read_bytes()
        if saved is not None: assert content==saved
        saved=content
        assert json.loads(content)['packages'][0]['structure'] is None
    assert client.calls==1


@pytest.mark.parametrize('mutation',['quantity','expiry','side','crossed_strikes','explicit_label','price'])
def test_missing_condor_label_does_not_bypass_contract_or_price_validation(mutation):
    from kamandal_v2.intelligence.source_contract_verification import _disagreement
    _,_,proposal=_condor_case()
    proposed=proposal.packages[0]
    legs=list(proposed.legs)
    if mutation=='quantity': legs[0]=replace(legs[0],quantity=2)
    if mutation=='expiry': legs[0]=replace(legs[0],expiration='2026-09-30')
    if mutation=='side': legs[0]=replace(legs[0],side='sell')
    if mutation=='crossed_strikes': legs[0]=replace(legs[0],strike='7660')
    # Equal signatures alone cannot authorize an invalid shape.
    proposed=replace(proposed,legs=tuple(legs))
    independent=replace(proposed,structure='call_calendar' if mutation=='explicit_label' else None)
    if mutation=='price': independent=replace(independent,displayed_price={'amount':'3','effect':'credit'})
    assert _disagreement(proposed,replace(proposal,packages=(independent,))) == (
        'source_price_disagrees_with_image' if mutation=='price' else 'source_structure_disagrees_with_image')
