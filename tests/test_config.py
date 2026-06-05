from kamandal_v2.config import load_control


def test_exit_loss_watch_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("KAMANDAL_EXIT_MAX_LOSS_ACTION", "review")
    monkeypatch.setenv("KAMANDAL_EXIT_MAX_LOSS_REQUIRES_CONFIRMATION", "false")
    monkeypatch.setenv("KAMANDAL_EXIT_SHORT_STRIKE_BUFFER_PCT", "1.5")
    monkeypatch.setenv("KAMANDAL_EXIT_LOSS_WATCH_MAX_LEG_BID_ASK_PCT", "0.75")
    monkeypatch.setenv("KAMANDAL_EXIT_LOSS_WATCH_CONFIRMATIONS_REQUIRED", "3")
    monkeypatch.setenv("KAMANDAL_EXIT_LOSS_WATCH_WINDOW_MINUTES", "45")

    exit_pricing = load_control()["live"]["exit_pricing"]

    assert exit_pricing["max_loss_action"] == "review"
    assert exit_pricing["max_loss_requires_confirmation"] is False
    assert exit_pricing["short_strike_buffer_pct"] == 1.5
    assert exit_pricing["loss_watch_max_leg_bid_ask_pct"] == 0.75
    assert exit_pricing["loss_watch_confirmations_required"] == 3
    assert exit_pricing["loss_watch_window_minutes"] == 45
