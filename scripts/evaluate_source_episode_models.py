#!/usr/bin/env python3
"""Compare source-episode interpretation models on the frozen operator corpus.

This harness is intentionally effect-free. It compiles sanitized fixture posts,
scores the durable events, and writes a local report. It never publishes ideas,
runs the planner, touches the operator Sheet, or calls a broker.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import yaml
from agent_broker import ProviderBinding

from kamandal_v2.intelligence.llm_client import BrokerJsonClient
from kamandal_v2.intelligence.source_episode_compiler import compile_source_episode_packet
from kamandal_v2.paths import PROJECT_ROOT

CORPUS = PROJECT_ROOT / "tests/fixtures/trade_source_interpretation/gold-v0.jsonl"
ROUTING = PROJECT_ROOT / "tests/fixtures/agent-broker-routing-policy.yaml"
PROFILES = {
    "greg_harmon": PROJECT_ROOT / "config/correspondents/greg_harmon.yaml",
    "mike_butler": PROJECT_ROOT / "config/correspondents/mike_butler.yaml",
}
EFFECT_KEYS = {
    "sheet_write",
    "active_idea_publication",
    "planner_run",
    "shadow_admission",
    "live_admission",
    "broker_effects",
    "order_effects",
    "external_send",
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        default="gpt-5.6-luna,gpt-5.6-terra",
        help="Comma-separated Codex model names.",
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "outputs/source_episode_model_evaluation",
    )
    return parser.parse_args()


def _load_cases(path: Path = CORPUS) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: case must be an object")
        cases.append(payload)
    return cases


def _classification(case: Mapping[str, Any]) -> str:
    text = str(case.get("text") or "")
    lowered = text.lower()
    source = str(case.get("source_id") or "")
    if source == "greg_harmon":
        if lowered.startswith("premium earnings"):
            return "earnings_bundle"
        if "trade idea" in lowered:
            return "earnings_idea"
        if re.search(r"\b(?:added|bought|sold|closed|covered)\b", lowered):
            return "trade_journal"
        return "unknown"
    if source == "mike_butler":
        if re.search(r"\b(?:closed|rolled|moved|still have|half off)\b", lowered):
            return "observed_package_followup"
        if re.search(r"\b(?:new|downside|upside|calendar|diagonal|crab|butterfl)\b", lowered):
            return "observed_package_open"
    return "unknown"


def _build_packets(cases: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cases_list = list(cases)
    newest = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
    for index, case in enumerate(cases_list):
        source_id = str(case["source_id"])
        post_ref = str(case["post_ref"])
        text = str(case.get("text") or "")
        symbols = list(dict.fromkeys(re.findall(r"\$([A-Z][A-Z0-9./-]{0,19})\b", text)))
        expected_events = case.get("expected_events") or []
        media_needed = "needs_media" in str(case.get("gold_status") or "") or (
            source_id == "mike_butler"
            and any(
                isinstance(event, Mapping)
                and (event.get("legs") or event.get("expected_package_count"))
                for event in expected_events
            )
        )
        source: dict[str, Any] = {
            "kind": "public_x_post",
            "source_id": post_ref,
            "source_url": f"https://x.com/i/status/{post_ref.split(':')[-1]}",
            "published_at": (newest - timedelta(minutes=index)).isoformat().replace("+00:00", "Z"),
            "author_handle": source_id,
            "expanded_urls": [],
            "observation_sources": ["frozen_operator_corpus"],
        }
        if media_needed:
            source["media"] = [
                {
                    "media_index": 1,
                    "type": "photo",
                    "cache_status": "missing",
                    "artifact_path": "",
                    "sha256": "",
                }
            ]
        grouped[source_id].append(
            {
                "schema": "birdclaw.correspondent_signal.v1",
                "signal_id": post_ref,
                "profile_id": source_id,
                "source": source,
                "classification": {
                    "type": _classification(case),
                    "rule_id": "frozen_corpus_projection",
                    "interpretation_status": "evaluation_only",
                },
                "literal": {
                    "text": text,
                    "symbols": [
                        {"symbol": symbol, "origin": "literal_cashtag"}
                        for symbol in symbols
                    ],
                    "idea_number": None,
                },
            }
        )
    return {
        source_id: {
            "schema": "birdclaw.correspondent_signals.v1",
            "generated_at": "2026-09-03T19:00:00Z",
            "records": records,
        }
        for source_id, records in grouped.items()
    }


def _event_key(event: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(event.get("action") or ""),
        str(event.get("symbol") or ""),
        str(event.get("structure_hint") or ""),
    )


def _score(
    cases: Iterable[Mapping[str, Any]],
    episodes: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    case_by_ref = {str(case["post_ref"]): case for case in cases}
    episode_by_ref = {str(episode["post_ref"]): episode for episode in episodes}
    expected_total = 0
    actual_total = 0
    matched_total = 0
    expected_entry_total = 0
    matched_entry_total = 0
    false_new_entries: list[str] = []
    invented_media_packages: list[str] = []
    unsafe_effects: list[str] = []
    discrepancies: list[dict[str, Any]] = []

    for post_ref, case in case_by_ref.items():
        expected = list(case.get("expected_events") or [])
        actual = list((episode_by_ref.get(post_ref) or {}).get("events") or [])
        expected_total += len(expected)
        actual_total += len(actual)
        unmatched_actual = set(range(len(actual)))
        matches: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        missing: list[tuple[str, str, str]] = []
        for wanted in expected:
            expected_entry_total += int(bool(wanted.get("planner_new_entry")))
            wanted_key = _event_key(wanted)
            found = next(
                (index for index in unmatched_actual if _event_key(actual[index]) == wanted_key),
                None,
            )
            if found is None:
                missing.append(wanted_key)
                continue
            unmatched_actual.remove(found)
            matched_total += 1
            observed = actual[found]
            matches.append((wanted, observed))
            if wanted.get("planner_new_entry") and observed.get("planner_new_entry"):
                matched_entry_total += 1
            if observed.get("planner_new_entry") and not wanted.get("planner_new_entry"):
                false_new_entries.append(f"{post_ref}:{wanted_key}")

        for index in sorted(unmatched_actual):
            observed = actual[index]
            if observed.get("planner_new_entry"):
                false_new_entries.append(f"{post_ref}:{_event_key(observed)}")
        if "needs_media" in str(case.get("gold_status") or ""):
            for observed in actual:
                packages = observed.get("exact_packages") or []
                if any(
                    isinstance(package, Mapping) and package.get("complete")
                    for package in packages
                ):
                    invented_media_packages.append(f"{post_ref}:{_event_key(observed)}")
        episode_effects = (episode_by_ref.get(post_ref) or {}).get("effects") or {}
        if any(bool(episode_effects.get(key)) for key in EFFECT_KEYS):
            unsafe_effects.append(post_ref)
        if missing or unmatched_actual:
            discrepancies.append(
                {
                    "post_ref": post_ref,
                    "missing": [list(item) for item in missing],
                    "extra": [_event_key(actual[index]) for index in sorted(unmatched_actual)],
                }
            )

    event_recall = matched_total / expected_total if expected_total else 1.0
    event_precision = matched_total / actual_total if actual_total else float(expected_total == 0)
    entry_recall = matched_entry_total / expected_entry_total if expected_entry_total else 1.0
    hard_gate_pass = not false_new_entries and not invented_media_packages and not unsafe_effects
    return {
        "expected_event_count": expected_total,
        "actual_event_count": actual_total,
        "matched_event_count": matched_total,
        "event_recall": round(event_recall, 4),
        "event_precision": round(event_precision, 4),
        "expected_entry_count": expected_entry_total,
        "matched_entry_count": matched_entry_total,
        "entry_recall": round(entry_recall, 4),
        "false_new_entry_count": len(false_new_entries),
        "false_new_entries": false_new_entries,
        "invented_media_package_count": len(invented_media_packages),
        "invented_media_packages": invented_media_packages,
        "unsafe_effect_count": len(unsafe_effects),
        "hard_gate_pass": hard_gate_pass,
        "discrepancies": discrepancies,
    }


@contextmanager
def _preserve_explicit_binding() -> Iterator[None]:
    key = "AGENT_BROKER_ROUTING_FILE"
    prior = os.environ.get(key)
    os.environ[key] = str(ROUTING)
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prior


def _run_model(
    model: str,
    repetition: int,
    cases: list[dict[str, Any]],
    *,
    reasoning_effort: str,
) -> dict[str, Any]:
    binding = ProviderBinding(
        "codex",
        {
            "model": model,
            "reasoning_effort": reasoning_effort,
            "verbosity": "low",
            "sandbox": "read-only",
            "approval_policy": "never",
            "ignore_user_config": True,
            "ephemeral": True,
        },
    )
    client = BrokerJsonClient(
        actor="source_episode_interpreter",
        lane_id="kamandal_evaluation",
        timeout_seconds=600,
        binding=binding,
    )
    packets = _build_packets(cases)
    episodes: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    compiler_failures: list[dict[str, str]] = []
    for source_id, packet in packets.items():
        profile = yaml.safe_load(PROFILES[source_id].read_text(encoding="utf-8"))
        try:
            compilation = compile_source_episode_packet(packet, profile, client)
        except Exception as exc:  # noqa: BLE001 - a failed model run is evaluation evidence.
            compiler_failures.append(
                {
                    "source_id": source_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            )
            continue
        else:
            episodes.extend(compilation.episodes)
            receipts.extend(compilation.model_receipts)
    score = _score(cases, episodes)
    score["compiler_failure_count"] = len(compiler_failures)
    score["hard_gate_pass"] = bool(score["hard_gate_pass"] and not compiler_failures)
    return {
        "model": model,
        "repetition": repetition,
        "reasoning_effort": reasoning_effort,
        "score": score,
        "compiler_failures": compiler_failures,
        "receipts": receipts,
        "episodes": episodes,
        "effects": {key: False for key in sorted(EFFECT_KEYS)},
    }


def _aggregate(runs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[str(run["model"])].append(run)
    result: dict[str, Any] = {}
    for model, model_runs in grouped.items():
        scores = [run["score"] for run in model_runs]
        result[model] = {
            "runs": len(scores),
            "hard_gate_passes": sum(bool(score["hard_gate_pass"]) for score in scores),
            "mean_event_recall": round(statistics.mean(score["event_recall"] for score in scores), 4),
            "mean_event_precision": round(
                statistics.mean(score["event_precision"] for score in scores), 4
            ),
            "mean_entry_recall": round(statistics.mean(score["entry_recall"] for score in scores), 4),
            "total_false_new_entries": sum(score["false_new_entry_count"] for score in scores),
            "total_invented_media_packages": sum(
                score["invented_media_package_count"] for score in scores
            ),
            "total_compiler_failures": sum(score["compiler_failure_count"] for score in scores),
        }
    return result


def _markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Source Episode Model Evaluation",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "All runs were effect-free: no Sheet, planner, shadow/live, broker, order, or send effects.",
        "",
        "| Model | Hard gates | Event recall | Event precision | Entry recall | False entries | Invented media packages | Compiler failures |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model, summary in payload["aggregate"].items():
        lines.append(
            f"| {model} | {summary['hard_gate_passes']}/{summary['runs']} | "
            f"{summary['mean_event_recall']:.1%} | {summary['mean_event_precision']:.1%} | "
            f"{summary['mean_entry_recall']:.1%} | {summary['total_false_new_entries']} | "
            f"{summary['total_invented_media_packages']} | {summary['total_compiler_failures']} |"
        )
    lines.extend(["", "## Runs", ""])
    for run in payload["runs"]:
        score = run["score"]
        lines.extend(
            [
                f"### {run['model']} / repetition {run['repetition']}",
                "",
                f"- Hard gate: {'PASS' if score['hard_gate_pass'] else 'FAIL'}",
                f"- Events: {score['matched_event_count']}/{score['expected_event_count']} expected matched; "
                f"{score['actual_event_count']} emitted",
                f"- Planner entries: {score['matched_entry_count']}/{score['expected_entry_count']} expected matched; "
                f"{score['false_new_entry_count']} false new entries",
                f"- Invented media packages: {score['invented_media_package_count']}",
                f"- Compiler failures: {score['compiler_failure_count']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = _args()
    if args.repetitions < 1 or args.repetitions > 5:
        raise ValueError("repetitions must be between 1 and 5")
    models = [item.strip() for item in args.models.split(",") if item.strip()]
    if not models:
        raise ValueError("at least one model is required")
    cases = _load_cases()
    runs: list[dict[str, Any]] = []
    with _preserve_explicit_binding():
        for model in models:
            for repetition in range(1, args.repetitions + 1):
                runs.append(
                    _run_model(
                        model,
                        repetition,
                        cases,
                        reasoning_effort=args.reasoning_effort,
                    )
                )
    payload = {
        "schema": "kamandal.source_episode_model_evaluation.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus_path": str(CORPUS),
        "corpus_case_count": len(cases),
        "routing_path": str(ROUTING),
        "aggregate": _aggregate(runs),
        "runs": runs,
        "effects": {key: False for key in sorted(EFFECT_KEYS)},
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "latest.json"
    markdown_path = args.output_dir / "latest.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(markdown_path), "aggregate": payload["aggregate"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
