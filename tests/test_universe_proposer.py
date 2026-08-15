from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.tools.universe_proposer import collect_out_of_universe_symbols, micro_stock_guard, run_weekly_universe_review


def test_micro_stock_guard_requires_sourced_price_and_liquidity() -> None:
    assert micro_stock_guard("GOOD", market_facts={"price": 80, "avg_dollar_volume": 50_000_000, "market_cap": 5_000_000_000})
    assert not micro_stock_guard("LOW", market_facts={"price": 8, "avg_dollar_volume": 50_000_000, "market_cap": 5_000_000_000})
    assert not micro_stock_guard("THIN", market_facts={"price": 80, "avg_dollar_volume": 1_000_000, "market_cap": 5_000_000_000})
    assert not micro_stock_guard("UNK", market_facts={"price": None, "avg_dollar_volume": None, "market_cap": None})


def test_proposer_reads_plan_diagnostics_dedupes_sheet_and_records_evidence(tmp_path) -> None:
    audit = tmp_path / "latest_plan_run.json"
    audit.write_text(json.dumps({"idea_diagnostics": [
        {"underlying": "GOOD", "status": "out_of_universe"},
        {"underlying": "GOOD", "status": "out_of_universe"},
        {"underlying": "EXIST", "status": "out_of_universe"},
    ]}))
    os.utime(audit, (datetime.now(UTC).timestamp(), datetime.now(UTC).timestamp()))
    store = LocalStore(tmp_path / "kamandal.db")

    proposals = collect_out_of_universe_symbols(
        store,
        existing_symbols={"EXIST"},
        audit_path=audit,
        cutoff=datetime.now(UTC),
        market_facts_loader=lambda _symbol: {
            "price": 75.0,
            "avg_dollar_volume": 40_000_000.0,
            "market_cap": 4_000_000_000.0,
        },
    )

    assert [proposal["symbol"] for proposal in proposals] == ["GOOD"]
    assert "verified price=75.00" in proposals[0]["notes"]
    assert proposals[0]["enabled"] == "FALSE"


def test_proposer_prefers_replay_safe_durable_discovery_over_overwriteable_audit(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.record_discovery_evidence(symbol="DURABLE", source_profile="x", source_record_id="1", exclusion_reason="outside", evidence_ref="x:1")
    store.record_discovery_evidence(symbol="DURABLE", source_profile="youtube", source_record_id="2", exclusion_reason="outside", evidence_ref="youtube:2")

    proposals = collect_out_of_universe_symbols(
        store,
        cutoff=datetime.now(UTC),
        market_facts_loader=lambda _symbol: {"price": 75.0, "avg_dollar_volume": 40_000_000.0, "market_cap": 4_000_000_000.0},
    )

    assert [proposal["symbol"] for proposal in proposals] == ["DURABLE"]
    assert proposals[0]["proposal_source"] == "durable_discovery"


def test_proposer_uses_committed_review_window_and_ranks_recency_deterministically(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.record_universe_review_commit(review_id="week-1", committed_at="2026-08-07T18:00:00Z")
    store.record_discovery_evidence(symbol="OLDER", source_profile="x", source_record_id="1", exclusion_reason="outside", evidence_ref="x:1", observed_at="2026-08-10T12:00:00Z")
    store.record_discovery_evidence(symbol="NEWER", source_profile="x", source_record_id="2", exclusion_reason="outside", evidence_ref="x:2", observed_at="2026-08-12T12:00:00Z")
    store.record_discovery_evidence(symbol="BEFORE", source_profile="x", source_record_id="3", exclusion_reason="outside", evidence_ref="x:3", observed_at="2026-08-07T17:59:59Z")

    proposals = collect_out_of_universe_symbols(
        store,
        cutoff=datetime(2026, 8, 14, 18, tzinfo=UTC),
        market_facts_loader=lambda _symbol: {"price": 75.0, "avg_dollar_volume": 40_000_000.0, "market_cap": 4_000_000_000.0},
    )

    assert [proposal["symbol"] for proposal in proposals] == ["NEWER", "OLDER"]
    assert "2026-08-07 through 2026-08-14" in proposals[0]["proposal_reason"]


def test_weekly_review_commits_zero_and_nonzero_windows_only_after_exact_publish(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    cutoff = datetime(2026, 8, 14, 18, tzinfo=UTC)
    zero = run_weekly_universe_review(store, universe_rows=[], publish=lambda rows: len(rows), cutoff=cutoff)

    assert zero.committed is True
    assert zero.proposal_count == 0
    assert store.latest_universe_review_commit_at() == cutoff.isoformat()

    store.record_discovery_evidence(symbol="GOOD", source_profile="x", source_record_id="1", exclusion_reason="outside", evidence_ref="x:1", observed_at="2026-08-15T12:00:00Z")
    next_cutoff = datetime(2026, 8, 21, 18, tzinfo=UTC)
    review = run_weekly_universe_review(
        store,
        universe_rows=[],
        publish=lambda rows: len(rows),
        cutoff=next_cutoff,
        market_facts_loader=lambda _symbol: {"price": 75.0, "avg_dollar_volume": 40_000_000.0, "market_cap": 4_000_000_000.0},
    )

    assert review.proposal_count == review.published_count == 1
    assert store.latest_universe_review_commit_at() == next_cutoff.isoformat()


def test_weekly_review_does_not_advance_boundary_when_publication_fails(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.record_discovery_evidence(symbol="GOOD", source_profile="x", source_record_id="1", exclusion_reason="outside", evidence_ref="x:1", observed_at="2026-08-15T12:00:00Z")

    try:
        run_weekly_universe_review(
            store,
            universe_rows=[],
            publish=lambda _rows: 0,
            cutoff=datetime(2026, 8, 21, 18, tzinfo=UTC),
            market_facts_loader=lambda _symbol: {"price": 75.0, "avg_dollar_volume": 40_000_000.0, "market_cap": 4_000_000_000.0},
        )
    except RuntimeError as exc:
        assert "inexact" in str(exc)
    else:
        raise AssertionError("inexact proposal publication must fail")
    assert store.latest_universe_review_commit_at() is None
