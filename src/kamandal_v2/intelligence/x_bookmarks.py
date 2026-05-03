"""Import sanitized X bookmark exports as Kamandal source documents."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from kamandal_v2.paths import resolve_path


DEFAULT_LATEST_STATE = "~/.openclaw/workspace-main/state/x_bookmark_shadow/latest.json"
DEFAULT_TRIAL_ROOT = "~/.openclaw/workspace-main/experiments/birdclaw-trial"


@dataclass(slots=True)
class XBookmarkImportResult:
    source_file: Path
    source_doc_path: Path
    digest_path: Path
    record_count: int
    cashtags: dict[str, int]
    symbol_hits: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": str(self.source_file),
            "source_doc_path": str(self.source_doc_path),
            "digest_path": str(self.digest_path),
            "record_count": self.record_count,
            "cashtags": dict(self.cashtags),
            "symbol_hits": dict(self.symbol_hits),
        }


def import_x_bookmarks(
    *,
    source_file: str | Path | None = None,
    latest_state: str | Path = DEFAULT_LATEST_STATE,
    trial_root: str | Path = DEFAULT_TRIAL_ROOT,
    output_dir: str | Path = "data/source_docs/x_bookmarks",
    digest_dir: str | Path = "data/digest/x_bookmarks",
    run_date: date | None = None,
    allowed_symbols: set[str] | None = None,
    limit: int = 50,
) -> XBookmarkImportResult:
    """Convert a sanitized public X bookmark export into LLM-readable text."""

    resolved_source = _resolve_source_file(source_file, latest_state, trial_root)
    payload = json.loads(resolved_source.read_text(encoding="utf-8"))
    records = _records(payload)[: max(1, limit)]
    today = run_date or date.today()
    run_name = _run_name(resolved_source)

    output_path = resolve_path(output_dir) / run_name
    digest_path = resolve_path(digest_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    digest_path.mkdir(parents=True, exist_ok=True)

    source_doc = output_path / "x_bookmarks.txt"
    digest_file = digest_path / f"{today.isoformat()}_{run_name}.md"
    cashtags = _cashtags(records)
    symbol_hits = _symbol_hits(records, allowed_symbols or set())

    source_doc.write_text(_source_doc_text(resolved_source, records), encoding="utf-8")
    digest_file.write_text(_digest_text(resolved_source, records, cashtags, symbol_hits), encoding="utf-8")

    return XBookmarkImportResult(
        source_file=resolved_source,
        source_doc_path=source_doc,
        digest_path=digest_file,
        record_count=len(records),
        cashtags=cashtags,
        symbol_hits=symbol_hits,
    )


def _resolve_source_file(
    source_file: str | Path | None,
    latest_state: str | Path,
    trial_root: str | Path,
) -> Path:
    if source_file:
        path = resolve_path(source_file)
        if not path.exists():
            raise FileNotFoundError(f"X bookmark source file not found: {path}")
        return path

    state_path = resolve_path(latest_state)
    if not state_path.exists():
        raise FileNotFoundError(f"X bookmark latest state not found: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_path = str(state.get("raw_path") or "")
    if not raw_path:
        raise RuntimeError(f"X bookmark latest state missing raw_path: {state_path}")
    raw_name = Path(raw_path).name
    stem = raw_name.removesuffix(".raw.json")
    root = resolve_path(trial_root)
    candidates = [
        root / "data" / "jarvis-sanitized-exports" / f"{stem}.public-export.json",
        root / "data" / "jarvis-sanitized-exports" / f"{stem}.sanitized.json",
        root / "data" / "jarvis-imports" / f"{stem}.normalized.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No sanitized X bookmark export found for latest state: " + ", ".join(str(item) for item in candidates))


def _records(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        raise RuntimeError("X bookmark source must be a JSON object")
    schema = str(payload.get("schema") or "")
    if schema not in {
        "jarvis.birdclaw.explicit-public-post-export.v1",
        "jarvis.birdclaw.sanitized-posts.v1",
    }:
        raise RuntimeError(f"Unsupported X bookmark schema: {schema}")
    raw_records = payload.get("records") or payload.get("items") or []
    if not isinstance(raw_records, list):
        raise RuntimeError("X bookmark source records/items must be a list")
    records: list[dict[str, str]] = []
    for item in raw_records:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        record_id = str(item.get("id") or item.get("sourceId") or item.get("source_id") or "").strip()
        created_at = str(item.get("created_at") or item.get("createdAt") or "").strip()
        url = str(item.get("url") or "").strip()
        provenance = str(item.get("provenance") or item.get("importProvenance") or "").strip()
        records.append({
            "id": record_id,
            "created_at": created_at,
            "url": url,
            "text": text,
            "provenance": provenance,
        })
    return records


def _source_doc_text(source_file: Path, records: list[dict[str, str]]) -> str:
    lines = [
        "Source: sanitized X bookmark public export",
        f"Source file: {source_file}",
        f"Record count: {len(records)}",
        "Extraction guidance: treat $TICKER cashtags as ticker evidence; do not infer symbols from ordinary words.",
        "",
    ]
    for index, record in enumerate(records, start=1):
        lines.extend([
            f"Record {index}",
            f"id: {record['id']}",
            f"created_at: {record['created_at']}",
            f"url: {record['url']}",
            "text:",
            record["text"],
            "",
        ])
    return "\n".join(lines).strip() + "\n"


def _digest_text(
    source_file: Path,
    records: list[dict[str, str]],
    cashtags: dict[str, int],
    symbol_hits: dict[str, int],
) -> str:
    lines = [
        "# X Bookmark Source Import",
        "",
        f"- source_file: {source_file}",
        f"- records: {len(records)}",
        f"- cashtags: {_format_counts(cashtags)}",
        f"- universe_symbol_hits: {_format_counts(symbol_hits)}",
        "",
        "## Records",
    ]
    for record in records:
        text = " ".join(record["text"].split())
        lines.append(f"- {record['created_at']} {record['url']} - {text[:280]}")
    return "\n".join(lines).strip() + "\n"


def _cashtags(records: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for tag in re.findall(r"\$([A-Za-z]{1,6})\b", record["text"]):
            symbol = tag.upper()
            counts[symbol] = counts.get(symbol, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _symbol_hits(records: list[dict[str, str]], symbols: set[str]) -> dict[str, int]:
    cashtag_counts = _cashtags(records)
    counts: dict[str, int] = {}
    for symbol in symbols:
        count = cashtag_counts.get(symbol.upper(), 0)
        if count:
            counts[symbol.upper()] = count
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def _run_name(source_file: Path) -> str:
    name = source_file.name
    for suffix in (".public-export.json", ".sanitized.json", ".normalized.json", ".json"):
        name = name.removesuffix(suffix)
    return name.replace(".", "_")
