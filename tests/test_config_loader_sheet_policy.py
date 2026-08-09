from __future__ import annotations

from kamandal_v2.planner import config_loader


def test_universe_expansion_policy_flows_from_google_sheet_playbook_row(monkeypatch) -> None:  # noqa: ANN001
    sheet_playbook = {
        "playbook_id": "short_strangle_sheet",
        "enabled": "TRUE",
        "strategy_family": "short_strangle",
        "structure": "short_strangle",
        "leg_count": "2",
        "profiles": "large_stocks",
        "universe_expansion_enabled": "TRUE",
        "underlying_price_min": "61",
        "underlying_price_max": "181",
        "iv_rank_min": "41",
        "iv_rank_max": "91",
    }
    monkeypatch.setattr(
        config_loader,
        "pull_sheet_tables",
        lambda _config: {
            "universe": [{"symbol": "TLT", "enabled": "TRUE", "profile": "rates_etf"}],
            "playbooks": [sheet_playbook],
            "daily_plan": [],
        },
    )

    _universe, playbooks = config_loader.load_planner_config({}, source="sheet")

    assert len(playbooks) == 1
    playbook = playbooks[0]
    assert playbook.universe_expansion_enabled is True
    assert playbook.underlying_price_min == 61.0
    assert playbook.underlying_price_max == 181.0
    assert playbook.iv_rank_min == 41.0
    assert playbook.iv_rank_max == 91.0


def test_missing_google_sheet_policy_has_no_repository_fallback(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        config_loader,
        "pull_sheet_tables",
        lambda _config: {
            "universe": [{"symbol": "TLT", "enabled": "TRUE", "profile": "rates_etf"}],
            "playbooks": [
                {
                    "playbook_id": "short_strangle_sheet",
                    "enabled": "TRUE",
                    "strategy_family": "short_strangle",
                    "structure": "short_strangle",
                    "leg_count": "2",
                }
            ],
            "daily_plan": [],
        },
    )

    _universe, playbooks = config_loader.load_planner_config({}, source="sheet")

    playbook = playbooks[0]
    assert playbook.universe_expansion_enabled is False
    assert playbook.underlying_price_min is None
    assert playbook.underlying_price_max is None
    assert playbook.iv_rank_min is None
    assert playbook.iv_rank_max is None


def test_staged_playbooks_are_exclusively_routed_away_from_baseline_planner(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        config_loader,
        "pull_sheet_tables",
        lambda _config: {
            "universe": [],
            "playbooks": [
                {"playbook_id": "baseline", "enabled": "TRUE", "csa_stage": "baseline"},
                {"playbook_id": "shadow", "enabled": "TRUE", "csa_stage": "shadow"},
                {"playbook_id": "pilot", "enabled": "TRUE", "csa_stage": "pilot_live"},
                {"playbook_id": "live", "enabled": "TRUE", "csa_stage": "live"},
            ],
            "daily_plan": [],
        },
    )

    _universe, playbooks = config_loader.load_planner_config({}, source="sheet")

    assert [playbook.playbook_id for playbook in playbooks] == ["baseline"]
    assert playbooks[0].deployment_stage == "baseline"
