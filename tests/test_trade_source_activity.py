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


def test_activity_reads_matching_closed_lifecycles_without_crossing_idea_and_exact(tmp_path):
    import json
    import sqlite3
    path = tmp_path / 'state.db'
    store = LocalStore(path)
    for output, idea, kind in [('idea-output', 'idea-1', 'idea'), ('revision-1', '', 'exact_package')]:
        store.event('trade_source_output_observed', {
            'source_id': 'mike_butler', 'post_ref': 'x-post:123', 'output_id': output,
            'planner_idea_id': idea, 'classification': kind,
            'normalized_output': {'symbol': 'GLD', 'structure': 'call_diagonal', 'action': 'open', 'thesis': 'Bullish longer-term idea'},
        })
    # Only the fields consumed by this read-only projection are needed here.
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE csa_lifecycles (id TEXT PRIMARY KEY, payload TEXT)')
        for name, identity, mode, status in [
            ('live-1', {'idea_id': 'idea-1'}, 'live', 'closed'),
            ('shadow-1', {'idea_id': 'exact-idea', 'evidence_revision_id': 'revision-1'}, 'shadow', 'open'),
            ('unrelated', {'idea_id': 'other'}, 'live', 'open'),
        ]:
            db.execute('INSERT INTO csa_lifecycles VALUES (?,?)', (name, json.dumps({'lifecycle_id': name, 'status': status, 'updated_at': '2026-09-08T20:00:00Z', 'metadata': {'execution_mode': mode, 'source_identity': identity}})))
    before = path.read_bytes()
    rows = [dict(zip(TRADE_SOURCE_ACTIVITY_HEADER, row)) for row in activity_rows(LocalStore(path, read_only=True))]
    by_id = {row['output_id']: row for row in rows}
    assert by_id['idea-output']['lifecycle_status'] == 'live:closed'
    assert by_id['revision-1']['lifecycle_status'] == 'shadow:open'
    assert by_id['revision-1']['symbol'] == 'GLD'
    assert by_id['revision-1']['source_url'] == 'https://x.com/i/status/123'
    assert by_id['revision-1']['interpretation'] == 'Bullish longer-term idea'
    assert path.read_bytes() == before


def test_activity_writer_is_atomic_raw_and_preserves_other_columns(monkeypatch):
    from types import SimpleNamespace
    from kamandal_v2 import sheets
    calls = []
    worksheet = SimpleNamespace(row_count=100, update=lambda **kw: calls.append(kw))
    client = SimpleNamespace(_worksheet=lambda *a, **kw: worksheet,
                             _retry=lambda fn, **kw: fn())
    monkeypatch.setattr(sheets.GoogleSheetClient, 'from_config', lambda config: client)
    assert sheets.write_trade_source_activity({}, [['=NOT_A_FORMULA']], ['interpretation']) == 1
    assert len(calls) == 1
    assert calls[0]['value_input_option'] == 'RAW'
    assert calls[0]['range_name'] == 'A1:A100'
    assert calls[0]['values'][1] == ['=NOT_A_FORMULA']
    assert calls[0]['values'][-1] == ['']


def test_exact_failure_retains_separate_idea_row_and_correct_post(tmp_path):
    from kamandal_v2.intelligence.correspondent_activation import _record_exact_outputs
    from kamandal_v2.intelligence.trade_sources import TradeSourceMode
    store = LocalStore(tmp_path / 'state.db')
    store.event('trade_source_output_observed', {'output_id': 'event-1', 'classification': 'idea', 'post_ref': 'x-post:123', 'planner_disposition': 'published'})
    _record_exact_outputs(store, observed_batches=[], failures=[{'source_id': 'event-1', 'post_ref': 'x-post:123', 'reason': 'exact legs missing'}], source_id='greg_harmon', source_mode=TradeSourceMode.SHADOW, acquisition={})
    rows = [dict(zip(TRADE_SOURCE_ACTIVITY_HEADER, row)) for row in activity_rows(store)]
    assert len(rows) == 2
    assert {row['classification'] for row in rows} == {'idea', 'residual'}
    assert all(row['post_ref'] == 'x-post:123' for row in rows)
