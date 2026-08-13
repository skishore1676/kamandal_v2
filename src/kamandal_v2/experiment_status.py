"""Read-only projection of Kamandal's existing CSA evidence.

This module deliberately sits beside the existing scorecard and economics
builders.  It does not author experiments, read or write Google Sheets, run a
shadow cycle, submit orders, change a stage, or call TradeLab.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from kamandal_v2.paths import resolve_path

STATUS_SCHEMA = "tradelab.app_experiment_status.v1"
EVIDENCE_SCHEMA = "kamandal.strategy_experiment_evidence.v1"
ECONOMIC_SCHEMA = "kamandal.strategy_weekly_economics.v1"
SOURCE_STATUSES = frozenset({"ok", "partial", "stale", "unavailable"})
EFFECT_KEYS = ("sheet_write", "stage_change", "broker_action", "order_action")
ACTIVE_EXPERIMENT_STAGES = frozenset({"shadow", "pilot_live", "live"})
CENTRAL = ZoneInfo("America/Chicago")


class ExperimentStatusError(ValueError):
    """Raised when a locally constructed status packet is unsafe."""


def build_experiment_status_from_paths(
    *,
    database: str | Path,
    report_dir: str | Path,
    through: date | str | None = None,
) -> dict[str, Any]:
    """Read existing reports or the SQLite store and return one status packet.

    File reads are preferred because they preserve the same durable products
    already consumed by TradeLab.  The SQLite fallback is opened read-only and
    uses the existing app-owned aggregators; it never creates tables or files.
    """

    as_of = _as_date(through)
    database_path = resolve_path(database)
    report_path = resolve_path(report_dir)
    scorecards, scorecard_problems, scorecard_sources = _load_scorecards(report_path, as_of)
    economics, economics_problem, economics_source = _load_economics(report_path, as_of)
    current_stages, policy_source = _load_current_policy(report_path, as_of)

    source_status = "ok"
    source_limitations: list[str] = []
    if scorecard_problems:
        source_status = "partial"
        source_limitations.extend(scorecard_problems)

    if not scorecards:
        scorecard = _read_scorecard(database_path, as_of)
        if scorecard is None:
            source_status = "unavailable"
            source_limitations.append("source_unavailable")
        else:
            scorecards = [scorecard]
            scorecard_sources.append({"kind": "sqlite", "path": str(database_path)})

    if economics is None:
        economics = _read_economics(database_path, as_of)
        if economics is not None:
            economics_source = {"kind": "sqlite", "path": str(database_path)}
        else:
            if source_status == "ok":
                source_status = "partial"
            source_limitations.append(economics_problem or "missing_evidence")

    # The policy snapshot is the app-owned read-only record of the current
    # Sheet composition. If it is unavailable, limit the fallback to the most
    # recent scorecard rather than turning historical baseline rows into live
    # experiment rows.
    if current_stages:
        effective_stages = current_stages
        if policy_source and policy_source.get("trading_date") != as_of.isoformat():
            if source_status == "ok":
                source_status = "stale"
            source_limitations.append("stale_source")
    else:
        effective_stages = _latest_scorecard_stages(scorecards)

    packet = build_experiment_status(
        scorecards,
        economics=economics,
        source_status=source_status,
        as_of=as_of,
        current_stages=effective_stages or None,
        source_limitations=source_limitations,
        provenance={
            "scorecards": scorecard_sources,
            "economics": economics_source,
            "policy": policy_source,
            "database": str(database_path),
            "report_dir": str(report_path),
        },
    )
    validate_experiment_status(packet)
    return packet


def build_experiment_status(
    scorecards: Iterable[Mapping[str, Any]],
    *,
    economics: Mapping[str, Any] | None,
    source_status: str = "ok",
    as_of: date | str | None = None,
    current_stages: Mapping[str, str] | None = None,
    source_limitations: Iterable[str] = (),
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Adapt existing Kamandal evidence into the shared TradeLab envelope."""

    if source_status not in SOURCE_STATUSES:
        raise ExperimentStatusError(f"invalid source_status: {source_status}")
    through = _as_date(as_of)
    cards = [
        dict(card)
        for card in scorecards
        if card.get("schema") == EVIDENCE_SCHEMA
        and str(card.get("trading_date") or "") <= through.isoformat()
    ]
    cards.sort(key=lambda card: str(card.get("trading_date") or ""))
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for card in cards:
        for row in card.get("experiments") or []:
            experiment_id = str(row.get("experiment_id") or row.get("playbook_id") or "")
            if experiment_id:
                grouped[experiment_id].append((card, dict(row)))

    if current_stages is not None:
        grouped = {
            experiment_id: observations
            for experiment_id, observations in grouped.items()
            if current_stages.get(experiment_id) in ACTIVE_EXPERIMENT_STAGES
        }

    economic_rows = _economic_rows(economics)
    limitations = _unique_strings(source_limitations)
    experiments = [
        _build_experiment(
            experiment_id,
            observations,
            economic_rows,
            source_status,
            limitations,
            stage_override=(current_stages or {}).get(experiment_id),
        )
        for experiment_id, observations in sorted(grouped.items())
    ]
    packet = {
        "schema": STATUS_SCHEMA,
        "app": "kamandal",
        "as_of": through.isoformat(),
        "source_status": source_status,
        "source_schemas": [EVIDENCE_SCHEMA, ECONOMIC_SCHEMA],
        "experiments": experiments,
        "limitations": limitations,
        "provenance": dict(provenance or {}),
        "effects": {key: False for key in EFFECT_KEYS},
    }
    return packet


def validate_experiment_status(payload: Mapping[str, Any]) -> None:
    """Validate the projection without importing TradeLab into Kamandal."""

    if payload.get("schema") != STATUS_SCHEMA or payload.get("app") != "kamandal":
        raise ExperimentStatusError("invalid Kamandal status envelope")
    if payload.get("source_status") not in SOURCE_STATUSES:
        raise ExperimentStatusError("invalid status source_status")
    effects = payload.get("effects")
    if not isinstance(effects, Mapping) or any(effects.get(key) is not False for key in EFFECT_KEYS):
        raise ExperimentStatusError("status envelope claims an effect")
    if not isinstance(payload.get("experiments"), list):
        raise ExperimentStatusError("status envelope experiments must be a list")
    for experiment in payload["experiments"]:
        required = {
            "experiment_id",
            "stage",
            "configuration_identity",
            "observation_window",
            "observations",
            "opportunities",
            "entries",
            "closed",
            "metrics",
            "health",
            "limitations",
        }
        if not isinstance(experiment, Mapping) or not required <= set(experiment):
            raise ExperimentStatusError("status experiment is incomplete")
        for key in ("experiment_id", "stage", "configuration_identity"):
            if not isinstance(experiment[key], str) or not experiment[key].strip():
                raise ExperimentStatusError(f"invalid experiment {key}")
        window = experiment["observation_window"]
        if not isinstance(window, Mapping) or not all(isinstance(window.get(key), str) for key in ("start", "end")):
            raise ExperimentStatusError("invalid observation window")
        for key in ("observations", "opportunities", "entries", "closed"):
            value = experiment[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExperimentStatusError(f"invalid experiment {key}")
        if not isinstance(experiment["metrics"], Mapping):
            raise ExperimentStatusError("invalid experiment metrics")
        if experiment["health"] not in {"collecting", "inconclusive", "ready_for_review"}:
            raise ExperimentStatusError("invalid experiment health")
        if not isinstance(experiment["limitations"], list) or not all(
            isinstance(item, str) and item for item in experiment["limitations"]
        ):
            raise ExperimentStatusError("invalid experiment limitations")


def _build_experiment(
    experiment_id: str,
    observations: list[tuple[dict[str, Any], dict[str, Any]]],
    economic_rows: Mapping[tuple[str, str], Mapping[str, Any]],
    source_status: str,
    source_limitations: list[str],
    stage_override: str | None = None,
) -> dict[str, Any]:
    active = _current_stage_observations(observations, stage_override=stage_override)
    dates = [str(card.get("trading_date")) for card, _row in active if card.get("trading_date")]
    stage = stage_override or (str(active[-1][1].get("stage") or "shadow") if active else "shadow")
    policy_hashes = sorted(
        {
            str(policy_hash)
            for _card, row in active
            for policy_hash in (row.get("policy_hashes") or [])
            if policy_hash
        }
    )
    row_limitations = list(source_limitations)
    if source_status == "partial" and "partial_source" not in row_limitations:
        row_limitations.append("partial_source")
    if source_status in {"stale", "unavailable"}:
        row_limitations.append(f"{source_status}_source")
    if not policy_hashes:
        row_limitations.append("missing_configuration_identity")
    if len(policy_hashes) > 1:
        row_limitations.append("ambiguous_evidence")

    opportunities = sum(int(row.get("opportunities") or 0) for _card, row in active)
    entries = sum(_entry_count(row) for _card, row in active)
    closed, metrics, economic_limitations = _economic_facts(experiment_id, stage, economic_rows)
    row_limitations.extend(economic_limitations)
    for card, row in active:
        if card.get("run_errors") or str(card.get("evidence_status") or "") == "RED":
            row_limitations.append("data_quality_issue")
        if int(row.get("unexpected_broker_effects") or 0) > 0:
            row_limitations.append("unexpected_broker_effect")
        if card.get("zero_unexpected_broker_effect") is False:
            row_limitations.append("unexpected_broker_effect")

    row_limitations = _unique_strings(row_limitations)
    health = "inconclusive" if _has_incomplete_limitation(row_limitations) else (
        "collecting" if closed == 0 else "ready_for_review"
    )
    return {
        "experiment_id": experiment_id,
        "playbook_id": experiment_id,
        "stage": stage,
        "configuration_identity": _configuration_identity(policy_hashes),
        "observation_window": {
            "start": dates[0] if dates else "",
            "end": dates[-1] if dates else "",
        },
        "observations": len(active),
        "opportunities": opportunities,
        "entries": entries,
        "closed": closed,
        "metrics": metrics,
        "health": health,
        "limitations": row_limitations,
    }


def _current_stage_observations(
    observations: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    stage_override: str | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    active = [item for item in observations if int(item[0].get("runs") or 0) > 0]
    if not active:
        return []
    stage = stage_override or str(active[-1][1].get("stage") or "shadow")
    if stage_override:
        active = [
            item for item in active
            if str(item[1].get("stage") or "shadow") == stage_override
        ]
        if not active:
            return []
    current: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for card, row in reversed(active):
        if str(row.get("stage") or "shadow") != stage:
            break
        current.append((card, row))
    return list(reversed(current))


def _entry_count(row: Mapping[str, Any]) -> int:
    fills = row.get("fills") or {}
    live_intents = row.get("live_intents") or {}
    return int(fills.get("filled") or 0) + sum(
        int(live_intents.get(status) or 0)
        for status in ("filled", "filled_via_replacement", "manual_fill_recorded")
    )


def _economic_facts(
    experiment_id: str,
    stage: str,
    rows: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[int, dict[str, Any], list[str]]:
    row = rows.get((experiment_id, stage))
    if row is None:
        row = next((value for (candidate, _stage), value in rows.items() if candidate == experiment_id), None)
    if row is None:
        return 0, {}, ["missing_evidence"]
    limitations = list(row.get("quality_issues") or [])
    closed = int(row.get("closed_in_period") or 0)
    realized = _number(row.get("realized_pnl_usd"))
    closed_bpr = _number(row.get("closed_bpr_usd"))
    metrics: dict[str, Any] = {
        key: row.get(key)
        for key in (
            "realized_pnl_usd",
            "open_unrealized_pnl_usd",
            "total_pnl_usd",
            "closed_bpr_usd",
            "open_bpr_usd",
            "realized_return_on_bpr_pct",
            "total_return_on_bpr_pct",
            "win_rate_pct",
        )
        if row.get(key) is not None
    }
    if realized is not None and closed_bpr and closed_bpr > 0:
        metrics["closed_net_r"] = round(realized / closed_bpr, 6)
    if str(row.get("economic_status") or "") in {"invalid", "inconclusive"}:
        limitations.append("data_quality_issue")
    if not limitations and row.get("economic_status") in {"no_data", "unavailable"}:
        limitations.append("missing_evidence")
    return closed, metrics, _unique_strings(limitations)


def _configuration_identity(policy_hashes: list[str]) -> str:
    if len(policy_hashes) == 1:
        return policy_hashes[0]
    if policy_hashes:
        return "multiple_policy_hashes:" + ",".join(policy_hashes)
    return "unavailable"


def _economic_rows(packet: Mapping[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
    if not packet or packet.get("schema") != ECONOMIC_SCHEMA:
        return {}
    return {
        (str(row.get("playbook_id") or ""), str(row.get("stage") or "shadow")): dict(row)
        for row in packet.get("economic_rows") or []
        if row.get("playbook_id")
    }


def _load_scorecards(
    report_dir: Path,
    through: date,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    cards: list[dict[str, Any]] = []
    problems: list[str] = []
    sources: list[dict[str, Any]] = []
    for path in sorted(report_dir.glob("csa1_scorecard_????-??-??.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            problems.append("partial_source")
            continue
        if payload.get("schema") != EVIDENCE_SCHEMA:
            problems.append("partial_source")
            continue
        trading_date = str(payload.get("trading_date") or "")
        if not trading_date or trading_date > through.isoformat():
            continue
        cards.append(payload)
        sources.append({"kind": "scorecard", "path": str(path), "trading_date": trading_date})
    return cards, _unique_strings(problems), sources


def _load_current_policy(
    report_dir: Path,
    through: date,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    policy_dir = report_dir.parent.parent / "run" / "strategy_policy"
    candidates = sorted(policy_dir.glob("strategy_policy_????-??-??.json"))
    selected: tuple[Path, dict[str, Any]] | None = None
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        trading_date = str(payload.get("trading_date") or "")
        if trading_date and trading_date <= through.isoformat():
            selected = (path, payload)
    if selected is None:
        return {}, None
    path, payload = selected
    rows = ((payload.get("tables") or {}).get("playbooks") or [])
    stages = {
        str(row.get("playbook_id")): str(row.get("csa_stage") or "baseline").strip().lower()
        for row in rows
        if row.get("playbook_id")
    }
    return stages, {
        "kind": "policy_snapshot",
        "path": str(path),
        "trading_date": payload.get("trading_date"),
        "snapshot_hash": payload.get("snapshot_hash"),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _latest_scorecard_stages(
    scorecards: Iterable[Mapping[str, Any]],
) -> dict[str, str]:
    cards = [dict(card) for card in scorecards]
    if not cards:
        return {}
    latest_date = max(str(card.get("trading_date") or "") for card in cards)
    stages: dict[str, str] = {}
    for card in cards:
        if str(card.get("trading_date") or "") != latest_date:
            continue
        for row in card.get("experiments") or []:
            experiment_id = str(row.get("experiment_id") or row.get("playbook_id") or "")
            if experiment_id:
                stages[experiment_id] = str(row.get("stage") or "shadow")
    return stages


def _load_economics(
    report_dir: Path,
    through: date,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(report_dir.glob("csa1_weekly_economics_????-??-??.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("schema") != ECONOMIC_SCHEMA or str(payload.get("through") or "") > through.isoformat():
            continue
        if any(payload.get(key) is not False for key in ("recommendation_authority", "sheet_write_authority", "execution_authority", "alpha_claim_authority")):
            return None, "partial_source", None
        candidates.append((path, payload))
    if not candidates:
        return None, "missing_evidence", None
    path, payload = candidates[-1]
    return payload, None, {"kind": "economics", "path": str(path), "through": payload.get("through")}


def _read_scorecard(database: Path, through: date) -> dict[str, Any] | None:
    try:
        from kamandal_v2.strategy_lanes.reports import build_csa_scorecard

        report = build_csa_scorecard(database, trading_date=through)
    except (OSError, sqlite3.Error, ValueError):
        return None
    return report if report.get("schema_ready") else None


def _read_economics(database: Path, through: date) -> dict[str, Any] | None:
    try:
        from kamandal_v2.strategy_lanes.reports import build_csa_weekly_economics

        report = build_csa_weekly_economics(database, through_date=through)
    except (OSError, sqlite3.Error, ValueError):
        return None
    return report if report.get("schema") == ECONOMIC_SCHEMA else None


def _as_date(value: date | str | None) -> date:
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value))
    return datetime.now(CENTRAL).date()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unique_strings(values: Iterable[Any]) -> list[str]:
    return sorted({str(value) for value in values if value})


def _has_incomplete_limitation(values: Iterable[str]) -> bool:
    return any(
        value in {
            "partial_source",
            "stale_source",
            "unavailable_source",
            "source_unavailable",
            "missing_evidence",
            "ambiguous_evidence",
            "data_quality_issue",
            "unexpected_broker_effect",
        }
        for value in values
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kamandal experiment-status")
    parser.add_argument("--format", choices=("json",), default="json")
    parser.add_argument("--db", default="data/kamandal_v2.db")
    parser.add_argument("--report-dir", default="data/reports/csa1")
    parser.add_argument("--through", default="")
    args = parser.parse_args(argv)
    packet = build_experiment_status_from_paths(
        database=args.db,
        report_dir=args.report_dir,
        through=args.through or None,
    )
    print(json.dumps(packet, indent=2, sort_keys=True))
    return 0


__all__ = [
    "ECONOMIC_SCHEMA",
    "EVIDENCE_SCHEMA",
    "ExperimentStatusError",
    "STATUS_SCHEMA",
    "build_experiment_status",
    "build_experiment_status_from_paths",
    "main",
    "validate_experiment_status",
]
