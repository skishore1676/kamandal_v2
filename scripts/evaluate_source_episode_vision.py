#!/usr/bin/env python3
"""Run the episode compiler against the existing six-post public-image corpus.

The old exact extractor's labels are scoring inputs only. This exercises the
current episode compiler with all seven original images, not missing-media
placeholders. It is a regression corpus, not a new independent holdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml
from agent_broker import ProviderBinding

from evaluate_source_episode_models import _RecordingClient, _preserve_explicit_binding, _usage_summary
from kamandal_v2.intelligence.llm_client import BrokerJsonClient
from kamandal_v2.intelligence.source_episode_compiler import compile_source_episode_packet
from kamandal_v2.intelligence.source_episode_projection import _observed_leg
from kamandal_v2.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT / "tests/fixtures/mike_observed_packages"


def packets_and_labels() -> tuple[dict, list[dict]]:
    fixtures = json.loads((ROOT / "ground-truth.json").read_text())["fixtures"]
    records = []
    for fixture in fixtures:
        media = []
        for index, descriptor in enumerate(fixture["images"], 1):
            path = (ROOT / descriptor["path"]).resolve()
            if hashlib.sha256(path.read_bytes()).hexdigest() != descriptor["sha256"]:
                raise ValueError(f"fixture image hash mismatch: {path.name}")
            media.append({"media_index": index, "type": "photo", "cache_status": "cached",
                          "sha256": descriptor["sha256"], "artifact_path": str(path)})
        records.append({
            "signal_id": f"x-post:{fixture['post_id']}", "profile_id": "mike_butler",
            "source": {"kind": "public_x_post", "published_at": fixture["published_at"], "media": media},
            "classification": {"type": "unknown"},
            "literal": {"text": fixture["post_text"], "symbols": [
                {"symbol": symbol} for symbol in dict.fromkeys(re.findall(r"\$([A-Z][A-Z0-9.]*)", fixture["post_text"]))]},
        })
    return {"generated_at": "2026-08-28T20:00:00Z", "records": records}, fixtures


def package_key(package: dict, symbol: str, action: str, published_at: str) -> tuple:
    day = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
    legs = [_observed_leg(leg, day) for leg in package["legs"]]
    return symbol, action, tuple(sorted((leg.expiration, leg.strike, leg.option_type, leg.order_code, leg.quantity) for leg in legs))


def score_packages(fixtures: list[dict], episodes: list[dict]) -> dict:
    by_ref = {item["post_ref"]: item for item in episodes}
    expected_count = matched_count = emitted_count = wrong_count = 0
    rows = []
    for fixture in fixtures:
        post_ref = f"x-post:{fixture['post_id']}"
        expected = Counter(package_key(p, p["symbol"], p["action"], fixture["published_at"])
                           for p in fixture["expected_extraction"]["packages"] if p["complete"])
        actual = Counter()
        errors = []
        for event in (by_ref.get(post_ref) or {}).get("events", []):
            for package in event.get("exact_packages", []):
                if not package.get("complete"):
                    continue
                try:
                    actual[package_key(package, event["symbol"], event["action"], fixture["published_at"])] += 1
                except (ValueError, TypeError, KeyError) as exc:
                    errors.append(str(exc))
        matched = sum((actual & expected).values())
        wrong = sum((actual - expected).values()) + len(errors)
        expected_count += sum(expected.values())
        emitted_count += sum(actual.values()) + len(errors)
        matched_count += matched
        wrong_count += wrong
        rows.append({"post_ref": post_ref, "expected": sum(expected.values()), "matched": matched,
                     "wrong_complete": wrong, "missing": list((expected - actual).elements()),
                     "extra": list((actual - expected).elements()), "errors": errors})
    return {"expected_packages": expected_count, "matched_packages": matched_count,
            "emitted_complete_packages": emitted_count, "wrong_complete_packages": wrong_count,
            "exact_contract_recall": matched_count / expected_count,
            "exact_contract_precision": matched_count / emitted_count if emitted_count else None,
            "rows": rows,
            "limits": "Checks symbol, action and every leg's side/effect, strike, expiration, type and quantity. Displayed prices, image locator accuracy and downstream admission are not scored here."}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", required=True)
    parser.add_argument("--codex-binary", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet, fixtures = packets_and_labels()
    profile = yaml.safe_load((PROJECT_ROOT / "config/correspondents/mike_butler.yaml").read_text())
    binding = ProviderBinding("codex", {"model": args.model, "reasoning_effort": args.reasoning_effort,
        "binary": args.codex_binary, "verbosity": "low", "sandbox": "read-only", "approval_policy": "never",
        "ignore_user_config": True, "ephemeral": True})
    client = _RecordingClient(BrokerJsonClient(actor="source_episode_interpreter", lane_id="kamandal_evaluation",
                                              timeout_seconds=600, binding=binding))
    episodes = []
    error = None
    try:
        with _preserve_explicit_binding(), tempfile.TemporaryDirectory(prefix="kamandal-vision-input-only-") as isolated:
            prior = Path.cwd()
            try:
                os.chdir(isolated)
                compilation = compile_source_episode_packet(packet, profile, client)
                episodes = list(compilation.episodes)
            finally:
                os.chdir(prior)
    except Exception as exc:
        error = str(exc)[-1500:]
    result = {"model": args.model, "reasoning_effort": args.reasoning_effort, "compiler_error": error,
              "corpus": "six previously reviewed posts, seven original public images; not a fresh holdout",
              "score": score_packages(fixtures, episodes), "episodes": episodes, "model_turns": client.turns,
              "usage": _usage_summary(client.turns), "trading_effects": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in {"episodes", "model_turns"}}))


if __name__ == "__main__":
    main()
