import json
import sqlite3

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


def test_total_position_cap_includes_open_shadow_positions(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    control = load_control()
    control["runtime"]["mode"] = "shadow"
    control["portfolio"]["max_positions"] = 1
    control["shadow"] = {
        "account_size_override": 20_000,
        "buying_power_override": 20_000,
        "bpr_used_override": 0,
        "max_positions_override": "",
        "candidate_filter_mode": "warn",
    }

    first = run_shadow_cycle(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )
    second = run_plan(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        store=store,
        audit=AuditWriter(tmp_path / "audit2"),
    )

    assert first.plans
    assert second.plans == []
    assert second.metrics["candidates_eligible"] > 0


def test_daily_plan_write_is_preserved_when_no_eligible_plans(tmp_path, monkeypatch) -> None:
    from kamandal_v2.planner import engine

    store = LocalStore(tmp_path / "kamandal.db")
    control = load_control()
    control["runtime"]["mode"] = "shadow"
    control["portfolio"]["max_positions"] = 0
    control["shadow"] = {
        "account_size_override": 20_000,
        "buying_power_override": 20_000,
        "bpr_used_override": 0,
        "max_positions_override": "",
        "candidate_filter_mode": "warn",
    }
    calls = []
    monkeypatch.setattr(engine, "write_daily_plan", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = engine.run_plan(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        write_sheet=True,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert result.plans == []
    assert calls == []


def test_shadow_position_cap_override_allows_exploration(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    control = load_control()
    control["runtime"]["mode"] = "shadow"
    control["portfolio"]["max_positions"] = 1
    control["shadow"] = {
        "account_size_override": 20_000,
        "buying_power_override": 20_000,
        "bpr_used_override": 0,
        "max_positions_override": 20,
        "candidate_filter_mode": "warn",
    }

    run_shadow_cycle(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )
    second = run_plan(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        store=store,
        audit=AuditWriter(tmp_path / "audit2"),
    )

    assert second.plans


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


def test_shadow_uses_paper_account_override(tmp_path) -> None:
    control = load_control()
    control["shadow"] = {
        "account_size_override": 20_000,
        "buying_power_override": 20_000,
        "bpr_used_override": 0,
        "candidate_filter_mode": "warn",
    }

    result = run_plan(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert result.metrics["account_size_raw"] == 5_000
    assert result.metrics["account_size_effective"] == 20_000
    assert result.plans[0].portfolio_before.account_size == 20_000


def test_shadow_cycle_accumulates_open_fills_into_portfolio(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    control = load_control()
    control["shadow"] = {
        "account_size_override": 20_000,
        "buying_power_override": 20_000,
        "bpr_used_override": 0,
        "max_positions_override": 1,
        "idea_cooldown_days": 1,
        "candidate_filter_mode": "warn",
    }

    first = run_shadow_cycle(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )
    control["shadow"]["max_positions_override"] = 2
    second = run_plan(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        store=store,
        audit=AuditWriter(tmp_path / "audit2"),
    )

    assert first.plans[0].portfolio_after.positions_count == 1
    assert second.plans[0].portfolio_before.positions_count == 1
    assert second.plans[0].portfolio_before.bpr_used == first.plans[0].total_bpr
    assert any(candidate.rejection_reason == "shadow_idea_already_open" for candidate in second.candidates)
    with sqlite3.connect(tmp_path / "kamandal.db") as conn:
        assert conn.execute("SELECT count(*) FROM shadow_fills WHERE status = 'open'").fetchone()[0] == 1
        row = conn.execute(
            "SELECT idea_id, playbook_id, net_credit, estimated_bpr, delta, theta FROM shadow_fills WHERE status = 'open'"
        ).fetchone()
        assert row[0]
        assert row[1]
        assert row[2] is not None
        assert row[3] is not None
        assert row[4] is not None
        assert row[5] is not None


def test_shadow_cycle_blocks_same_day_reentry_after_close(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    control = load_control()
    control["shadow"] = {
        "account_size_override": 20_000,
        "buying_power_override": 20_000,
        "bpr_used_override": 0,
        "idea_cooldown_days": 1,
        "candidate_filter_mode": "warn",
    }

    first = run_shadow_cycle(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )
    fill_id = f"{first.plan_run_id}:{first.plans[0].candidates[0].candidate_id}"
    store.close_shadow_fill(fill_id, reason="profit_target", pnl=50.0, payload={"test": True})

    second = run_plan(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        store=store,
        audit=AuditWriter(tmp_path / "audit2"),
    )

    first_idea_id = first.plans[0].candidates[0].idea_id
    reentry_candidates = [candidate for candidate in second.candidates if candidate.idea_id == first_idea_id]
    assert reentry_candidates
    assert all(candidate.rejection_reason == "shadow_idea_already_traded_today" for candidate in reentry_candidates)


def test_shadow_idea_cooldown_can_be_disabled(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    control = load_control()
    control["shadow"] = {
        "account_size_override": 20_000,
        "buying_power_override": 20_000,
        "bpr_used_override": 0,
        "idea_cooldown_days": 0,
        "candidate_filter_mode": "warn",
    }

    first = run_shadow_cycle(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )
    fill_id = f"{first.plan_run_id}:{first.plans[0].candidates[0].candidate_id}"
    store.close_shadow_fill(fill_id, reason="profit_target", pnl=50.0, payload={"test": True})

    second = run_plan(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        store=store,
        audit=AuditWriter(tmp_path / "audit2"),
    )

    first_idea_id = first.plans[0].candidates[0].idea_id
    reentry_candidates = [candidate for candidate in second.candidates if candidate.idea_id == first_idea_id]
    assert reentry_candidates
    assert not any(candidate.rejection_reason == "shadow_idea_already_traded_today" for candidate in reentry_candidates)


def test_open_shadow_ideas_backfills_legacy_fills_from_candidates(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    control = load_control()
    first = run_shadow_cycle(
        control,
        idea_paths=[SAMPLE_IDEAS],
        config_source="seed",
        provider="fixture",
        write_sheet=False,
        store=store,
        audit=AuditWriter(tmp_path / "audit"),
    )
    candidate = first.plans[0].candidates[0]
    with sqlite3.connect(tmp_path / "kamandal.db") as conn:
        fill_payload = json.loads(conn.execute(
            "SELECT payload FROM shadow_fills WHERE candidate_id = ?",
            (candidate.candidate_id,),
        ).fetchone()[0])
        fill_payload.pop("idea_id", None)
        conn.execute(
            "UPDATE shadow_fills SET payload = ? WHERE candidate_id = ?",
            (json.dumps(fill_payload), candidate.candidate_id),
        )

    assert candidate.idea_id in store.open_shadow_idea_ids()


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


def test_rejection_summary_groups_zero_match_ideas(tmp_path) -> None:
    idea_file = tmp_path / "ideas.yaml"
    idea_file.write_text(
        """
ideas:
  - idea_id: xyz_unknown
    source: test
    underlying: XYZ
    direction: bullish
    thesis_tags: [momentum]
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

    assert result.rejection_summary == [
        "1 XYZ: no playbook match - No enabled universe entry for underlying."
    ]
