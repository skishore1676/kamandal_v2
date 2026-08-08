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
    assert "trade_bundle_json" in header
    assert "portfolio_delta_change" in header
    assert "portfolio_theta_change" in header
    assert "plan_reasons" in header
    assert "plan_metrics_json" in header
    assert "plan_detail_json" in header
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
    headers = seed_headers()["playbooks"]

    assert by_id["short_put"][1] == "TRUE"
    assert by_id["put_spread"][1] == "TRUE"
    assert by_id["call_spread"][1] == "TRUE"
    assert by_id["iron_condor"][1] == "TRUE"
    assert by_id["call_calendar"][1] == "TRUE"
    assert by_id["call_calendar"][3] == "call_calendar"
    cap_index = headers.index("live_max_bpr_per_order")
    assert by_id["put_spread"][cap_index] == 500
    assert by_id["call_spread"][cap_index] == 500
    assert by_id["iron_condor"][cap_index] == 500
    assert by_id["call_calendar"][cap_index] == 1200


def test_seed_playbook_rows_match_header_length() -> None:
    tables = build_seed_tables(load_control())
    header_len = len(seed_headers()["playbooks"])

    assert all(len(row) == header_len for row in tables["playbooks"])


def test_seed_does_not_set_universe_expansion_policy() -> None:
    tables = build_seed_tables(load_control())
    headers = seed_headers()["playbooks"]
    enabled_index = headers.index("universe_expansion_enabled")
    price_min_index = headers.index("underlying_price_min")
    price_max_index = headers.index("underlying_price_max")

    assert all(row[enabled_index] == "" for row in tables["playbooks"])
    assert all(row[price_min_index] == "" for row in tables["playbooks"])
    assert all(row[price_max_index] == "" for row in tables["playbooks"])


def test_seed_does_not_arm_csa_policy() -> None:
    tables = build_seed_tables(load_control())
    headers = seed_headers()["playbooks"]

    for field_name in ("csa_stage", "source_mode", "management_policy_json"):
        field_index = headers.index(field_name)
        assert all(row[field_index] == "" for row in tables["playbooks"])
