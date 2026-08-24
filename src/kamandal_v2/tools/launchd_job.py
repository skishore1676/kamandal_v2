"""Kamandal-owned launchd job runner."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from typing import Any

from kamandal_v2.config import load_control
from kamandal_v2.live.health import run_live_health
from kamandal_v2.ops.alerts import AlertResult, default_lathi_bus_profile, send_lathi_alert, tail
from kamandal_v2.ops.log_rotation import rotate_log_directories
from kamandal_v2.ops.market_calendar import MARKET_HOLIDAYS, is_non_trading_day
from kamandal_v2.ops.launchd_registry import (
    ALL_JOBS,
    CENTRAL,
    JOB_LABEL_SUFFIXES,
    JOB_SCHEDULES,
    MONITORED_JOBS,
    SCRIPT_JOBS,
)
from kamandal_v2.stores.sqlite import LocalStore


RESULT_PREFIX = "KAMANDAL_LAUNCHD_JOB="
HOLIDAYS = MARKET_HOLIDAYS
ACTIONABLE_HEALTH_REASONS = {
    "close_order_stale",
}
NON_PAGING_OPERATOR_STATES = {"self_handled", "self_healing"}
OPERATOR_ATTENTION_STATES = {"operator_needed", "blocked_self_healing"}
DELEGATED_ATTENTION_SURFACES = {"external_review"}
DERIVED_OWNER_REASONS = {
    # The planner job owns snapshot refresh and pages on failure. Entry safety
    # already fails closed while this derived health condition is present.
    "risk_account_snapshot_stale",
}
OPERATOR_ACTIONS = {
    "close_order_stale": "Check the working close at the broker; cancel or reprice it if it is no longer progressing.",
    "urgent_close_order_stale": "Check the urgent close at the broker now; cancel or reprice it so Kamandal can continue.",
    "exit_pipeline_stalled": "Check the live-approved-orders job and broker state; the approved close has not reached a working order.",
    "failed_preflight_close": "Resolve the broker conflict or close the position manually; Kamandal cannot submit the required close.",
    "failed_close_order": "Inspect the broker rejection and close manually if the conflict cannot be cleared.",
    "portfolio_bpr_over_hard_cap": "Reduce exposure or confirm the broker/account snapshot; the portfolio is above the hard risk cap.",
    "risk_account_snapshot_stale": "Check the account snapshot job and broker connectivity; safe new entries are blocked until the snapshot refreshes.",
    "risk_daily_drawdown_breaker": "Review today's account drawdown and decide whether the entry pause should remain in place.",
    "risk_weekly_drawdown_breaker": "Review the weekly account drawdown and decide whether the entry pause should remain in place.",
    "pending_entry_approvals": "Approve, reject, or clear the waiting entry in the configured approval surface.",
    "position_target_reached": "Approve or reject the staged profit exit; automatic exit approval is not enabled.",
    "loss_watch": "Review the loss-watch position and choose close or hold; automatic loss action is not enabled.",
}
LIVE_HEALTH_ATTENTION_STATE_EVENT = "live_health_attention_state"
SCHEDULED_HEALTH_ATTENTION_STATE_EVENT = "scheduled_health_attention_state"
SCRIPT_JOB_FAILURE_STATE_PREFIX = "launchd_job_failure_state"
HIGH_FREQUENCY_FAILURE_THRESHOLDS = {
    "live-approved-orders": 3,
    "unified-lifecycle-management": 3,
}
LOCK_ALREADY_RUNNING = 75
LOCK_UNVERIFIABLE = 76
LIVE_RECONCILIATION_TIMEOUT_SECONDS = 300.0


class ScriptTimeoutError(TimeoutError):
    def __init__(self, script: str, timeout_seconds: float, stdout: str, stderr: str) -> None:
        self.script = script
        self.timeout_seconds = timeout_seconds
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            "\n".join(
                [
                    f"{script} timed out after {timeout_seconds:.1f} seconds",
                    "",
                    "partial stdout:",
                    tail(stdout, max_lines=24, max_chars=2400),
                    "",
                    "partial stderr:",
                    tail(stderr, max_lines=24, max_chars=2400),
                ]
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=ALL_JOBS)
    parser.add_argument("--force", action="store_true", help="Run even when today is not a trading day")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--alert-mode", default=os.getenv("KAMANDAL_LAUNCHD_ALERT_MODE", "live"), choices=["off", "spool", "live"])
    parser.add_argument("--alert-profile", default=default_lathi_bus_profile())
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root or Path(__file__).resolve().parents[3]).resolve()
    os.chdir(repo_root)

    if args.job == "scheduled-job-health":
        rotate_log_directories(repo_root=repo_root)

    if should_skip_for_calendar(args.job, force=args.force):
        print_result({"job": args.job, "status": "skipped", "reason": "non_trading_day"})
        return 0

    try:
        if args.job == "live-health-report":
            return live_health_report_job(args)
        if args.job == "scheduled-job-health":
            return scheduled_job_health_report_job(args, repo_root=repo_root)
        if args.job == "daily-report":
            return daily_report_job(args)
        return script_job(args, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 - scheduled jobs must alert and fail closed.
        alert = failure_alert(args, title=f"Kamandal launchd job failed: {args.job}", detail=str(exc))
        print_result({"job": args.job, "status": "failed", "error": str(exc), "alert": alert.to_dict()})
        return 2


def script_job(args: argparse.Namespace, *, repo_root: Path) -> int:
    script = SCRIPT_JOBS[args.job]
    completed = run_script(
        script,
        repo_root=repo_root,
        force=args.force,
        timeout_seconds=job_timeout_seconds(args.job),
    )
    result = {
        "job": args.job,
        "status": "ok" if completed.returncode == 0 else "failed",
        "return_code": completed.returncode,
        "stdout_tail": tail(completed.stdout),
        "stderr_tail": tail(completed.stderr),
    }
    if completed.returncode == 0:
        recovery_alert = clear_script_failure_attention(
            LocalStore(repo_root / "data" / "kamandal_v2.db"),
            args,
        )
        if recovery_alert is not None:
            result["recovery_alert"] = recovery_alert.to_dict()
        print_result(result)
        return 0
    if completed.returncode in {LOCK_ALREADY_RUNNING, LOCK_UNVERIFIABLE}:
        print_result(
            {
                **result,
                "status": "skipped_already_running" if completed.returncode == LOCK_ALREADY_RUNNING else "blocked_unverifiable_lock",
            }
        )
        return 0 if completed.returncode == LOCK_ALREADY_RUNNING else 2

    store = LocalStore(repo_root / "data" / "kamandal_v2.db")
    attention = script_failure_attention(store, args.job, completed)
    alert: AlertResult | None = None
    if attention["notify"]:
        alert = failure_alert(args, title=f"Kamandal launchd job failed: {args.job}", detail=command_failure_detail(completed))
        record_script_failure_notification(store, args.job, attention, delivered=alert.ok or args.alert_mode == "off")
    print_result({**result, "attention": attention, "alert": alert.to_dict() if alert else None})
    return 2


def live_health_report_job(args: argparse.Namespace) -> int:
    config = load_control()
    store = LocalStore()
    report = run_live_health(store, config)
    attention = health_attention(report)
    if args.alert_mode == "live":
        attention = dedupe_health_attention(store, attention)
    alert: AlertResult | None = None
    delivery_status = "not_requested"
    if attention["notify"]:
        alert = send_lathi_alert(
            title="Kamandal live health",
            body=render_live_health_summary(report, attention=attention),
            level=str(attention["level"]),
            mode=args.alert_mode,
            profile=args.alert_profile,
        )
        delivery_status = "ok" if alert.ok or args.alert_mode == "off" else "failed"
        if alert.ok and args.alert_mode == "live":
            record_health_attention_open(store, attention)
        elif delivery_status == "failed":
            store.event(
                "live_health_alert_delivery_failed",
                {
                    "health": report.get("overall"),
                    "reasons": report.get("reasons"),
                    "attention": attention,
                    "alert": alert.to_dict(),
                },
            )
    print_result(
        {
            "job": args.job,
            "status": "ok",
            "delivery_status": delivery_status,
            "health": report.get("overall"),
            "counts": report.get("counts"),
            "reasons": report.get("reasons"),
            "attention": attention,
            "alert": alert.to_dict() if alert else None,
        }
    )
    return 0


def daily_report_job(args: argparse.Namespace) -> int:
    from kamandal_v2.ops.daily_report import (
        write_daily_report,
    )
    from kamandal_v2.paths import resolve_path

    config = load_control()
    # Best-effort live probes for RYG APP block (mirrors Bhiksha pattern)
    store_path = resolve_path("data/kamandal_v2.db")
    output_dir = resolve_path("data/reports")
    result = write_daily_report(store_path, output_dir=output_dir, config=config)
    level = "info" if result.report.get("status", {}).get("level") in ("GREEN", "YELLOW") else "error"
    # Reports are passive evidence, not operator attention. The JSON/Markdown
    # artifacts remain available to TradeLab and operator surfaces; Telegram is
    # reserved for the incident owner.
    print_result(
        {
            "job": args.job,
            "status": "ok",
            "report_date": result.report.get("trading_date"),
            "report_status": result.report.get("status", {}).get("level"),
            "json_path": str(result.json_path),
            "markdown_path": str(result.markdown_path),
            "ryg_path": str(result.ryg_markdown_path),
            "strategy_evidence_paths": result.report.get("strategy_evidence_artifacts"),
            "alert": None,
            "delivery_status": "local_artifact_only",
            "level": level,
        }
    )
    return 0


def scheduled_job_health_report_job(args: argparse.Namespace, *, repo_root: Path) -> int:
    report = scheduled_job_health(repo_root=repo_root)
    store = LocalStore()
    uncovered_issues = [issue for issue in report["issues"] if not script_failure_issue_covered(store, issue)]
    attention = {"notify": bool(uncovered_issues), "level": "error" if uncovered_issues else "info", "reason": "scheduled_job_failure" if uncovered_issues else "all_scheduled_jobs_healthy"}
    if args.alert_mode == "live":
        attention = dedupe_scheduled_health_attention(store, attention, uncovered_issues)
    alert: AlertResult | None = None
    ok = True
    if attention["notify"]:
        alert = send_lathi_alert(
            title="Kamandal scheduled job health",
            body=render_scheduled_job_health_summary({**report, "issues": uncovered_issues}),
            level=str(attention["level"]),
            mode=args.alert_mode,
            profile=args.alert_profile,
        )
        ok = alert.ok or args.alert_mode == "off"
        if alert.ok and args.alert_mode == "live":
            record_scheduled_health_attention_open(store, attention)
    print_result(
        {
            "job": args.job,
            "status": "ok" if ok else "failed",
            "checked_at": report["checked_at"],
            "issues": report["issues"],
            "uncovered_issues": uncovered_issues,
            "attention": attention,
            "alert": alert.to_dict() if alert else None,
        }
    )
    return 0 if ok else 2


def should_skip_for_calendar(_job: str, *, force: bool) -> bool:
    if force:
        return False
    return is_non_trading_day(datetime.now(CENTRAL).date())


def job_timeout_seconds(job: str) -> float:
    specific = os.getenv(f"KAMANDAL_LAUNCHD_JOB_TIMEOUT_SECONDS_{job.upper().replace('-', '_')}")
    if specific:
        return float(specific)
    if job == "live-reconciliation":
        return LIVE_RECONCILIATION_TIMEOUT_SECONDS
    return float(os.getenv("KAMANDAL_LAUNCHD_JOB_TIMEOUT_SECONDS", "1800"))


def run_script(
    script: str,
    *,
    repo_root: Path,
    force: bool,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    env["PYTHONUNBUFFERED"] = "1"
    if force:
        env["KAMANDAL_FORCE_RUN"] = "1"
    process = subprocess.Popen(  # noqa: S603
        ["/bin/bash", str(repo_root / "scripts" / script)],
        cwd=str(repo_root),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=10.0)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise ScriptTimeoutError(script, timeout_seconds, stdout or "", stderr or "") from None
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def failure_alert(args: argparse.Namespace, *, title: str, detail: str) -> AlertResult:
    body = "\n".join(
        [
            f"Job: {args.job}",
            f"Host repo: {Path.cwd()}",
            "",
            detail,
        ]
    )
    return send_lathi_alert(
        title=title,
        body=body,
        level="error",
        mode=args.alert_mode,
        profile=args.alert_profile,
    )


def command_failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        [
            f"Return code: {completed.returncode}",
            "",
            "stdout:",
            tail(completed.stdout, max_lines=18, max_chars=1200),
            "",
            "stderr:",
            tail(completed.stderr, max_lines=18, max_chars=1600),
        ]
    )


def script_failure_attention(
    store: LocalStore,
    job: str,
    completed: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    event_type = _script_failure_event_type(job)
    previous = store.latest_event(event_type) or {}
    summary = stable_failure_summary(completed)
    fingerprint = hashlib.sha256(f"{job}\n{summary}".encode("utf-8")).hexdigest()
    same_open = previous.get("status") == "open" and previous.get("fingerprint") == fingerprint
    consecutive = int(previous.get("consecutive") or 0) + 1 if same_open else 1
    notified = bool(previous.get("notified")) if same_open else False
    threshold = HIGH_FREQUENCY_FAILURE_THRESHOLDS.get(job, 1)
    attention = {
        "status": "open",
        "job": job,
        "fingerprint": fingerprint,
        "summary": summary,
        "consecutive": consecutive,
        "threshold": threshold,
        "notified": notified,
        "notify": consecutive >= threshold and not notified,
        "reason": "operator_incident" if consecutive >= threshold else "retrying_before_operator_page",
    }
    store.event(event_type, attention)
    return attention


def record_script_failure_notification(
    store: LocalStore,
    job: str,
    attention: dict[str, Any],
    *,
    delivered: bool,
) -> None:
    store.event(
        _script_failure_event_type(job),
        {
            **attention,
            "notify": False,
            "notified": delivered,
            "reason": "operator_notified" if delivered else "operator_delivery_failed",
        },
    )


def clear_script_failure_attention(store: LocalStore, args: argparse.Namespace) -> AlertResult | None:
    event_type = _script_failure_event_type(args.job)
    previous = store.latest_event(event_type) or {}
    if previous.get("status") != "open":
        return None
    store.event(
        event_type,
        {
            "status": "cleared",
            "job": args.job,
            "fingerprint": previous.get("fingerprint") or "",
            "consecutive": int(previous.get("consecutive") or 0),
            "notified": bool(previous.get("notified")),
        },
    )
    if not previous.get("notified"):
        return None
    return send_lathi_alert(
        title=f"KAMANDAL RECOVERED: {args.job}",
        body=f"Kamandal job {args.job} recovered after {int(previous.get('consecutive') or 0)} failed run(s).",
        level="info",
        mode=args.alert_mode,
        profile=args.alert_profile,
    )


def script_failure_issue_covered(store: LocalStore, issue: dict[str, Any]) -> bool:
    if str(issue.get("reason") or "") != "last_run_failed":
        return False
    job = str(issue.get("job") or "")
    state = store.latest_event(_script_failure_event_type(job)) or {}
    if state.get("status") != "open":
        return False
    if state.get("notified"):
        return True
    return int(state.get("consecutive") or 0) < int(state.get("threshold") or HIGH_FREQUENCY_FAILURE_THRESHOLDS.get(job, 1))


def stable_failure_summary(completed: subprocess.CompletedProcess[str]) -> str:
    signals: list[str] = []
    stdout = completed.stdout or ""
    start = stdout.find("{")
    if start >= 0:
        try:
            _collect_failure_signals(json.loads(stdout[start:]), signals)
        except json.JSONDecodeError:
            pass
    if not signals:
        lines = [line.strip() for line in (completed.stderr or "").splitlines() if line.strip()]
        if not lines:
            lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        signals = lines[-2:]
    normalized = " | ".join(signals) or f"return_code={completed.returncode}"
    normalized = re.sub(r"20\d\d-\d\d-\d\d[T ][0-9:.+-]+Z?", "<timestamp>", normalized)
    normalized = re.sub(r"run_[0-9TZ:-]+", "run_<timestamp>", normalized)
    return normalized[:2000]


def _collect_failure_signals(value: Any, signals: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "errors" and isinstance(item, list):
                signals.extend(str(entry) for entry in item if str(entry))
            elif key == "error" and isinstance(item, str) and item:
                signals.append(item)
            elif isinstance(item, (dict, list)):
                _collect_failure_signals(item, signals)
    elif isinstance(value, list):
        for item in value:
            _collect_failure_signals(item, signals)


def _script_failure_event_type(job: str) -> str:
    return f"{SCRIPT_JOB_FAILURE_STATE_PREFIX}:{job}"


def alert_level_for_health(report: dict[str, Any]) -> str:
    status = str(report.get("overall") or "GREEN").upper()
    if status == "RED":
        return "error"
    if status in {"YELLOW", "NO_DATA"}:
        return "warning"
    return "info"


def health_attention(report: dict[str, Any]) -> dict[str, Any]:
    status = str(report.get("overall") or "GREEN").upper()
    events = [event for event in report.get("events") or [] if isinstance(event, dict)]
    actionable_reasons = _actionable_health_reasons()
    attention_events = []
    for event in events:
        if str(event.get("attention_surface") or "") in DELEGATED_ATTENTION_SURFACES:
            continue
        operator_state = str(event.get("operator_state") or "")
        if operator_state in NON_PAGING_OPERATOR_STATES:
            continue
        reason = str(event.get("reason") or "")
        if reason in DERIVED_OWNER_REASONS:
            continue
        severity = str(event.get("severity") or "").lower()
        if operator_state in OPERATOR_ATTENTION_STATES or severity == "red" or reason in actionable_reasons:
            attention_events.append(event)
    if attention_events:
        reasons = sorted({str(event.get("reason") or "") for event in attention_events if event.get("reason")})
        level = "error" if any(str(event.get("severity") or "").lower() == "red" for event in attention_events) else "warning"
        return {"notify": True, "level": level, "reason": ",".join(reasons) or "operator_attention_required", "events": attention_events}
    if status == "RED" and not events:
        return {"notify": True, "level": "error", "reason": "red_live_health_without_events", "events": []}
    return {
        "notify": False,
        "level": alert_level_for_health(report),
        "reason": "no_operator_attention_required",
        "events": [],
    }


def attention_fingerprint(attention: dict[str, Any]) -> str:
    identities = []
    for event in attention.get("events") or []:
        identities.append(
            {
                key: str(event.get(key) or "")
                for key in ("reason", "group_id", "ticket_hash", "issue_id", "snapshot_id")
            },
        )
    payload = {
        "reason": str(attention.get("reason") or ""),
        "events": sorted(identities, key=lambda item: json.dumps(item, sort_keys=True)),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def dedupe_health_attention(store: LocalStore, attention: dict[str, Any]) -> dict[str, Any]:
    previous = store.latest_event(LIVE_HEALTH_ATTENTION_STATE_EVENT) or {}
    if not attention.get("notify"):
        if previous.get("status") == "open":
            store.event(
                LIVE_HEALTH_ATTENTION_STATE_EVENT,
                {
                    "status": "cleared",
                    "fingerprint": previous.get("fingerprint") or "",
                    "reason": previous.get("reason") or "",
                },
            )
        return attention

    fingerprint = attention_fingerprint(attention)
    if previous.get("status") == "open" and previous.get("fingerprint") == fingerprint:
        return {
            **attention,
            "notify": False,
            "reason": "unchanged_operator_attention",
            "attention_reason": attention.get("reason") or "",
            "fingerprint": fingerprint,
        }
    return {**attention, "fingerprint": fingerprint}


def record_health_attention_open(store: LocalStore, attention: dict[str, Any]) -> None:
    store.event(
        LIVE_HEALTH_ATTENTION_STATE_EVENT,
        {
            "status": "open",
            "fingerprint": attention.get("fingerprint") or attention_fingerprint(attention),
            "reason": attention.get("reason") or "",
        },
    )


def scheduled_attention_fingerprint(issues: list[dict[str, Any]]) -> str:
    identities = [
        {
            "job": str(issue.get("job") or ""),
            "reason": str(issue.get("reason") or ""),
            "detail": str(issue.get("detail") or ""),
        }
        for issue in issues
    ]
    return hashlib.sha256(
        json.dumps(sorted(identities, key=lambda item: json.dumps(item, sort_keys=True)), sort_keys=True).encode("utf-8")
    ).hexdigest()


def dedupe_scheduled_health_attention(
    store: LocalStore,
    attention: dict[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    previous = store.latest_event(SCHEDULED_HEALTH_ATTENTION_STATE_EVENT) or {}
    if not attention.get("notify"):
        if previous.get("status") == "open":
            store.event(
                SCHEDULED_HEALTH_ATTENTION_STATE_EVENT,
                {
                    "status": "cleared",
                    "fingerprint": previous.get("fingerprint") or "",
                    "reason": previous.get("reason") or "",
                },
            )
        return attention
    fingerprint = scheduled_attention_fingerprint(issues)
    if previous.get("status") == "open" and previous.get("fingerprint") == fingerprint:
        return {
            **attention,
            "notify": False,
            "reason": "unchanged_scheduled_job_failure",
            "attention_reason": attention.get("reason") or "",
            "fingerprint": fingerprint,
        }
    return {**attention, "fingerprint": fingerprint}


def record_scheduled_health_attention_open(store: LocalStore, attention: dict[str, Any]) -> None:
    store.event(
        SCHEDULED_HEALTH_ATTENTION_STATE_EVENT,
        {
            "status": "open",
            "fingerprint": attention.get("fingerprint") or "",
            "reason": attention.get("reason") or "scheduled_job_failure",
        },
    )


def _actionable_health_reasons() -> set[str]:
    raw = os.getenv("KAMANDAL_HEALTH_NOTIFY_REASONS", "").strip()
    if not raw:
        return set(ACTIONABLE_HEALTH_REASONS)
    return {item.strip() for item in raw.split(",") if item.strip()}


def render_live_health_summary(report: dict[str, Any], *, attention: dict[str, Any] | None = None) -> str:
    counts = report.get("counts") or {}
    scale = report.get("scale") or {}
    if attention is not None:
        lines = ["Kamandal exhausted safe automatic recovery and needs operator attention."]
        if attention.get("fingerprint"):
            lines.append(f"Incident: {str(attention['fingerprint'])[:12]}")
    else:
        lines = [
            f"Status: {report.get('overall')}",
            f"Score: {scale.get('score', '')}",
            (
                "Counts: "
                f"groups={counts.get('open_groups', 0)} "
                f"pending_entries={counts.get('pending_entry_approvals', 0)} "
                f"recon_blockers={counts.get('reconciliation_blockers', 0)} "
                f"loss_watch={counts.get('loss_watch_groups', 0)} "
                f"working_close={counts.get('working_close_orders', 0)}"
            ),
        ]
    reasons = (
        sorted({str(event.get("reason") or "") for event in attention.get("events") or [] if event.get("reason")})
        if attention is not None
        else report.get("reasons") or []
    )
    if reasons:
        lines.append("Reasons: " + ", ".join(str(reason) for reason in reasons))
    events = (attention or {}).get("events") if attention is not None else report.get("events")
    events = events or []
    for event in events[:8]:
        group = f" group={event.get('group_id')}" if event.get("group_id") else ""
        detail = str(event.get("detail") or "")
        lines.append(f"- {event.get('reason')}{group}: {detail}")
        action = OPERATOR_ACTIONS.get(str(event.get("reason") or ""))
        if action:
            lines.append(f"  Action: {action}")
    if len(events) > 8:
        lines.append(f"- plus {len(events) - 8} more events")
    return "\n".join(lines)


def scheduled_job_health(
    *,
    repo_root: Path,
    now: datetime | None = None,
    log_dir: Path | None = None,
    launchd_dir: Path | None = None,
    label_prefix: str | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(CENTRAL)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CENTRAL)
    log_dir = log_dir or Path(os.getenv("KAMANDAL_LAUNCHD_LOG_DIR", str(repo_root / "data" / "logs" / "launchd")))
    launchd_dir = launchd_dir or Path(os.getenv("KAMANDAL_LAUNCHD_DIR", str(Path.home() / "Library" / "LaunchAgents")))
    label_prefix = label_prefix or os.getenv("KAMANDAL_LAUNCHD_LABEL_PREFIX", "com.kamandal.v2")
    grace = int(os.getenv("KAMANDAL_JOB_HEALTH_GRACE_MINUTES", "20"))
    rows = []
    issues = []
    for job in MONITORED_JOBS:
        schedule = JOB_SCHEDULES[job]
        label = f"{label_prefix}.{JOB_LABEL_SUFFIXES[job]}"
        log_path = log_dir / f"{label}.out.log"
        plist_path = launchd_dir / f"{label}.plist"
        expectation = expected_job_observation(schedule, now=now, grace_minutes=grace)
        observation = read_launchd_observation(log_path, plist_path=plist_path)
        observation = fresher_success_observation(job, observation, repo_root=repo_root, now=now)
        issue = evaluate_job_observation(job, expectation, observation)
        row = {
            "job": job,
            "label": label,
            "expected": expectation,
            "last": observation,
            "issue": issue,
        }
        rows.append(row)
        if issue:
            issues.append(issue)
    return {
        "checked_at": now.isoformat(),
        "grace_minutes": grace,
        "issues": issues,
        "jobs": rows,
    }


def expected_job_observation(schedule: JobSchedule, *, now: datetime, grace_minutes: int) -> dict[str, Any]:
    if schedule.weekday is not None and now.weekday() != schedule.weekday:
        return {"status": "not_expected_today", "reason": "weekday_specific"}
    today = now.date()
    if is_non_trading_day(today):
        return {"status": "not_expected_today", "reason": "non_trading_day"}
    grace = timedelta(minutes=grace_minutes)
    if schedule.fixed_times and schedule.cadence_minutes and schedule.window_start and schedule.window_end:
        first_fixed = min(datetime.combine(today, value, CENTRAL) for value in schedule.fixed_times)
        if now < first_fixed:
            start_dt = datetime.combine(today, schedule.window_start, CENTRAL)
            end_dt = datetime.combine(today, schedule.window_end, CENTRAL)
            if now < start_dt + grace:
                return {"status": "not_due_yet", "expected_after": start_dt.isoformat()}
            latest_reference = min(now, end_dt)
            acceptable_after = latest_reference - timedelta(minutes=schedule.cadence_minutes + grace_minutes)
            return {"status": "due", "acceptable_after": acceptable_after.isoformat(), "expected_by": latest_reference.isoformat()}
        cadence_due = _cadence_times(
            today=today,
            start=schedule.window_start,
            end=schedule.window_end,
            step_minutes=schedule.cadence_minutes,
        )
        scheduled = sorted(
            {
                *cadence_due,
                *(datetime.combine(today, value, CENTRAL) for value in schedule.fixed_times),
            }
        )
        return _fixed_schedule_expectation(scheduled, now=now, grace=grace)
    if schedule.cadence_minutes and schedule.window_start and schedule.window_end:
        start_dt = datetime.combine(today, schedule.window_start, CENTRAL)
        end_dt = datetime.combine(today, schedule.window_end, CENTRAL)
        if now < start_dt + grace:
            return {"status": "not_due_yet", "expected_after": start_dt.isoformat()}
        latest_reference = min(now, end_dt)
        acceptable_after = latest_reference - timedelta(minutes=schedule.cadence_minutes + grace_minutes)
        return {"status": "due", "acceptable_after": acceptable_after.isoformat(), "expected_by": latest_reference.isoformat()}

    scheduled = [datetime.combine(today, value, CENTRAL) for value in schedule.fixed_times]
    return _fixed_schedule_expectation(scheduled, now=now, grace=grace)


def _cadence_times(
    *,
    today: date,
    start: time,
    end: time,
    step_minutes: int,
) -> list[datetime]:
    current = datetime.combine(today, start, CENTRAL)
    last = datetime.combine(today, end, CENTRAL)
    values: list[datetime] = []
    while current <= last:
        values.append(current)
        current += timedelta(minutes=step_minutes)
    return values


def _fixed_schedule_expectation(
    scheduled: list[datetime],
    *,
    now: datetime,
    grace: timedelta,
) -> dict[str, Any]:
    due_times = [value for value in scheduled if value <= now]
    if not due_times:
        next_time = min(scheduled, default=None)
        return {"status": "not_due_yet", "expected_after": next_time.isoformat() if next_time else ""}
    latest_due = max(due_times)
    if now < latest_due + grace:
        return {"status": "pending_grace", "expected_by": latest_due.isoformat(), "acceptable_after": latest_due.isoformat()}
    acceptable_after = latest_due - grace
    return {"status": "due", "acceptable_after": acceptable_after.isoformat(), "expected_by": latest_due.isoformat()}


def read_launchd_observation(log_path: Path, *, plist_path: Path | None = None) -> dict[str, Any]:
    installed_at = None
    if plist_path and plist_path.exists():
        installed_at = datetime.fromtimestamp(plist_path.stat().st_mtime, CENTRAL).isoformat()
    if not log_path.exists():
        return {"status": "missing_log", "log_path": str(log_path), "installed_at": installed_at}
    payload: dict[str, Any] | None = None
    try:
        for line in reversed(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
            if not line.startswith(RESULT_PREFIX):
                continue
            parsed = json.loads(line.split("=", 1)[1])
            if isinstance(parsed, dict):
                payload = parsed
                break
    except Exception as exc:  # noqa: BLE001 - health readback should degrade into an issue.
        return {"status": "unreadable_log", "log_path": str(log_path), "error": str(exc)}
    mtime = datetime.fromtimestamp(log_path.stat().st_mtime, CENTRAL)
    return {
        "status": str((payload or {}).get("status") or "no_result_line"),
        "job": str((payload or {}).get("job") or ""),
        "health": (payload or {}).get("health"),
        "reasons": (payload or {}).get("reasons"),
        "delivery_status": (payload or {}).get("delivery_status"),
        "mtime": mtime.isoformat(),
        "log_path": str(log_path),
        "installed_at": installed_at,
    }


def fresher_success_observation(
    job: str,
    observation: dict[str, Any],
    *,
    repo_root: Path,
    now: datetime,
) -> dict[str, Any]:
    artifact = intelligence_success_artifact(job, repo_root=repo_root, now=now)
    if not artifact:
        return observation
    observed_at = _observation_time(observation)
    if observed_at and artifact["observed_at"] <= observed_at:
        return observation
    return {
        **observation,
        "status": "ok",
        "job": job,
        "mtime": artifact["observed_at"].isoformat(),
        "success_source": "artifact",
        "artifact_path": str(artifact["path"]),
        "previous_status": observation.get("status"),
        "previous_log_path": observation.get("log_path"),
    }


def intelligence_success_artifact(job: str, *, repo_root: Path, now: datetime) -> dict[str, Any] | None:
    today = now.astimezone(CENTRAL).date().isoformat()
    candidates: list[Path]
    if job == "x-bookmarks":
        candidates = [
            repo_root / "data" / "digest" / "x_bookmarks" / today / "llm" / f"{today}_llm_raw.json",
            repo_root / "data" / "digest" / "x_bookmarks" / today / "llm" / f"{today}_llm.md",
            repo_root / "data" / "ideas" / "active" / f"x_bookmarks_imported_{today}.yaml",
        ]
    elif job == "youtube":
        candidates = [
            repo_root / "data" / "digest" / "youtube" / today / f"{today}_llm_raw.json",
            repo_root / "data" / "digest" / "youtube" / today / f"{today}_llm.md",
            repo_root / "data" / "ideas" / "active" / f"llm_imported_{today}.yaml",
        ]
    else:
        return None
    existing = [path for path in candidates if path.exists() and path.stat().st_size > 0]
    if not existing:
        return None
    newest = max(existing, key=lambda path: path.stat().st_mtime)
    return {
        "path": newest,
        "observed_at": datetime.fromtimestamp(newest.stat().st_mtime, CENTRAL),
    }


def _observation_time(observation: dict[str, Any]) -> datetime | None:
    raw = observation.get("mtime")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CENTRAL)
    return parsed


def evaluate_job_observation(job: str, expectation: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any] | None:
    if expectation["status"] in {"not_due_yet", "pending_grace", "not_expected_today"}:
        return None
    if observation.get("installed_at") and expectation.get("expected_by"):
        installed_at = datetime.fromisoformat(str(observation["installed_at"]))
        expected_by = datetime.fromisoformat(str(expectation["expected_by"]))
        if installed_at > expected_by:
            # The current plist became active after the most recent scheduled
            # tick.  An absent or older log belongs to the prior activation and
            # cannot make the newly installed schedule retroactively stale.
            return None
    if observation["status"] in {"missing_log", "unreadable_log", "no_result_line"}:
        return {"job": job, "reason": observation["status"], "detail": observation.get("error") or observation.get("log_path")}
    observed_job = observation.get("job")
    if observed_job and observed_job != job:
        return {"job": job, "reason": "wrong_job_in_log", "detail": f"expected {job}, saw {observed_job}"}
    acceptable_after = datetime.fromisoformat(str(expectation["acceptable_after"]))
    observed_at = datetime.fromisoformat(str(observation["mtime"]))
    if observed_at < acceptable_after:
        return {"job": job, "reason": "stale_last_run", "detail": f"last={observed_at.isoformat()} expected_after={acceptable_after.isoformat()}"}
    if str(observation["status"]).lower() == "failed":
        # Surface Agent Broker exhaustion distinctly from generic script failure
        detail = observation.get("log_path") or ""
        # Try to extract last broker chain from log tail if present
        try:
            import json as _j
            tail = (observation.get("stderr_tail") or observation.get("stdout_tail") or "")[-1200:]
            if "Agent Broker" in tail:
                # Keep it short for Tower findings (Blackboard truncates)
                short = tail.split("Agent Broker")[-1].strip()[:180]
                detail = f"Agent Broker: {short} | log: {detail}"
        except: pass
        return {"job": job, "reason": "last_run_failed", "detail": detail}
    return None


def render_scheduled_job_health_summary(report: dict[str, Any]) -> str:
    lines = [
        "Kamandal scheduled job health",
        f"Checked: {report.get('checked_at')}",
        f"Issues: {len(report.get('issues') or [])}",
    ]
    for issue in report.get("issues") or []:
        lines.append(f"- {issue.get('job')}: {issue.get('reason')} {issue.get('detail') or ''}".rstrip())
    return "\n".join(lines)


def print_result(payload: dict[str, Any]) -> None:
    print(RESULT_PREFIX + json.dumps(payload, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
