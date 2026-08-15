from __future__ import annotations

import json

from kamandal_v2.config import load_control
from kamandal_v2.seed import build_seed_tables, seed_headers
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.planning import run_unified_books


def _rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    tables = build_seed_tables(load_control())
    headers = seed_headers()
    universe = [dict(zip(headers["universe"], [*row, *[""] * len(headers["universe"])])) for row in tables["universe"]]
    playbooks = [dict(zip(headers["playbooks"], [*row, *[""] * len(headers["playbooks"])])) for row in tables["playbooks"]]
    playbooks.append(
        {
            "playbook_id": "short_strangle_shadow",
            "enabled": "TRUE",
            "strategy_family": "short_strangle",
            "structure": "short_strangle",
            "csa_stage": "shadow",
            "source_mode": "market_scan",
            "dte_min": "30",
            "dte_max": "45",
            "short_delta_min": "0.14",
            "short_delta_max": "0.20",
            "exit_dte_min": "21",
            "management_policy_json": json.dumps({"lifecycle": {"tested_side_confirmation": 2, "roll": {"min_credit": 0.1}}}),
        }
    )
    return universe, playbooks


def test_unified_books_keep_live_and_shadow_policy_ownership_isolated(tmp_path) -> None:
    universe, playbooks = _rows()
    control = load_control()
    result = run_unified_books(
        control,
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=LocalStore(tmp_path / "kamandal.db"),
        audit_root=tmp_path / "audit",
    )

    assert result.compilation.ok
    assert "short_strangle_shadow" not in result.live.policy_ids
    assert result.shadow.policy_ids == ("short_strangle_shadow",)
    assert result.live.errors == ()
    assert result.shadow.errors == ()
    assert result.live.result is not None
    assert result.shadow.result is not None


def test_one_book_failure_does_not_erase_other_book(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    universe, playbooks = _rows()
    from kamandal_v2.strategy_engine import planning

    original = planning.run_plan

    def fail_shadow(config, **kwargs):  # noqa: ANN001
        if config["runtime"]["mode"] == "shadow":
            raise RuntimeError("shadow fixture failure")
        return original(config, **kwargs)

    monkeypatch.setattr(planning, "run_plan", fail_shadow)
    result = run_unified_books(
        load_control(),
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=LocalStore(tmp_path / "kamandal.db"),
        audit_root=tmp_path / "audit",
    )

    assert result.live.result is not None
    assert result.live.errors == ()
    assert result.shadow.result is None
    assert result.shadow.errors == ("RuntimeError: shadow fixture failure",)


def test_market_scan_and_portfolio_hedge_inputs_join_the_same_book(tmp_path) -> None:
    universe, playbooks = _rows()
    for row in playbooks:
        if row["playbook_id"] == "short_strangle_shadow":
            row["source_mode"] = "market_scan"
    playbooks.append(
        {
            "playbook_id": "hedge_call_spread",
            "enabled": "TRUE",
            "strategy_family": "call_spread",
            "structure": "call_spread",
            "mode": "live",
            "source_mode": "portfolio_hedge",
            "dte_min": "30",
            "dte_max": "45",
            "short_delta_min": "0.20",
            "short_delta_max": "0.30",
            "spread_width": "5",
            "management_policy_json": json.dumps({"lifecycle": {"portfolio_delta_trigger": -999, "hedge_underlyings": ["SPY"]}}),
        }
    )

    result = run_unified_books(
        load_control(),
        universe_rows=universe,
        playbook_rows=playbooks,
        idea_paths=["tests/fixtures/sample_ideas.yaml"],
        store=LocalStore(tmp_path / "kamandal.db"),
        audit_root=tmp_path / "audit",
    )

    assert result.compilation.ok
    assert any(idea.source == "market_scan" for idea in result.shadow.result.ideas)
    assert any(idea.source == "portfolio_hedge" for idea in result.live.result.ideas)
