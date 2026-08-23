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


@pytest.mark.parametrize(
    ("close", "hour", "minute", "allowed", "reason"),
    [
        (False, 8, 29, False, "market_not_open"),
        (True, 8, 29, False, "market_not_open"),
        (False, 8, 30, False, "entry_not_open"),
        (True, 8, 30, True, "within_submission_window"),
        (False, 8, 59, False, "entry_not_open"),
        (False, 9, 0, True, "within_submission_window"),
    ],
)
def test_open_and_close_windows_have_distinct_morning_boundaries(close: bool, hour: int, minute: int, allowed: bool, reason: str) -> None:
    verdict = submission_window(
        _config(),
        {"underlying": "AAPL", "intent_type": "close" if close else "open"},
        close=close,
        now=datetime(2026, 7, 24, hour, minute, tzinfo=CENTRAL),
    )

    assert verdict["allowed"] is allowed
    assert verdict["reason"] == reason


def test_strangle_replacement_uses_the_entry_window() -> None:
    verdict = submission_window(
        _config(),
        {"underlying": "AAPL", "intent_type": "adjust", "csa_action_type": "adjust"},
        close=False,
        now=datetime(2026, 7, 24, 8, 59, tzinfo=CENTRAL),
    )

    assert verdict["allowed"] is False
    assert verdict["reason"] == "entry_not_open"
    assert verdict["retryable_current_session"] is True


def test_adjustment_cannot_inherit_close_permission_from_management_queue() -> None:
    verdict = submission_window(
        _config(),
        {"underlying": "AAPL", "intent_type": "adjust", "csa_action_type": "adjust"},
        close=True,
        now=datetime(2026, 7, 24, 8, 30, tzinfo=CENTRAL),
    )

    assert verdict["allowed"] is False
    assert verdict["reason"] == "entry_not_open"


def test_adverse_close_uses_loss_buffers_while_scheduled_close_keeps_close_window() -> None:
    now = datetime(2026, 7, 24, 14, 45, tzinfo=CENTRAL)
    adverse = submission_window(
        _config(),
        {"underlying": "AAPL", "intent_type": "close", "csa_action_type": "close", "csa_action_reason_class": "adverse_price_loss"},
        close=True,
        now=now,
    )
    scheduled = submission_window(
        _config(),
        {"underlying": "AAPL", "intent_type": "close", "csa_action_type": "close", "csa_action_reason_class": "time_decision"},
        close=True,
        now=now,
    )

    assert adverse["allowed"] is False
    assert adverse["reason"] == "adverse_exit_closing_buffer"
    assert scheduled["allowed"] is True


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

    result = execution._execute_ticket(
        {},
        NoBrokerCalls(),
        store,
        ticket,
        submit=True,
        close=close,
    )

    assert result["status"] == deferred_status
    assert store.live_order_intent("ticket-1")["_ledger_status"] == deferred_status


def test_pre_window_entry_waits_then_requires_fresh_rebuild(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    ticket = {
        "ticket_hash": "ticket-waiting",
        "order_id": "order-waiting",
        "plan_id": "plan-waiting",
        "candidate_id": "candidate-waiting",
        "intent_type": "open",
        "underlying": "GLD",
        "submit_payload": {},
    }
    store.save_live_order_intent(ticket, status="stage_approved_pending_submit")
    freshness_checks = []
    monkeypatch.setattr(execution, "_ticket_fresh", lambda *_args, **_kwargs: freshness_checks.append(True) or False)
    windows = iter(
        [
            {"allowed": False, "reason": "entry_not_open", "intent_type": "open"},
            {"allowed": True, "reason": "within_submission_window", "intent_type": "open"},
        ]
    )
    monkeypatch.setattr(execution, "submission_window", lambda *_args, **_kwargs: next(windows))

    class NoBrokerCalls:
        def preflight_ticket(self, _ticket):
            raise AssertionError("stale waiting ticket must be rebuilt before broker preflight")

        def place_order_ticket(self, _ticket):
            raise AssertionError("stale waiting ticket must not submit")

    waiting = execution._execute_ticket(
        {}, NoBrokerCalls(), store, ticket, submit=True, close=False
    )
    assert waiting["status"] == "waiting_entry_window"
    assert freshness_checks == []
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "waiting_entry_window"

    retry_ticket = store.live_order_intent(ticket["ticket_hash"])
    stale = execution._execute_ticket(
        {}, NoBrokerCalls(), store, retry_ticket, submit=True, close=False
    )
    assert stale["failure_code"] == "ticket_preflight_stale"
    assert freshness_checks == [True]
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "blocked_preflight_stale"


def test_waiting_entry_is_not_an_operator_failure() -> None:
    waiting = {
        "processed": 1,
        "results": [{"ticket_hash": "ticket-waiting", "status": "waiting_entry_window", "reason": "entry_not_open"}],
    }

    assert execution._selected_entry_failure(waiting, submit=True) is None
