"""Publish configured correspondent signals into Kamandal's active idea lane."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from kamandal_v2.intelligence.chart_seeds import SOURCE_SCHEMA as CHART_EVALUATION_SCHEMA
from kamandal_v2.intelligence.correspondent_signals import (
    import_correspondent_signals,
    load_correspondent_profile,
    validate_correspondent_packet,
)
from kamandal_v2.intelligence.llm_client import JsonLlmClient
from kamandal_v2.intelligence.market_questions import run_market_question_exchange
from kamandal_v2.paths import resolve_path
from kamandal_v2.stores.sqlite import LocalStore


ACTIVATION_SCHEMA = "kamandal.correspondent_activation.v1"
CommandRunner = Callable[[list[str], Path], str]


@dataclass(frozen=True, slots=True)
class CorrespondentActivationResult:
    status: str
    activated_at: str
    profile_count: int
    record_count: int
    planner_idea_count: int
    active_idea_paths: tuple[Path, ...]
    receipt_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ACTIVATION_SCHEMA,
            "status": self.status,
            "activated_at": self.activated_at,
            "profile_count": self.profile_count,
            "record_count": self.record_count,
            "planner_idea_count": self.planner_idea_count,
            "active_idea_paths": [str(path) for path in self.active_idea_paths],
            "receipt_path": str(self.receipt_path),
            "effects": _effects(active_idea_publication=True),
        }


def activate_correspondent_sources(
    settings: dict[str, Any],
    *,
    universe_symbols: Iterable[str],
    command_runner: CommandRunner | None = None,
    market_command_runner: CommandRunner | None = None,
    store: LocalStore | None = None,
    intent_client: JsonLlmClient | None = None,
) -> CorrespondentActivationResult:
    """Translate configured Birdclaw correspondents and publish eligible ideas.

    Active files are replaced only after every enabled profile translates. If any
    profile fails, every configured profile is atomically replaced with an empty idea
    payload so an earlier signal cannot linger in the planner.
    """

    if settings.get("enabled") is not True:
        raise ValueError("correspondent activation is not enabled")
    if settings.get("mode") != "active_planner":
        raise ValueError("correspondent activation mode must be active_planner")

    profiles = _enabled_profiles(settings.get("profiles"))
    if not profiles:
        raise ValueError("correspondent activation requires at least one enabled profile")

    trial_root = resolve_path(settings.get("trial_root") or "~/Documents/birdclaw")
    birdclawctl = resolve_path(settings.get("birdclawctl") or trial_root / "birdclawctl")
    output_root = resolve_path(settings.get("output_dir") or "data/research/correspondent_signals")
    active_root = resolve_path(settings.get("active_ideas_dir") or "data/ideas/active")
    since_hours = max(1, int(settings.get("since_hours") or 336))
    limit = max(1, int(settings.get("limit") or 200))
    runner = command_runner or _run_command
    discovery_store = store or LocalStore()
    universe = {str(symbol).strip().upper() for symbol in universe_symbols if str(symbol).strip()}
    chart_evaluation_paths = _chart_evaluation_paths(settings)
    market_question_settings = settings.get("market_questions") or {}
    activated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    output_root.mkdir(parents=True, exist_ok=True)
    active_root.mkdir(parents=True, exist_ok=True)
    staged: list[dict[str, Any]] = []
    active_paths = tuple(active_root / f"correspondent_{profile['profile_id']}.yaml" for profile in profiles)

    try:
        if not birdclawctl.is_file():
            raise FileNotFoundError(f"Birdclaw CLI not found: {birdclawctl}")
        for profile in profiles:
            source_profile_id = profile["source_profile_id"]
            stdout = runner(
                [
                    str(birdclawctl),
                    "export",
                    "correspondent-signals",
                    "--profile",
                    source_profile_id,
                    "--since-hours",
                    str(since_hours),
                    "--limit",
                    str(limit),
                    "--json",
                ],
                trial_root,
            )
            packet = validate_correspondent_packet(json.loads(stdout))
            if packet["profile"]["profile_id"] != source_profile_id:
                raise ValueError(f"Birdclaw returned the wrong profile for {profile['profile_id']}")
            packet_text = json.dumps(packet, indent=2, sort_keys=True) + "\n"
            packet_sha = hashlib.sha256(packet_text.encode()).hexdigest()
            packet_path = output_root / "packets" / profile["profile_id"] / f"{packet_sha}.json"
            _atomic_write(packet_path, packet_text)

            profile_payload, _profile_text = load_correspondent_profile(profile["profile_path"])
            market_questions = run_market_question_exchange(
                packet,
                profile_payload,
                market_question_settings if isinstance(market_question_settings, dict) else {},
                command_runner=market_command_runner,
            )
            current_chart_paths = list(chart_evaluation_paths)
            if market_questions.response_path is not None:
                current_chart_paths.append(market_questions.response_path)

            imported = import_correspondent_signals(
                packet_path,
                profile_path=profile["profile_path"],
                universe_symbols=universe,
                chart_evaluation_paths=current_chart_paths,
                output_dir=output_root,
                store=discovery_store,
                intent_client=intent_client,
            )
            planner_text = imported.planner_ideas_path.read_text(encoding="utf-8")
            planner_payload = yaml.safe_load(planner_text) or {}
            ideas = planner_payload.get("ideas") or []
            if not isinstance(ideas, list) or len(ideas) != imported.planner_idea_count:
                raise ValueError(f"planner idea artifact is invalid for {profile['profile_id']}")
            staged.append(
                {
                    "profile_id": profile["profile_id"],
                    "source_profile_id": source_profile_id,
                    "batch_id": imported.batch_id,
                    "record_count": imported.record_count,
                    "planner_idea_count": imported.planner_idea_count,
                    "planner_text": planner_text,
                    "active_path": active_root / f"correspondent_{profile['profile_id']}.yaml",
                    "translation_path": str(imported.translation_path),
                    "source_acquisition": packet.get("acquisition") or {"status": "missing"},
                    "market_questions": market_questions.to_dict(),
                }
            )
    except Exception as exc:
        for profile, active_path in zip(profiles, active_paths, strict=True):
            _atomic_write(active_path, _empty_planner_payload(profile["profile_id"], status="failed_closed"))
        failure = {
            "schema": ACTIVATION_SCHEMA,
            "status": "failed_closed",
            "activated_at": activated_at,
            "profiles": [profile["profile_id"] for profile in profiles],
            "error_type": type(exc).__name__,
            "active_idea_paths": [str(path) for path in active_paths],
            "effects": _effects(active_idea_publication=True),
        }
        receipt_path = _write_receipt(output_root, failure)
        raise RuntimeError(f"correspondent activation failed closed; receipt={receipt_path}") from exc

    for item in staged:
        _atomic_write(item["active_path"], item["planner_text"])

    payload = {
        "schema": ACTIVATION_SCHEMA,
        "status": "succeeded",
        "activated_at": activated_at,
        "mode": "active_planner",
        "profiles": [
            {
                key: item[key]
                for key in (
                    "profile_id",
                    "source_profile_id",
                    "batch_id",
                    "record_count",
                    "planner_idea_count",
                    "active_path",
                    "translation_path",
                    "source_acquisition",
                    "market_questions",
                )
            }
            for item in staged
        ],
        "record_count": sum(int(item["record_count"]) for item in staged),
        "planner_idea_count": sum(int(item["planner_idea_count"]) for item in staged),
        "effects": _effects(active_idea_publication=True),
    }
    for profile in payload["profiles"]:
        profile["active_path"] = str(profile["active_path"])
    receipt_path = _write_receipt(output_root, payload)
    return CorrespondentActivationResult(
        status="succeeded",
        activated_at=activated_at,
        profile_count=len(staged),
        record_count=payload["record_count"],
        planner_idea_count=payload["planner_idea_count"],
        active_idea_paths=active_paths,
        receipt_path=receipt_path,
    )


def _enabled_profiles(raw_profiles: object) -> list[dict[str, Any]]:
    if not isinstance(raw_profiles, list):
        raise ValueError("correspondent profiles must be a list")
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_profiles:
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            continue
        profile_id = str(raw.get("profile_id") or "").strip()
        source_profile_id = str(raw.get("source_profile_id") or profile_id).strip()
        profile_path = str(raw.get("profile_path") or "").strip()
        if not profile_id or not source_profile_id or not profile_path:
            raise ValueError("enabled correspondent profile is incomplete")
        if profile_id in seen or not profile_id.replace("_", "").isalnum():
            raise ValueError(f"invalid or duplicate correspondent profile: {profile_id}")
        seen.add(profile_id)
        profiles.append(
            {
                "profile_id": profile_id,
                "source_profile_id": source_profile_id,
                "profile_path": resolve_path(profile_path),
            }
        )
    return profiles


def _run_command(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return completed.stdout


def _chart_evaluation_paths(settings: dict[str, Any]) -> list[Path]:
    chart_config = settings.get("chart_seeds") or {}
    if not isinstance(chart_config, dict):
        return []
    if chart_config.get("enabled") is not True:
        return []
    raw_dir = chart_config.get("evaluation_dir") or chart_config.get("output_dir") or "data/research/chart_seeds"
    evaluation_dir = resolve_path(raw_dir)
    if not evaluation_dir.is_dir():
        return []
    patterns = chart_config.get("patterns") or ["*.json", "**/*.json"]
    paths: list[Path] = []
    for pattern in patterns:  # type: ignore[arg-type]
        try:
            paths.extend(evaluation_dir.glob(str(pattern)))
        except Exception:
            continue
    # Also accept explicit list
    explicit = chart_config.get("evaluation_paths") or []
    for raw in explicit:  # type: ignore[arg-type]
        try:
            candidate = resolve_path(raw)
            if candidate.is_file():
                paths.append(candidate)
        except Exception:
            continue
    # Discovery is schema-based, not filename-based. The output tree also contains
    # seed requests and import receipts, and neither is an evaluation.
    seen: set[str] = set()
    unique: list[Path] = []
    for path in sorted(paths):
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("schema") == CHART_EVALUATION_SCHEMA:
            unique.append(path)
    return unique


def _empty_planner_payload(profile_id: str, *, status: str) -> str:
    return yaml.safe_dump(
        {
            "schema": "kamandal.correspondent_planner_ideas.v1",
            "profile_id": profile_id,
            "activation_status": status,
            "ideas": [],
        },
        sort_keys=False,
    )


def _write_receipt(output_root: Path, payload: dict[str, Any]) -> Path:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    run_id = hashlib.sha256(text.encode()).hexdigest()[:16]
    run_path = output_root / "activation" / "runs" / f"{run_id}.json"
    latest_path = output_root / "activation" / "latest.json"
    _atomic_write(run_path, text)
    _atomic_write(latest_path, text)
    return latest_path


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _effects(*, active_idea_publication: bool) -> dict[str, bool]:
    return {
        "active_idea_publication": active_idea_publication,
        "planner_run": False,
        "shadow_admission": False,
        "live_admission": False,
        "sheet_write": False,
        "broker": False,
        "orders": False,
        "external_send": False,
    }
