from __future__ import annotations

import json
from pathlib import Path
import subprocess

from kamandal_v2.live.operator_review import create_operator_review_request
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.tools import launchd_control, launchd_status, review_queue
from kamandal_v2.tools.launchd_job import RESULT_PREFIX


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

    def fake_run(command, **kwargs):  # noqa: ANN001
        calls.append((command, kwargs))
        stdout = RESULT_PREFIX + json.dumps({"job": "x-bookmarks", "status": "ok", "return_code": 0}) + "\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("kamandal_v2.tools.launchd_control.subprocess.run", fake_run)
    monkeypatch.setenv("KAMANDAL_CONTROL_LOCK_DIR", str(tmp_path / "locks"))

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
    assert payload["payload"]["job"] == "x-bookmarks"
    assert payload["payload"]["runner_result"]["status"] == "ok"
    assert calls[0][0][2:4] == ["kamandal_v2.tools.launchd_job", "x-bookmarks"]
    assert "--force" in calls[0][0]


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
