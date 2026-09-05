from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path("scripts").resolve()))
SPEC = importlib.util.spec_from_file_location("vision_evaluation", Path("scripts/evaluate_source_episode_vision.py"))
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_image_packet_contains_evidence_and_never_gold_labels():
    packet, fixtures = MODULE.packets_and_labels()
    assert len(packet["records"]) == len(fixtures) == 6
    assert sum(len(record["source"]["media"]) for record in packet["records"]) == 7
    assert all(set(record) == {"signal_id", "profile_id", "source", "classification", "literal"}
               for record in packet["records"])


def test_wrong_side_is_not_credited_and_omission_is_distinct_from_wrong_complete():
    _, fixtures = MODULE.packets_and_labels()
    fixture = fixtures[0]
    package = deepcopy(fixture["expected_extraction"]["packages"][0])
    episode = {"post_ref": f"x-post:{fixture['post_id']}", "events": [
        {"action": package["action"], "symbol": package["symbol"], "exact_packages": [package]}
    ]}
    assert MODULE.score_packages([fixture], [episode])["matched_packages"] == 1
    package["legs"][0]["order_code"] = "BTO"
    score = MODULE.score_packages([fixture], [episode])
    assert score["matched_packages"] == 0
    assert score["wrong_complete_packages"] == 1
    omitted = MODULE.score_packages([fixture], [])
    assert omitted["matched_packages"] == 0
    assert omitted["wrong_complete_packages"] == 0
