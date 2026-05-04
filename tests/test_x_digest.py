import json
import sqlite3
from datetime import UTC, datetime, timedelta

from kamandal_v2.intelligence.x_digest import import_x_digest


def _create_digest_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table x_digest_posts (
          id integer primary key autoincrement,
          source_id text,
          normalized_text_hash text,
          text text not null,
          url text,
          author text,
          created_at text,
          first_seen_at text not null,
          last_seen_at text not null,
          seen_count integer not null default 1,
          first_seen_run_id text not null,
          last_seen_run_id text not null,
          metadata_json text not null default '{}'
        );
        create table x_digest_post_sources (
          id integer primary key autoincrement,
          post_id integer not null,
          source text not null,
          seen_at text not null,
          run_id text not null,
          metadata_json text not null default '{}'
        );
        create table x_digest_runs (
          run_id text primary key,
          schema text not null,
          started_at text not null,
          finished_at text,
          status text not null
        );
        """
    )
    return conn


def _insert_post(conn, *, source_id, text, source, run_id, first_run_id=None, seen_count=1, hours_ago=1):
    now = datetime.now(UTC) - timedelta(hours=hours_ago)
    created_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    first_run_id = first_run_id or run_id
    cursor = conn.execute(
        """
        insert into x_digest_posts(
          source_id, text, url, author, created_at, first_seen_at, last_seen_at,
          seen_count, first_seen_run_id, last_seen_run_id, metadata_json
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            text,
            f"https://x.com/example/status/{source_id}",
            "source_author",
            created_at,
            created_at,
            created_at,
            seen_count,
            first_run_id,
            run_id,
            json.dumps({"importProvenance": {"publicHandle": "source_author"}}),
        ),
    )
    conn.execute(
        "insert into x_digest_post_sources(post_id, source, seen_at, run_id, metadata_json) values (?, ?, ?, ?, ?)",
        (cursor.lastrowid, source, created_at, run_id, json.dumps({"url": f"https://x.com/example/status/{source_id}"})),
    )


def test_import_x_digest_reads_sqlite_sources_and_writes_docs(tmp_path) -> None:
    db_path = tmp_path / "birdclaw.sqlite"
    conn = _create_digest_db(db_path)
    _insert_post(conn, source_id="1", text="Strong AI momentum for $NVDA.", source="bookmarks", run_id="run1")
    _insert_post(conn, source_id="2", text="$AMD looks extended intraday.", source="timeline", run_id="run1")
    _insert_post(
        conn,
        source_id="3",
        text="$SPY old repeated macro post.",
        source="timeline",
        run_id="run2",
        first_run_id="run1",
        seen_count=2,
    )
    conn.commit()
    conn.close()

    result = import_x_digest(
        db_path=db_path,
        latest_state="",
        output_dir=tmp_path / "source_docs",
        digest_dir=tmp_path / "digest",
        allowed_symbols={"NVDA", "AMD", "SPY"},
        sources=["bookmarks", "timeline"],
        include_resurfaced=False,
    )

    assert result.record_count == 2
    assert result.records_by_source == {"bookmarks": 1, "timeline": 1}
    assert result.skipped_resurfaced_count == 1
    assert result.cashtags == {"AMD": 1, "NVDA": 1}
    assert result.symbol_hits == {"AMD": 1, "NVDA": 1}
    assert len(result.source_doc_paths) == 2
    docs = "\n".join(path.read_text(encoding="utf-8") for path in result.source_doc_paths)
    assert "Birdclaw canonical X digest SQLite" in docs
    assert "source_lane: bookmarks" in docs
    assert "source_priority: 3" in docs
    assert "source lane and author are provenance only, not thesis tags" in docs
    assert "$SPY old repeated" not in docs


def test_import_x_digest_resolves_db_from_state(tmp_path) -> None:
    trial_root = tmp_path / "trial"
    db_path = trial_root / "birdclaw-home" / "birdclaw.sqlite"
    db_path.parent.mkdir(parents=True)
    conn = _create_digest_db(db_path)
    _insert_post(conn, source_id="1", text="$TSLA looks overextended.", source="timeline", run_id="run1")
    conn.commit()
    conn.close()

    state = tmp_path / "latest.json"
    state.write_text(
        json.dumps({"canonical_store": {"schema": "jarvis.x.digest.sqlite.v1", "db": "birdclaw-home/birdclaw.sqlite"}}),
        encoding="utf-8",
    )

    result = import_x_digest(
        latest_state=state,
        trial_root=trial_root,
        output_dir=tmp_path / "source_docs",
        digest_dir=tmp_path / "digest",
        sources=["timeline"],
    )

    assert result.db_path == db_path
    assert result.record_count == 1
    assert result.cashtags == {"TSLA": 1}
