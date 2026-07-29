from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kamandal_v2.domain.models import PortfolioState
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
    shadow = payload["shadow_evidence"]
    assert shadow["schema"] == "kamandal.shadow_evidence_status.v1"
    assert shadow["collector"]["state"] == "retired"
    assert shadow["evidence_state"] == "empty"
    assert shadow["alpha_eligible"] is False
    assert shadow["protected_effects"] == {
        "database_write": False,
        "broker_call": False,
        "order_submit": False,
        "schedule_change": False,
    }


def test_launchd_status_reports_retired_shadow_history_without_reclassifying_it(
    tmp_path: Path,
) -> None:
    import sqlite3

    db = tmp_path / "kamandal.db"
    repo = tmp_path / "repo"
    (repo / "data" / "logs" / "launchd").mkdir(parents=True)
    eod = repo / "data" / "reports" / "eod" / "2026-05-22_shadow_eod.json"
    eod.parent.mkdir(parents=True)
    eod.write_text("{}\n", encoding="utf-8")
    LocalStore(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO shadow_fills
            (id, plan_run_id, plan_id, candidate_id, underlying, structure,
             status, opened_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fill-1",
                "run-1",
                "plan-1",
                "candidate-1",
                "SPY",
                "put_spread",
                "open",
                "2026-06-12 03:25:11",
                "{}",
            ),
        )
        conn.execute(
            """
            INSERT INTO shadow_marks
            (id, marked_at, position_count, mid_pnl, natural_pnl, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("mark-1", "2026-06-12T14:30:00Z", 1, 0, 0, "{}"),
        )

    payload = launchd_status.build_status(
        repo_root=repo,
        db_path=db,
        config=_config(),
        now=datetime(2026, 7, 28, 20, tzinfo=UTC),
    )

    shadow = payload["shadow_evidence"]
    assert shadow["observed_at"] == "2026-07-28T20:00:00+00:00"
    assert shadow["collector"]["state"] == "retired"
    assert shadow["evidence_state"] == "historical_only"
    assert shadow["history"]["status_counts"] == {"open": 1}
    assert shadow["history"]["open_fills"] == 1
    assert shadow["history"]["last_fill_activity_at"] == "2026-06-12T03:25:11+00:00"
    assert shadow["history"]["last_mark_at"] == "2026-06-12T14:30:00+00:00"
    assert shadow["alpha_eligible"] is False
    assert shadow["findings"] == [
        "shadow_collection_retired",
        "historical_shadow_evidence_only",
        "legacy_open_shadow_fills_unmanaged",
    ]
    assert shadow["collector_hash"].startswith("sha256:")
    assert shadow["history_hash"].startswith("sha256:")


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


def _stale_snapshot_status(
    tmp_path: Path,
    *,
    now: datetime,
) -> dict:
    db = tmp_path / f"kamandal-{now:%Y%m%d%H%M}.db"
    repo = tmp_path / f"repo-{now:%Y%m%d%H%M}"
    (repo / "data" / "logs" / "launchd").mkdir(parents=True)
    store = LocalStore(db)
    store.save_account_snapshot(
        "run_20260724T194010Z",
        PortfolioState(
            account_size=12_000,
            buying_power=9_000,
            bpr_used=3_000,
            positions_count=3,
        ),
    )
    return launchd_status.build_status(
        repo_root=repo,
        db_path=db,
        config={
            "runtime": {"market_timezone": "America/Chicago"},
            "risk_manager": {
                "enabled": True,
                "max_account_snapshot_age_minutes": 1440,
            },
        },
        now=now,
    )


def test_launchd_status_defers_stale_snapshot_attention_on_non_trading_day(tmp_path: Path) -> None:
    payload = _stale_snapshot_status(
        tmp_path,
        now=datetime(2026, 7, 26, 12, 20, tzinfo=UTC),
    )

    live_health = next(item for item in payload["units"] if item["unit_id"] == "kamandal:live-health")
    event = live_health["finding_details"][0]
    assert payload["live_health"]["overall"] == "RED"
    assert payload["live_health"]["risk_manager"]["blocked"] is True
    assert live_health["lifecycle"] == "idle"
    assert live_health["operator_state"] == "self_handled"
    assert event["operator_state"] == "self_handled"
    assert event["attention_deferred_reason"] == "non_trading_day"


def test_launchd_status_defers_stale_snapshot_attention_on_market_holiday(tmp_path: Path) -> None:
    payload = _stale_snapshot_status(
        tmp_path,
        now=datetime(2026, 9, 7, 15, 0, tzinfo=UTC),
    )

    live_health = next(item for item in payload["units"] if item["unit_id"] == "kamandal:live-health")
    assert live_health["lifecycle"] == "idle"
    assert live_health["operator_state"] == "self_handled"
    assert live_health["finding_details"][0]["attention_deferred_reason"] == "non_trading_day"


def test_launchd_status_waits_for_first_snapshot_refresh_on_trading_day(tmp_path: Path) -> None:
    payload = _stale_snapshot_status(
        tmp_path,
        now=datetime(2026, 7, 27, 14, 30, tzinfo=UTC),  # 09:30 CT
    )

    live_health = next(item for item in payload["units"] if item["unit_id"] == "kamandal:live-health")
    event = live_health["finding_details"][0]
    assert live_health["lifecycle"] == "running"
    assert live_health["operator_state"] == "self_healing"
    assert event["attention_deferred_reason"] == "awaiting_first_account_snapshot_refresh"
    assert event["attention_actionable_after"] == "2026-07-27T09:45:00-05:00"


def test_launchd_status_escalates_stale_snapshot_after_refresh_grace(tmp_path: Path) -> None:
    payload = _stale_snapshot_status(
        tmp_path,
        now=datetime(2026, 7, 27, 14, 46, tzinfo=UTC),  # 09:46 CT
    )

    live_health = next(item for item in payload["units"] if item["unit_id"] == "kamandal:live-health")
    event = live_health["finding_details"][0]
    assert live_health["lifecycle"] == "stuck"
    assert live_health["operator_state"] == "operator_needed"
    assert event["operator_state"] == "operator_needed"


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
