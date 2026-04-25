from kamandal_v2.config import load_control
from kamandal_v2.schemas import DAILY_PLAN_HEADER, PLAYBOOKS_HEADER, UNIVERSE_HEADER
from kamandal_v2.seed import build_seed_tables, seed_headers


def test_seed_headers_match_expected_tabs() -> None:
    headers = seed_headers()
    assert headers["universe"] == UNIVERSE_HEADER
    assert headers["playbooks"] == PLAYBOOKS_HEADER
    assert headers["daily_plan"] == DAILY_PLAN_HEADER


def test_daily_plan_header_represents_plan_bundles() -> None:
    header = seed_headers()["daily_plan"]
    assert "plan_rank" in header
    assert "plan_id" in header
    assert "plan_trade_count" in header
    assert "trade_bundle" in header
    assert "portfolio_delta_change" in header
    assert "portfolio_theta_change" in header
    assert "plan_reasons" in header
    assert "candidate_rank" not in header
    assert "underlying" not in header
    assert "delta_impact" not in header
    assert "rank" not in header
    assert "score" not in header


def test_seed_tables_include_core_rows() -> None:
    tables = build_seed_tables(load_control())
    universe_symbols = {row[0] for row in tables["universe"]}
    playbook_ids = {row[0] for row in tables["playbooks"]}

    assert {"SPY", "QQQ", "IWM"}.issubset(universe_symbols)
    assert {
        "short_put",
        "put_spread",
        "call_spread",
        "iron_condor",
        "call_calendar",
    }.issubset(playbook_ids)
    assert len(tables["daily_plan"]) == 0


def test_core_playbooks_are_enabled() -> None:
    tables = build_seed_tables(load_control())
    by_id = {row[0]: row for row in tables["playbooks"]}

    assert by_id["short_put"][1] == "TRUE"
    assert by_id["put_spread"][1] == "TRUE"
    assert by_id["call_spread"][1] == "TRUE"
    assert by_id["iron_condor"][1] == "TRUE"
    assert by_id["call_calendar"][1] == "TRUE"
    assert by_id["call_calendar"][3] == "call_calendar"
