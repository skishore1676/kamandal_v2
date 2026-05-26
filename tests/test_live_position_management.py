from kamandal_v2.domain.models import Playbook
from kamandal_v2.events.earnings import EarningsStore
from kamandal_v2.live.position_management import live_exit_decision, mark_live_group, normalize_profit_target_pct


def _playbook(structure: str, *, profit_target_pct: float = 50.0) -> Playbook:
    return Playbook(
        playbook_id=f"{structure}_test",
        enabled=True,
        strategy_family=structure,
        structure=structure,
        variant="test",
        leg_count=1,
        profiles=["large_stocks"],
        profit_target_pct=profit_target_pct,
        exit_dte_min=21,
        half_time_exit=False,
    )


def _config() -> dict:
    return {"live": {"exit_pricing": {"profit_target_trigger_pct": 95, "min_profit_to_trigger": 5, "require_fresh_quotes": True}}}


def test_profit_target_pct_is_literal_percent_not_fraction() -> None:
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
    assert decision["recommended_close_net"] == -50.0


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
