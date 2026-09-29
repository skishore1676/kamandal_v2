"""Explicit, Sheet-owned same-day condor windows using the market calendar."""
from datetime import datetime, timedelta
from typing import Any

from kamandal_v2.live.option_sessions import submission_window


def expiry_day_buffers(management: dict[str, Any]) -> tuple[int, int]:
    policy = management.get('lifecycle') or {}
    entry = policy.get('expiry_day_entry_minutes_before_close')
    exit_ = policy.get('expiry_day_exit_minutes_before_close')
    if (type(entry) is not int or type(exit_) is not int
            or not 15 <= exit_ < entry <= 180):
        raise ValueError('same-day condor requires explicit entry/exit buffers with 15 <= exit < entry <= 180')
    return entry, exit_


def expiry_day_window(config: dict[str, Any], management: dict[str, Any], underlying: str, observed_at: str) -> dict[str, Any]:
    entry, exit_ = expiry_day_buffers(management)
    now = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
    session = submission_window(config, {'underlying': underlying}, close=False, now=now)
    close = datetime.fromisoformat(session['session_close_at'])
    entry_at, exit_at = close - timedelta(minutes=entry), close - timedelta(minutes=exit_)
    return {'entry_until': entry_at.isoformat(), 'exit_at': exit_at.isoformat(),
            'entry_allowed': session['allowed'] and now < entry_at, 'exit_due': now >= exit_at}
