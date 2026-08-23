from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from kamandal_v2 import cli
from kamandal_v2.experiment_status import (
    STATUS_SCHEMA,
    build_experiment_status,
    build_experiment_status_from_paths,
    validate_experiment_status,
)
from kamandal_v2.strategy_lanes.reports import (
    ScorecardWriteResult,
    WeeklyEconomicsWriteResult,
    write_csa_experiment_status,
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
            "open_bpr_usd": 500.0,
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
    assert experiment["metrics"]["closed_bpr_usd"] == 1_000.0
    assert experiment["metrics"]["open_bpr_usd"] == 500.0
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


def test_status_projection_does_not_treat_recovered_run_errors_as_current_failure() -> None:
    card = _card("2026-08-12")
    card["run_errors"] = ["historical quote timeout"]
    card["runtime_status"] = "GREEN"
    card["active_run_errors"] = []

    packet = build_experiment_status(
        [card],
        economics=_economics(),
        as_of="2026-08-12",
    )

    assert packet["experiments"][0]["health"] == "ready_for_review"
    assert "data_quality_issue" not in packet["experiments"][0]["limitations"]


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


def test_status_projection_does_not_promote_historical_baseline_rows_to_experiments(tmp_path: Path) -> None:
    report_dir = tmp_path / "data" / "reports" / "csa1"
    report_dir.mkdir(parents=True)
    policy_dir = tmp_path / "data" / "run" / "strategy_policy"
    policy_dir.mkdir(parents=True)
    policy = {
        "schema": "kamandal.strategy_policy_snapshot.v1",
        "trading_date": "2026-08-12",
        "snapshot_hash": "snapshot-1",
        "tables": {"playbooks": [
            {"playbook_id": "short_strangle_high_iv", "csa_stage": "shadow"},
            {"playbook_id": "legacy_baseline", "csa_stage": "baseline"},
        ]},
    }
    (policy_dir / "strategy_policy_2026-08-12.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    current = _card("2026-08-12")
    historical_baseline = _card("2026-08-11")
    historical_baseline["experiments"][0]["experiment_id"] = "legacy_baseline"
    historical_baseline["experiments"][0]["playbook_id"] = "legacy_baseline"
    historical_baseline["experiments"][0]["stage"] = "shadow"
    for card in (historical_baseline, current):
        (report_dir / f"csa1_scorecard_{card['trading_date']}.json").write_text(
            json.dumps(card), encoding="utf-8"
        )

    packet = build_experiment_status_from_paths(
        database=tmp_path / "unused.db",
        report_dir=report_dir,
        through="2026-08-12",
    )

    assert [row["experiment_id"] for row in packet["experiments"]] == [
        "short_strangle_high_iv"
    ]
    assert packet["experiments"][0]["stage"] == "shadow"
    assert packet["provenance"]["policy"]["snapshot_hash"] == "snapshot-1"


def test_current_policy_mode_wins_over_legacy_csa_stage(tmp_path: Path) -> None:
    report_dir = tmp_path / "data" / "reports" / "csa1"
    report_dir.mkdir(parents=True)
    policy_dir = tmp_path / "data" / "run" / "strategy_policy"
    policy_dir.mkdir(parents=True)
    policy = {
        "schema": "kamandal.strategy_policy_snapshot.v1",
        "trading_date": "2026-08-21",
        "snapshot_hash": "snapshot-mode-authority",
        "tables": {"playbooks": [{
            "playbook_id": "short_strangle_high_iv",
            "enabled": "TRUE",
            "mode": "live",
            "csa_stage": "shadow",
        }, {
            "playbook_id": "earnings_calendar_directional",
            "enabled": "TRUE",
            "mode": "live",
            "csa_stage": "baseline",
        }, {
            "playbook_id": "disabled_strategy",
            "enabled": "FALSE",
            "mode": "live",
            "csa_stage": "baseline",
        }]},
    }
    policy["tables"]["playbooks"][0]["notes"] = "new operator explanation"
    prior_policy = json.loads(json.dumps(policy))
    prior_policy["trading_date"] = "2026-08-20"
    prior_policy["snapshot_hash"] = "snapshot-prior"
    prior_policy["tables"]["playbooks"][0]["notes"] = "old operator explanation"
    prior_policy["tables"]["playbooks"][0]["csa_stage"] = "baseline"
    (policy_dir / "strategy_policy_2026-08-20.json").write_text(
        json.dumps(prior_policy), encoding="utf-8"
    )
    (policy_dir / "strategy_policy_2026-08-21.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    card = _card("2026-08-21", stage="live")
    prior_card = _card("2026-08-20", stage="live")
    (report_dir / "csa1_scorecard_2026-08-20.json").write_text(
        json.dumps(prior_card), encoding="utf-8"
    )
    (report_dir / "csa1_scorecard_2026-08-21.json").write_text(
        json.dumps(card), encoding="utf-8"
    )

    packet = build_experiment_status_from_paths(
        database=tmp_path / "unused.db",
        report_dir=report_dir,
        through="2026-08-21",
    )

    by_id = {row["experiment_id"]: row for row in packet["experiments"]}
    assert set(by_id) == {
        "earnings_calendar_directional",
        "short_strangle_high_iv",
    }
    assert by_id["short_strangle_high_iv"]["stage"] == "live"
    assert by_id["short_strangle_high_iv"]["observations"] == 2
    assert "ambiguous_evidence" not in by_id["short_strangle_high_iv"]["limitations"]
    assert by_id["earnings_calendar_directional"]["observations"] == 2
    assert by_id["earnings_calendar_directional"]["health"] == "collecting"
    assert packet["generated_at"].endswith("+00:00")


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


def test_scorecard_status_writer_is_atomic_and_schema_valid(tmp_path: Path, monkeypatch) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "csa1_scorecard_2026-08-12.json").write_text(
        json.dumps(_card("2026-08-12")), encoding="utf-8"
    )
    (report_dir / "csa1_weekly_economics_2026-08-12.json").write_text(
        json.dumps(_economics()), encoding="utf-8"
    )

    written = write_csa_experiment_status(
        sqlite_path=tmp_path / "unused.db",
        output_dir=report_dir,
        through_date="2026-08-12",
    )
    payload = json.loads(written.json_path.read_text(encoding="utf-8"))
    assert written.json_path.name == "csa1_experiment_status_2026-08-12.json"
    assert payload["schema"] == STATUS_SCHEMA
    assert payload["experiments"][0]["experiment_id"] == "short_strangle_high_iv"
    assert not list(report_dir.glob("*.tmp"))

    prior = written.json_path.read_text(encoding="utf-8")
    def fail_builder(**_kwargs):
        raise RuntimeError("status build failed")

    monkeypatch.setattr(
        "kamandal_v2.experiment_status.build_experiment_status_from_paths",
        fail_builder,
    )
    try:
        write_csa_experiment_status(
            sqlite_path=tmp_path / "unused.db",
            output_dir=report_dir,
            through_date="2026-08-12",
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("status build failure should propagate")
    assert written.json_path.read_text(encoding="utf-8") == prior


def test_scorecard_command_emits_status_without_a_second_command(tmp_path: Path, monkeypatch, capsys) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    scorecard = _card("2026-08-12")
    economics = _economics()
    scorecard_path = report_dir / "csa1_scorecard_2026-08-12.json"
    economics_path = report_dir / "csa1_weekly_economics_2026-08-12.json"

    def fake_scorecard(*_args, **_kwargs):
        scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
        return ScorecardWriteResult(scorecard, scorecard_path, scorecard_path, scorecard_path)

    def fake_economics(*_args, **_kwargs):
        economics_path.write_text(json.dumps(economics), encoding="utf-8")
        return WeeklyEconomicsWriteResult(economics, economics_path, economics_path, economics_path)

    from kamandal_v2.strategy_lanes import reports as reports_module

    monkeypatch.setattr(cli, "load_control", lambda: {})
    monkeypatch.setattr(reports_module, "write_csa_scorecard", fake_scorecard)
    monkeypatch.setattr(reports_module, "write_csa_weekly_economics", fake_economics)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "kamandal",
            "csa-shadow-scorecard",
            "--db",
            str(tmp_path / "unused.db"),
            "--output-dir",
            str(report_dir),
            "--trading-date",
            "2026-08-12",
        ],
    )

    cli.main()
    output = json.loads(capsys.readouterr().out)
    status_path = Path(output["experiment_status_json_path"])
    assert status_path.exists()
    assert output["experiment_status"]["schema"] == STATUS_SCHEMA
    assert json.loads(status_path.read_text(encoding="utf-8"))["as_of"] == "2026-08-12"
