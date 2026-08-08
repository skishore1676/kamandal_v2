"""Kamandal launchd and live-health status contract for Lathi."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import plistlib
import socket
from pathlib import Path
import sqlite3
from typing import Any

from kamandal_v2.config import load_control
from kamandal_v2.live.health import run_live_health
from kamandal_v2.ops.alerts import default_lathi_bus_profile
from kamandal_v2.ops.launchd_registry import CENTRAL, launchd_jobs
from kamandal_v2.paths import PROJECT_ROOT
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.tools.launchd_job import scheduled_job_health
from kamandal_v2.tools.review_queue import build_review_queue


SCHEMA = "kamandal.launchd.status.v1"


def build_status(
    *,
    repo_root: str | Path | None = None,
    db_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    repo = Path(repo_root).expanduser().resolve() if repo_root else PROJECT_ROOT
    store = LocalStore(db_path or "data/kamandal_v2.db")
    config = config if config is not None else load_control()
    checked_at = now or datetime.now(tz=UTC)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    checked_at = checked_at.astimezone(UTC)
    generated_at = checked_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    schedule_report = scheduled_job_health(repo_root=repo, now=checked_at.astimezone(CENTRAL))
    live_health = _safe_live_health(store, config, now=checked_at)
    review_queue = build_review_queue(store=store)
    shadow_evidence = _shadow_evidence_status(
        repo=repo,
        db_path=Path(store.sqlite_path),
        observed_at=checked_at,
    )
    units = [
        *[_job_unit(job, schedule_report) for job in launchd_jobs()],
        _live_health_unit(live_health),
        _review_queue_unit(review_queue),
    ]
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "host": socket.gethostname(),
        "repo_root": str(repo),
        "db_path": str(store.sqlite_path),
        "source_id": "kamandal",
        "units": units,
        "jobs": [unit for unit in units if unit["kind"] == "external_launchd_job"],
        "live_health": live_health,
        "shadow_evidence": shadow_evidence,
        "scheduled_job_health": schedule_report,
        "review_queue": {
            "schema": review_queue.get("schema"),
            "generated_at": review_queue.get("generated_at"),
            "counts": review_queue.get("counts"),
        },
        "transport": _transport_state(config),
        "counts": {
            "units": len(units),
            "jobs": len([unit for unit in units if unit["kind"] == "external_launchd_job"]),
            "scheduled_job_issues": len(schedule_report.get("issues") or []),
            "review_requests_active": int(((review_queue.get("counts") or {}).get("active")) or 0),
            "live_health_reasons": len(live_health.get("reasons") or []),
        },
    }


def _shadow_evidence_status(
    *,
    repo: Path,
    db_path: Path,
    observed_at: datetime,
) -> dict[str, Any]:
    """Describe the shadow lane without reviving or mutating retired jobs."""
    from kamandal_v2.ops.launchd_registry import DISABLED_BY_DEFAULT

    registered_shadow_jobs = sorted(
        job.job
        for job in launchd_jobs()
        if "shadow" in job.job
    )
    plist_enabled = _csa_plist_enabled_jobs()
    active_shadow_jobs = [
        job for job in registered_shadow_jobs if job not in DISABLED_BY_DEFAULT or job in plist_enabled
    ]
    staged_shadow_jobs = [job for job in registered_shadow_jobs if job not in active_shadow_jobs]
    state = "active" if active_shadow_jobs else "staged_disabled" if staged_shadow_jobs else "retired"
    collector = {
        "state": state,
        "basis": (
            "active_shadow_jobs_in_launchd_registry"
            if active_shadow_jobs
            else "shadow_jobs_registered_disabled_by_default"
            if staged_shadow_jobs
            else "no_shadow_job_in_active_launchd_registry"
        ),
        "active_jobs": active_shadow_jobs,
        "staged_disabled_jobs": staged_shadow_jobs,
        "retired_legacy_jobs": ["market-shadow", "shadow-eod-report"],
    }
    history: dict[str, Any] = {
        "status_counts": {},
        "total_fills": 0,
        "open_fills": 0,
        "last_fill_activity_at": None,
        "last_mark_at": None,
        "last_eod_at": None,
    }
    findings: list[str] = []
    try:
        with _read_only_connection(db_path) as conn:
            if _table_exists(conn, "shadow_fills"):
                history["status_counts"] = {
                    str(status): int(count)
                    for status, count in conn.execute(
                        "SELECT COALESCE(status, 'unknown'), COUNT(*) "
                        "FROM shadow_fills GROUP BY status ORDER BY status"
                    ).fetchall()
                }
                history["total_fills"] = sum(history["status_counts"].values())
                history["open_fills"] = int(history["status_counts"].get("open", 0))
                raw_fill_time = conn.execute(
                    "SELECT MAX(COALESCE(closed_at, opened_at)) FROM shadow_fills"
                ).fetchone()[0]
                history["last_fill_activity_at"] = _sqlite_timestamp(raw_fill_time)
            if _table_exists(conn, "shadow_marks"):
                raw_mark_time = conn.execute(
                    "SELECT MAX(marked_at) FROM shadow_marks"
                ).fetchone()[0]
                history["last_mark_at"] = _sqlite_timestamp(raw_mark_time)
    except (OSError, sqlite3.Error) as exc:
        findings.append(f"shadow_history_unreadable:{type(exc).__name__}")

    eod_root = repo / "data" / "reports" / "eod"
    eod_files = sorted(
        (path for path in eod_root.glob("*_shadow_eod.json") if path.is_file()),
        key=lambda path: (path.stat().st_mtime, path.name),
    ) if eod_root.is_dir() else []
    if eod_files:
        history["last_eod_at"] = datetime.fromtimestamp(
            eod_files[-1].stat().st_mtime,
            tz=UTC,
        ).isoformat()

    has_history = bool(
        history["total_fills"]
        or history["last_mark_at"]
        or history["last_eod_at"]
    )
    evidence_state = (
        "collecting"
        if collector["state"] == "active"
        else "historical_only"
        if has_history
        else "empty"
    )
    if collector["state"] == "retired":
        findings.append("shadow_collection_retired")
    if collector["state"] == "staged_disabled":
        findings.append("shadow_collection_staged_disabled")
    if evidence_state == "historical_only":
        findings.append("historical_shadow_evidence_only")
    if collector["state"] != "active" and history["open_fills"]:
        findings.append("legacy_open_shadow_fills_unmanaged")

    collector_hash = _semantic_hash(collector)
    history_hash = _semantic_hash(history)
    semantic = {
        "collector": collector,
        "history": history,
        "evidence_state": evidence_state,
        "alpha_eligible": False,
        "findings": findings,
    }
    return {
        "schema": "kamandal.shadow_evidence_status.v1",
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        **semantic,
        "collector_hash": collector_hash,
        "history_hash": history_hash,
        "semantic_hash": _semantic_hash(semantic),
        "protected_effects": {
            "database_write": False,
            "broker_call": False,
            "order_submit": False,
            "schedule_change": False,
        },
    }


def _csa_plist_enabled_jobs() -> set[str]:
    from kamandal_v2.ops.launchd_registry import DISABLED_BY_DEFAULT, JOB_LABEL_SUFFIXES

    enabled: set[str] = set()
    root = Path.home() / "Library" / "LaunchAgents"
    for job in DISABLED_BY_DEFAULT:
        path = root / f"com.kamandal.v2.{JOB_LABEL_SUFFIXES[job]}.plist"
        if not path.exists():
            continue
        try:
            payload = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            continue
        if payload.get("Disabled") is False:
            enabled.add(job)
    return enabled


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.expanduser().resolve()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=8)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _sqlite_timestamp(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().replace(" ", "T").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _semantic_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _job_unit(job: Any, schedule_report: dict[str, Any]) -> dict[str, Any]:
    rows = {str(row.get("job")): row for row in schedule_report.get("jobs") or []}
    row = rows.get(job.job) or {}
    last = row.get("last") or {}
    issue = row.get("issue")
    findings = []
    if issue:
        findings.append(str(issue.get("reason") or "scheduled_job_issue"))
        if issue.get("detail"):
            findings.append(str(issue["detail"]))
    delivery_status = str(last.get("delivery_status") or "")
    if delivery_status == "failed":
        findings.append("alert_delivery_failed")
    lifecycle = "stuck" if issue else "armed"
    last_status = str(last.get("status") or "")
    if last_status.lower() == "failed":
        lifecycle = "stuck"
    return {
        "unit_id": job.label,
        "job": job.job,
        "kind": "external_launchd_job",
        "serves_job": "C",
        "declared_enabled": True,
        "effective_enabled": bool(last.get("installed_at") or last.get("log_path")),
        "risk_class": job.risk_class,
        "lifecycle": lifecycle,
        "schedule": job.schedule_label,
        "next_fire": None,
        "last_run_status": last_status or None,
        "last_run_at": last.get("mtime"),
        "observed_at": schedule_report.get("checked_at"),
        "findings": findings,
        "operator_state": "self_healing" if delivery_status == "failed" and lifecycle == "armed" else None,
        "available_actions": list(job.available_actions),
        "action_requirements": job.action_requirements or {},
        "source_id": "kamandal",
        "readiness_role": job.purpose,
    }


def _live_health_unit(live_health: dict[str, Any]) -> dict[str, Any]:
    status = str(live_health.get("overall") or "NO_DATA").upper()
    lifecycle = _live_health_lifecycle(live_health)
    events = list(live_health.get("events") or [])
    return {
        "unit_id": "kamandal:live-health",
        "kind": "external_health",
        "serves_job": "C",
        "declared_enabled": True,
        "effective_enabled": True,
        "risk_class": "trading_health",
        "lifecycle": lifecycle,
        "schedule": None,
        "next_fire": None,
        "last_run_status": status.lower(),
        "last_run_at": live_health.get("checked_at"),
        "observed_at": live_health.get("checked_at"),
        "findings": [str(reason) for reason in live_health.get("reasons") or []],
        "finding_details": events,
        "self_healing": live_health.get("self_healing") or {},
        "operator_state": _operator_state(events, lifecycle),
        "available_actions": ["live-status", "live-health-report-now"],
        "action_requirements": {
            "live-status": {"requires_confirmation": False, "reason": "Read-only live-health status."},
            "live-health-report-now": {
                "requires_confirmation": False,
                "reason": "Read-only live-health report; alert sending is controlled by alert mode.",
            },
        },
        "source_id": "kamandal",
        "readiness_role": "Kamandal live book health.",
    }


def _live_health_lifecycle(live_health: dict[str, Any]) -> str:
    status = str(live_health.get("overall") or "NO_DATA").upper()
    events = list(live_health.get("events") or [])
    states = {str(event.get("operator_state") or "") for event in events}
    states.discard("")
    if states and states <= {"self_handled"}:
        return "idle"
    if states and states <= {"self_healing", "self_handled"}:
        return "running"
    if status == "GREEN":
        return "idle"
    if status == "RED":
        return "stuck"
    return "waiting_you"


def _operator_state(events: list[dict[str, Any]], lifecycle: str) -> str:
    states = {str(event.get("operator_state") or "") for event in events}
    states.discard("")
    if lifecycle == "idle":
        if states <= {"self_handled"} and "self_handled" in states:
            return "self_handled"
        return "clear"
    if lifecycle == "stuck":
        return "operator_needed"
    if not states:
        return "operator_needed"
    if states <= {"self_healing", "self_handled"}:
        return "self_healing" if "self_healing" in states else "self_handled"
    if "blocked_self_healing" in states:
        return "blocked_self_healing"
    return "operator_needed"


def _review_queue_unit(review_queue: dict[str, Any]) -> dict[str, Any]:
    active = int(((review_queue.get("counts") or {}).get("active")) or 0)
    return {
        "unit_id": "kamandal:review-queue",
        "kind": "external_review_queue",
        "serves_job": "C",
        "declared_enabled": True,
        "effective_enabled": True,
        "risk_class": "trading_review",
        "lifecycle": "waiting_you" if active else "idle",
        "schedule": None,
        "next_fire": None,
        "last_run_status": "waiting_you" if active else "empty",
        "last_run_at": review_queue.get("generated_at"),
        "observed_at": review_queue.get("generated_at"),
        "findings": [f"{active} active review request(s)"] if active else [],
        "available_actions": ["send-pending-review-requests"] if active else [],
        "action_requirements": {
            "send-pending-review-requests": {
                "requires_confirmation": True,
                "reason": "This sends bounded Kamandal review cards to the operator surface.",
            }
        },
        "source_id": "kamandal",
        "readiness_role": "Kamandal operator review queue.",
    }


def _safe_live_health(
    store: LocalStore,
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        return run_live_health(store, config, now=now)
    except Exception as exc:  # noqa: BLE001 - status must degrade instead of crashing Control Tower.
        return {
            "checked_at": _now(),
            "overall": "NO_DATA",
            "counts": {},
            "reasons": ["live_health_failed"],
            "events": [{"severity": "red", "reason": "live_health_failed", "detail": str(exc)}],
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }


def _transport_state(config: dict[str, Any]) -> dict[str, Any]:
    review = ((config.get("live") or {}).get("operator_review") or {})
    return {
        "lathi_bus_profile": str(review.get("lathi_profile") or default_lathi_bus_profile()),
        "operator_review_transport": str(review.get("transport") or "lathi_bus"),
        "live_send_mode": str(review.get("lathi_mode") or "configured_by_runtime"),
        "status": "configured",
    }


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--repo-root", default="", help="Optional repo root override")
    parser.add_argument("--db", default="", help="Optional SQLite path; defaults to Kamandal data DB")
    args = parser.parse_args(argv)

    payload = build_status(
        repo_root=args.repo_root or None,
        db_path=args.db or None,
    )
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
