"""Kamandal-owned launchd job runner."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from kamandal_v2.config import load_control
from kamandal_v2.live.health import run_live_health
from kamandal_v2.ops.alerts import AlertResult, default_lathi_bus_profile, send_lathi_alert, tail
from kamandal_v2.ops.log_rotation import rotate_log_directories
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
HOLIDAYS = {
    "2026-01-01",
    "2026-01-19",
    "2026-02-16",
    "2026-04-03",
    "2026-05-25",
    "2026-06-19",
    "2026-07-03",
    "2026-09-07",
    "2026-11-26",
    "2026-12-25",
    "2027-01-01",
    "2027-01-18",
    "2027-02-15",
    "2027-03-26",
    "2027-05-31",
    "2027-06-18",
    "2027-07-05",
    "2027-09-06",
    "2027-11-25",
    "2027-12-24",
}
ACTIONABLE_HEALTH_REASONS = {
    "close_order_stale",
    "portfolio_bpr_over_target",
    "stale_failed_close_order",
}


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
        return script_job(args, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 - scheduled jobs must alert and fail closed.
        alert = failure_alert(args, title=f"Kamandal launchd job failed: {args.job}", detail=str(exc))
        print_result({"job": args.job, "status": "failed", "error": str(exc), "alert": alert.to_dict()})
        return 2


def script_job(args: argparse.Namespace, *, repo_root: Path) -> int:
    script = SCRIPT_JOBS[args.job]
    completed = run_script(script, repo_root=repo_root, force=args.force)
    result = {
        "job": args.job,
        "status": "ok" if completed.returncode == 0 else "failed",
        "return_code": completed.returncode,
        "stdout_tail": tail(completed.stdout),
        "stderr_tail": tail(completed.stderr),
    }
    if completed.returncode == 0:
        print_result(result)
        return 0

    alert = failure_alert(args, title=f"Kamandal launchd job failed: {args.job}", detail=command_failure_detail(completed))
    print_result({**result, "alert": alert.to_dict()})
    return 2


def live_health_report_job(args: argparse.Namespace) -> int:
    config = load_control()
    report = run_live_health(LocalStore(), config)
    attention = health_attention(report)
    alert: AlertResult | None = None
    ok = True
    if attention["notify"]:
        alert = send_lathi_alert(
            title="Kamandal live health",
            body=render_live_health_summary(report),
            level=str(attention["level"]),
            mode=args.alert_mode,
            profile=args.alert_profile,
        )
        ok = alert.ok or args.alert_mode == "off"
    print_result(
        {
            "job": args.job,
            "status": "ok" if ok else "failed",
            "health": report.get("overall"),
            "counts": report.get("counts"),
            "reasons": report.get("reasons"),
            "attention": attention,
            "alert": alert.to_dict() if alert else None,
        }
    )
    return 0 if ok else 2


def scheduled_job_health_report_job(args: argparse.Namespace, *, repo_root: Path) -> int:
    report = scheduled_job_health(repo_root=repo_root)
    attention = {"notify": bool(report["issues"]), "level": "error" if report["issues"] else "info", "reason": "scheduled_job_failure" if report["issues"] else "all_scheduled_jobs_healthy"}
    alert: AlertResult | None = None
    ok = True
    if attention["notify"]:
        alert = send_lathi_alert(
            title="Kamandal scheduled job health",
            body=render_scheduled_job_health_summary(report),
            level=str(attention["level"]),
            mode=args.alert_mode,
            profile=args.alert_profile,
        )
        ok = alert.ok or args.alert_mode == "off"
    print_result(
        {
            "job": args.job,
            "status": "ok" if ok else "failed",
            "checked_at": report["checked_at"],
            "issues": report["issues"],
            "attention": attention,
            "alert": alert.to_dict() if alert else None,
        }
    )
    return 0 if ok else 2


def should_skip_for_calendar(_job: str, *, force: bool) -> bool:
    if force:
        return False
    today = datetime.now(CENTRAL).date()
    if today.weekday() >= 5:
        return True
    if os.getenv("KAMANDAL_MARKET_HOLIDAY_CALENDAR", "nyse").lower() == "off":
        return False
    return today.isoformat() in HOLIDAYS


def run_script(script: str, *, repo_root: Path, force: bool) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    env["PYTHONUNBUFFERED"] = "1"
    if force:
        env["KAMANDAL_FORCE_RUN"] = "1"
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(repo_root / "scripts" / script)],
        check=False,
        cwd=str(repo_root),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=float(os.getenv("KAMANDAL_LAUNCHD_JOB_TIMEOUT_SECONDS", "1800")),
    )


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
    red_events = [event for event in events if str(event.get("severity") or "").lower() == "red"]
    if status == "RED" or red_events:
        reasons = sorted({str(event.get("reason") or "") for event in red_events if event.get("reason")})
        return {"notify": True, "level": "error", "reason": ",".join(reasons) or "red_live_health"}
    actionable_reasons = _actionable_health_reasons()
    reasons = {str(reason) for reason in report.get("reasons") or []}
    matched = sorted(reasons & actionable_reasons)
    if matched:
        return {"notify": True, "level": "warning", "reason": ",".join(matched)}
    return {"notify": False, "level": alert_level_for_health(report), "reason": "no_operator_attention_required"}


def _actionable_health_reasons() -> set[str]:
    raw = os.getenv("KAMANDAL_HEALTH_NOTIFY_REASONS", "").strip()
    if not raw:
        return set(ACTIONABLE_HEALTH_REASONS)
    return {item.strip() for item in raw.split(",") if item.strip()}


def render_live_health_summary(report: dict[str, Any]) -> str:
    counts = report.get("counts") or {}
    scale = report.get("scale") or {}
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
    reasons = report.get("reasons") or []
    if reasons:
        lines.append("Reasons: " + ", ".join(str(reason) for reason in reasons))
    events = report.get("events") or []
    for event in events[:8]:
        group = f" group={event.get('group_id')}" if event.get("group_id") else ""
        detail = str(event.get("detail") or "")
        lines.append(f"- {event.get('reason')}{group}: {detail}")
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
    grace = timedelta(minutes=grace_minutes)
    if schedule.cadence_minutes and schedule.window_start and schedule.window_end:
        start_dt = datetime.combine(today, schedule.window_start, CENTRAL)
        end_dt = datetime.combine(today, schedule.window_end, CENTRAL)
        if now < start_dt + grace:
            return {"status": "not_due_yet", "expected_after": start_dt.isoformat()}
        latest_reference = min(now, end_dt)
        acceptable_after = latest_reference - timedelta(minutes=schedule.cadence_minutes + grace_minutes)
        return {"status": "due", "acceptable_after": acceptable_after.isoformat(), "expected_by": latest_reference.isoformat()}

    due_times = [datetime.combine(today, scheduled, CENTRAL) for scheduled in schedule.fixed_times if datetime.combine(today, scheduled, CENTRAL) <= now]
    if not due_times:
        next_time = min((datetime.combine(today, scheduled, CENTRAL) for scheduled in schedule.fixed_times), default=None)
        return {"status": "not_due_yet", "expected_after": next_time.isoformat() if next_time else ""}
    latest_due = max(due_times)
    if now < latest_due + grace:
        return {"status": "pending_grace", "expected_by": latest_due.isoformat(), "acceptable_after": latest_due.isoformat()}
    acceptable_after = latest_due - timedelta(minutes=grace_minutes)
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
    if observation["status"] == "missing_log" and observation.get("installed_at") and expectation.get("expected_by"):
        installed_at = datetime.fromisoformat(str(observation["installed_at"]))
        expected_by = datetime.fromisoformat(str(expectation["expected_by"]))
        if installed_at > expected_by:
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
        return {"job": job, "reason": "last_run_failed", "detail": observation.get("log_path")}
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
