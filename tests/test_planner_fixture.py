import json

from kamandal_v2.config import load_control
from kamandal_v2.planner.engine import run_plan, run_shadow_cycle
from kamandal_v2.stores.audit import AuditWriter
from kamandal_v2.stores.sqlite import LocalStore


SAMPLE_IDEAS = "tests/fixtures/sample_ideas.yaml"


def _run(tmp_path):
    return run_plan(
        load_control(),
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )


def test_tsla_call_calendar_idea_builds_valid_candidates(tmp_path) -> None:
    result = _run(tmp_path)
    tsla_candidates = [candidate for candidate in result.candidates if candidate.underlying == "TSLA"]

    assert tsla_candidates
    assert {candidate.structure for candidate in tsla_candidates} == {"call_calendar"}
    assert all(candidate.eligible for candidate in tsla_candidates)


def test_nvda_strangle_idea_is_rejected_until_enabled(tmp_path) -> None:
    result = _run(tmp_path)

    assert all(candidate.underlying != "NVDA" for candidate in result.candidates)


def test_mixed_fixture_produces_ranked_plan_bundles_with_guardrails(tmp_path) -> None:
    control = load_control()
    result = run_plan(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert result.plans
    top_plan = result.plans[0]
    assert top_plan.plan_rank == 1
    assert top_plan.operator_action == "approve"
    assert len(top_plan.candidates) <= control["portfolio"]["max_positions"]
    assert top_plan.bpr_utilization_pct <= control["portfolio"]["hard_max_bpr_utilization_pct"]
    assert all(
        (bpr / top_plan.portfolio_before.account_size) * 100 <= control["portfolio"]["max_bpr_per_underlying_pct"]
        for bpr in top_plan.portfolio_after.per_underlying_bpr.values()
    )
    assert len({candidate.underlying for candidate in top_plan.candidates}) == len(top_plan.candidates)


def test_shadow_cycle_creates_auto_approval_audit(tmp_path) -> None:
    result = run_shadow_cycle(
        load_control(),
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert result.plans
    assert result.plans[0].operator_action == "approve"


def test_daily_plan_rows_include_json_drilldown(tmp_path) -> None:
    result = _run(tmp_path)
    row = result.daily_plan_rows[0]
    bundle = json.loads(row[8])
    metrics = json.loads(row[27])
    detail = json.loads(row[28])

    assert bundle
    assert "change" in metrics
    assert detail["plan_id"] == result.plans[0].plan_id
    assert detail["candidates"]


def test_bearish_thesis_can_choose_playbook_without_strategy_hint(tmp_path) -> None:
    idea_file = tmp_path / "ideas.yaml"
    idea_file.write_text(
        """
ideas:
  - idea_id: qqq_overextended
    source: test
    underlying: QQQ
    direction: bearish
    strategy_hint: ""
    thesis_tags: [overextended]
    horizon_days: 14
    operator_status: approved
""",
        encoding="utf-8",
    )

    result = run_plan(
        load_control(),
        idea_paths=[idea_file],
        config_source="seed",
        provider="fixture",
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert result.candidates
    assert result.plans
    assert any(candidate.structure == "call_spread" for candidate in result.candidates)


def test_plan_audit_includes_idea_match_diagnostics(tmp_path) -> None:
    idea_file = tmp_path / "ideas.yaml"
    idea_file.write_text(
        """
ideas:
  - idea_id: qqq_short_horizon
    source: test
    underlying: QQQ
    direction: bullish
    strategy_hint: ""
    thesis_tags: [momentum, breakout]
    horizon_days: 7
    operator_status: approved
""",
        encoding="utf-8",
    )

    result = run_plan(
        load_control(),
        idea_paths=[idea_file],
        config_source="seed",
        provider="fixture",
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert "ideas_without_playbook_match" in result.metrics
    diagnostic = result.idea_diagnostics[0]
    assert diagnostic["idea_id"] == "qqq_short_horizon"
    assert diagnostic["status"] in {"matched_playbooks", "no_playbook_match"}
    assert diagnostic["summary"]
    assert diagnostic["reason_counts"]
