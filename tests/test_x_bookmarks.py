import json

from kamandal_v2.intelligence.x_bookmarks import import_x_bookmarks


def test_import_x_bookmarks_from_public_export(tmp_path) -> None:
    source = tmp_path / "xurl-bookmarks-20260503-104523.public-export.json"
    source.write_text(
        json.dumps({
            "schema": "jarvis.birdclaw.explicit-public-post-export.v1",
            "sourceType": "explicit-public-post-export",
            "records": [
                {
                    "id": "1",
                    "created_at": "2026-05-03T10:00:00Z",
                    "text": "AI infrastructure looks bullish for $NVDA and $AMD.",
                    "url": "https://x.com/example/status/1",
                    "provenance": "test",
                },
                {
                    "id": "2",
                    "created_at": "2026-05-03T11:00:00Z",
                    "text": "$GOOGL earnings beat but search risk remains. Now is not ticker evidence.",
                    "url": "https://x.com/example/status/2",
                    "provenance": "test",
                },
            ],
        }),
        encoding="utf-8",
    )

    result = import_x_bookmarks(
        source_file=source,
        output_dir=tmp_path / "source_docs",
        digest_dir=tmp_path / "digest",
        allowed_symbols={"NVDA", "AMD", "GOOGL", "SPY", "NOW"},
    )

    assert result.record_count == 2
    assert result.cashtags == {"AMD": 1, "GOOGL": 1, "NVDA": 1}
    assert result.symbol_hits == {"AMD": 1, "GOOGL": 1, "NVDA": 1}
    source_doc = result.source_doc_path.read_text(encoding="utf-8")
    assert "sanitized X bookmark public export" in source_doc
    assert "treat $TICKER cashtags as ticker evidence" in source_doc
    assert "$NVDA" in result.digest_path.read_text(encoding="utf-8")


def test_import_x_bookmarks_resolves_latest_state(tmp_path) -> None:
    trial_root = tmp_path / "trial"
    exports = trial_root / "data" / "jarvis-sanitized-exports"
    exports.mkdir(parents=True)
    source = exports / "xurl-bookmarks-20260503-104523.public-export.json"
    source.write_text(
        json.dumps({
            "schema": "jarvis.birdclaw.explicit-public-post-export.v1",
            "records": [{"id": "1", "text": "$TSLA looks overextended.", "url": "https://x.com/example/status/1"}],
        }),
        encoding="utf-8",
    )
    latest = tmp_path / "latest.json"
    latest.write_text(
        json.dumps({"raw_path": "data/quarantine/xurl-bookmarks-20260503-104523.raw.json"}),
        encoding="utf-8",
    )

    result = import_x_bookmarks(
        latest_state=latest,
        trial_root=trial_root,
        output_dir=tmp_path / "source_docs",
        digest_dir=tmp_path / "digest",
    )

    assert result.source_file == source
    assert result.cashtags == {"TSLA": 1}
