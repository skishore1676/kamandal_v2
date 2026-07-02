"""Import Birdclaw SQLite X digest posts as Kamandal LLM source docs."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from kamandal_v2.paths import resolve_path


DEFAULT_DAILY_STATE = "~/.openclaw/workspace-main/state/x_daily_digest/latest.json"
DEFAULT_TRIAL_ROOT = "~/Documents/birdclaw"
DEFAULT_DIGEST_DB = "~/Documents/birdclaw/birdclaw-home/birdclaw.sqlite"
SOURCE_PRIORITIES = {"bookmarks": 3, "bookmark": 3, "timeline": 1}


@dataclass(frozen=True, slots=True)
class XDigestRecord:
    post_id: int
    source_id: str
    source: str
    text: str
    url: str
    author: str
    created_at: str
    first_seen_at: str
    last_seen_at: str
    seen_at: str
    seen_count: int
    run_id: str
    delta: str
    metadata: dict[str, Any]
    source_metadata: dict[str, Any]


@dataclass(slots=True)
class XDigestImportResult:
    db_path: Path
    state_path: Path | None
    source_doc_dir: Path
    source_doc_paths: list[Path]
    digest_path: Path
    record_count: int
    records_by_source: dict[str, int]
    skipped_resurfaced_count: int
    cashtags: dict[str, int]
    symbol_hits: dict[str, int]
    source_contract: str
    freshness: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": str(self.db_path),
            "state_path": str(self.state_path) if self.state_path else None,
            "source_doc_dir": str(self.source_doc_dir),
            "source_doc_paths": [str(path) for path in self.source_doc_paths],
            "digest_path": str(self.digest_path),
            "record_count": self.record_count,
            "records_by_source": dict(self.records_by_source),
            "skipped_resurfaced_count": self.skipped_resurfaced_count,
            "cashtags": dict(self.cashtags),
            "symbol_hits": dict(self.symbol_hits),
            "source_contract": self.source_contract,
            "freshness": dict(self.freshness),
        }


def import_x_digest(
    *,
    db_path: str | Path | None = None,
    latest_state: str | Path = DEFAULT_DAILY_STATE,
    trial_root: str | Path = DEFAULT_TRIAL_ROOT,
    output_dir: str | Path = "data/source_docs/x_digest",
    digest_dir: str | Path = "data/digest/x_digest",
    run_date: date | None = None,
    sources: Iterable[str] = ("bookmarks", "timeline"),
    allowed_symbols: set[str] | None = None,
    limit: int = 50,
    since_hours: int = 96,
    include_resurfaced: bool = False,
    birdclawctl: str | Path | None = None,
) -> XDigestImportResult:
    """Read Birdclaw's canonical X digest export and emit LLM source docs.

    Birdclaw owns X collection and deduplication. Kamandal only reads sanitized,
    canonical digest records, preserving source lane metadata for extraction.
    """

    state_path = _resolve_optional_state(latest_state)
    resolved_db = _resolve_db_path(db_path, state_path=state_path, trial_root=trial_root)
    normalized_sources = _normalize_sources(sources)
    cutoff = datetime.now(UTC) - timedelta(hours=max(1, int(since_hours)))
    per_source_limit = max(1, int(limit))
    export = _load_records_from_birdclawctl(
        birdclawctl=birdclawctl,
        trial_root=trial_root,
        sources=normalized_sources,
        limit=per_source_limit,
        since_hours=max(1, int(since_hours)),
        cutoff=cutoff,
        include_resurfaced=include_resurfaced,
    )
    if export is None:
        if not resolved_db.exists():
            raise FileNotFoundError(f"Birdclaw X digest DB not found: {resolved_db}")
        records, skipped_resurfaced = _load_records(
            resolved_db,
            sources=normalized_sources,
            limit=per_source_limit,
            cutoff=cutoff,
            include_resurfaced=include_resurfaced,
        )
        source_contract = "birdclaw.sqlite.fallback"
        freshness: dict[str, Any] = {}
    else:
        records, skipped_resurfaced, resolved_db, freshness = export
        source_contract = "birdclawctl.export.digest.v1"

    today = run_date or date.today()
    output_root = resolve_path(output_dir) / today.isoformat()
    output_root.mkdir(parents=True, exist_ok=True)
    digest_path = resolve_path(digest_dir)
    digest_path.mkdir(parents=True, exist_ok=True)

    grouped = _group_by_source(records, normalized_sources)
    source_doc_paths: list[Path] = []
    for source, source_records in grouped.items():
        if not source_records:
            continue
        path = output_root / f"x_{_safe_name(source)}.txt"
        path.write_text(_source_doc_text(resolved_db, state_path, source, source_records), encoding="utf-8")
        source_doc_paths.append(path)

    cashtags = _cashtags(records)
    symbol_hits = _symbol_hits(records, allowed_symbols or set())
    digest_file = digest_path / f"{today.isoformat()}_x_digest.md"
    digest_file.write_text(
        _digest_text(resolved_db, state_path, records, grouped, skipped_resurfaced, cashtags, symbol_hits),
        encoding="utf-8",
    )

    return XDigestImportResult(
        db_path=resolved_db,
        state_path=state_path,
        source_doc_dir=output_root,
        source_doc_paths=source_doc_paths,
        digest_path=digest_file,
        record_count=len(records),
        records_by_source={source: len(items) for source, items in grouped.items() if items},
        skipped_resurfaced_count=skipped_resurfaced,
        cashtags=cashtags,
        symbol_hits=symbol_hits,
        source_contract=source_contract,
        freshness=freshness,
    )


def _resolve_optional_state(latest_state: str | Path) -> Path | None:
    if not latest_state:
        return None
    path = resolve_path(latest_state)
    return path if path.exists() else None


def _resolve_db_path(db_path: str | Path | None, *, state_path: Path | None, trial_root: str | Path) -> Path:
    if db_path:
        return resolve_path(db_path)
    if state_path:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
        raw_db = str(((state.get("canonical_store") or {}).get("db") or "")).strip()
        if raw_db:
            candidate = Path(raw_db).expanduser()
            if candidate.is_absolute():
                return candidate
            rooted = resolve_path(trial_root) / candidate
            if rooted.exists():
                return rooted
    return resolve_path(DEFAULT_DIGEST_DB)


def _resolve_birdclawctl(birdclawctl: str | Path | None, trial_root: str | Path) -> Path | None:
    if birdclawctl:
        return resolve_path(birdclawctl)
    candidate = resolve_path(trial_root) / "birdclawctl"
    return candidate if candidate.exists() else None


def _normalize_sources(sources: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for source in sources:
        value = str(source or "").strip().lower()
        if not value:
            continue
        if value == "bookmark":
            value = "bookmarks"
        if value not in normalized:
            normalized.append(value)
    return normalized or ["bookmarks", "timeline"]


def _load_records(
    db_path: Path,
    *,
    sources: list[str],
    limit: int,
    cutoff: datetime,
    include_resurfaced: bool,
) -> tuple[list[XDigestRecord], int]:
    placeholders = ",".join("?" for _ in sources)
    sql = f"""
        with latest_sources as (
          select
            post_id,
            source,
            seen_at,
            run_id,
            metadata_json,
            row_number() over (partition by post_id, source order by seen_at desc, id desc) as rn
          from x_digest_post_sources
          where source in ({placeholders})
        )
        select
          p.id as post_id,
          coalesce(p.source_id, '') as source_id,
          p.text,
          coalesce(p.url, '') as url,
          coalesce(p.author, '') as author,
          coalesce(p.created_at, '') as created_at,
          p.first_seen_at,
          p.last_seen_at,
          p.seen_count,
          p.first_seen_run_id,
          p.metadata_json,
          s.source,
          s.seen_at,
          s.run_id,
          s.metadata_json as source_metadata_json
        from latest_sources s
        join x_digest_posts p on p.id = s.post_id
        where s.rn = 1
        order by s.seen_at desc, p.created_at desc, p.id desc
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, sources).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[XDigestRecord]] = {source: [] for source in sources}
    skipped_resurfaced = 0
    for row in rows:
        seen_at = _parse_dt(row["seen_at"])
        if seen_at and seen_at < cutoff:
            continue
        delta = "NEW" if row["first_seen_run_id"] == row["run_id"] else "SEEN_RECENTLY"
        if delta != "NEW" and not include_resurfaced:
            skipped_resurfaced += 1
            continue
        source = str(row["source"] or "x")
        if len(grouped.setdefault(source, [])) >= limit:
            continue
        grouped[source].append(_record_from_row(row, delta=delta))

    ordered: list[XDigestRecord] = []
    for source in sorted(grouped, key=lambda item: (-SOURCE_PRIORITIES.get(item, 0), item)):
        ordered.extend(grouped[source])
    return ordered, skipped_resurfaced


def _load_records_from_birdclawctl(
    *,
    birdclawctl: str | Path | None,
    trial_root: str | Path,
    sources: list[str],
    limit: int,
    since_hours: int,
    cutoff: datetime,
    include_resurfaced: bool,
) -> tuple[list[XDigestRecord], int, Path, dict[str, Any]] | None:
    command_path = _resolve_birdclawctl(birdclawctl, trial_root)
    if command_path is None or not command_path.exists():
        return None
    command = [
        str(command_path),
        "export",
        "digest",
        "--json",
        "--sources",
        ",".join(sources),
        "--since-hours",
        str(since_hours),
        "--limit",
        str(max(limit * max(1, len(sources)), limit)),
    ]
    completed = subprocess.run(command, cwd=command_path.parent, capture_output=True, text=True, timeout=60, check=False)
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("schema") != "birdclaw.digest_export.v1":
        return None
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
    if freshness.get("is_stale") is True:
        raise RuntimeError("Birdclaw X digest export is stale")
    raw_records = payload.get("records") or []
    if not isinstance(raw_records, list):
        raise RuntimeError("Birdclaw X digest export records must be a list")
    grouped: dict[str, list[XDigestRecord]] = {source: [] for source in sources}
    skipped_resurfaced = 0
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("source") or "x").strip().lower()
        if source == "bookmark":
            source = "bookmarks"
        if source not in sources:
            continue
        seen_at = _parse_dt(str(raw.get("seen_at") or ""))
        if seen_at and seen_at < cutoff:
            continue
        delta = str(raw.get("delta") or "NEW")
        if delta != "NEW" and not include_resurfaced:
            skipped_resurfaced += 1
            continue
        if len(grouped.setdefault(source, [])) >= limit:
            continue
        grouped[source].append(_record_from_export(raw, source=source, delta=delta))

    ordered: list[XDigestRecord] = []
    for source in sorted(grouped, key=lambda item: (-SOURCE_PRIORITIES.get(item, 0), item)):
        ordered.extend(grouped[source])
    db_path = resolve_path(payload.get("db_path") or DEFAULT_DIGEST_DB)
    return ordered, skipped_resurfaced, db_path, freshness


def _record_from_export(raw: dict[str, Any], *, source: str, delta: str) -> XDigestRecord:
    return XDigestRecord(
        post_id=int(raw.get("post_id") or 0),
        source_id=str(raw.get("source_id") or ""),
        source=source,
        text=str(raw.get("text") or "").strip(),
        url=str(raw.get("url") or ""),
        author=str(raw.get("author") or ""),
        created_at=str(raw.get("created_at") or ""),
        first_seen_at=str(raw.get("first_seen_at") or ""),
        last_seen_at=str(raw.get("last_seen_at") or ""),
        seen_at=str(raw.get("seen_at") or ""),
        seen_count=int(raw.get("seen_count") or 0),
        run_id=str(raw.get("run_id") or ""),
        delta=delta,
        metadata={},
        source_metadata={},
    )


def _record_from_row(row: sqlite3.Row, *, delta: str) -> XDigestRecord:
    return XDigestRecord(
        post_id=int(row["post_id"]),
        source_id=str(row["source_id"] or ""),
        source=str(row["source"] or "x"),
        text=str(row["text"] or "").strip(),
        url=str(row["url"] or ""),
        author=str(row["author"] or ""),
        created_at=str(row["created_at"] or ""),
        first_seen_at=str(row["first_seen_at"] or ""),
        last_seen_at=str(row["last_seen_at"] or ""),
        seen_at=str(row["seen_at"] or ""),
        seen_count=int(row["seen_count"] or 0),
        run_id=str(row["run_id"] or ""),
        delta=delta,
        metadata=_json_object(row["metadata_json"]),
        source_metadata=_json_object(row["source_metadata_json"]),
    )


def _parse_dt(raw: str) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _group_by_source(records: list[XDigestRecord], sources: list[str]) -> dict[str, list[XDigestRecord]]:
    grouped: dict[str, list[XDigestRecord]] = {source: [] for source in sources}
    for record in records:
        grouped.setdefault(record.source, []).append(record)
    return grouped


def _source_doc_text(db_path: Path, state_path: Path | None, source: str, records: list[XDigestRecord]) -> str:
    priority = SOURCE_PRIORITIES.get(source, 1)
    lines = [
        "Source: Birdclaw canonical X digest SQLite",
        f"Source lane: {source}",
        f"Source priority: {priority}",
        f"DB: {db_path}",
        f"State: {state_path or 'none'}",
        f"Record count: {len(records)}",
        "Extraction guidance: treat $TICKER cashtags as ticker evidence; do not infer symbols from ordinary words.",
        "Extraction guidance: source lane and author are provenance only, not thesis tags.",
        "Extraction guidance: bookmark lane implies stronger operator intent than timeline lane.",
        "",
    ]
    for index, record in enumerate(records, start=1):
        lines.extend([
            f"Record {index}",
            f"post_id: {record.post_id}",
            f"source_id: {record.source_id}",
            f"source_lane: {record.source}",
            f"source_priority: {priority}",
            f"author: {record.author}",
            f"created_at: {record.created_at}",
            f"seen_at: {record.seen_at}",
            f"delta: {record.delta}",
            f"seen_count: {record.seen_count}",
            f"url: {record.url}",
            "text:",
            record.text,
            "",
        ])
    return "\n".join(lines).strip() + "\n"


def _digest_text(
    db_path: Path,
    state_path: Path | None,
    records: list[XDigestRecord],
    grouped: dict[str, list[XDigestRecord]],
    skipped_resurfaced: int,
    cashtags: dict[str, int],
    symbol_hits: dict[str, int],
) -> str:
    lines = [
        "# X Digest Source Import",
        "",
        f"- db_path: {db_path}",
        f"- state_path: {state_path or 'none'}",
        f"- records: {len(records)}",
        f"- by_source: {_format_counts({key: len(value) for key, value in grouped.items() if value})}",
        f"- skipped_resurfaced: {skipped_resurfaced}",
        f"- cashtags: {_format_counts(cashtags)}",
        f"- universe_symbol_hits: {_format_counts(symbol_hits)}",
        "",
    ]
    for source, source_records in grouped.items():
        if not source_records:
            continue
        lines.append(f"## {source}")
        for record in source_records:
            text = " ".join(record.text.split())
            lines.append(
                f"- {record.created_at} @{record.author or 'unknown'} {record.delta} "
                f"seen={record.seen_count} {record.url} - {text[:280]}"
            )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _cashtags(records: Iterable[XDigestRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for tag in re.findall(r"\$([A-Za-z]{1,6})\b", record.text):
            symbol = tag.upper()
            counts[symbol] = counts.get(symbol, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _symbol_hits(records: Iterable[XDigestRecord], symbols: set[str]) -> dict[str, int]:
    counts = _cashtags(records)
    hits = {symbol.upper(): counts[symbol.upper()] for symbol in symbols if counts.get(symbol.upper())}
    return dict(sorted(hits.items(), key=lambda item: (-item[1], item[0])))


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def _safe_name(raw: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in raw.strip().lower())
    return safe or "x"
