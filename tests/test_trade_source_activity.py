from __future__ import annotations

from kamandal_v2.intelligence.trade_source_activity import activity_rows
from kamandal_v2.schemas import TRADE_SOURCE_ACTIVITY_HEADER
from kamandal_v2.stores.sqlite import LocalStore


def test_activity_projection_joins_output_to_planner_disposition(tmp_path) -> None:  # noqa: ANN001
    store = LocalStore(tmp_path / "kamandal.db")
    store.event(
        "trade_source_output_observed",
        {
            "observed_at": "2026-09-03T14:00:00Z",
            "source_id": "mike_butler",
            "post_ref": "x-post:1",
            "output_id": "output-1",
            "planner_idea_id": "idea-1",
            "acquisition_status": "complete",
            "classification": "idea",
            "normalized_output": {"underlying": "META"},
            "action": "open",
            "symbol": "META",
            "structure": "call_diagonal",
            "link_state": "not_needed",
            "evidence_status": "complete",
            "capability_support": "supported",
            "planner_disposition": "published",
            "effective_mode": "shadow",
            "reason": "",
        },
    )
    store.event(
        "trade_source_planner_disposition",
        {
            "source_id": "mike_butler",
            "idea_id": "idea-1",
            "status": "eligible_not_selected",
            "reason": "portfolio_optimizer",
            "mode": "shadow",
        },
    )

    rows = activity_rows(store)
    assert len(rows) == 1
    projected = dict(zip(TRADE_SOURCE_ACTIVITY_HEADER, rows[0], strict=True))
    assert projected["source_id"] == "mike_butler"
    assert projected["classification"] == "idea"
    assert projected["action"] == "open"
    assert projected["symbol"] == "META"
    assert projected["structure"] == "call_diagonal"
    assert projected["link_status"] == "not_needed"
    assert projected["evidence_status"] == "complete"
    assert projected["planner_disposition"] == "eligible_not_selected"
    assert projected["reason"] == "portfolio_optimizer"
    assert projected["effective_mode"] == "shadow"
