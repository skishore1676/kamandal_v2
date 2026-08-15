from __future__ import annotations

from datetime import date

from kamandal_v2.strategy_engine.event_timing import entry_session_due, event_exit_due, final_pre_event_session


def test_bmo_and_amc_events_have_distinct_final_entry_and_exit_sessions() -> None:
    event = date(2026, 8, 13)  # Thursday
    assert final_pre_event_session(event, "BMO") == date(2026, 8, 12)
    assert final_pre_event_session(event, "AMC") == event
    assert not event_exit_due(event_date=event, time_of_day="BMO", observed_at="2026-08-13T13:29:00Z")
    assert event_exit_due(event_date=event, time_of_day="BMO", observed_at="2026-08-13T13:30:00Z")
    assert not event_exit_due(event_date=event, time_of_day="AMC", observed_at="2026-08-13T20:00:00Z")
    assert event_exit_due(event_date=event, time_of_day="AMC", observed_at="2026-08-14T13:30:00Z")
    assert entry_session_due(event_date=event, time_of_day="BMO", observed_at="2026-08-12T13:30:00Z")
    assert entry_session_due(event_date=event, time_of_day="AMC", observed_at="2026-08-13T13:30:00Z")


def test_event_sessions_skip_good_friday_and_weekends() -> None:
    event = date(2026, 4, 6)  # Monday BMO; Good Friday is April 3.

    assert final_pre_event_session(event, "BMO") == date(2026, 4, 2)
    assert entry_session_due(event_date=event, time_of_day="BMO", observed_at="2026-04-02T13:30:00Z")
    assert not entry_session_due(event_date=event, time_of_day="BMO", observed_at="2026-04-03T13:30:00Z")
    assert not event_exit_due(event_date=event, time_of_day="BMO", observed_at="2026-04-04T13:30:00Z")
    assert event_exit_due(event_date=event, time_of_day="BMO", observed_at="2026-04-06T13:30:00Z")


def test_amc_exit_uses_next_open_session_across_thanksgiving_and_early_close() -> None:
    event = date(2026, 11, 26)  # Thanksgiving; an AMC result exits Friday's early session.

    assert final_pre_event_session(event, "AMC") == date(2026, 11, 25)
    assert not event_exit_due(event_date=event, time_of_day="AMC", observed_at="2026-11-26T20:00:00Z")
    assert event_exit_due(event_date=event, time_of_day="AMC", observed_at="2026-11-27T14:30:00Z")
