"""Kamandal-owned launchd job runner."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from zoneinfo import ZoneInfo

from kamandal_v2.config import load_control
from kamandal_v2.live.health import run_live_health
from kamandal_v2.ops.alerts import AlertResult, send_lathi_alert, tail
from kamandal_v2.stores.sqlite import LocalStore


CENTRAL = ZoneInfo("America/Chicago")
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
SCRIPT_JOBS = {
    "x-bookmarks": "run_x_bookmark_extraction.sh",
    "youtube": "run_youtube_extraction.sh",
    "my-ideas": "run_my_ideas_import.sh",
    "live-reconciliation": "run_live_reconciliation.sh",
    "live-advisory": "run_live_advisory.sh",
    "live-approved-orders": "run_live_approved_orders.sh",
    "live-management": "run_live_management.sh",
    "earnings": "run_earnings_capture.sh",
    "iv": "run_iv_capture.sh",
    "iv-afternoon": "run_iv_capture.sh",
    "weekly-reviewer": "run_weekly_reviewer.sh",
}
ALL_JOBS = sorted([*SCRIPT_JOBS, "live-health-report"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=ALL_JOBS)
    parser.add_argument("--force", action="store_true", help="Run even when today is not a trading day")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--alert-mode", default=os.getenv("KAMANDAL_LAUNCHD_ALERT_MODE", "live"), choices=["off", "spool", "live"])
    parser.add_argument("--alert-profile", default=os.getenv("KAMANDAL_LATHI_PROFILE", "jarvis-northstar"))
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root or Path(__file__).resolve().parents[3]).resolve()
    os.chdir(repo_root)

    if should_skip_for_calendar(args.job, force=args.force):
        print_result({"job": args.job, "status": "skipped", "reason": "non_trading_day"})
        return 0

    try:
        if args.job == "live-health-report":
            return live_health_report_job(args)
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
    level = alert_level_for_health(report)
    body = render_live_health_summary(report)
    alert = send_lathi_alert(
        title="Kamandal live health",
        body=body,
        level=level,
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
            "alert": alert.to_dict(),
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
            tail(completed.stdout),
            "",
            "stderr:",
            tail(completed.stderr),
        ]
    )


def alert_level_for_health(report: dict[str, Any]) -> str:
    status = str(report.get("overall") or "GREEN").upper()
    if status == "RED":
        return "error"
    if status in {"YELLOW", "NO_DATA"}:
        return "warning"
    return "info"


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


def print_result(payload: dict[str, Any]) -> None:
    print(RESULT_PREFIX + json.dumps(payload, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
