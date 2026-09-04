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
    load_correspondent_profile,
    validate_correspondent_packet,
)
from kamandal_v2.intelligence.llm_client import JsonLlmClient
from kamandal_v2.intelligence.market_questions import run_market_question_exchange
from kamandal_v2.intelligence.observed_packages import ObservedPackageBatch
from kamandal_v2.intelligence.source_episode_compiler import (
    compile_source_episode_packet,
    load_episode_history,
    write_episode_compilation,
)
from kamandal_v2.intelligence.source_episode_projection import (
    SourceEpisodeProjection,
    project_source_episode_compilation,
)
from kamandal_v2.intelligence.trade_sources import (
    TradeSourceMode,
    TradeSourceOutputKind,
    TradeSourcePolicy,
    compile_trade_source_policies,
)
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
    observed_package_feed_path: Path | None = None
    observed_package_batch_count: int = 0
    source_failure_count: int = 0

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
            "observed_package_feed_path": str(self.observed_package_feed_path) if self.observed_package_feed_path else None,
            "observed_package_batch_count": self.observed_package_batch_count,
            "source_failure_count": self.source_failure_count,
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
    observed_package_client: JsonLlmClient | None = None,
    source_episode_client: JsonLlmClient | None = None,
    trade_source_rows: Iterable[dict[str, Any]] | None = None,
) -> CorrespondentActivationResult:
    """Translate configured Birdclaw correspondents and publish eligible ideas.

    Each source replaces only its own active output. A source failure clears that
    source's stale idea file while successful siblings remain available.
    """

    if settings.get("enabled") is not True:
        raise ValueError("correspondent activation is not enabled")
    if settings.get("mode") != "active_planner":
        raise ValueError("correspondent activation mode must be active_planner")

    profiles = _enabled_profiles(settings.get("profiles"))
    if not profiles:
        raise ValueError("correspondent activation requires at least one enabled profile")
    source_policy_by_key = _source_policy_map(profiles, trade_source_rows)

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
    episode_root = output_root / "source_episodes"
    market_question_settings = settings.get("market_questions") or {}
    activated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    output_root.mkdir(parents=True, exist_ok=True)
    active_root.mkdir(parents=True, exist_ok=True)
    staged: list[dict[str, Any]] = []
    observed_batches: list[ObservedPackageBatch] = []
    observed_failures: list[dict[str, str]] = []
    source_failures: list[dict[str, str]] = []
    active_paths = tuple(active_root / f"correspondent_{profile['profile_id']}.yaml" for profile in profiles)

    if not birdclawctl.is_file():
        for profile, active_path in zip(profiles, active_paths, strict=True):
            _atomic_write(active_path, _empty_planner_payload(profile["profile_id"], status="failed_closed"))
        failure = {
            "schema": ACTIVATION_SCHEMA,
            "status": "failed_closed",
            "activated_at": activated_at,
            "profiles": [profile["profile_id"] for profile in profiles],
            "error_type": "FileNotFoundError",
            "active_idea_paths": [str(path) for path in active_paths],
            "effects": _effects(active_idea_publication=True),
        }
        receipt_path = _write_receipt(output_root, failure)
        raise RuntimeError(f"correspondent activation failed closed; receipt={receipt_path}")

    for profile in profiles:
        active_path = active_root / f"correspondent_{profile['profile_id']}.yaml"
        try:
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

            idea_policy = source_policy_by_key.get((profile["profile_id"], TradeSourceOutputKind.IDEA))
            exact_policy = source_policy_by_key.get((profile["profile_id"], TradeSourceOutputKind.EXACT_PACKAGE))
            planner_text = _empty_planner_payload(profile["profile_id"], status="source_idea_off")
            planner_idea_count = 0
            normalized_idea_count = 0
            translation_path = ""
            market_questions_payload: dict[str, Any] = {"status": "not_applicable", "reason": "source_idea_off"}
            batch_id = packet_sha
            profile_batches: list[ObservedPackageBatch] = []
            profile_observed_failures: list[dict[str, str]] = []
            inference_enabled = bool(
                (idea_policy is not None and idea_policy.inference_enabled)
                or (exact_policy is not None and exact_policy.inference_enabled)
            )
            if inference_enabled:
                if not profile["profile_path"]:
                    raise ValueError(f"{profile['profile_id']}: source inference requires profile_path")
                profile_payload, _profile_text = load_correspondent_profile(profile["profile_path"])
                current_chart_paths = list(chart_evaluation_paths)
                if idea_policy is not None and idea_policy.inference_enabled:
                    market_questions = run_market_question_exchange(
                        packet,
                        profile_payload,
                        market_question_settings if isinstance(market_question_settings, dict) else {},
                        command_runner=market_command_runner,
                    )
                    if market_questions.response_path is not None:
                        current_chart_paths.append(market_questions.response_path)
                    market_questions_payload = market_questions.to_dict()
                compiler_client = source_episode_client or intent_client or observed_package_client
                compilation = compile_source_episode_packet(
                    packet,
                    profile_payload,
                    compiler_client,
                    # Load enough persisted episodes for idempotent reuse; the
                    # compiler independently bounds what the model can see.
                    history=load_episode_history(episode_root, profile["profile_id"], limit=500),
                )
                compilation_path = write_episode_compilation(compilation, episode_root)
                projected = project_source_episode_compilation(
                    compilation,
                    packet,
                    profile_payload,
                    universe_symbols=universe,
                    chart_evaluation_paths=current_chart_paths,
                )
                translation_path = str(compilation_path)
                batch_id = compilation_path.stem
                normalized_idea_count = len(projected.planner_ideas)
                if idea_policy is not None and idea_policy.inference_enabled:
                    _record_episode_outputs(
                        discovery_store,
                        projected,
                        source_id=profile["profile_id"],
                        idea_mode=idea_policy.mode,
                        exact_mode=exact_policy.mode if exact_policy is not None else TradeSourceMode.OFF,
                        acquisition=packet.get("acquisition") or {"status": "missing"},
                    )
                    if idea_policy.planner_enabled:
                        planner_text = _planner_payload(profile["profile_id"], projected.planner_ideas)
                        planner_idea_count = len(projected.planner_ideas)
                    else:
                        planner_text = _empty_planner_payload(profile["profile_id"], status="observed_only")
                if exact_policy is not None and exact_policy.inference_enabled:
                    profile_batches = list(projected.observed_batches)
                    profile_observed_failures = list(projected.failures)
                elif idea_policy is None or not idea_policy.inference_enabled:
                    _record_episode_outputs(
                        discovery_store,
                        projected,
                        source_id=profile["profile_id"],
                        idea_mode=TradeSourceMode.OFF,
                        exact_mode=exact_policy.mode if exact_policy is not None else TradeSourceMode.OFF,
                        acquisition=packet.get("acquisition") or {"status": "missing"},
                    )

            _record_exact_outputs(
                discovery_store,
                observed_batches=profile_batches,
                failures=profile_observed_failures,
                source_id=profile["profile_id"],
                source_mode=exact_policy.mode if exact_policy is not None else TradeSourceMode.OFF,
                acquisition=packet.get("acquisition") or {"status": "missing"},
            )
            observed_batches.extend(profile_batches)
            observed_failures.extend(profile_observed_failures)
            staged.append(
                {
                    "profile_id": profile["profile_id"],
                    "source_profile_id": source_profile_id,
                    "batch_id": batch_id,
                    "record_count": len(packet.get("records") or []),
                    "normalized_idea_count": normalized_idea_count,
                    "planner_idea_count": planner_idea_count,
                    "planner_text": planner_text,
                    "active_path": active_path,
                    "translation_path": translation_path,
                    "source_acquisition": packet.get("acquisition") or {"status": "missing"},
                    "market_questions": market_questions_payload,
                    "idea_mode": idea_policy.mode.value if idea_policy is not None else "off",
                    "exact_package_mode": exact_policy.mode.value if exact_policy is not None else "off",
                }
            )
        except Exception as exc:  # noqa: BLE001 - one source must not erase a healthy sibling.
            _atomic_write(active_path, _empty_planner_payload(profile["profile_id"], status="failed_closed"))
            source_failures.append(
                {
                    "profile_id": profile["profile_id"],
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            discovery_store.event(
                "trade_source_activation_failed",
                {
                    "source_id": profile["profile_id"],
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "broker_effects": False,
                },
            )

    for item in staged:
        _atomic_write(item["active_path"], item["planner_text"])

    observed_feed_path = _write_observed_package_feed(
        output_root,
        activated_at=activated_at,
        batches=observed_batches,
        failures=observed_failures,
    )

    payload = {
        "schema": ACTIVATION_SCHEMA,
        "status": "degraded" if source_failures else "succeeded",
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
                    "normalized_idea_count",
                    "planner_idea_count",
                    "active_path",
                    "translation_path",
                    "source_acquisition",
                    "market_questions",
                    "idea_mode",
                    "exact_package_mode",
                )
            }
            for item in staged
        ],
        "record_count": sum(int(item["record_count"]) for item in staged),
        "planner_idea_count": sum(int(item["planner_idea_count"]) for item in staged),
        "observed_package_feed_path": str(observed_feed_path),
        "observed_package_batch_count": len(observed_batches),
        "observed_package_failure_count": len(observed_failures),
        "source_failures": source_failures,
        "effects": _effects(active_idea_publication=True),
    }
    for profile in payload["profiles"]:
        profile["active_path"] = str(profile["active_path"])
    receipt_path = _write_receipt(output_root, payload)
    return CorrespondentActivationResult(
        status=payload["status"],
        activated_at=activated_at,
        profile_count=len(profiles),
        record_count=payload["record_count"],
        planner_idea_count=payload["planner_idea_count"],
        active_idea_paths=active_paths,
        receipt_path=receipt_path,
        observed_package_feed_path=observed_feed_path,
        observed_package_batch_count=len(observed_batches),
        source_failure_count=len(source_failures),
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
        legacy_source_mode = str(raw.get("source_mode") or "idea").strip().lower()
        profile_path = str(raw.get("profile_path") or "").strip()
        if legacy_source_mode not in {"idea", "observed_package"}:
            raise ValueError(f"unsupported correspondent source mode: {legacy_source_mode}")
        if not profile_id or not source_profile_id:
            raise ValueError("enabled correspondent profile is incomplete")
        if profile_id in seen or not profile_id.replace("_", "").isalnum():
            raise ValueError(f"invalid or duplicate correspondent profile: {profile_id}")
        seen.add(profile_id)
        profiles.append(
            {
                "profile_id": profile_id,
                "source_profile_id": source_profile_id,
                "profile_path": resolve_path(profile_path) if profile_path else None,
                "legacy_source_mode": legacy_source_mode,
            }
        )
    return profiles


def _source_policy_map(
    profiles: list[dict[str, Any]],
    rows: Iterable[dict[str, Any]] | None,
) -> dict[tuple[str, TradeSourceOutputKind], TradeSourcePolicy]:
    if rows is not None:
        compilation = compile_trade_source_policies(
            rows,
            required_source_ids=(profile["profile_id"] for profile in profiles),
        )
        if not compilation.ok:
            raise ValueError("invalid trade_sources policy: " + "; ".join(compilation.errors))
        return compilation.by_key()

    # Compatibility only for tests and already-frozen pre-migration snapshots.
    # The deployed target always supplies the operator-owned Sheet rows.
    policies: dict[tuple[str, TradeSourceOutputKind], TradeSourcePolicy] = {}
    for profile in profiles:
        source_id = profile["profile_id"]
        legacy_exact = profile["legacy_source_mode"] == "observed_package"
        policies[(source_id, TradeSourceOutputKind.IDEA)] = TradeSourcePolicy(
            source_id,
            TradeSourceOutputKind.IDEA,
            TradeSourceMode.OFF if legacy_exact else TradeSourceMode.LIVE,
            "legacy compatibility",
        )
        policies[(source_id, TradeSourceOutputKind.EXACT_PACKAGE)] = TradeSourcePolicy(
            source_id,
            TradeSourceOutputKind.EXACT_PACKAGE,
            TradeSourceMode.SHADOW if legacy_exact else TradeSourceMode.OFF,
            "legacy compatibility",
        )
    return policies


def _record_episode_outputs(
    store: LocalStore,
    projection: SourceEpisodeProjection,
    *,
    source_id: str,
    idea_mode: TradeSourceMode,
    exact_mode: TradeSourceMode,
    acquisition: dict[str, Any],
) -> None:
    acquisition_status = str(acquisition.get("status") or "missing")
    idea_ids = {str(item.get("idea_id") or "") for item in projection.planner_ideas}
    for observed in projection.observations:
        classification = str(observed.get("classification") or "residual")
        is_idea = "idea" in classification.split(",")
        effective_mode = idea_mode if is_idea else exact_mode if "exact_package" in classification else TradeSourceMode.OFF
        opportunity_id = "corr_opp_" + hashlib.sha256(
            str(observed.get("opportunity_group_id") or "missing").encode()
        ).hexdigest()[:16]
        store.event(
            "trade_source_output_observed",
            {
                "observed_at": "",
                "source_id": source_id,
                "post_ref": str(observed.get("post_ref") or ""),
                "output_id": str(observed.get("event_id") or ""),
                "planner_idea_id": opportunity_id if opportunity_id in idea_ids else "",
                "opportunity_group_id": str(observed.get("opportunity_group_id") or ""),
                "acquisition_status": acquisition_status,
                "classification": classification,
                "normalized_output": observed.get("normalized_output") or {},
                "capability_support": "supported" if not observed.get("reason") else "review",
                "planner_disposition": (
                    "published"
                    if opportunity_id in idea_ids and idea_mode in {TradeSourceMode.SHADOW, TradeSourceMode.LIVE}
                    else "observed_only"
                    if opportunity_id in idea_ids
                    else "parked"
                ),
                "effective_mode": effective_mode.value,
                "reason": str(observed.get("reason") or ""),
                "action": str(observed.get("action") or ""),
                "symbol": str(observed.get("symbol") or ""),
                "structure": str(observed.get("structure") or ""),
                "evidence_status": str(observed.get("evidence_status") or ""),
                "link_state": str(observed.get("link_state") or ""),
                "broker_effects": False,
            },
        )
        if "outside_configured_universe" in str(observed.get("reason") or ""):
            symbol = str(observed.get("symbol") or "").strip().upper()
            if symbol:
                store.record_discovery_evidence(
                    symbol=symbol,
                    source_profile=source_id,
                    source_record_id=str(observed.get("event_id") or ""),
                    exclusion_reason="outside_enabled_universe",
                    evidence_ref=f"source_episode:{source_id}:{observed.get('event_id')}",
                    observed_at="",
                )


def _record_exact_outputs(
    store: LocalStore,
    *,
    observed_batches: Iterable[ObservedPackageBatch],
    failures: Iterable[dict[str, str]],
    source_id: str,
    source_mode: TradeSourceMode,
    acquisition: dict[str, Any],
) -> None:
    acquisition_status = str(acquisition.get("status") or "missing")
    for batch in observed_batches:
        for package in batch.packages:
            store.event(
                "trade_source_output_observed",
                {
                    "observed_at": "",
                    "source_id": source_id,
                    "post_ref": batch.canonical_post_id,
                    "output_id": package.evidence_revision_id,
                    "acquisition_status": acquisition_status,
                    "classification": "exact_package",
                    "normalized_output": package.to_dict(),
                    "capability_support": "pending_playbook_match",
                    "planner_disposition": "pending" if source_mode is TradeSourceMode.SHADOW else "observed_only",
                    "effective_mode": source_mode.value,
                    "reason": package.blocker or "",
                    "broker_effects": False,
                },
            )
    for failure in failures:
        store.event(
            "trade_source_output_observed",
            {
                "observed_at": "",
                "source_id": source_id,
                "post_ref": str(failure.get("source_id") or ""),
                "output_id": str(failure.get("source_id") or ""),
                "acquisition_status": acquisition_status,
                "classification": "residual",
                "normalized_output": failure,
                "capability_support": "unknown",
                "planner_disposition": "parked",
                "effective_mode": source_mode.value,
                "reason": str(failure.get("reason") or ""),
                "broker_effects": False,
            },
        )


def _write_observed_package_feed(
    output_root: Path,
    *,
    activated_at: str,
    batches: list[ObservedPackageBatch],
    failures: list[dict[str, str]],
) -> Path:
    batches_payload = [batch.to_dict() for batch in batches]
    canonical = json.dumps(batches_payload, sort_keys=True, separators=(",", ":"))
    payload = {
        "schema": "kamandal.observed_package_feed.v1",
        "generated_at": activated_at,
        "batches_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "batches": batches_payload,
        "failures": failures,
        "effects": _effects(active_idea_publication=False),
    }
    root = output_root / "observed_packages"
    immutable_path = root / "runs" / f"{payload['batches_sha256']}.json"
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _atomic_write(immutable_path, text)
    latest = root / "latest.json"
    _atomic_write(latest, text)
    return latest


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


def _planner_payload(profile_id: str, ideas: Iterable[dict[str, Any]]) -> str:
    return yaml.safe_dump(
        {
            "schema": "kamandal.correspondent_planner_ideas.v1",
            "profile_id": profile_id,
            "activation_status": "active",
            "ideas": [dict(item) for item in ideas],
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
