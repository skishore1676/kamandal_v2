from __future__ import annotations

import json

from kamandal_v2.tools.chart_seed_request import build_seed_request_from_translation


def test_chart_seed_request_contains_only_parked_weekly_ideas(tmp_path) -> None:
    translation = tmp_path / "translation.json"
    translation.write_text(json.dumps({
        "profile": {"profile_id": "greg_harmon"},
        "batch_id": "batch-1",
        "records": [
            {"tweet_type": "weekly_ideas", "symbol": "AMZN", "planner_eligible": False, "planner_blockers": ["chart_evaluation_missing"]},
            {"tweet_type": "trade_journal", "symbol": "META", "planner_eligible": False, "planner_blockers": ["chart_evaluation_missing"]},
            {"tweet_type": "weekly_ideas", "symbol": "MSFT", "planner_eligible": False, "planner_blockers": ["stale"]},
        ],
    }))

    request = build_seed_request_from_translation(translation, output_dir=tmp_path / "requests")
    payload = json.loads(request.read_text())

    assert payload["symbols"] == ["AMZN"]
    assert payload["translation_batch_id"] == "batch-1"
