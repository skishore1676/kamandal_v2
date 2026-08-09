from __future__ import annotations

import json

import pytest

from kamandal_v2.strategy_lanes.daily_policy import (
    capture_daily_policy_snapshot,
    load_daily_policy_snapshot,
)


def _tables(stage: str = "shadow") -> dict:
    return {
        "universe": [{"symbol": "XYZ", "enabled": "TRUE", "profile": "large_cap"}],
        "playbooks": [
            {
                "playbook_id": "short_strangle_daily",
                "enabled": "TRUE",
                "strategy_family": "short_strangle",
                "structure": "short_strangle",
                "csa_stage": stage,
                "source_mode": "market_scan",
                "management_policy_json": json.dumps(
                    {
                        "lifecycle": {
                            "tested_side_confirmation": 2,
                            "roll": {"min_credit": 0.1, "duration_trigger_dte": 21},
                            "adjustment_limit": 2,
                            "inversion": {"allowed": True, "max_width": 5},
                            "cooldown": {"minutes": 30},
                            "loss_stages": {"watch_multiple": 2, "close_multiple": 3},
                            "fill": {"max_attempts": 4, "price_increment": 0.05},
                        }
                    }
                ),
                "sizing_method": "fixed_contracts",
                "sizing_value": 1,
                "max_contracts": 1,
                "score_weight_credit": 1,
                "score_weight_pop": 1,
                "score_weight_liquidity": 1,
                "score_weight_spread": 1,
                "max_bid_ask_pct": 1,
                "min_option_oi": 1,
                "dte_min": 30,
                "dte_max": 60,
                "short_delta_min": 0.1,
                "short_delta_max": 0.2,
                "iv_rank_min": 35,
                "iv_rank_max": 100,
                "profit_target_pct": 50,
                "exit_dte_min": 21,
                "live_max_bpr_per_order": 2500,
            }
        ],
    }


def test_daily_policy_is_captured_once_and_ignores_midday_sheet_edits(tmp_path, monkeypatch) -> None:
    pulls = [_tables("shadow"), _tables("live")]
    call_count = 0

    def pull(_config):  # noqa: ANN001, ANN202
        nonlocal call_count
        result = pulls[call_count]
        call_count += 1
        return result

    monkeypatch.setattr("kamandal_v2.strategy_lanes.daily_policy.pull_sheet_tables", pull)
    first = capture_daily_policy_snapshot(
        {},
        trading_date="2026-08-10",
        snapshot_dir=tmp_path,
        captured_at="2026-08-10T14:22:00Z",
    )
    second = capture_daily_policy_snapshot(
        {},
        trading_date="2026-08-10",
        snapshot_dir=tmp_path,
        captured_at="2026-08-10T16:00:00Z",
    )

    assert call_count == 1
    assert first.snapshot_hash == second.snapshot_hash
    assert second.captured_at == "2026-08-10T14:22:00Z"
    assert second.policy.policies[0].stage.value == "shadow"


def test_daily_policy_detects_tampering(tmp_path) -> None:
    snapshot = capture_daily_policy_snapshot(
        {},
        trading_date="2026-08-10",
        snapshot_dir=tmp_path,
        tables=_tables(),
        captured_at="2026-08-10T14:22:00Z",
    )
    payload = json.loads(snapshot.path.read_text(encoding="utf-8"))
    payload["tables"]["playbooks"][0]["csa_stage"] = "live"
    snapshot.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        load_daily_policy_snapshot({}, trading_date="2026-08-10", snapshot_dir=tmp_path)
