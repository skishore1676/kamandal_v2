from kamandal_v2.domain.models import Playbook
from kamandal_v2.events.earnings import EarningsStore
from kamandal_v2.live.orders import build_close_ticket
from kamandal_v2.live.position_management import live_exit_decision, mark_live_group, normalize_profit_target_pct


def _playbook(structure: str, *, profit_target_pct: float = 50.0, max_loss_multiple: float | None = 2.0) -> Playbook:
    return Playbook(
        playbook_id=f"{structure}_test",
        enabled=True,
        strategy_family=structure,
        structure=structure,
        variant="test",
        leg_count=1,
        profiles=["large_stocks"],
        profit_target_pct=profit_target_pct,
        max_loss_multiple=max_loss_multiple,
        exit_dte_min=21,
        half_time_exit=False,
    )


def _config() -> dict:
    return {"live": {"exit_pricing": {"profit_target_trigger_pct": 95, "min_profit_to_trigger": 5, "require_fresh_quotes": True}}}


def test_profit_target_pct_is_literal_percent_not_fraction() -> None:
    assert normalize_profit_target_pct(0.25) == 25
    assert normalize_profit_target_pct(0.5) == 50
    assert normalize_profit_target_pct(1) == 1
    assert normalize_profit_target_pct("50") == 50


def test_debit_position_profit_target_uses_entry_debit_and_close_credit() -> None:
    group = {
        "group_id": "group_debit",
        "underlying": "AMZN",
        "playbook_id": "long_call_test",
        "structure": "long_call",
        "opened_at": "2026-05-01 14:00:00",
        "candidate": {
            "net_credit": -10.0,
            "legs": [
                {"side": "buy", "option_type": "call", "expiration": "2026-08-21", "strike": 100.0, "quantity": 1}
            ],
        },
    }
    quotes = {("2026-08-21", "call", 100.0): {"bid": 14.7, "ask": 15.3}}
    playbook = _playbook("long_call", profit_target_pct=50)

    mark = mark_live_group(group, quotes, playbook, quote_fresh=True, config=_config())
    decision = live_exit_decision(mark, playbook, EarningsStore(), _config())

    assert mark["entry_kind"] == "debit"
    assert mark["entry_value"] == 1000.0
    assert mark["target_profit"] == 500.0
    assert mark["target_close_net"] == 1500.0
    assert mark["pnl_mid"] == 500.0
    assert decision["action"] == "close"
    assert decision["reason"] == "profit_target"
    assert decision["recommended_close_net"] == 1500.0


def test_one_percent_debit_target_closes_near_one_percent_gain() -> None:
    group = {
        "group_id": "group_debit",
        "underlying": "AMZN",
        "playbook_id": "long_call_test",
        "structure": "long_call",
        "opened_at": "2026-05-01 14:00:00",
        "candidate": {
            "net_credit": -10.0,
            "legs": [
                {"side": "buy", "option_type": "call", "expiration": "2026-08-21", "strike": 100.0, "quantity": 1}
            ],
        },
    }
    quotes = {("2026-08-21", "call", 100.0): {"bid": 10.08, "ask": 10.12}}
    playbook = _playbook("long_call", profit_target_pct=1)

    mark = mark_live_group(group, quotes, playbook, quote_fresh=True, config=_config())
    decision = live_exit_decision(mark, playbook, EarningsStore(), _config())

    assert mark["target_profit"] == 10.0
    assert mark["target_close_net"] == 1010.0
    assert mark["target_progress_pct"] == 100.0
    assert decision["action"] == "close"
    assert decision["reason"] == "profit_target"


def test_credit_position_profit_target_uses_entry_credit_and_close_debit() -> None:
    group = {
        "group_id": "group_credit",
        "underlying": "TSLA",
        "playbook_id": "call_spread_test",
        "structure": "call_spread",
        "opened_at": "2026-05-01 14:00:00",
        "candidate": {
            "net_credit": 1.0,
            "legs": [
                {"side": "sell", "option_type": "call", "expiration": "2026-07-17", "strike": 200.0, "quantity": 1},
                {"side": "buy", "option_type": "call", "expiration": "2026-07-17", "strike": 205.0, "quantity": 1},
            ],
        },
    }
    quotes = {
        ("2026-07-17", "call", 200.0): {"bid": 0.45, "ask": 0.55},
        ("2026-07-17", "call", 205.0): {"bid": 0.10, "ask": 0.15},
    }
    playbook = _playbook("call_spread", profit_target_pct=50)

    mark = mark_live_group(group, quotes, playbook, quote_fresh=True, config=_config())
    decision = live_exit_decision(mark, playbook, EarningsStore(), _config())

    assert mark["entry_kind"] == "credit"
    assert mark["entry_value"] == 100.0
    assert mark["target_profit"] == 50.0
    assert mark["target_close_net"] == -50.0
    assert mark["pnl_mid"] == 62.5
    assert decision["action"] == "close"
    assert decision["reason"] == "profit_target"
    assert decision["recommended_close_net"] == -37.5


def test_credit_profit_target_close_limit_prefers_current_cheaper_midpoint() -> None:
    group = {
        "group_id": "nvda_credit_group",
        "underlying": "NVDA",
        "playbook_id": "put_spread_test",
        "structure": "put_spread",
        "opened_at": "2026-05-01 14:00:00",
        "candidate": {
            "candidate_id": "nvda_credit_candidate",
            "idea_id": "nvda_idea",
            "underlying": "NVDA",
            "playbook_id": "put_spread_test",
            "structure": "put_spread",
            "net_credit": 2.9054,
            "legs": [
                {
                    "role": "short_put",
                    "side": "sell",
                    "option_type": "put",
                    "expiration": "2026-07-17",
                    "strike": 145.0,
                    "quantity": 1,
                    "mid": 1.40,
                    "bid": 1.35,
                    "ask": 1.45,
                    "delta": -0.25,
                    "gamma": 0.0,
                    "theta": -0.01,
                    "vega": 0.1,
                    "open_interest": 1000,
                },
                {
                    "role": "long_put",
                    "side": "buy",
                    "option_type": "put",
                    "expiration": "2026-07-17",
                    "strike": 140.0,
                    "quantity": 1,
                    "mid": 0.25,
                    "bid": 0.25,
                    "ask": 0.25,
                    "delta": -0.15,
                    "gamma": 0.0,
                    "theta": -0.01,
                    "vega": 0.1,
                    "open_interest": 1000,
                },
            ],
        },
    }
    quotes = {
        ("2026-07-17", "put", 145.0): {"bid": 1.35, "ask": 1.45},
        ("2026-07-17", "put", 140.0): {"bid": 0.25, "ask": 0.25},
    }
    playbook = _playbook("put_spread", profit_target_pct=50)

    mark = mark_live_group(group, quotes, playbook, quote_fresh=True, config=_config())
    decision = live_exit_decision(mark, playbook, EarningsStore(), _config())
    ticket = build_close_ticket(group, close_net_credit=float(decision["recommended_close_net"]) / 100.0)

    assert mark["close_mid_net"] == -115.0
    assert mark["target_close_net"] == -145.27
    assert decision["action"] == "close"
    assert decision["reason"] == "profit_target"
    assert decision["recommended_close_net"] == -115.0
    assert ticket["submit_payload"]["limitPrice"] == "1.15"
    assert ticket["submit_payload"]["limitPrice"] != "1.50"


def test_live_exit_holds_when_quotes_are_not_fresh() -> None:
    group = {
        "group_id": "group_debit",
        "underlying": "AMZN",
        "playbook_id": "long_call_test",
        "structure": "long_call",
        "opened_at": "2026-05-01 14:00:00",
        "candidate": {
            "net_credit": -10.0,
            "legs": [
                {"side": "buy", "option_type": "call", "expiration": "2026-08-21", "strike": 100.0, "quantity": 1}
            ],
        },
    }
    quotes = {("2026-08-21", "call", 100.0): {"bid": 20.0, "ask": 20.2}}
    playbook = _playbook("long_call", profit_target_pct=50)

    mark = mark_live_group(group, quotes, playbook, quote_fresh=False, config=_config())
    decision = live_exit_decision(mark, playbook, EarningsStore(), _config())

    assert decision["action"] == "hold"
    assert decision["reason"] == "fresh_quotes_missing"


def test_credit_spread_mid_loss_triggers_review_not_close_without_confirmation() -> None:
    group = {
        "group_id": "group_credit_loss",
        "underlying": "TSLA",
        "playbook_id": "put_spread_test",
        "structure": "put_spread",
        "opened_at": "2026-05-01 14:00:00",
        "candidate": {
            "net_credit": 1.0,
            "legs": [
                {"side": "sell", "option_type": "put", "expiration": "2026-07-17", "strike": 100.0, "quantity": 1},
                {"side": "buy", "option_type": "put", "expiration": "2026-07-17", "strike": 95.0, "quantity": 1},
            ],
        },
    }
    quotes = {
        ("2026-07-17", "put", 100.0): {"bid": 4.0, "ask": 4.2, "delta": -0.55},
        ("2026-07-17", "put", 95.0): {"bid": 2.0, "ask": 2.2, "delta": -0.35},
    }
    playbook = _playbook("put_spread", max_loss_multiple=2.0)

    mark = mark_live_group(group, quotes, playbook, quote_fresh=True, config=_config(), underlying_price=102.0)
    mark["loss_watch_observations"] = {"count": 2, "window_minutes": 120}
    decision = live_exit_decision(mark, playbook, EarningsStore(), _config())

    assert mark["entry_kind"] == "credit"
    assert mark["close_mid_net"] == -200.0
    assert mark["pnl_mid"] == -100.0
    assert mark["close_debit_multiple_of_entry"] == 2.0
    assert mark["max_loss_watch"] is True
    assert mark["short_strike_state"]["breached"] is False
    assert decision["action"] == "review"
    assert decision["reason"] == "loss_watch"
    assert decision["urgency"] == "high"


def test_loss_watch_debounces_until_repeated_observation() -> None:
    group = {
        "group_id": "group_credit_loss",
        "underlying": "TSLA",
        "playbook_id": "put_spread_test",
        "structure": "put_spread",
        "opened_at": "2026-05-01 14:00:00",
        "candidate": {
            "net_credit": 1.0,
            "legs": [
                {"side": "sell", "option_type": "put", "expiration": "2026-07-17", "strike": 100.0, "quantity": 1},
                {"side": "buy", "option_type": "put", "expiration": "2026-07-17", "strike": 95.0, "quantity": 1},
            ],
        },
    }
    quotes = {
        ("2026-07-17", "put", 100.0): {"bid": 4.0, "ask": 4.2, "delta": -0.55},
        ("2026-07-17", "put", 95.0): {"bid": 2.0, "ask": 2.2, "delta": -0.35},
    }
    playbook = _playbook("put_spread", max_loss_multiple=2.0)

    mark = mark_live_group(group, quotes, playbook, quote_fresh=True, config=_config(), underlying_price=102.0)
    mark["loss_watch_observations"] = {"count": 1, "window_minutes": 120}
    decision = live_exit_decision(mark, playbook, EarningsStore(), _config())

    assert mark["max_loss_watch"] is True
    assert decision["action"] == "hold"
    assert decision["reason"] == "loss_watch_debouncing"
    assert decision["urgency"] == "normal"


def test_credit_spread_max_loss_can_close_when_structurally_confirmed_by_config() -> None:
    group = {
        "group_id": "group_credit_loss",
        "underlying": "TSLA",
        "playbook_id": "put_spread_test",
        "structure": "put_spread",
        "opened_at": "2026-05-01 14:00:00",
        "candidate": {
            "net_credit": 1.0,
            "legs": [
                {"side": "sell", "option_type": "put", "expiration": "2026-07-17", "strike": 100.0, "quantity": 1},
                {"side": "buy", "option_type": "put", "expiration": "2026-07-17", "strike": 95.0, "quantity": 1},
            ],
        },
    }
    quotes = {
        ("2026-07-17", "put", 100.0): {"bid": 4.0, "ask": 4.2, "delta": -0.55},
        ("2026-07-17", "put", 95.0): {"bid": 2.0, "ask": 2.2, "delta": -0.35},
    }
    config = _config()
    config["live"]["exit_pricing"]["max_loss_action"] = "close_when_confirmed"
    playbook = _playbook("put_spread", max_loss_multiple=2.0)

    mark = mark_live_group(group, quotes, playbook, quote_fresh=True, config=config, underlying_price=98.0)
    mark["loss_watch_observations"] = {"count": 2, "window_minutes": 120}
    decision = live_exit_decision(mark, playbook, EarningsStore(), config)

    assert mark["max_loss_watch"] is True
    assert mark["short_strike_state"]["breached"] is True
    assert decision["action"] == "close"
    assert decision["reason"] == "max_loss"
    assert decision["urgency"] == "critical"
    assert decision["recommended_close_net"] == -200.0


def test_loss_watch_does_not_close_when_quotes_are_too_wide() -> None:
    group = {
        "group_id": "group_credit_loss",
        "underlying": "TSLA",
        "playbook_id": "put_spread_test",
        "structure": "put_spread",
        "opened_at": "2026-05-01 14:00:00",
        "candidate": {
            "net_credit": 1.0,
            "legs": [
                {"side": "sell", "option_type": "put", "expiration": "2026-07-17", "strike": 100.0, "quantity": 1},
                {"side": "buy", "option_type": "put", "expiration": "2026-07-17", "strike": 95.0, "quantity": 1},
            ],
        },
    }
    quotes = {
        ("2026-07-17", "put", 100.0): {"bid": 3.0, "ask": 5.0, "delta": -0.55},
        ("2026-07-17", "put", 95.0): {"bid": 0.5, "ask": 3.5, "delta": -0.35},
    }
    config = _config()
    config["live"]["exit_pricing"]["max_loss_action"] = "close_when_confirmed"
    playbook = _playbook("put_spread", max_loss_multiple=2.0)

    mark = mark_live_group(group, quotes, playbook, quote_fresh=True, config=config, underlying_price=98.0)
    mark["loss_watch_observations"] = {"count": 2, "window_minutes": 120}
    decision = live_exit_decision(mark, playbook, EarningsStore(), config)

    assert mark["max_loss_watch"] is True
    assert mark["max_leg_bid_ask_pct"] > config["live"]["exit_pricing"].get("loss_watch_max_leg_bid_ask_pct", 1.0)
    assert decision["action"] == "review"
    assert decision["reason"] == "loss_watch_quote_block"
