from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from kamandal_v2.live.option_sessions import submission_window
from kamandal_v2.live import execution
from kamandal_v2.stores.sqlite import LocalStore


CENTRAL = ZoneInfo("America/Chicago")


def _config() -> dict:
    return {
        "runtime": {"market_timezone": "America/Chicago"},
        "live": {
            "option_submission": {
                "enabled": True,
                "regular_close_time": "15:00",
                "extended_close_time": "15:15",
                "extended_close_symbols": ["SPY"],
                "entry_buffer_minutes": 30,
                "close_buffer_minutes": 5,
                "early_close_dates": {
                    "2026-11-27": {
                        "regular_close_time": "12:00",
                        "extended_close_time": "12:15",
                    }
                },
            }
        },
    }


@pytest.mark.parametrize(
    ("underlying", "hour", "minute", "allowed"),
    [
        ("AAPL", 14, 54, True),
        ("AAPL", 14, 55, False),
        ("SPY", 15, 9, True),
        ("SPY", 15, 10, False),
    ],
)
def test_close_cutoff_is_product_aware(
    underlying: str,
    hour: int,
    minute: int,
    allowed: bool,
) -> None:
    verdict = submission_window(
        _config(),
        {"underlying": underlying},
        close=True,
        now=datetime(2026, 7, 24, hour, minute, tzinfo=CENTRAL),
    )

    assert verdict["allowed"] is allowed
    assert verdict["uses_extended_session"] is (underlying == "SPY")


def test_entry_cutoff_is_earlier_than_close_cutoff() -> None:
    verdict = submission_window(
        _config(),
        {"underlying": "AAPL"},
        close=False,
        now=datetime(2026, 7, 24, 14, 30, tzinfo=CENTRAL),
    )

    assert verdict["allowed"] is False
    assert verdict["reason"] == "entry_cutoff_reached"
    assert verdict["retryable_next_session"] is False


def test_early_close_override_moves_spy_cutoff() -> None:
    verdict = submission_window(
        _config(),
        {"underlying": "SPY"},
        close=True,
        now=datetime(2026, 11, 27, 12, 10, tzinfo=CENTRAL),
    )

    assert verdict["allowed"] is False
    assert verdict["submission_cutoff_at"].startswith("2026-11-27T12:10:00")


def test_weekend_fails_closed() -> None:
    verdict = submission_window(
        _config(),
        {"underlying": "SPY"},
        close=True,
        now=datetime(2026, 7, 25, 10, 0, tzinfo=CENTRAL),
    )

    assert verdict["allowed"] is False
    assert verdict["reason"] == "market_closed_non_trading_day"
    assert verdict["retryable_next_session"] is True


@pytest.mark.parametrize(
    ("close", "initial_status", "deferred_status"),
    [
        (False, "pending_approval", "deferred_entry_cutoff"),
        (True, "pending_close_approval", "deferred_market_closed"),
    ],
)
def test_execute_ticket_defers_without_calling_broker(
    tmp_path,
    monkeypatch,
    close: bool,
    initial_status: str,
    deferred_status: str,
) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "ticket_hash": "ticket-1",
        "order_id": "order-1",
        "plan_id": "plan-1",
        "candidate_id": "candidate-1",
        "intent_type": "close" if close else "open",
        "underlying": "SPY",
        "submit_payload": {},
    }
    store.save_live_order_intent(ticket, status=initial_status)
    monkeypatch.setattr(execution, "_ticket_fresh", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(execution, "_same_day_close_blocked", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        execution,
        "submission_window",
        lambda *_args, **_kwargs: {
            "allowed": False,
            "reason": "close_cutoff_reached" if close else "entry_cutoff_reached",
            "intent_type": "close" if close else "open",
        },
    )

    class NoBrokerCalls:
        def preflight_ticket(self, _ticket):
            raise AssertionError("preflight must not run after the cutoff")

        def place_order_ticket(self, _ticket):
            raise AssertionError("submission must not run after the cutoff")

    result = execution._execute_ticket(  # noqa: SLF001
        {},
        NoBrokerCalls(),
        store,
        ticket,
        submit=True,
        close=close,
    )

    assert result["status"] == deferred_status
    assert store.live_order_intent("ticket-1")["_ledger_status"] == deferred_status
