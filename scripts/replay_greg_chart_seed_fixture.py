#!/usr/bin/env python3
"""Run the broker-inert Birdclaw -> Cartographer -> Kamandal fixture contract."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/research/greg_chart_seed_fixture"),
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
    source_id = "x-post:2079000000000000001"
    seed_request_path = output_root / "seed-request.json"
    birdclaw = _json_command(
        [
            "node",
            str(birdclaw_root / "src" / "cli.mjs"),
            "export",
            "greg-weekly-seeds",
            "--json",
            "--as-of",
            "2026-07-27T13:00:00-05:00",
            "--post-id",
            "2079000000000000001",
        ],
        cwd=birdclaw_root,
        env={
            **os.environ,
            "BIRDCLAW_DB": str(db_path),
            "BIRDCLAW_NOW": "2026-07-27T13:00:00Z",
        },
    )
    seed_request_path.write_text(json.dumps(birdclaw, indent=2) + "\n", encoding="utf-8")

    cartographer_output = output_root / "cartographer"
    cartographer_cli = cartographer_root / ".venv" / "bin" / "market-cartographer"
    if not cartographer_cli.is_file():
        raise RuntimeError("Market Cartographer environment missing; run `uv sync` in that repo")
    chart = _json_command(
        [
            str(cartographer_cli),
            "evaluate-seeds",
            "--input",
            str(seed_request_path),
            "--provider",
            "fixture",
            "--output",
            str(cartographer_output),
            "--render",
            "--format",
            "json",
        ],
        cwd=cartographer_root,
    )

    kamandal_output = output_root / "kamandal"
    kamandal_cli = kamandal_root / ".venv" / "bin" / "kamandal"
    if not kamandal_cli.is_file():
        raise RuntimeError("Kamandal environment missing; run `uv sync` in that repo")
    imported = _json_command(
        [
            str(kamandal_cli),
            "import-chart-seeds",
            "--input",
            str(cartographer_output / "seed-evaluation.json"),
            "--output-dir",
            str(kamandal_output),
        ],
        cwd=kamandal_root,
    )

    if birdclaw["source"]["source_id"] != source_id:
        raise RuntimeError("Birdclaw source identity changed during export")
    if chart["source"]["source_id"] != source_id:
        raise RuntimeError("Market Cartographer lost Birdclaw source identity")
    if imported["source_id"] != source_id:
        raise RuntimeError("Kamandal lost Birdclaw source identity")
    if chart["data"]["mode"] != "DEMO DATA":
        raise RuntimeError("fixture replay must remain visibly DEMO DATA")
    if chart["effects"]["planner_admission"] is not False:
        raise RuntimeError("Market Cartographer unexpectedly admitted planner effects")
    if imported["planner_eligible"] is not False:
        raise RuntimeError("Kamandal unexpectedly admitted fixture watches to the planner")
    if any(imported["effects"].values()):
        raise RuntimeError("Kamandal fixture replay reported a protected effect")

    receipt = {
        "schema": "kamandal.greg_chart_seed_replay.v1",
        "status": "succeeded",
        "mode": "DEMO DATA",
        "source_id": source_id,
        "chart_run_id": chart["run_id"],
        "kamandal_import_id": imported["import_id"],
        "symbols": [seed["symbol"] for seed in birdclaw["seeds"]],
        "artifacts": {
            "seed_request": str(seed_request_path),
            "chart_evaluation": str(cartographer_output / "seed-evaluation.json"),
            "chart_receipt": str(cartographer_output / "receipt.json"),
            "kamandal_watch": imported["watch_path"],
            "kamandal_review": imported["review_path"],
            "kamandal_receipt": imported["receipt_path"],
        },
        "effects": {
            "network": False,
            "x_mutation": False,
            "broker": False,
            "orders": False,
            "sheet_write": False,
            "planner_admission": False,
            "shadow_admission": False,
            "external_send": False,
        },
    }
    receipt_path = output_root / "replay-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
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
        timestamp = "2026-07-26T12:00:00Z"
        text = "Dragonfly Capital: 5 Trade Ideas for Monday $DE $HAS $PGR $SJM $TWLO"
        source_id = "2079000000000000001"
        connection.execute(
            """
            insert into x_digest_posts(
              source_id, normalized_text_hash, text, url, author, created_at,
              first_seen_at, last_seen_at, seen_count, first_seen_run_id,
              last_seen_run_id, metadata_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, 1, 'fixture-run', 'fixture-run', ?)
            """,
            (
                source_id,
                "fixture-hash",
                text,
                f"https://x.com/harmongreg/status/{source_id}",
                "harmongreg",
                timestamp,
                timestamp,
                timestamp,
                json.dumps(
                    {
                        "importProvenance": {
                            "publicHandle": "harmongreg",
                            "publicUrls": "https://dragonflycap.com/weekly-fixture",
                        }
                    }
                ),
            ),
        )
        connection.execute(
            """
            insert into x_digest_post_sources(post_id, source, seen_at, run_id)
            values (1, 'timeline', ?, 'fixture-run')
            """,
            (timestamp,),
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


if __name__ == "__main__":
    raise SystemExit(main())
