from __future__ import annotations

import json

import pytest

from kamandal_v2.ops.stage_receipt import reconciliation_stage


def test_stage_receipt_records_completed_stage(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "latest.json"
    monkeypatch.setenv("KAMANDAL_STAGE_RECEIPT_PATH", str(path))
    monkeypatch.setenv("KAMANDAL_STAGE_RUN_ID", "recon-test-1")

    with reconciliation_stage("broker_positions"):
        pass

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == "kamandal.stage_receipt.v1"
    assert payload["run_id"] == "recon-test-1"
    assert payload["current_stage"] == "broker_positions"
    assert payload["stages"][-1]["status"] == "completed"


def test_stage_receipt_records_python_failure(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "latest.json"
    monkeypatch.setenv("KAMANDAL_STAGE_RECEIPT_PATH", str(path))
    monkeypatch.setenv("KAMANDAL_STAGE_RUN_ID", "recon-test-2")

    with pytest.raises(RuntimeError, match="sheet stalled"):
        with reconciliation_stage("daily_plan_sheet"):
            raise RuntimeError("sheet stalled")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["current_stage"] == "daily_plan_sheet"
    assert payload["stages"][-1]["error"] == "RuntimeError: sheet stalled"
