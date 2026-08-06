"""Autonomously generate Market Cartographer seed requests from Greg weekly_ideas that lack chart evaluation.

Intended to run inside run_x_bookmark_extraction.sh *before* correspondent activation,
so chart evaluation can unblock weekly_ideas in the same cycle. No human input.
Control surface remains Google Sheet / Telegram via Lathi if the request fails.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kamandal_v2.paths import resolve_path


def build_seed_request_from_translation(
    translation_path: str | Path,
    *,
    output_dir: str | Path,
    max_symbols: int = 8,
) -> Path | None:
    """Read a correspondent translation.json, collect weekly_ideas symbols that are
    currently parked with chart_evaluation_missing, and write a seed request JSON
    for Market Cartographer.

    Returns the written request path, or None if nothing pending.
    """
    translation_path = resolve_path(translation_path)
    if not translation_path.is_file():
        return None
    payload = json.loads(translation_path.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = list(payload.get("records") or [])
    pending: list[str] = []
    for rec in records:
        if str(rec.get("tweet_type") or "") != "weekly_ideas":
            continue
        if not rec.get("planner_eligible"):
            blockers = set(rec.get("planner_blockers") or [])
            if "chart_evaluation_missing" not in blockers:
                continue
        symbol = str(rec.get("symbol") or "").strip().upper()
        if symbol and symbol not in pending:
            pending.append(symbol)
        if len(pending) >= max_symbols:
            break

    if not pending:
        return None

    output_dir = resolve_path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    request_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    request_path = output_dir / f"seed-request-{request_id}.json"
    request_payload = {
        "schema": "market_cartographer.seed_request.v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": str(payload.get("profile", {}).get("profile_id") or "greg_harmon"),
        "translation_batch_id": str(payload.get("batch_id") or ""),
        "symbols": pending,
        "provider_hint": "mala",
        "notes": "auto-generated from weekly_ideas pending chart_evaluation_missing",
    }
    request_path.write_text(json.dumps(request_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return request_path


def latest_translation_path(output_root: str | Path, profile_id: str = "greg_harmon") -> Path | None:
    """Find the most recent translation.json for a profile under output_root/runs/<profile>."""
    output_root = resolve_path(output_root)
    runs_dir = output_root / "runs" / profile_id
    if not runs_dir.is_dir():
        return None
    latest: Path | None = None
    latest_mtime = -1.0
    for run_dir in runs_dir.iterdir():
        candidate = run_dir / "translation.json"
        if candidate.is_file():
            mtime = candidate.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest = candidate
    return latest
