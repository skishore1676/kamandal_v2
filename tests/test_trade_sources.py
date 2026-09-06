from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from kamandal_v2.config import load_control
from kamandal_v2.domain.models import Idea, Playbook, PortfolioState
from kamandal_v2.intelligence.trade_sources import (
    TradeSourceMode,
    TradeSourceOutputKind,
    compile_trade_source_policies,
)
from kamandal_v2.seed import build_seed_tables, seed_headers
from kamandal_v2.strategy_engine.planning import _source_groups
from kamandal_v2.strategy_engine.policy import ExecutionMode, compile_playbook_policies


def _source_rows() -> list[dict[str, str]]:
    return [
        {"source_id": "greg_harmon", "output_kind": "idea", "mode": "live", "notes": ""},
        {"source_id": "greg_harmon", "output_kind": "exact_package", "mode": "observe", "notes": ""},
        {"source_id": "mike_butler", "output_kind": "idea", "mode": "observe", "notes": ""},
        {"source_id": "mike_butler", "output_kind": "exact_package", "mode": "shadow", "notes": ""},
    ]


def _seed_playbook(playbook_id: str) -> dict[str, object]:
    with patch("kamandal_v2.seed.OLD_KAMANDAL_ROOT", Path("/__trade_source_test_no_legacy__")):
        tables = build_seed_tables(load_control())
    header = seed_headers()["playbooks"]
    for values in tables["playbooks"]:
        row = dict(zip(header, values, strict=False))
        if row.get("playbook_id") == playbook_id:
            row["max_bid_ask_pct"] = row.get("max_bid_ask_pct") or "0.25"
            row["profit_target_pct"] = row.get("profit_target_pct") or "40"
            row["max_loss_multiple"] = row.get("max_loss_multiple") or "1"
            row["exit_dte_min"] = row.get("exit_dte_min") or "14"
            row["resting_profit_enabled"] = "FALSE"
            row["resting_profit_arm_progress_pct"] = "25"
            return row
    raise AssertionError(f"seed playbook missing: {playbook_id}")


def test_trade_source_policy_requires_one_pair_and_scopes_exact_live() -> None:
    valid = compile_trade_source_policies(_source_rows(), required_source_ids=("greg_harmon", "mike_butler"))
    assert valid.ok
    assert valid.by_key()[("mike_butler", TradeSourceOutputKind.EXACT_PACKAGE)].mode is TradeSourceMode.SHADOW

    incomplete = compile_trade_source_policies(_source_rows()[:-1], required_source_ids=("mike_butler",))
    assert any("missing required row mike_butler/exact_package" in error for error in incomplete.errors)

    exact_live = _source_rows()
    exact_live[-1] = {**exact_live[-1], "mode": "live"}
    rejected = compile_trade_source_policies(exact_live)
    assert any("requires explicit live_structures" in error for error in rejected.errors)
    exact_live[-1]["live_structures"] = "short_strangle"
    scoped = compile_trade_source_policies(exact_live)
    assert scoped.ok
    policy = scoped.by_key()[("mike_butler", TradeSourceOutputKind.EXACT_PACKAGE)]
    assert policy.mode_for_structure("short_strangle") is TradeSourceMode.LIVE
    assert policy.mode_for_structure("call_diagonal") is TradeSourceMode.SHADOW


def test_source_mode_is_a_ceiling_over_existing_playbook_mode() -> None:
    row = _seed_playbook("call_spread")
    row.update({"mode": "live", "csa_stage": "live", "accepted_inputs": "idea"})
    compilation = compile_playbook_policies([row])
    assert compilation.ok
    playbook = Playbook.from_row(row)
    idea = Idea.from_dict(
        {
            "idea_id": "mike-idea-1",
            "source": "correspondent:mike_butler:x-post-1",
            "underlying": "SPY",
            "direction": "bullish",
            "horizon_days": 30,
            "operator_status": "approved",
        }
    )
    policies = compile_trade_source_policies(
        [
            {"source_id": "mike_butler", "output_kind": "idea", "mode": "shadow"},
            {"source_id": "mike_butler", "output_kind": "exact_package", "mode": "off"},
        ]
    ).by_key()

    live_groups = _source_groups(
        [idea], [], [playbook], policies=compilation.policies,
        portfolio=PortfolioState(20_000, 20_000, 0, 0), mode=ExecutionMode.LIVE,
        trade_source_policies=policies,
    )
    shadow_groups = _source_groups(
        [idea], [], [playbook], policies=compilation.policies,
        portfolio=PortfolioState(20_000, 20_000, 0, 0), mode=ExecutionMode.SHADOW,
        trade_source_policies=policies,
    )
    assert live_groups == []
    assert len(shadow_groups) == 1
    assert shadow_groups[0].playbooks[0].playbook_id == "call_spread"


def test_exact_package_has_one_source_neutral_manager_per_structure() -> None:
    first = _seed_playbook("call_calendar")
    first.update({"mode": "live", "csa_stage": "live", "accepted_inputs": "idea,exact_package"})
    second = dict(first, playbook_id="duplicate_call_calendar")

    valid = compile_playbook_policies([first])
    assert valid.ok
    assert valid.policies[0].accepted_inputs == ("idea", "exact_package")

    ambiguous = compile_playbook_policies([first, second])
    assert any("ambiguous accepting playbooks" in error for error in ambiguous.errors)
