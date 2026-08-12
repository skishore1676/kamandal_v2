from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from kamandal_v2.experiment_status import (
    STATUS_SCHEMA,
    build_experiment_status,
    build_experiment_status_from_paths,
    validate_experiment_status,
)


def _card(day: str, *, stage: str = "shadow") -> dict[str, object]:
    return {
        "schema": "kamandal.strategy_experiment_evidence.v1",
        "trading_date": day,
        "runs": 1,
        "evidence_status": "COLLECTING",
        "run_errors": [],
        "zero_unexpected_broker_effect": True,
        "experiments": [{
            "experiment_id": "short_strangle_high_iv",
            "stage": stage,
            "policy_hashes": ["policy-hash-1"],
            "opportunities": 2,
            "fills": {"filled": 1},
            "live_intents": {},
            "unexpected_broker_effects": 0,
        }],
    }


def _economics(*, closed: int = 2, pnl: float = 80.0) -> dict[str, object]:
    return {
        "schema": "kamandal.strategy_weekly_economics.v1",
        "economic_rows": [{
            "playbook_id": "short_strangle_high_iv",
            "stage": "shadow",
            "closed_in_period": closed,
            "realized_pnl_usd": pnl,
            "open_unrealized_pnl_usd": 0.0,
            "total_pnl_usd": pnl,
            "closed_bpr_usd": 1_000.0,
            "economic_status": "observed",
            "quality_issues": [],
        }],
    }


def test_status_projection_adapts_existing_facts_without_authority() -> None:
    packet = build_experiment_status(
        [_card("2026-08-10"), _card("2026-08-11"), _card("2026-08-12")],
        economics=_economics(),
        as_of="2026-08-12",
    )
    validate_experiment_status(packet)
    experiment = packet["experiments"][0]

    assert packet["schema"] == STATUS_SCHEMA
    assert packet["app"] == "kamandal"
    assert experiment["observations"] == 3
    assert experiment["opportunities"] == 6
    assert experiment["entries"] == 3
    assert experiment["closed"] == 2
    assert experiment["metrics"]["closed_net_r"] == 0.08
    assert experiment["health"] == "ready_for_review"
    assert packet["effects"] == {
        "sheet_write": False,
        "stage_change": False,
        "broker_action": False,
        "order_action": False,
    }


def test_status_projection_marks_missing_economics_inconclusive() -> None:
    packet = build_experiment_status(
        [_card("2026-08-12")],
        economics=None,
        source_status="partial",
        as_of="2026-08-12",
    )
    experiment = packet["experiments"][0]

    assert experiment["health"] == "inconclusive"
    assert "missing_evidence" in experiment["limitations"]
    assert "partial_source" in experiment["limitations"]


def test_status_command_reads_existing_reports_without_recomputing_or_writing(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "csa1_scorecard_2026-08-12.json").write_text(
        json.dumps(_card("2026-08-12")), encoding="utf-8"
    )
    economics = _economics()
    economics.update({
        "recommendation_authority": False,
        "sheet_write_authority": False,
        "execution_authority": False,
        "alpha_claim_authority": False,
        "through": "2026-08-12",
    })
    (report_dir / "csa1_weekly_economics_2026-08-12.json").write_text(
        json.dumps(economics), encoding="utf-8"
    )

    packet = build_experiment_status_from_paths(
        database=tmp_path / "unused.db",
        report_dir=report_dir,
        through="2026-08-12",
    )

    assert packet["source_status"] == "ok"
    assert packet["experiments"][0]["closed"] == 2
    assert packet["experiments"][0]["health"] == "ready_for_review"
    assert not (tmp_path / "unused.db").exists()


def test_status_command_is_read_only_and_does_not_import_legacy_cli(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"
    report_dir = tmp_path / "reports"
    code = (
        "import json, sys; "
        "from kamandal_v2.entrypoint import main; "
        f"rc = main(['experiment-status', '--db', {str(database)!r}, '--report-dir', {str(report_dir)!r}, '--through', '2026-08-12']); "
        "assert rc == 0; assert 'kamandal_v2.cli' not in sys.modules"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["source_status"] == "unavailable"
    assert not database.exists()
    assert not report_dir.exists()
