from kamandal_v2.config import load_control


def test_exit_loss_watch_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("KAMANDAL_EXIT_MAX_LOSS_ACTION", "review")
    monkeypatch.setenv("KAMANDAL_EXIT_MAX_LOSS_REQUIRES_CONFIRMATION", "false")
    monkeypatch.setenv("KAMANDAL_EXIT_SHORT_STRIKE_BUFFER_PCT", "1.5")
    monkeypatch.setenv("KAMANDAL_EXIT_PROFIT_FLOOR_PCT", "65")
    monkeypatch.setenv("KAMANDAL_EXIT_LOSS_WATCH_MAX_LEG_BID_ASK_PCT", "0.75")
    monkeypatch.setenv("KAMANDAL_EXIT_LOSS_WATCH_CONFIRMATIONS_REQUIRED", "3")
    monkeypatch.setenv("KAMANDAL_EXIT_LOSS_WATCH_WINDOW_MINUTES", "45")

    exit_pricing = load_control()["live"]["exit_pricing"]

    assert exit_pricing["max_loss_action"] == "review"
    assert exit_pricing["max_loss_requires_confirmation"] is False
    assert exit_pricing["short_strike_buffer_pct"] == 1.5
    assert exit_pricing["profit_floor_pct"] == 65.0
    assert exit_pricing["loss_watch_max_leg_bid_ask_pct"] == 0.75
    assert exit_pricing["loss_watch_confirmations_required"] == 3
    assert exit_pricing["loss_watch_window_minutes"] == 45


def test_portfolio_delta_guard_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("KAMANDAL_PORTFOLIO_DELTA_GUARD_ENABLED", "false")
    monkeypatch.setenv("KAMANDAL_PORTFOLIO_DELTA_GUARD_MODE", "strict")
    monkeypatch.setenv("KAMANDAL_PORTFOLIO_DELTA_GUARD_APPLY_MODES", "live,shadow")
    monkeypatch.setenv("KAMANDAL_PORTFOLIO_DELTA_GUARD_MIN_DELTA", "-1.25")
    monkeypatch.setenv("KAMANDAL_PORTFOLIO_DELTA_GUARD_MAX_DELTA", "0.25")
    monkeypatch.setenv("KAMANDAL_PORTFOLIO_DELTA_GUARD_ALLOW_IMPROVEMENT", "false")

    guard = load_control()["portfolio"]["delta_guard"]

    assert guard["enabled"] is False
    assert guard["mode"] == "strict"
    assert guard["apply_modes"] == "live,shadow"
    assert guard["min_delta"] == -1.25
    assert guard["max_delta"] == 0.25
    assert guard["allow_improvement_when_outside"] is False


def test_operator_review_lathi_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("KAMANDAL_OPERATOR_REVIEW_TRANSPORT", "openclaw")
    monkeypatch.setenv("KAMANDAL_OPERATOR_REVIEW_LATHI_BUS_PROFILE", "codex-northstar")
    monkeypatch.setenv("KAMANDAL_OPERATOR_REVIEW_LATHI_BUS_MODE", "spool")

    review = load_control()["live"]["operator_review"]

    assert review["transport"] == "openclaw"
    assert review["lathi_profile"] == "codex-northstar"
    assert review["lathi_mode"] == "spool"


def test_planner_vertical_width_search_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("KAMANDAL_PLANNER_WIDTH_SEARCH_ENABLED", "true")
    monkeypatch.setenv("KAMANDAL_PLANNER_WIDTH_SEARCH_WIDTHS", "5.0,7.5,10.0")

    width_search = load_control()["planner"]["vertical_width_search"]

    assert width_search["enabled"] is True
    assert width_search["widths"] == [5.0, 7.5, 10.0]


def test_planner_vertical_width_search_defaults_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("KAMANDAL_PLANNER_WIDTH_SEARCH_ENABLED", raising=False)
    monkeypatch.delenv("KAMANDAL_PLANNER_WIDTH_SEARCH_WIDTHS", raising=False)

    width_search = load_control()["planner"]["vertical_width_search"]

    assert width_search["enabled"] is False
    assert width_search["widths"] == [5.0, 7.5, 10.0]
    assert width_search["respect_bpr_cap"] is True
