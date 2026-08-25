from __future__ import annotations

import json

from kamandal_v2.schemas import UNIVERSE_HEADER
from kamandal_v2.strategy_engine.sheet_policy_gate import validate_sheet_policy


def _tables(*, watch_multiple: int | None = 2) -> dict[str, list[dict[str, object]]]:
    loss_stages = {"close_multiple": 3}
    if watch_multiple is not None:
        loss_stages["watch_multiple"] = watch_multiple
    playbook = {
        "playbook_id": "short_strangle_high_iv",
        "enabled": "TRUE",
        "strategy_family": "short_strangle",
        "structure": "short_strangle",
        "profiles": "index_etf",
        "csa_stage": "shadow",
        "source_mode": "market_scan",
        "dte_min": 35,
        "dte_max": 50,
        "short_delta_min": 0.14,
        "short_delta_max": 0.22,
        "iv_rank_min": 30,
        "iv_rank_max": 100,
        "profit_target_pct": 40,
        "max_loss_multiple": 3,
        "loss_close_multiple": 3,
        "exit_dte_min": 21,
        "sizing_method": "fixed_contracts",
        "sizing_value": 1,
        "max_contracts": 1,
        "score_weight_credit": 1,
        "score_weight_pop": 1,
        "score_weight_liquidity": 1,
        "score_weight_spread": 1,
        "max_bid_ask_pct": 0.25,
        "min_option_oi": 100,
        "live_max_bpr_per_order": 2500,
        "management_policy_json": json.dumps(
            {
                "lifecycle": {
                    "tested_side_confirmation": 2,
                    "roll": {"min_credit": 0.1, "duration_trigger_dte": 21},
                    "adjustment_limit": 2,
                    "inversion": {"allowed": True, "max_width": 5},
                    "cooldown": {"minutes": 30},
                    "loss_stages": loss_stages,
                    "fill": {"max_attempts": 4, "price_increment": 0.05},
                }
            }
        ),
    }
    universe_row = {key: "" for key in UNIVERSE_HEADER}
    universe_row.update(
        {
                "symbol": "SPY",
                "enabled": "TRUE",
                "profile": "index_etf",
                "allowed_playbooks": "short_strangle",
        }
    )
    return {
        "universe": [universe_row],
        "playbooks": [playbook],
        "daily_plan": [],
    }


def test_sheet_policy_gate_accepts_one_snapshot_across_all_compilers() -> None:
    result = validate_sheet_policy({}, tables=_tables(), read_at="2026-08-24T15:00:00Z")

    assert result.ok
    assert result.unified_policy_count == 1
    assert result.csa_policy_count == 1
    assert result.snapshot_hash
    assert result.to_dict()["source"] == "google_sheet"


def test_sheet_policy_gate_catches_missing_watch_multiple_before_deploy() -> None:
    result = validate_sheet_policy({}, tables=_tables(watch_multiple=None), read_at="2026-08-24T15:00:00Z")

    assert not result.ok
    assert result.unified_policy_count == 1
    assert result.csa_policy_count == 0
    assert any("lifecycle.loss_stages.watch_multiple" in error for error in result.csa_errors)


def test_sheet_policy_gate_catches_missing_universe_operator_columns() -> None:
    tables = _tables()
    for field in ("tier", "proposal_source", "proposal_reason", "proposal_date"):
        tables["universe"][0].pop(field)

    result = validate_sheet_policy({}, tables=tables, read_at="2026-08-24T15:00:00Z")

    assert not result.ok
    assert any("universe_header_missing" in error for error in result.model_errors)
