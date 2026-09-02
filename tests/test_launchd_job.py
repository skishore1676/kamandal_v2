from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from kamandal_v2.ops.alerts import AlertResult
from kamandal_v2.tools import launchd_job


def test_launchd_job_skips_non_trading_day(monkeypatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setattr(launchd_job, "should_skip_for_calendar", lambda _job, force: True)

    code = launchd_job.main(["iv"])

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith(launchd_job.RESULT_PREFIX)
    payload = json.loads(out.split("=", 1)[1])
    assert payload["status"] == "skipped"


def test_script_job_failure_sends_alert(monkeypatch, tmp_path, capsys) -> None:  # noqa: ANN001
    args = SimpleNamespace(job="iv", force=False, alert_mode="spool", alert_profile="kamandal-northstar")
    completed = subprocess.CompletedProcess(["run"], 9, stdout="bad stdout", stderr="bad stderr")
    monkeypatch.setattr(
        launchd_job,
        "run_script",
        lambda _script, repo_root, force, timeout_seconds: completed,
    )
    monkeypatch.setattr(
        launchd_job,
        "failure_alert",
        lambda *_args, **_kwargs: AlertResult(attempted=True, ok=True, mode="spool"),
    )

    code = launchd_job.script_job(args, repo_root=tmp_path)

    out = capsys.readouterr().out
    assert code == 2
    payload = json.loads(out.split("=", 1)[1])
    assert payload["status"] == "failed"
    assert payload["alert"]["ok"] is True


def test_source_job_failure_is_warning_and_does_not_claim_trading_is_blocked(monkeypatch) -> None:  # noqa: ANN001
    args = SimpleNamespace(job="x-bookmarks", alert_mode="spool", alert_profile="test")
    captured = {}
    monkeypatch.setattr(
        launchd_job,
        "send_lathi_alert",
        lambda **kwargs: captured.update(kwargs) or AlertResult(attempted=True, ok=True, mode="spool"),
    )

    launchd_job.failure_alert(args, title="ignored generic title", detail="Birdclaw digest is stale")

    assert captured["title"] == "Kamandal source lane degraded: x-bookmarks"
    assert captured["level"] == "warning"
    assert "Other idea lanes, planning, and portfolio management continue" in captured["body"]
    assert "trading may be blocked" not in captured["body"].lower()


def test_source_job_failure_retries_before_degraded_lane_page(tmp_path) -> None:
    completed = subprocess.CompletedProcess(["run"], 1, stdout="", stderr="Birdclaw digest is stale")
    store = launchd_job.LocalStore(tmp_path / "kamandal.db")

    first = launchd_job.script_failure_attention(store, "x-bookmarks", completed)
    second = launchd_job.script_failure_attention(store, "x-bookmarks", completed)
    third = launchd_job.script_failure_attention(store, "x-bookmarks", completed)

    assert first["notify"] is False
    assert first["reason"] == "retrying_before_operator_page"
    assert second["notify"] is False
    assert third["notify"] is True
    assert third["reason"] == "source_lane_degraded"
    assert third["threshold"] == 3


def test_frequent_script_failure_pages_once_after_retry_threshold(monkeypatch, tmp_path, capsys) -> None:  # noqa: ANN001
    args = SimpleNamespace(job="unified-lifecycle-management", force=False, alert_mode="spool", alert_profile="test")
    completed = subprocess.CompletedProcess(
        ["run"], 1,
        stdout=json.dumps({"ok": False, "branches": [{"result": {"errors": ["active leg quote missing"]}}]}),
        stderr="",
    )
    alerts = []
    monkeypatch.setattr(launchd_job, "run_script", lambda *_args, **_kwargs: completed)
    monkeypatch.setattr(
        launchd_job,
        "failure_alert",
        lambda *_args, **_kwargs: alerts.append("failure") or AlertResult(attempted=True, ok=True, mode="spool"),
    )

    payloads = []
    for _ in range(5):
        assert launchd_job.script_job(args, repo_root=tmp_path) == 2
        payloads.append(json.loads(capsys.readouterr().out.split("=", 1)[1]))

    assert alerts == ["failure"]
    assert [item["attention"]["notify"] for item in payloads] == [False, False, True, False, False]
    assert payloads[-1]["attention"]["consecutive"] == 5


def test_success_clears_paged_script_failure_with_one_recovery(monkeypatch, tmp_path, capsys) -> None:  # noqa: ANN001
    args = SimpleNamespace(job="unified-lifecycle-management", force=False, alert_mode="spool", alert_profile="test")
    failed = subprocess.CompletedProcess(["run"], 1, stdout="", stderr="stable failure")
    monkeypatch.setattr(launchd_job, "run_script", lambda *_args, **_kwargs: failed)
    monkeypatch.setattr(launchd_job, "failure_alert", lambda *_args, **_kwargs: AlertResult(attempted=True, ok=True, mode="spool"))
    for _ in range(3):
        launchd_job.script_job(args, repo_root=tmp_path)
        capsys.readouterr()

    recoveries = []
    monkeypatch.setattr(launchd_job, "run_script", lambda *_args, **_kwargs: subprocess.CompletedProcess(["run"], 0, stdout="ok", stderr=""))
    monkeypatch.setattr(
        launchd_job,
        "send_lathi_alert",
        lambda **kwargs: recoveries.append(kwargs["title"]) or AlertResult(attempted=True, ok=True, mode="spool"),
    )

    assert launchd_job.script_job(args, repo_root=tmp_path) == 0
    payload = json.loads(capsys.readouterr().out.split("=", 1)[1])
    assert recoveries == ["KAMANDAL RECOVERED: unified-lifecycle-management"]
    assert payload["recovery_alert"]["ok"] is True


@pytest.mark.parametrize(
    ("return_code", "expected_status", "expected_code"),
    [(75, "skipped_already_running", 0), (76, "blocked_unverifiable_lock", 2)],
)
def test_script_job_reports_lock_state_without_generic_alert(
    monkeypatch, tmp_path, capsys, return_code, expected_status, expected_code
) -> None:  # noqa: ANN001
    args = SimpleNamespace(job="iv", force=False, alert_mode="spool", alert_profile="test")
    completed = subprocess.CompletedProcess(["run"], return_code, stdout="lock status", stderr="")
    monkeypatch.setattr(
        launchd_job,
        "run_script",
        lambda _script, repo_root, force, timeout_seconds: completed,
    )
    monkeypatch.setattr(
        launchd_job,
        "failure_alert",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("lock state is structured")),
    )

    code = launchd_job.script_job(args, repo_root=tmp_path)

    payload = json.loads(capsys.readouterr().out.split("=", 1)[1])
    assert code == expected_code
    assert payload["status"] == expected_status


def test_reconciliation_has_bounded_default_timeout(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("KAMANDAL_LAUNCHD_JOB_TIMEOUT_SECONDS_LIVE_RECONCILIATION", raising=False)
    monkeypatch.delenv("KAMANDAL_LAUNCHD_JOB_TIMEOUT_SECONDS", raising=False)

    assert launchd_job.job_timeout_seconds("live-reconciliation") == 300.0
    assert launchd_job.job_timeout_seconds("iv") == 1800.0


def test_run_script_timeout_keeps_partial_output_and_terminates_group(tmp_path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "hang.sh"
    script.write_text("#!/bin/bash\necho reached-stage\nwhile true; do sleep 1; done\n", encoding="utf-8")

    with pytest.raises(launchd_job.ScriptTimeoutError, match="reached-stage"):
        launchd_job.run_script("hang.sh", repo_root=tmp_path, force=False, timeout_seconds=0.1)


def test_live_health_summary_and_alert_level() -> None:
    report = {
        "overall": "YELLOW",
        "scale": {"score": 70},
        "counts": {
            "open_groups": 9,
            "pending_entry_approvals": 1,
            "reconciliation_blockers": 0,
            "loss_watch_groups": 2,
            "working_close_orders": 1,
        },
        "reasons": ["loss_watch"],
        "events": [{"reason": "loss_watch", "group_id": "group1", "detail": "PLTR watch"}],
    }

    assert launchd_job.alert_level_for_health(report) == "warning"
    summary = launchd_job.render_live_health_summary(report)
    assert "Status: YELLOW" in summary
    assert "groups=9" in summary
    assert "PLTR watch" in summary


def test_live_health_report_job_suppresses_green_alert(monkeypatch, capsys) -> None:  # noqa: ANN001
    args = SimpleNamespace(job="live-health-report", alert_mode="spool", alert_profile="kamandal-northstar")
    monkeypatch.setattr(launchd_job, "load_control", lambda: {})
    monkeypatch.setattr(
        launchd_job,
        "run_live_health",
        lambda _store, _config: {
            "overall": "GREEN",
            "scale": {"score": 100},
            "counts": {},
            "reasons": [],
            "events": [],
        },
    )
    monkeypatch.setattr(launchd_job, "send_lathi_alert", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("green should not notify")))

    code = launchd_job.live_health_report_job(args)

    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out.split("=", 1)[1])
    assert payload["health"] == "GREEN"
    assert payload["attention"]["notify"] is False
    assert payload["alert"] is None


def test_live_health_report_job_sends_red_alert(monkeypatch, capsys) -> None:  # noqa: ANN001
    args = SimpleNamespace(job="live-health-report", alert_mode="spool", alert_profile="kamandal-northstar")
    monkeypatch.setattr(launchd_job, "load_control", lambda: {})
    monkeypatch.setattr(
        launchd_job,
        "run_live_health",
        lambda _store, _config: {
            "overall": "RED",
            "scale": {"score": 25},
            "counts": {},
            "reasons": ["failed_close_order"],
            "events": [{"severity": "red", "reason": "failed_close_order", "detail": "broker refused close"}],
        },
    )
    monkeypatch.setattr(
        launchd_job,
        "send_lathi_alert",
        lambda **_kwargs: AlertResult(attempted=True, ok=True, mode="spool"),
    )

    code = launchd_job.live_health_report_job(args)

    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out.split("=", 1)[1])
    assert payload["attention"]["notify"] is True
    assert payload["alert"]["ok"] is True


def test_live_health_report_delivery_failure_does_not_fail_domain_job(monkeypatch, capsys) -> None:  # noqa: ANN001
    args = SimpleNamespace(job="live-health-report", alert_mode="spool", alert_profile="kamandal-northstar")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(launchd_job, "load_control", lambda: {})
    monkeypatch.setattr(
        launchd_job,
        "LocalStore",
        lambda: SimpleNamespace(event=lambda name, payload: events.append((name, payload))),
    )
    monkeypatch.setattr(
        launchd_job,
        "run_live_health",
        lambda _store, _config: {
            "overall": "RED",
            "scale": {"score": 25},
            "counts": {},
            "reasons": ["failed_close_order"],
            "events": [{"severity": "red", "reason": "failed_close_order", "operator_state": "operator_needed"}],
        },
    )
    monkeypatch.setattr(
        launchd_job,
        "send_lathi_alert",
        lambda **_kwargs: AlertResult(attempted=True, ok=False, mode="spool", error="timeout"),
    )

    code = launchd_job.live_health_report_job(args)

    payload = json.loads(capsys.readouterr().out.split("=", 1)[1])
    assert code == 0
    assert payload["status"] == "ok"
    assert payload["delivery_status"] == "failed"
    assert events[0][0] == "live_health_alert_delivery_failed"


def test_health_attention_suppresses_self_handled_red_state() -> None:
    attention = launchd_job.health_attention(
        {
            "overall": "RED",
            "events": [
                {
                    "severity": "red",
                    "reason": "risk_cluster_at_cap",
                    "operator_state": "self_handled",
                },
            ],
        },
    )

    assert attention["notify"] is False
    assert attention["reason"] == "no_operator_attention_required"


def test_health_attention_leaves_stale_snapshot_to_planner_failure_owner() -> None:
    attention = launchd_job.health_attention(
        {
            "overall": "RED",
            "events": [
                {
                    "severity": "red",
                    "reason": "risk_account_snapshot_stale",
                    "operator_state": "operator_needed",
                },
            ],
        },
    )

    assert attention["notify"] is False
    assert attention["reason"] == "no_operator_attention_required"


def test_daily_report_stays_passive_without_telegram(monkeypatch, capsys, tmp_path) -> None:  # noqa: ANN001
    from kamandal_v2.ops import daily_report

    args = SimpleNamespace(job="daily-report", alert_mode="live", alert_profile="kamandal-northstar")
    report = {
        "trading_date": "2026-08-17",
        "status": {"level": "RED"},
    }
    result = SimpleNamespace(
        report=report,
        json_path=Path(tmp_path / "report.json"),
        markdown_path=Path(tmp_path / "report.md"),
        ryg_markdown_path=Path(tmp_path / "report_ryg.md"),
    )
    monkeypatch.setattr(launchd_job, "load_control", lambda: {})
    monkeypatch.setattr(daily_report, "write_daily_report", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        launchd_job,
        "send_lathi_alert",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("daily report must stay off Telegram")),
    )

    assert launchd_job.daily_report_job(args) == 0

    payload = json.loads(capsys.readouterr().out.split("=", 1)[1])
    assert payload["status"] == "ok"
    assert payload["alert"] is None
    assert payload["delivery_status"] == "local_artifact_only"


def test_health_attention_leaves_external_review_to_lathi() -> None:
    attention = launchd_job.health_attention(
        {
            "overall": "RED",
            "events": [
                {
                    "severity": "red",
                    "reason": "reconciliation_blocker",
                    "operator_state": "operator_needed",
                    "attention_surface": "external_review",
                },
            ],
        },
    )

    assert attention["notify"] is False


def test_health_attention_deduplicates_one_open_incident_until_clear(tmp_path) -> None:
    store = launchd_job.LocalStore(tmp_path / "kamandal.db")
    attention = launchd_job.health_attention(
        {
            "overall": "RED",
            "events": [
                {
                    "severity": "red",
                    "reason": "failed_close_order",
                    "operator_state": "operator_needed",
                    "group_id": "group-1",
                    "ticket_hash": "ticket-1",
                },
            ],
        },
    )

    first = launchd_job.dedupe_health_attention(store, attention)
    assert first["notify"] is True
    launchd_job.record_health_attention_open(store, first)

    repeated = launchd_job.dedupe_health_attention(store, attention)
    assert repeated["notify"] is False
    assert repeated["reason"] == "unchanged_operator_attention"

    cleared = launchd_job.dedupe_health_attention(
        store,
        {"notify": False, "level": "info", "reason": "no_operator_attention_required", "events": []},
    )
    assert cleared["notify"] is False
    assert store.latest_event(launchd_job.LIVE_HEALTH_ATTENTION_STATE_EVENT)["status"] == "cleared"

    reopened = launchd_job.dedupe_health_attention(store, attention)
    assert reopened["notify"] is True


def test_scheduled_health_deduplicates_same_failure_until_clear(tmp_path) -> None:
    store = launchd_job.LocalStore(tmp_path / "kamandal.db")
    issues = [{"job": "youtube", "reason": "last_run_failed", "detail": "/tmp/youtube.log"}]
    attention = {"notify": True, "level": "error", "reason": "scheduled_job_failure"}

    first = launchd_job.dedupe_scheduled_health_attention(store, attention, issues)
    assert first["notify"] is True
    launchd_job.record_scheduled_health_attention_open(store, first)

    repeated = launchd_job.dedupe_scheduled_health_attention(store, attention, issues)
    assert repeated["notify"] is False
    assert repeated["reason"] == "unchanged_scheduled_job_failure"

    launchd_job.dedupe_scheduled_health_attention(
        store,
        {"notify": False, "level": "info", "reason": "all_scheduled_jobs_healthy"},
        [],
    )
    assert store.latest_event(launchd_job.SCHEDULED_HEALTH_ATTENTION_STATE_EVENT)["status"] == "cleared"

    reopened = launchd_job.dedupe_scheduled_health_attention(store, attention, issues)
    assert reopened["notify"] is True


def test_watchdog_includes_unified_ownership_jobs() -> None:
    assert "daily-report" in launchd_job.MONITORED_JOBS
    assert {"unified-planning", "unified-lifecycle-management"}.issubset(launchd_job.MONITORED_JOBS)
    assert "universe-proposer" not in launchd_job.MONITORED_JOBS


def test_scheduled_job_health_detects_stale_frequent_job(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    label = "com.kamandal.v2.unified_lifecycle_management"
    log_path = log_dir / f"{label}.out.log"
    log_path.write_text(launchd_job.RESULT_PREFIX + json.dumps({"job": "unified-lifecycle-management", "status": "ok"}) + "\n")
    stale = datetime(2026, 6, 30, 9, 0, tzinfo=launchd_job.CENTRAL).timestamp()
    os.utime(log_path, (stale, stale))

    report = launchd_job.scheduled_job_health(
        repo_root=tmp_path,
        log_dir=log_dir,
        launchd_dir=tmp_path / "LaunchAgents",
        label_prefix="com.kamandal.v2",
        now=datetime(2026, 6, 30, 10, 0, tzinfo=launchd_job.CENTRAL),
    )

    management = [issue for issue in report["issues"] if issue["job"] == "unified-lifecycle-management"][0]
    assert management["reason"] == "stale_last_run"


def test_expected_job_observation_suppresses_weekend_fixed_time_job() -> None:
    schedule = launchd_job.JOB_SCHEDULES["earnings"]

    expectation = launchd_job.expected_job_observation(
        schedule,
        now=datetime(2026, 7, 11, 9, 0, tzinfo=launchd_job.CENTRAL),
        grace_minutes=20,
    )

    assert expectation == {"status": "not_expected_today", "reason": "non_trading_day"}


def test_expected_job_observation_suppresses_weekend_cadence_job() -> None:
    schedule = launchd_job.JOB_SCHEDULES["unified-lifecycle-management"]

    expectation = launchd_job.expected_job_observation(
        schedule,
        now=datetime(2026, 7, 11, 14, 0, tzinfo=launchd_job.CENTRAL),
        grace_minutes=20,
    )

    assert expectation == {"status": "not_expected_today", "reason": "non_trading_day"}


def test_combined_management_schedule_uses_final_pre_close_run() -> None:
    schedule = launchd_job.JOB_SCHEDULES["unified-lifecycle-management"]

    expectation = launchd_job.expected_job_observation(
        schedule,
        now=datetime(2026, 7, 24, 15, 26, tzinfo=launchd_job.CENTRAL),
        grace_minutes=20,
    )

    assert expectation["status"] == "due"
    assert expectation["expected_by"].startswith("2026-07-24T15:15:00")


def test_expected_job_observation_suppresses_market_holiday(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("KAMANDAL_MARKET_HOLIDAY_CALENDAR", raising=False)
    schedule = launchd_job.JOB_SCHEDULES["earnings"]

    expectation = launchd_job.expected_job_observation(
        schedule,
        now=datetime(2026, 7, 3, 9, 0, tzinfo=launchd_job.CENTRAL),
        grace_minutes=20,
    )

    assert expectation == {"status": "not_expected_today", "reason": "non_trading_day"}


def test_expected_job_observation_honors_disabled_holiday_calendar(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("KAMANDAL_MARKET_HOLIDAY_CALENDAR", "off")
    schedule = launchd_job.JOB_SCHEDULES["earnings"]

    expectation = launchd_job.expected_job_observation(
        schedule,
        now=datetime(2026, 7, 3, 9, 0, tzinfo=launchd_job.CENTRAL),
        grace_minutes=20,
    )

    assert expectation["status"] == "due"


def test_scheduled_job_health_accepts_recent_frequent_job(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(launchd_job, "MONITORED_JOBS", ["unified-lifecycle-management"])
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    label = "com.kamandal.v2.unified_lifecycle_management"
    log_path = log_dir / f"{label}.out.log"
    log_path.write_text(launchd_job.RESULT_PREFIX + json.dumps({"job": "unified-lifecycle-management", "status": "ok"}) + "\n")
    recent = datetime(2026, 6, 30, 9, 55, tzinfo=launchd_job.CENTRAL).timestamp()
    os.utime(log_path, (recent, recent))

    report = launchd_job.scheduled_job_health(
        repo_root=tmp_path,
        log_dir=log_dir,
        launchd_dir=tmp_path / "LaunchAgents",
        label_prefix="com.kamandal.v2",
        now=datetime(2026, 6, 30, 10, 0, tzinfo=launchd_job.CENTRAL),
    )

    assert report["issues"] == []


def test_scheduled_job_health_suppresses_missing_log_when_installed_after_due(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(launchd_job, "MONITORED_JOBS", ["x-bookmarks"])
    log_dir = tmp_path / "logs"
    launchd_dir = tmp_path / "LaunchAgents"
    log_dir.mkdir()
    launchd_dir.mkdir()
    plist = launchd_dir / "com.kamandal.v2.x_bookmarks.plist"
    plist.write_text("plist")
    installed_after_due = datetime(2026, 6, 30, 15, 0, tzinfo=launchd_job.CENTRAL).timestamp()
    os.utime(plist, (installed_after_due, installed_after_due))

    report = launchd_job.scheduled_job_health(
        repo_root=tmp_path,
        log_dir=log_dir,
        launchd_dir=launchd_dir,
        label_prefix="com.kamandal.v2",
        now=datetime(2026, 6, 30, 16, 0, tzinfo=launchd_job.CENTRAL),
    )

    assert report["issues"] == []


def test_scheduled_job_health_ignores_pre_activation_stale_log(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(launchd_job, "MONITORED_JOBS", ["x-bookmarks"])
    log_dir = tmp_path / "logs"
    launchd_dir = tmp_path / "LaunchAgents"
    log_dir.mkdir()
    launchd_dir.mkdir()
    label = "com.kamandal.v2.x_bookmarks"
    log_path = log_dir / f"{label}.out.log"
    log_path.write_text(
        launchd_job.RESULT_PREFIX + json.dumps({"job": "x-bookmarks", "status": "ok"}) + "\n"
    )
    prior_run = datetime(2026, 6, 30, 11, 45, tzinfo=launchd_job.CENTRAL).timestamp()
    os.utime(log_path, (prior_run, prior_run))
    plist = launchd_dir / f"{label}.plist"
    plist.write_text("plist")
    activated_after_last_tick = datetime(2026, 6, 30, 14, 21, tzinfo=launchd_job.CENTRAL).timestamp()
    os.utime(plist, (activated_after_last_tick, activated_after_last_tick))

    report = launchd_job.scheduled_job_health(
        repo_root=tmp_path,
        log_dir=log_dir,
        launchd_dir=launchd_dir,
        label_prefix="com.kamandal.v2",
        now=datetime(2026, 6, 30, 14, 30, tzinfo=launchd_job.CENTRAL),
    )

    assert report["issues"] == []


def test_scheduled_job_health_accepts_newer_x_bookmarks_artifact(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(launchd_job, "MONITORED_JOBS", ["x-bookmarks"])
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    label = "com.kamandal.v2.x_bookmarks"
    log_path = log_dir / f"{label}.out.log"
    log_path.write_text(launchd_job.RESULT_PREFIX + json.dumps({"job": "x-bookmarks", "status": "failed"}) + "\n")
    failed_at = datetime(2026, 7, 2, 8, 55, tzinfo=launchd_job.CENTRAL).timestamp()
    os.utime(log_path, (failed_at, failed_at))
    artifact = tmp_path / "data" / "digest" / "x_bookmarks" / "2026-07-02" / "llm" / "2026-07-02_llm_raw.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('[{"ideas": []}]', encoding="utf-8")
    healed_at = datetime(2026, 7, 2, 15, 6, tzinfo=launchd_job.CENTRAL).timestamp()
    os.utime(artifact, (healed_at, healed_at))

    report = launchd_job.scheduled_job_health(
        repo_root=tmp_path,
        log_dir=log_dir,
        launchd_dir=tmp_path / "LaunchAgents",
        label_prefix="com.kamandal.v2",
        now=datetime(2026, 7, 2, 15, 30, tzinfo=launchd_job.CENTRAL),
    )

    assert report["issues"] == []
    last = report["jobs"][0]["last"]
    assert last["status"] == "ok"
    assert last["success_source"] == "artifact"
    assert last["previous_status"] == "failed"
    assert last["artifact_path"] == str(artifact)


def test_scheduled_job_health_keeps_newer_x_bookmarks_failure(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(launchd_job, "MONITORED_JOBS", ["x-bookmarks"])
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    artifact = tmp_path / "data" / "digest" / "x_bookmarks" / "2026-07-02" / "llm" / "2026-07-02_llm_raw.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('[{"ideas": []}]', encoding="utf-8")
    old_success_at = datetime(2026, 7, 2, 14, 5, tzinfo=launchd_job.CENTRAL).timestamp()
    os.utime(artifact, (old_success_at, old_success_at))
    label = "com.kamandal.v2.x_bookmarks"
    log_path = log_dir / f"{label}.out.log"
    log_path.write_text(launchd_job.RESULT_PREFIX + json.dumps({"job": "x-bookmarks", "status": "failed"}) + "\n")
    failed_at = datetime(2026, 7, 2, 14, 10, tzinfo=launchd_job.CENTRAL).timestamp()
    os.utime(log_path, (failed_at, failed_at))

    report = launchd_job.scheduled_job_health(
        repo_root=tmp_path,
        log_dir=log_dir,
        launchd_dir=tmp_path / "LaunchAgents",
        label_prefix="com.kamandal.v2",
        now=datetime(2026, 7, 2, 15, 30, tzinfo=launchd_job.CENTRAL),
    )

    assert report["issues"] == [{"job": "x-bookmarks", "reason": "last_run_failed", "detail": str(log_path)}]
