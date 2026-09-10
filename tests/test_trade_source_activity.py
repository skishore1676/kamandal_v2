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


def test_retained_legacy_receipt_has_readable_interpretation_without_reclassification(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    store.event("trade_source_output_observed", {"output_id": "legacy", "classification": "residual", "effective_mode": "observe", "normalized_output": {"record": {"symbol": "GOOGL", "source_intent": {"reason": "Reports a bullish call crab."}}}})
    row = dict(zip(TRADE_SOURCE_ACTIVITY_HEADER, activity_rows(store)[0]))
    assert row["interpretation"] == "Reports a bullish call crab."
    assert row["symbol"] == "GOOGL"
    assert row["classification"] == "residual" and row["effective_mode"] == "observe"


def test_current_activity_folds_revisions_and_idea_without_merging_distinct_packages(tmp_path):
    import json
    store = LocalStore(tmp_path / 'state.db')
    for output, signature, reason in [('revision-old', 'legs-1', 'old-liquidity'),
                                      ('revision-new', 'legs-1', 'current-liquidity'),
                                      ('other-package', 'legs-2', 'other-strike')]:
        store.event('trade_source_output_observed', {
            'source_id': 'mike', 'post_ref': 'x-post:123', 'output_id': output,
            'classification': 'exact_package', 'planner_disposition': 'parked',
            'reason': reason, 'effective_mode': 'shadow', 'normalized_output': {
                'source_event_id': 'event-1', 'package_signature': signature,
                'symbol': 'SNOW', 'structure': 'call_calendar', 'action': 'open'}})
    store.event('trade_source_output_observed', {
        'source_id': 'mike', 'post_ref': 'x-post:123', 'output_id': 'event-1',
        'classification': 'idea,exact_package', 'planner_disposition': 'no_candidate',
        'effective_mode': 'live', 'normalized_output': {'thesis': 'Bullish calendars'}})
    store.event('trade_source_output_observed', {
        'source_id': 'mike', 'post_ref': 'x-post:123', 'output_id': 'failure',
        'classification': 'residual', 'planner_disposition': 'parked',
        'effective_mode': 'shadow', 'reason': 'source_too_old',
        'normalized_output': {'source_id': 'event-1', 'reason': 'source_too_old'}})
    rows = [dict(zip(TRADE_SOURCE_ACTIVITY_HEADER, r)) for r in activity_rows(store)]
    assert len(rows) == 2
    current = next(r for r in rows if r['output_id'] == 'revision-new')
    assert 'source_too_old' in current['reason'] and 'old-liquidity' not in current['reason']
    assert current['symbol'] == 'SNOW'
    assert current['effective_mode'] == 'Idea: live | Exact: shadow'
    history = json.loads(current['normalized_output'])['activity_history']
    assert {r['output_id'] for r in history} == {'revision-old', 'revision-new', 'event-1', 'failure'}


def test_translation_review_is_post_based_and_reset_survives_reobservation(tmp_path):
    from kamandal_v2.intelligence.trade_source_activity import translation_review_rows
    from kamandal_v2.schemas import TRADE_SOURCE_REVIEW_HEADER
    import sqlite3
    store = LocalStore(tmp_path / 'state.db')
    def observation(post, event, symbol, classification='idea'):
        store.event('trade_source_output_observed', {
            'source_id': 'mike_butler', 'post_ref': f'x-post:{post}', 'output_id': event,
            'classification': classification, 'planner_disposition': 'no_candidate',
            'reason': 'no_playbook_match', 'normalized_output': {
                'event_id': event, 'symbol': symbol, 'action': 'open', 'structure_hint': 'call_diagonal',
                'thesis': f'Bullish {symbol}', 'evidence_status': 'complete',
                'blockers': ['planner_structure_unsupported'], 'exact_packages': [{
                    'complete': False, 'blocker': 'Missing source image', 'legs': []}]}})
    observation('100', 'old-event', 'COST')
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute("UPDATE events SET created_at='2026-09-09 12:00:00'")
    observation('100', 'old-event', 'COST')  # A fresh poll must not refill old posts.
    observation('101', 'new-1', 'HIMS')
    observation('101', 'new-2', 'NFLX')
    store.event('trade_source_output_observed', {
        'source_id': 'mike_butler', 'post_ref': 'x-post:101', 'output_id': 'revision',
        'classification': 'exact_package', 'normalized_output': {'source_event_id': 'new-1'}})
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute("UPDATE events SET created_at='2026-09-10 14:00:00' WHERE id>1")
    rows = translation_review_rows(store, since='2026-09-10T13:00:00+00:00')
    assert len(rows) == 1
    row = dict(zip(TRADE_SOURCE_REVIEW_HEADER, rows[0]))
    assert row['Source post'].endswith('/101')
    assert 'HIMS' in row['Our understanding'] and 'NFLX' in row['Our understanding']
    assert 'Missing source image' in row['Missing or uncertain']
    assert 'no_playbook_match' not in str(row) and 'planner_structure_unsupported' not in str(row)
    assert row['Your correction'] == ''


def test_review_writer_keeps_corrections_in_place_when_order_changes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from kamandal_v2.sheets import write_translation_review, GoogleSheetClient
    from kamandal_v2.schemas import TRADE_SOURCE_REVIEW_HEADER
    monkeypatch.chdir(tmp_path)
    calls = []
    previous = [TRADE_SOURCE_REVIEW_HEADER,
                ['Mike', 'url-1', 'COST', 'old', '', '', 'This should be bearish'],
                ['Greg', 'url-2', 'JNJ', 'old', '', '', 'Keep my note']]
    worksheet = SimpleNamespace(row_count=100, get_all_values=lambda: previous,
                                update=lambda **kw: calls.append(kw))
    client = SimpleNamespace(_worksheet=lambda *a, **kw: worksheet, _retry=lambda fn, **kw: fn())
    monkeypatch.setattr(GoogleSheetClient, 'from_config', lambda _config: client)
    count = write_translation_review({}, [['Greg', 'url-2', 'JNJ', 'updated', '', '', ''],
                                         ['Mike', 'url-1', 'COST', 'updated', '', '', ''],
                                         ['Mike', 'url-3', 'NFLX', 'new', '', '', '']])
    assert count == 3 and len(calls) == 1
    assert calls[0]['range_name'] == 'A1:F4'  # Column G is never rewritten.
    assert calls[0]['values'][1][1] == 'url-1'
    assert calls[0]['values'][2][1] == 'url-2'
    assert calls[0]['values'][3][1] == 'url-3'


def test_review_migration_clears_old_noise_and_removes_internal_columns(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from kamandal_v2.sheets import write_translation_review, GoogleSheetClient
    from kamandal_v2.schemas import TRADE_SOURCE_REVIEW_HEADER
    monkeypatch.chdir(tmp_path)
    calls, resizes = [], []
    previous = [TRADE_SOURCE_ACTIVITY_HEADER, ['old data'] * 22]
    worksheet = SimpleNamespace(row_count=100, get_all_values=lambda: previous,
        update=lambda **kw: calls.append(kw), resize=lambda **kw: resizes.append(kw))
    client = SimpleNamespace(_worksheet=lambda *a, **kw: worksheet, _retry=lambda fn, **kw: fn())
    monkeypatch.setattr(GoogleSheetClient, 'from_config', lambda _config: client)
    assert write_translation_review({}, []) == 0
    assert calls[0]['values'][0][:7] == TRADE_SOURCE_REVIEW_HEADER
    assert calls[0]['values'][1] == [''] * 22
    assert resizes == [{'cols': 7}]


def test_translation_review_uses_latest_complete_batch(tmp_path):
    from kamandal_v2.intelligence.trade_source_activity import translation_review_rows
    store = LocalStore(tmp_path / 'state.db')
    for output, batch, thesis in [('removed', 'old', 'Wrong interpretation'), ('kept', 'new', 'Corrected interpretation')]:
        store.event('trade_source_output_observed', {
            'source_id': 'greg', 'post_ref': 'x-post:123', 'output_id': output,
            'translation_batch': batch, 'normalized_output': {'event_id': output, 'thesis': thesis}})
    rows = translation_review_rows(store)
    assert len(rows) == 1
    assert 'Corrected interpretation' in rows[0][3]
    assert 'Wrong interpretation' not in str(rows)
