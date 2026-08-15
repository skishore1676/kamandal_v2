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
