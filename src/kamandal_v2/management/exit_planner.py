"""Simple exit-plan helpers for shadow/open positions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ExitDecision:
    position_id: str
    action: str
    reason: str


def evaluate_exit(
    *,
    position_id: str,
    pnl_pct: float,
    dte_remaining: int,
    profit_target_pct: float,
    exit_dte_min: int,
    event_status: str = "clear",
) -> ExitDecision:
    if event_status not in {"clear", "unknown"}:
        return ExitDecision(position_id=position_id, action="close", reason="pre_event")
    if pnl_pct >= profit_target_pct:
        return ExitDecision(position_id=position_id, action="close", reason="profit_target")
    if dte_remaining <= exit_dte_min:
        return ExitDecision(position_id=position_id, action="close", reason="dte_target")
    return ExitDecision(position_id=position_id, action="hold", reason="no_exit")

