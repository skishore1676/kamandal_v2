from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

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
    args = SimpleNamespace(job="iv", force=False, alert_mode="spool", alert_profile="jarvis-northstar")
    completed = subprocess.CompletedProcess(["run"], 9, stdout="bad stdout", stderr="bad stderr")
    monkeypatch.setattr(launchd_job, "run_script", lambda _script, repo_root, force: completed)
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


def test_live_health_report_job_sends_lathi_alert(monkeypatch, capsys) -> None:  # noqa: ANN001
    args = SimpleNamespace(job="live-health-report", alert_mode="spool", alert_profile="jarvis-northstar")
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
    monkeypatch.setattr(
        launchd_job,
        "send_lathi_alert",
        lambda **_kwargs: AlertResult(attempted=True, ok=True, mode="spool"),
    )

    code = launchd_job.live_health_report_job(args)

    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out.split("=", 1)[1])
    assert payload["health"] == "GREEN"
    assert payload["alert"]["ok"] is True

