#!/usr/bin/env python3
"""Effect-free replay of source episodes for one Central-time trading day.

The replay uses the newest retained Birdclaw packet for each requested source,
compiles the full packet so same-packet lifecycle context is available, and
then reports only events published on the requested day.  It never publishes
ideas, writes the operator Sheet, runs the planner, or calls a broker.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from kamandal_v2.config import load_control
from kamandal_v2.intelligence.correspondent_signals import load_correspondent_profile
from kamandal_v2.intelligence.llm_client import build_llm_client
from kamandal_v2.intelligence.source_episode_compiler import compile_source_episode_packet
from kamandal_v2.intelligence.source_episode_projection import project_source_episode_compilation
from kamandal_v2.paths import PROJECT_ROOT


CENTRAL = ZoneInfo("America/Chicago")
PROFILES = {
    "greg_harmon": PROJECT_ROOT / "config/correspondents/greg_harmon.yaml",
    "mike_butler": PROJECT_ROOT / "config/correspondents/mike_butler.yaml",
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Trading day in YYYY-MM-DD, interpreted in America/Chicago.")
    parser.add_argument(
        "--profile",
        action="append",
        choices=tuple(PROFILES),
        help="Source profile to replay; repeat for more than one. Defaults to all profiles.",
    )
    parser.add_argument(
        "--packet-root",
        type=Path,
        default=PROJECT_ROOT / "data/research/correspondent_signals/packets",
    )
    parser.add_argument(
        "--policy-snapshot",
        type=Path,
        help="Frozen daily policy snapshot used only to determine the day's configured universe.",
    )
    return parser.parse_args()


def _latest_packet(root: Path, profile_id: str) -> tuple[Path, dict[str, Any]]:
    candidates = tuple((root / profile_id).glob("*.json"))
    if not candidates:
        raise FileNotFoundError(f"no retained packets for {profile_id} under {root}")
    path = max(candidates, key=lambda item: item.stat().st_mtime_ns)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"packet must be an object: {path}")
    return path, payload


def _record_day(record: Mapping[str, Any]) -> str:
    published_at = str(((record.get("source") or {}).get("published_at")) or "")
    if not published_at:
        return ""
    observed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    return observed.astimezone(CENTRAL).date().isoformat()


def _snapshot_path(day: str, configured: Path | None) -> Path:
    if configured is not None:
        return configured
    return PROJECT_ROOT / f"data/run/strategy_policy/strategy_policy_{day}.json"


def _universe(path: Path) -> tuple[str, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = ((payload.get("tables") or {}).get("universe") or [])
    return tuple(
        str(row.get("symbol") or "").strip().upper()
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("enabled") or "").strip().lower() in {"1", "true", "yes", "on"}
        and str(row.get("symbol") or "").strip()
    )


def main() -> int:
    args = _args()
    # Validate the date before any model work.
    datetime.strptime(args.date, "%Y-%m-%d")
    profiles = tuple(args.profile or PROFILES)
    snapshot_path = _snapshot_path(args.date, args.policy_snapshot)
    universe = _universe(snapshot_path)
    client = build_llm_client(load_control(), actor="source_episode_interpreter")
    results: list[dict[str, Any]] = []

    for profile_id in profiles:
        packet_path, packet = _latest_packet(args.packet_root, profile_id)
        profile, _ = load_correspondent_profile(PROFILES[profile_id])
        compilation = compile_source_episode_packet(packet, profile, client)
        projection = project_source_episode_compilation(
            compilation,
            packet,
            profile,
            universe_symbols=universe,
        )
        record_days = {
            str(record.get("signal_id") or ""): _record_day(record)
            for record in packet.get("records") or []
            if isinstance(record, Mapping)
        }
        episode_refs = {
            str(episode.get("post_ref") or "")
            for episode in compilation.episodes
            if record_days.get(str(episode.get("post_ref") or "")) == args.date
        }
        episodes = [
            episode for episode in compilation.episodes if str(episode.get("post_ref") or "") in episode_refs
        ]
        observations = [
            item for item in projection.observations if str(item.get("post_ref") or "") in episode_refs
        ]
        ideas = [
            item
            for item in projection.planner_ideas
            if any(f"post_ref={post_ref};" in str(item.get("notes") or "") for post_ref in episode_refs)
        ]
        exact_batches = [
            batch.to_dict()
            for batch in projection.observed_batches
            if batch.canonical_post_id in episode_refs
        ]
        results.append(
            {
                "profile_id": profile_id,
                "packet_path": str(packet_path),
                "packet_record_count": len(packet.get("records") or []),
                "day_record_count": len(episode_refs),
                "episodes": episodes,
                "planner_idea_projections": ideas,
                "exact_package_projections": exact_batches,
                "activity_observations": observations,
                "model_receipts": list(compilation.model_receipts),
            }
        )

    print(
        json.dumps(
            {
                "schema": "kamandal.source_episode_day_replay.v1",
                "trading_day": args.date,
                "timezone": str(CENTRAL),
                "policy_snapshot": str(snapshot_path),
                "universe_count": len(universe),
                "profiles": results,
                "effects": {
                    "sheet_write": False,
                    "active_idea_publication": False,
                    "planner_run": False,
                    "shadow_admission": False,
                    "live_admission": False,
                    "broker_effects": False,
                    "order_effects": False,
                    "external_send": False,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
