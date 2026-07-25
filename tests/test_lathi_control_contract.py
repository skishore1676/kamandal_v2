from __future__ import annotations

import json
from pathlib import Path

from kamandal_v2.live.operator_review import create_operator_review_request
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.tools import launchd_control, launchd_status, review_queue


def _config() -> dict:
    return {
        "live": {
            "operator_review": {"enabled": True, "target": "123"},
            "telegram_approval": {"target": "123"},
        },
        "broker": {"active": "public"},
    }


def _review_request(store: LocalStore, *, request_id: str = "or_recon_1") -> dict:
    return create_operator_review_request(
        _config(),
        request_type="live_reconciliation",
        subject_id="issue_1",
        title="Review issue",
        summary="Broker/local mismatch needs a decision.",
        allowed_actions=["hold", "dismiss"],
        payload={"issue_id": "issue_1", "group_id": "group_1"},
        store=store,
        request_id=request_id,
    )


def test_review_queue_outputs_lathi_review_units(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    request = _review_request(store)

    payload = review_queue.build_review_queue(store=store)

    assert payload["schema"] == "kamandal.review_queue.v1"
    assert payload["counts"]["active"] == 1
    unit = payload["review_requests"][0]
    assert unit["unit_id"] == request["request_id"]
    assert unit["kind"] == "external_review_request"
    assert unit["lifecycle"] == "waiting_you"
    assert unit["risk_class"] == "trading_review"
    assert unit["subject_fingerprint"].startswith("sha256:")
    assert unit["available_actions"] == ["dismiss", "hold"]
    assert unit["action_requirements"]["dismiss"]["requires_confirmation"] is True


def test_launchd_status_outputs_units_without_broker_mutation(tmp_path: Path) -> None:
    db = tmp_path / "kamandal.db"
    repo = tmp_path / "repo"
    (repo / "data" / "logs" / "launchd").mkdir(parents=True)
    store = LocalStore(db)
    _review_request(store)

    payload = launchd_status.build_status(repo_root=repo, db_path=db, config=_config())

    assert payload["schema"] == "kamandal.launchd.status.v1"
    assert payload["source_id"] == "kamandal"
    assert payload["db_path"] == str(db)
    unit_ids = {unit["unit_id"] for unit in payload["units"]}
    assert "kamandal:live-health" in unit_ids
    assert "kamandal:review-queue" in unit_ids
    assert payload["review_queue"]["counts"]["active"] == 1
    units = {unit["unit_id"]: unit for unit in payload["units"]}
    youtube = units["com.kamandal.v2.youtube"]
    assert youtube["available_actions"] == ["retry-job"]
    assert youtube["action_requirements"]["retry-job"]["command_args"] == ["--job", "youtube"]
    assert youtube["action_requirements"]["retry-job"]["requires_confirmation"] is False


def test_launchd_status_keeps_alert_delivery_failure_out_of_stuck_lifecycle(tmp_path: Path) -> None:
    db = tmp_path / "kamandal.db"
    repo = tmp_path / "repo"
    log_dir = repo / "data" / "logs" / "launchd"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "com.kamandal.v2.live_health_report.out.log"
    log_path.write_text(
        "KAMANDAL_LAUNCHD_JOB="
        + json.dumps(
            {
                "job": "live-health-report",
                "status": "ok",
                "health": "RED",
                "delivery_status": "failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = launchd_status.build_status(repo_root=repo, db_path=db, config=_config())

    unit = next(item for item in payload["units"] if item["unit_id"] == "com.kamandal.v2.live_health_report")
    assert unit["lifecycle"] == "armed"
    assert unit["findings"] == ["alert_delivery_failed"]
    assert unit["operator_state"] == "self_healing"


def test_launchd_status_marks_prior_day_pending_entries_self_healed(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "kamandal.db"
    repo = tmp_path / "repo"
    (repo / "data" / "logs" / "launchd").mkdir(parents=True)
    store = LocalStore(db)
    store.save_live_order_intent(
        {
            "ticket_hash": "old-entry-ticket",
            "order_id": "old-entry-order",
            "plan_id": "old-plan",
            "candidate_id": "old-cand",
            "idea_id": "old-idea",
            "intent_type": "open",
            "underlying": "NVDA",
        },
        status="pending_approval",
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE live_order_intents SET updated_at = ?, created_at = ? WHERE ticket_hash = ?",
            ("2000-01-01 00:00:00", "2000-01-01 00:00:00", "old-entry-ticket"),
        )

    payload = launchd_status.build_status(repo_root=repo, db_path=db, config={"runtime": {"market_timezone": "America/Chicago"}})

    units = {unit["unit_id"]: unit for unit in payload["units"]}
    live_health = units["kamandal:live-health"]
    assert live_health["lifecycle"] == "idle"
    assert live_health["operator_state"] == "clear"
    assert live_health["findings"] == []
    assert live_health["self_healing"]["entry_approvals_retired"] == 1


def test_launchd_status_marks_cluster_cap_self_handled(tmp_path: Path) -> None:
    db = tmp_path / "kamandal.db"
    repo = tmp_path / "repo"
    (repo / "data" / "logs" / "launchd").mkdir(parents=True)
    store = LocalStore(db)
    store.save_live_position_group("group_nvda", {"group_id": "group_nvda", "underlying": "NVDA"})
    store.save_live_position_group("group_amd", {"group_id": "group_amd", "underlying": "AMD"})

    payload = launchd_status.build_status(
        repo_root=repo,
        db_path=db,
        config={
            "risk_manager": {
                "enabled": True,
                "max_positions_per_cluster": 2,
                "correlation_clusters": {"semis": ["NVDA", "AMD", "MRVL"]},
            }
        },
    )

    units = {unit["unit_id"]: unit for unit in payload["units"]}
    live_health = units["kamandal:live-health"]
    assert live_health["lifecycle"] == "idle"
    assert live_health["operator_state"] == "self_handled"
    assert live_health["findings"] == ["risk_cluster_at_cap"]
    assert live_health["finding_details"][0]["operator_state"] == "self_handled"


def test_launchd_status_keeps_self_healing_health_off_human_attention(tmp_path: Path) -> None:
    db = tmp_path / "kamandal.db"
    repo = tmp_path / "repo"
    (repo / "data" / "logs" / "launchd").mkdir(parents=True)
    store = LocalStore(db)
    store.save_live_position_group("group_target", {"group_id": "group_target", "underlying": "XLF"})
    store.record_live_position_mark(
        "group_target",
        {
            "underlying": "XLF",
            "pnl_mid": 24.0,
            "pnl_natural": 6.0,
            "target_profit": 23.6,
            "target_progress_pct": 101.7,
            "trigger_progress_pct": 95.0,
            "quote_fresh": True,
        },
    )

    payload = launchd_status.build_status(
        repo_root=repo,
        db_path=db,
        config={
            "live": {
                "exit_approval_mode": "auto_rules",
                "exit_pricing": {"profit_target_trigger_pct": 95, "min_profit_to_trigger": 5},
            }
        },
    )

    live_health = next(item for item in payload["units"] if item["unit_id"] == "kamandal:live-health")
    assert live_health["lifecycle"] == "running"
    assert live_health["operator_state"] == "self_healing"
    assert live_health["findings"] == ["position_target_reached"]


def test_launchd_control_applies_review_decision_with_fingerprint(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    db = tmp_path / "kamandal.db"
    store = LocalStore(db)
    request = _review_request(store)
    store.save_live_reconciliation_issue(
        {
            "issue_id": "issue_1",
            "issue_type": "quantity_mismatch",
            "group_id": "group_1",
            "underlying": "AMZN",
            "status": "open",
            "payload": {},
        }
    )
    monkeypatch.setenv("KAMANDAL_CONTROL_LOCK_DIR", str(tmp_path / "locks"))
    fingerprint = review_queue.subject_fingerprint(request)

    code = launchd_control.main(
        [
            "apply-review-decision",
            "--db",
            str(db),
            "--request-id",
            request["request_id"],
            "--action",
            "hold",
            "--source",
            "lathi",
            "--action-id",
            "act-1",
            "--subject-fingerprint",
            fingerprint,
            "--json",
        ]
    )

    assert code == 0
    stored = store.operator_review_request(request["request_id"])
    assert stored is not None
    assert stored["_ledger_status"] == "held"
    assert store.live_reconciliation_issue("issue_1")["status"] == "held"


def test_launchd_control_refuses_fingerprint_mismatch(tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    db = tmp_path / "kamandal.db"
    store = LocalStore(db)
    request = _review_request(store)
    monkeypatch.setenv("KAMANDAL_CONTROL_LOCK_DIR", str(tmp_path / "locks"))

    code = launchd_control.main(
        [
            "apply-review-decision",
            "--db",
            str(db),
            "--request-id",
            request["request_id"],
            "--action",
            "hold",
            "--subject-fingerprint",
            "sha256:not-current",
            "--json",
        ]
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["result_status"] == "fingerprint_mismatch"
    assert store.operator_review_request(request["request_id"])["_ledger_status"] == "pending"


def test_launchd_control_retries_allowed_job(tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    calls = []

    class FakeProcess:
        pid = 4242

    def fake_popen(command, **kwargs):  # noqa: ANN001
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr("kamandal_v2.tools.launchd_control.subprocess.Popen", fake_popen)
    monkeypatch.setenv("KAMANDAL_CONTROL_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("KAMANDAL_CONTROL_RETRY_TRIGGER_MODE", "detached")

    code = launchd_control.main([
        "retry-job",
        "--job",
        "x-bookmarks",
        "--repo-root",
        str(tmp_path),
        "--json",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["status"] == "triggered"
    assert payload["result_status"] == "detached_triggered"
    assert payload["payload"]["job"] == "x-bookmarks"
    assert payload["payload"]["pid"] == 4242
    assert calls[0][0][2:4] == ["kamandal_v2.tools.launchd_job", "x-bookmarks"]
    assert "--force" in calls[0][0]


def test_launchd_control_retry_job_lock_is_scoped_per_job(tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    """Regression: retry-job for one job must not collide with a lock held for another.

    Prior to this fix, the control lock key for retry-job was the bare command
    name ("retry-job") regardless of --job, so two concurrent retries for
    different jobs (e.g. x-bookmarks and youtube) fought over the same lock
    file. Tower action journal 2026-07-02 (tower-b9b5497c..., tower-43466a...)
    showed exactly this: one retry-job call got lock_busy while a second,
    unrelated retry-job call hung past its caller-side timeout.
    """
    calls = []

    class FakeProcess:
        pid = 4242

    def fake_popen(command, **kwargs):  # noqa: ANN001
        calls.append(command)
        return FakeProcess()

    monkeypatch.setattr("kamandal_v2.tools.launchd_control.subprocess.Popen", fake_popen)
    lock_dir = tmp_path / "locks"
    monkeypatch.setenv("KAMANDAL_CONTROL_LOCK_DIR", str(lock_dir))
    monkeypatch.setenv("KAMANDAL_CONTROL_RETRY_TRIGGER_MODE", "detached")

    # Simulate a lock already held for the youtube retry-job (as if another
    # in-flight call holds it) and confirm a concurrent x-bookmarks retry-job
    # is unaffected because the lock key is now scoped by --job.
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "retry-job-youtube.lock").write_text("{}", encoding="utf-8")

    code = launchd_control.main([
        "retry-job",
        "--job",
        "x-bookmarks",
        "--repo-root",
        str(tmp_path),
        "--json",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["status"] == "triggered"
    assert payload["payload"]["job"] == "x-bookmarks"
    assert len(calls) == 1

    # The youtube retry-job is still correctly refused as lock_busy.
    code = launchd_control.main([
        "retry-job",
        "--job",
        "youtube",
        "--repo-root",
        str(tmp_path),
        "--json",
    ])
    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "lock_busy"


def test_launchd_control_retry_job_requires_job(capsys) -> None:  # noqa: ANN001
    code = launchd_control.main(["retry-job", "--json"])

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["result_status"] == "missing_arguments"


def test_launchd_control_retry_job_refuses_unknown_job(capsys) -> None:  # noqa: ANN001
    code = launchd_control.main(["retry-job", "--job", "live-management", "--json"])

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["result_status"] == "job_not_retryable"
