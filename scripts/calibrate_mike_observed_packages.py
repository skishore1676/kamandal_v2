#!/usr/bin/env python3
"""Compare shared vision labor with browser-grounded Mike package labels."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kamandal_v2.intelligence.llm_client import BrokerJsonClient, CodexCliJsonClient
from kamandal_v2.intelligence.observed_packages import (
    ObservedPackageBatch,
    ObservedPackageValidationError,
    extract_observed_packages,
    normalize_observed_package_output,
)


def main() -> int:
    args = _parse_args()
    fixture_path = args.fixture.expanduser().resolve()
    fixture_root = fixture_path.parent
    manifest = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixtures = list(manifest.get("fixtures") or [])
    if args.post_id:
        fixtures = [fixture for fixture in fixtures if fixture.get("post_id") in args.post_id]
    client = BrokerJsonClient(
        actor="observed_package_extractor",
        timeout_seconds=args.timeout_seconds,
        fallback=CodexCliJsonClient(timeout_seconds=args.timeout_seconds),
    )
    runs: list[dict[str, Any]] = []
    for repeat in range(1, args.repeat + 1):
        for fixture in fixtures:
            post_id = str(fixture["post_id"])
            print(f"calibrating repeat={repeat} post_id={post_id}", flush=True)
            image_paths = tuple(fixture_root / image["path"] for image in fixture["images"])
            expected = normalize_observed_package_output(
                fixture["expected_extraction"],
                source_profile=str(manifest["source_profile"]),
                canonical_post_id=post_id,
                published_at=str(fixture["published_at"]),
                image_sha256=tuple(str(image["sha256"]) for image in fixture["images"]),
                prompt_sha256="ground-truth",
            )
            try:
                observed = extract_observed_packages(
                    client,
                    source_profile=str(manifest["source_profile"]),
                    canonical_post_id=post_id,
                    published_at=str(fixture["published_at"]),
                    post_text=str(fixture["post_text"]),
                    image_paths=image_paths,
                )
                expected_view = _semantic_view(expected)
                observed_view = _semantic_view(observed)
                runs.append(
                    {
                        "repeat": repeat,
                        "post_id": post_id,
                        "status": "accepted",
                        "exact_match": observed_view == expected_view,
                        "expected": expected_view,
                        "observed": observed_view,
                        "provider": client.last_receipt_summary,
                        "output_sha256": observed.output_sha256,
                    }
                )
            except ObservedPackageValidationError as exc:
                runs.append(
                    {
                        "repeat": repeat,
                        "post_id": post_id,
                        "status": "parked",
                        "exact_match": False,
                        "error": str(exc),
                        "raw_output": exc.raw_output,
                        "provider": client.last_receipt_summary,
                    }
                )
            except (RuntimeError, ValueError) as exc:
                runs.append(
                    {
                        "repeat": repeat,
                        "post_id": post_id,
                        "status": "failed",
                        "exact_match": False,
                        "error": str(exc),
                        "provider": client.last_receipt_summary,
                    }
                )

    exact = sum(1 for run in runs if run["exact_match"])
    parked = sum(1 for run in runs if run["status"] == "parked")
    failed = sum(1 for run in runs if run["status"] == "failed")
    falsely_complete = sum(1 for run in runs if _has_falsely_complete_package(run))
    exact_rate = exact / len(runs) if runs else 0.0
    summary = {
        "schema": "kamandal.observed_package_calibration_report.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_path": str(fixture_path),
        "fixture_count": len(fixtures),
        "repeat_count": args.repeat,
        "run_count": len(runs),
        "exact_match_count": exact,
        "parked_count": parked,
        "failed_count": failed,
        "exact_match_rate": exact_rate,
        "falsely_complete_count": falsely_complete,
        "transport_gate_passed": exact_rate >= 0.90 and falsely_complete == 0 and failed == 0,
        "production_extraction_reliability_passed": exact == len(runs) and falsely_complete == 0 and failed == 0,
        "reliability_claimed": False,
        "runs": runs,
        "effects": {
            "sheet_writes": False,
            "broker_effects": False,
            "plans_created": False,
            "tickets_created": False,
            "lifecycles_created": False,
            "external_sends": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "runs"}, indent=2), flush=True)
    return 0 if summary["transport_gate_passed"] else 2


def _semantic_view(batch: ObservedPackageBatch) -> dict[str, Any]:
    packages = []
    for package in batch.packages:
        packages.append(
            {
                "media_index": package.media_index,
                "package_position": package.package_position,
                "action": package.action,
                "structure": package.structure,
                "symbol": package.symbol,
                "product_type": package.product_type,
                "displayed_trade_time": package.displayed_trade_time,
                "displayed_price": dict(package.displayed_price) if package.displayed_price else None,
                "complete": package.complete,
                "blocker": package.blocker,
                "legs": [leg.to_dict() for leg in package.legs],
            }
        )
    return {
        "post_disposition": batch.post_disposition,
        "post_blocker": batch.post_blocker,
        "packages": packages,
    }


def _has_falsely_complete_package(run: dict[str, Any]) -> bool:
    if run.get("status") != "accepted" or run.get("exact_match"):
        return False
    expected = run.get("expected") or {}
    observed = run.get("observed") or {}
    expected_by_locator = {
        (item["media_index"], item["package_position"]): item
        for item in expected.get("packages") or []
    }
    for item in observed.get("packages") or []:
        locator = (item["media_index"], item["package_position"])
        if item.get("complete") and item != expected_by_locator.get(locator):
            return True
    return False


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/mike_observed_packages/ground-truth.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/research/mike-observed-package-calibration/latest.json"),
    )
    parser.add_argument("--post-id", action="append", default=[])
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
