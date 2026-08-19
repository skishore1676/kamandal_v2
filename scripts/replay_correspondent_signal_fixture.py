#!/usr/bin/env python3
"""Replay two correspondent profiles through Birdclaw, Cartographer, and Kamandal."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from kamandal_v2.config import load_control
from kamandal_v2.market.fixture import FixtureMarketDataProvider
from kamandal_v2.planner.engine import run_plan
from kamandal_v2.stores.audit import AuditWriter
from kamandal_v2.stores.sqlite import LocalStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/research/correspondent_signal_fixture_v1"),
    )
    args = parser.parse_args()

    kamandal_root = Path(__file__).resolve().parents[1]
    code_root = kamandal_root.parent
    birdclaw_root = code_root / "birdclaw"
    cartographer_root = code_root / "market-cartographer"
    output_root = args.output_root.expanduser()
    if not output_root.is_absolute():
        output_root = (kamandal_root / output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    db_path = output_root / "fixture-birdclaw.sqlite"
    _fixture_db(db_path)
    acquisition_receipt_path = output_root / "fixture-birdclaw-acquisition.json"
    _write_json(
        acquisition_receipt_path,
        {
            "schema": "birdclaw.correspondent_acquisition_receipt.v1",
            "generated_at": "2026-08-01T14:55:00Z",
            "run_id": "fixture-acquisition-run",
            "status": "succeeded",
            "attempts": [
                {
                    "profile_id": "greg_harmon",
                    "author_handle": "harmongreg",
                    "status": "succeeded",
                    "coverage_status": "continuous",
                },
                {
                    "profile_id": "sample_person",
                    "author_handle": "sampleperson",
                    "status": "succeeded",
                    "coverage_status": "continuous",
                },
            ],
        },
    )
    common_env = {
        **os.environ,
        "BIRDCLAW_DB": str(db_path),
        "BIRDCLAW_NOW": "2026-08-01T15:00:00Z",
        "BIRDCLAW_CORRESPONDENT_ACQUISITION_PATH": str(acquisition_receipt_path),
    }
    greg_packet = _json_command(
        [
            "node",
            str(birdclaw_root / "src" / "cli.mjs"),
            "export",
            "correspondent-signals",
            "--profile",
            "greg_harmon",
            "--since-hours",
            "24",
            "--limit",
            "20",
            "--json",
        ],
        cwd=birdclaw_root,
        env=common_env,
    )
    greg_packet_path = output_root / "greg-signals.json"
    _write_json(greg_packet_path, greg_packet)

    weekly_request = _json_command(
        [
            "node",
            str(birdclaw_root / "src" / "cli.mjs"),
            "export",
            "correspondent-chart-seeds",
            "--profile",
            "greg_harmon",
            "--as-of",
            "2026-08-01T15:00:00Z",
            "--post-id",
            "1001",
            "--json",
        ],
        cwd=birdclaw_root,
        env=common_env,
    )
    weekly_request_path = output_root / "greg-weekly-request.json"
    _write_json(weekly_request_path, weekly_request)

    cartographer_cli = cartographer_root / ".venv" / "bin" / "market-cartographer"
    if not cartographer_cli.is_file():
        raise RuntimeError("Market Cartographer environment missing; run `uv sync` in that repo")
    chart_dir = output_root / "cartographer"
    chart = _json_command(
        [
            str(cartographer_cli),
            "evaluate-seeds",
            "--input",
            str(weekly_request_path),
            "--provider",
            "fixture",
            "--output",
            str(chart_dir),
            "--format",
            "json",
        ],
        cwd=cartographer_root,
    )

    kamandal_cli = kamandal_root / ".venv" / "bin" / "kamandal"
    if not kamandal_cli.is_file():
        raise RuntimeError("Kamandal environment missing; run `uv sync` in that repo")
    greg_import = _json_command(
        [
            str(kamandal_cli),
            "import-correspondent-signals",
            "--input",
            str(greg_packet_path),
            "--profile",
            "config/correspondents/greg_harmon.yaml",
            "--chart-evaluation",
            str(chart_dir / "seed-evaluation.json"),
            "--config-source",
            "seed",
            "--deterministic-intent",
            "--output-dir",
            str(output_root / "kamandal"),
        ],
        cwd=kamandal_root,
    )

    sample_packet = _json_command(
        [
            "node",
            str(birdclaw_root / "src" / "cli.mjs"),
            "export",
            "correspondent-signals",
            "--profile",
            str(birdclaw_root / "tests" / "fixtures" / "correspondent-profile-sample.json"),
            "--since-hours",
            "24",
            "--json",
        ],
        cwd=birdclaw_root,
        env=common_env,
    )
    sample_packet_path = output_root / "sample-signals.json"
    _write_json(sample_packet_path, sample_packet)
    sample_import = _json_command(
        [
            str(kamandal_cli),
            "import-correspondent-signals",
            "--input",
            str(sample_packet_path),
            "--profile",
            "tests/fixtures/correspondent-profile-sample.yaml",
            "--config-source",
            "seed",
            "--deterministic-intent",
            "--output-dir",
            str(output_root / "kamandal"),
        ],
        cwd=kamandal_root,
    )

    planner = run_plan(
        load_control(),
        idea_paths=[greg_import["planner_ideas_path"], sample_import["planner_ideas_path"]],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=LocalStore(output_root / "planner" / "fixture.db"),
        audit=AuditWriter(output_root / "planner" / "audit"),
        # A fixture replay must not inherit the host's IV or earnings stores.
        # Inject the deterministic market directly so the proof is portable.
        market_override=FixtureMarketDataProvider(),
    )
    eligible = sorted(
        {
            (candidate.underlying, candidate.structure)
            for candidate in planner.candidates
            if candidate.eligible
        }
    )
    if not any(symbol == "IWM" and structure == "call_spread" for symbol, structure in eligible):
        raise RuntimeError("Greg opening journal idea did not reach the existing call-spread planner")
    if not any(symbol == "SPY" and structure == "call_spread" for symbol, structure in eligible):
        raise RuntimeError("second profile did not reach the existing call-spread planner")

    expected_greg_types = {"weekly_ideas", "earnings_idea", "trade_journal", "irrelevant"}
    observed_greg_types = {record["classification"]["type"] for record in greg_packet["records"]}
    if not expected_greg_types.issubset(observed_greg_types):
        raise RuntimeError("Greg fixture did not cover every expected source family")
    if chart["data"]["mode"] != "DEMO DATA":
        raise RuntimeError("chart fixture must remain visibly DEMO DATA")
    for imported in (greg_import, sample_import):
        if any(imported["effects"].values()):
            raise RuntimeError("correspondent import reported a protected effect")
    greg_translation = json.loads(Path(greg_import["translation_path"]).read_text(encoding="utf-8"))
    sample_translation = json.loads(Path(sample_import["translation_path"]).read_text(encoding="utf-8"))
    if greg_translation["source_acquisition"]["status"] != "succeeded":
        raise RuntimeError("Greg Birdclaw acquisition health did not reach Kamandal translation")
    if sample_translation["source_acquisition"]["status"] != "succeeded":
        raise RuntimeError("second-profile Birdclaw acquisition health did not reach Kamandal translation")

    receipt = {
        "schema": "kamandal.correspondent_signal_replay.v1",
        "status": "succeeded",
        "mode": "DEMO DATA / FIXTURE PLANNER",
        "profiles": {
            "greg_harmon": {
                "source_records": len(greg_packet["records"]),
                "classifications": greg_packet["counts"],
                "acquisition_status": greg_packet["acquisition"]["status"],
                "batch_id": greg_import["batch_id"],
                "planner_idea_count": greg_import["planner_idea_count"],
            },
            "sample_person": {
                "source_records": len(sample_packet["records"]),
                "classifications": sample_packet["counts"],
                "acquisition_status": sample_packet["acquisition"]["status"],
                "batch_id": sample_import["batch_id"],
                "planner_idea_count": sample_import["planner_idea_count"],
            },
        },
        "chart_run_id": chart["run_id"],
        "planner": {
            "ideas": sorted((idea.underlying, tuple(idea.allowed_structures)) for idea in planner.ideas),
            "eligible_candidates": eligible,
            "plans": len(planner.plans),
            "write_sheet": False,
        },
        "artifacts": {
            "greg_packet": str(greg_packet_path),
            "birdclaw_acquisition": str(acquisition_receipt_path),
            "weekly_request": str(weekly_request_path),
            "chart_evaluation": str(chart_dir / "seed-evaluation.json"),
            "greg_translation": greg_import["translation_path"],
            "greg_review": greg_import["review_path"],
            "greg_planner_ideas": greg_import["planner_ideas_path"],
            "sample_packet": str(sample_packet_path),
            "sample_translation": sample_import["translation_path"],
            "sample_planner_ideas": sample_import["planner_ideas_path"],
        },
        "effects": {
            "network": False,
            "x_mutation": False,
            "sheet_write": False,
            "shadow_admission": False,
            "live_admission": False,
            "broker": False,
            "orders": False,
            "external_send": False,
        },
    }
    receipt_path = output_root / "replay-receipt.json"
    _write_json(receipt_path, receipt, idempotent=True)
    print(json.dumps(receipt, indent=2))
    return 0


def _fixture_db(path: Path) -> None:
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
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
            """
        )
        rows = [
            ("1001", "Dragonfly Capital: 5 Trade Ideas for Monday $SPY $QQQ", "harmongreg"),
            ("1002", "Took TSLA trade idea #4", "harmongreg"),
            ("1003", "Bought $IWM call spread for 1.25", "harmongreg"),
            ("1004", "Closed $IWM call spread", "harmongreg"),
            ("1005", "Great game last night", "harmongreg"),
            ("2001", "Swing entry: Bought $SPY call spread", "sampleperson"),
        ]
        for index, (source_id, text, author) in enumerate(rows, start=1):
            timestamp = f"2026-08-01T14:{index:02d}:00Z"
            connection.execute(
                """
                insert into x_digest_posts(
                  source_id, normalized_text_hash, text, url, author, created_at,
                  first_seen_at, last_seen_at, seen_count, first_seen_run_id,
                  last_seen_run_id, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, 1, 'fixture-run', 'fixture-run', '{}')
                """,
                (
                    source_id,
                    f"fixture-{source_id}",
                    text,
                    f"https://x.com/{author}/status/{source_id}",
                    author,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "insert into x_digest_post_sources(post_id, source, seen_at, run_id) values (?, 'timeline', ?, 'fixture-run')",
                (index, timestamp),
            )
        connection.commit()
    finally:
        connection.close()


def _json_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{completed.stderr}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command returned invalid JSON: {' '.join(command)}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"command returned non-object JSON: {' '.join(command)}")
    return payload


def _write_json(path: Path, payload: dict[str, Any], *, idempotent: bool = False) -> None:
    content = json.dumps(payload, indent=2) + "\n"
    if idempotent and path.exists() and path.read_text(encoding="utf-8") != content:
        raise RuntimeError(f"idempotent artifact collision: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
