"""Kamandal bounded control contract for Lathi Control Tower."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Iterator
import uuid

from kamandal_v2.config import load_control
from kamandal_v2.live.health import run_live_health
from kamandal_v2.live.operator_review import (
    OperatorReviewError,
    apply_operator_review_decision,
    send_pending_operator_review_requests,
)
from kamandal_v2.ops.alerts import AlertResult, default_lathi_bus_profile, send_lathi_alert
from kamandal_v2.ops.launchd_registry import launchd_job
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.tools.launchd_job import (
    health_attention,
    render_live_health_summary,
    scheduled_job_health,
)
from kamandal_v2.tools.review_queue import subject_fingerprint


SCHEMA = "kamandal.launchd.control_result.v1"
CONTROL_ACTIONS = {
    "live-status",
    "scheduled-job-health-now",
    "live-health-report-now",
    "retry-job",
    "send-pending-review-requests",
    "apply-review-decision",
}
RETRYABLE_JOBS = {"x-bookmarks", "youtube"}


def run_control_action(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    action_id = str(args.action_id or f"kamandal-{uuid.uuid4().hex[:16]}")
    store = LocalStore(args.db) if args.db else LocalStore()
    config = load_control()
    lock_key = _lock_key(args.command, getattr(args, "request_id", "") or "")
    try:
        with _control_lock(lock_key, action_id=action_id):
            if args.command == "live-status":
                result = _live_status(config, store, action_id)
            elif args.command == "scheduled-job-health-now":
                result = _scheduled_job_health_now(args, action_id)
            elif args.command == "live-health-report-now":
                result = _live_health_report_now(args, config, store, action_id)
            elif args.command == "retry-job":
                result = _retry_job(args, action_id)
            elif args.command == "send-pending-review-requests":
                result = _send_pending_review_requests(config, store, action_id)
            elif args.command == "apply-review-decision":
                result = _apply_review_decision(args, config, store, action_id)
            else:
                result = _base(args.command, action_id, ok=False, status="failed", error=f"unknown action: {args.command}")
    except ControlLockBusy as exc:
        result = _base(args.command, action_id, ok=False, status="lock_busy", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - contract should return structured failure.
        result = _base(
            args.command,
            action_id,
            ok=False,
            status="failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    status_code = 0 if result.get("ok") else 3 if result.get("status") == "lock_busy" else 2
    return status_code, result


def _live_status(config: dict[str, Any], store: LocalStore, action_id: str) -> dict[str, Any]:
    report = run_live_health(store, config)
    return _base(
        "live-status",
        action_id,
        ok=True,
        status="succeeded",
        result_status=str(report.get("overall") or "NO_DATA").lower(),
        payload=report,
    )


def _scheduled_job_health_now(args: argparse.Namespace, action_id: str) -> dict[str, Any]:
    repo_root = Path(args.repo_root).expanduser().resolve() if args.repo_root else Path.cwd()
    report = scheduled_job_health(repo_root=repo_root)
    issues = report.get("issues") or []
    return _base(
        "scheduled-job-health-now",
        action_id,
        ok=True,
        status="succeeded",
        result_status="degraded" if issues else "ok",
        payload=report,
    )


def _live_health_report_now(args: argparse.Namespace, config: dict[str, Any], store: LocalStore, action_id: str) -> dict[str, Any]:
    report = run_live_health(store, config)
    attention = health_attention(report)
    alert: AlertResult | None = None
    if attention.get("notify") and args.alert_mode != "off":
        alert = send_lathi_alert(
            title="Kamandal live health",
            body=render_live_health_summary(report),
            level=str(attention.get("level") or "warning"),
            mode=args.alert_mode,
            profile=args.alert_profile,
        )
    ok = True if alert is None else bool(alert.ok)
    return _base(
        "live-health-report-now",
        action_id,
        ok=ok,
        status="succeeded" if ok else "failed",
        result_status=str(report.get("overall") or "NO_DATA").lower(),
        payload={
            "health": report,
            "attention": attention,
            "alert": alert.to_dict() if alert else None,
        },
    )


def _retry_job(args: argparse.Namespace, action_id: str) -> dict[str, Any]:
    job = str(args.job or "")
    if job not in RETRYABLE_JOBS:
        return _base(
            "retry-job",
            action_id,
            ok=False,
            status="refused",
            result_status="job_not_retryable",
            error=f"retry-job supports only {sorted(RETRYABLE_JOBS)}",
            payload={"requested_job": job},
        )
    repo_root = Path(args.repo_root).expanduser().resolve() if args.repo_root else Path.cwd()
    trigger = _trigger_retry_job(
        job,
        action_id=action_id,
        repo_root=repo_root,
        alert_mode=str(args.alert_mode or "off"),
        alert_profile=str(args.alert_profile),
    )
    ok = bool(trigger.get("ok"))
    return _base(
        "retry-job",
        action_id,
        ok=ok,
        status="triggered" if ok else "failed",
        result_status=str(trigger.get("result_status") or ("trigger_accepted" if ok else "trigger_failed")),
        payload=trigger,
    )


def _trigger_retry_job(
    job: str,
    *,
    action_id: str,
    repo_root: Path,
    alert_mode: str,
    alert_profile: str,
) -> dict[str, Any]:
    registered = launchd_job(job)
    mode = os.getenv("KAMANDAL_CONTROL_RETRY_TRIGGER_MODE", "auto").strip().lower() or "auto"
    label = registered.label
    if mode in {"auto", "launchd"} and shutil.which("launchctl"):
        completed = _launchctl_kickstart(label)
        if completed.returncode == 0:
            return {
                "ok": True,
                "status": "triggered",
                "result_status": "launchd_triggered",
                "job": job,
                "label": label,
                "trigger_mode": "launchd",
                "action_id": action_id,
                "stdout_tail": _compact_tail(completed.stdout),
                "stderr_tail": _compact_tail(completed.stderr),
            }
        if mode == "launchd":
            return {
                "ok": False,
                "status": "failed",
                "result_status": "launchd_trigger_failed",
                "job": job,
                "label": label,
                "trigger_mode": "launchd",
                "action_id": action_id,
                "return_code": completed.returncode,
                "stdout_tail": _compact_tail(completed.stdout),
                "stderr_tail": _compact_tail(completed.stderr),
            }
    if mode in {"auto", "detached"}:
        return _trigger_detached_job(
            job,
            action_id=action_id,
            repo_root=repo_root,
            alert_mode=alert_mode,
            alert_profile=alert_profile,
            label=label,
        )
    return {
        "ok": False,
        "status": "failed",
        "result_status": "unsupported_trigger_mode",
        "job": job,
        "label": label,
        "trigger_mode": mode,
        "action_id": action_id,
        "error": f"unsupported retry trigger mode: {mode}",
    }


def _launchctl_kickstart(label: str) -> subprocess.CompletedProcess[str]:
    uid = os.getuid()
    return subprocess.run(  # noqa: S603
        ["launchctl", "kickstart", f"gui/{uid}/{label}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=float(os.getenv("KAMANDAL_CONTROL_TRIGGER_TIMEOUT_SECONDS", "10")),
    )


def _trigger_detached_job(
    job: str,
    *,
    action_id: str,
    repo_root: Path,
    alert_mode: str,
    alert_profile: str,
    label: str,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    command = [
        sys.executable,
        "-m",
        "kamandal_v2.tools.launchd_job",
        job,
        "--force",
        "--repo-root",
        str(repo_root),
        "--alert-mode",
        alert_mode,
        "--alert-profile",
        alert_profile,
    ]
    log_dir = repo_root / "data" / "logs" / "launchd" / "control_triggers"
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_action = re.sub(r"[^A-Za-z0-9_.-]+", "_", action_id)[:120]
    stdout_path = log_dir / f"{job}_{safe_action}.out.log"
    stderr_path = log_dir / f"{job}_{safe_action}.err.log"
    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=str(repo_root),
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()
    return {
        "ok": True,
        "status": "triggered",
        "result_status": "detached_triggered",
        "job": job,
        "label": label,
        "trigger_mode": "detached",
        "action_id": action_id,
        "pid": process.pid,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }


def _send_pending_review_requests(config: dict[str, Any], store: LocalStore, action_id: str) -> dict[str, Any]:
    result = send_pending_operator_review_requests(config, store=store)
    failed = [
        item
        for item in result.get("sent") or []
        if str(item.get("status") or "").lower() == "failed"
    ]
    return _base(
        "send-pending-review-requests",
        action_id,
        ok=not failed,
        status="succeeded" if not failed else "failed",
        result_status="sent" if result.get("sent") else "idle",
        payload=result,
    )


def _apply_review_decision(args: argparse.Namespace, config: dict[str, Any], store: LocalStore, action_id: str) -> dict[str, Any]:
    request = store.operator_review_request(args.request_id)
    if not request:
        return _base(
            "apply-review-decision",
            action_id,
            ok=False,
            status="refused",
            result_status="not_found",
            request_id=args.request_id,
            selected_action=args.review_action,
            error=f"operator review request not found: {args.request_id}",
        )
    current_fingerprint = subject_fingerprint(request)
    if args.subject_fingerprint and args.subject_fingerprint != current_fingerprint:
        return _base(
            "apply-review-decision",
            action_id,
            ok=False,
            status="refused",
            result_status="fingerprint_mismatch",
            request_id=args.request_id,
            selected_action=args.review_action,
            subject_id=str(request.get("subject_id") or ""),
            subject_fingerprint=current_fingerprint,
            error="subject_fingerprint did not match current Kamandal request",
        )
    try:
        result = apply_operator_review_decision(
            config,
            args.request_id,
            args.review_action,
            note=args.note,
            source=args.source,
            decided_by=args.decided_by,
            store=store,
        )
    except (OperatorReviewError, RuntimeError, ValueError) as exc:
        return _base(
            "apply-review-decision",
            action_id,
            ok=False,
            status="refused",
            result_status="validation_failed",
            request_id=args.request_id,
            selected_action=args.review_action,
            subject_id=str(request.get("subject_id") or ""),
            subject_fingerprint=current_fingerprint,
            error=str(exc),
            error_type=type(exc).__name__,
        )
    receipt = {
        "request_id": args.request_id,
        "action": args.review_action,
        "source": args.source,
        "decided_by": args.decided_by,
        "subject_id": str(request.get("subject_id") or ""),
        "subject_fingerprint": current_fingerprint,
        "apply_result": result,
    }
    store.event(
        "launchd_control_review_decision",
        {
            "action_id": action_id,
            **receipt,
        },
    )
    return _base(
        "apply-review-decision",
        action_id,
        ok=True,
        status="succeeded",
        result_status=str(result.get("request_status") or "applied"),
        request_id=args.request_id,
        selected_action=args.review_action,
        subject_id=str(request.get("subject_id") or ""),
        subject_fingerprint=current_fingerprint,
        payload=receipt,
        receipt_ref=f"kamandal:event:launchd_control_review_decision:{action_id}",
    )


def _compact_tail(text: str, *, max_lines: int = 20, max_chars: int = 2000) -> str:
    result = "\n".join(text.splitlines()[-max_lines:])
    if len(result) <= max_chars:
        return result
    marker = f"\n... [truncated {len(result) - max_chars} chars]"
    keep = max(max_chars - len(marker), 0)
    return result[:keep].rstrip() + marker


def _base(
    action: str,
    action_id: str,
    *,
    ok: bool,
    status: str,
    result_status: str | None = None,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
    error_type: str | None = None,
    request_id: str | None = None,
    selected_action: str | None = None,
    subject_id: str | None = None,
    subject_fingerprint: str | None = None,
    receipt_ref: str | None = None,
) -> dict[str, Any]:
    result = {
        "schema": SCHEMA,
        "generated_at": _now(),
        "ok": ok,
        "status": status,
        "result_status": result_status or status,
        "source_id": "kamandal",
        "action": action,
        "action_id": action_id,
    }
    if request_id:
        result["request_id"] = request_id
    if selected_action:
        result["selected_action"] = selected_action
    if subject_id:
        result["subject_id"] = subject_id
    if subject_fingerprint:
        result["subject_fingerprint"] = subject_fingerprint
    if receipt_ref:
        result["receipt_ref"] = receipt_ref
    if payload is not None:
        result["payload"] = payload
    if error:
        result["error"] = error
    if error_type:
        result["error_type"] = error_type
    return result


class ControlLockBusy(RuntimeError):
    pass


@contextmanager
def _control_lock(key: str, *, action_id: str) -> Iterator[None]:
    lock_dir = Path(os.getenv("KAMANDAL_CONTROL_LOCK_DIR", "data/logs/launchd/control_locks"))
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{key}.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ControlLockBusy(f"control action already in flight: {key}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"action_id": action_id, "created_at": _now()}, sort_keys=True))
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _lock_key(action: str, request_id: str = "") -> str:
    raw = f"{action}-{request_id}" if request_id else action
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)[:160]


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(CONTROL_ACTIONS))
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--action-id", default="")
    parser.add_argument("--db", default="", help="Optional SQLite path; defaults to Kamandal data DB")
    parser.add_argument("--repo-root", default="", help="Optional repo root for scheduled-job health")
    parser.add_argument("--alert-mode", choices=["off", "spool", "live"], default="off")
    parser.add_argument("--alert-profile", default=default_lathi_bus_profile())
    parser.add_argument("--job", default="")
    parser.add_argument("--request-id", default="")
    parser.add_argument("--action", dest="review_action", default="")
    parser.add_argument("--source", default="lathi")
    parser.add_argument("--decided-by", default="Lathi")
    parser.add_argument("--note", default="")
    parser.add_argument("--subject-fingerprint", default="")
    args = parser.parse_args(argv)

    if args.command == "apply-review-decision":
        if not args.request_id or not args.review_action:
            payload = _base(
                args.command,
                str(args.action_id or f"kamandal-{uuid.uuid4().hex[:16]}"),
                ok=False,
                status="failed",
                result_status="missing_arguments",
                error="apply-review-decision requires --request-id and --action",
            )
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return 2

    if args.command == "retry-job" and not args.job:
        payload = _base(
            args.command,
            str(args.action_id or f"kamandal-{uuid.uuid4().hex[:16]}"),
            ok=False,
            status="failed",
            result_status="missing_arguments",
            error="retry-job requires --job",
        )
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 2

    status_code, payload = run_control_action(args)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return status_code


if __name__ == "__main__":
    raise SystemExit(main())
