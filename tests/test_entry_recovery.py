from __future__ import annotations

from types import SimpleNamespace

import pytest

from kamandal_v2.live import advisory, execution
from kamandal_v2.ops.alerts import AlertResult
from kamandal_v2.stores.sqlite import LocalStore


def _config() -> dict:
    return {
        "live": {
            "stale_entry_recovery": {
                "enabled": True,
                "max_rebuilds_per_execution": 1,
                "notification_mode": "spool",
            }
        }
    }


def _stale_result() -> dict:
    return {
        "action": "APPROVE_LIVE",
        "submit": True,
        "processed": 1,
        "results": [
            {
                "status": "blocked",
                "reason": "ticket_preflight_stale",
                "failure_code": "ticket_preflight_stale",
                "ticket_hash": "old-ticket",
                "underlying": "V",
            }
        ],
    }


def test_fresh_selected_entry_failure_notifies_without_rebuild(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    monkeypatch.setattr(
        execution,
        "execute_live_approved",
        lambda *_args, **_kwargs: {
            "processed": 1,
            "results": [
                {
                    "status": "submit_failed",
                    "failure_code": "submit_failed",
                    "ticket_hash": "ticket",
                    "underlying": "AMD",
                }
            ],
        },
    )
    sent = []
    monkeypatch.setattr(
        execution,
        "send_lathi_alert",
        lambda **kwargs: sent.append(kwargs) or AlertResult(attempted=True, ok=True, mode="spool"),
    )

    result = execution.execute_live_approved_with_recovery(_config(), submit=True, store=store)

    assert result["recovery"] == {"attempted": False}
    assert result["operator_notification"]["needed"] is True
    assert len(sent) == 1
    assert "next scheduled planner" in sent[0]["body"]


def test_no_selected_entry_is_silent(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    monkeypatch.setattr(
        execution,
        "execute_live_approved",
        lambda *_args, **_kwargs: {"processed": 0, "results": []},
    )
    monkeypatch.setattr(
        execution,
        "send_lathi_alert",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no selected entry should stay silent")),
    )

    result = execution.execute_live_approved_with_recovery(_config(), submit=True, store=store)

    assert result["operator_notification"]["needed"] is False


@pytest.mark.parametrize("terminal_status", ["canceled", "cancelled", "expired"])
def test_terminal_unfilled_selected_entry_is_routine_and_silent(
    tmp_path, monkeypatch, terminal_status
) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    monkeypatch.setattr(
        execution,
        "execute_live_approved",
        lambda *_args, **_kwargs: {
            "processed": 1,
            "results": [
                {
                    "status": "blocked",
                    "reason": f"basket_ticket_failed:{terminal_status}",
                }
            ],
        },
    )
    monkeypatch.setattr(
        execution,
        "send_lathi_alert",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal unfilled entry must stay in evidence surfaces")
        ),
    )

    result = execution.execute_live_approved_with_recovery(_config(), submit=True, store=store)

    assert result["operator_notification"] == {
        "needed": False,
        "attempted": False,
        "reason": "no_selected_entry_failure",
    }


def test_self_handled_risk_limit_does_not_page_operator(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    monkeypatch.setattr(
        execution,
        "execute_live_approved",
        lambda *_args, **_kwargs: {
            "processed": 1,
            "results": [
                {
                    "status": "blocked",
                    "reason": "blocked_risk_manager:daily_new_positions_cap",
                    "underlying": "AAPL",
                }
            ],
            "health_gate": {
                "blocked": True,
                "reasons": ["daily_new_positions_cap"],
                "events": [
                    {
                        "reason": "daily_new_positions_cap",
                        "operator_state": "self_handled",
                    }
                ],
            },
        },
    )
    monkeypatch.setattr(
        execution,
        "send_lathi_alert",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("an owned safety stop must stay silent")
        ),
    )

    result = execution.execute_live_approved_with_recovery(_config(), submit=True, store=store)

    assert result["operator_notification"] == {
        "needed": False,
        "attempted": False,
        "reason": "no_selected_entry_failure",
    }


def test_retryable_close_deferral_is_owned_by_next_management_cycle(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    monkeypatch.setattr(
        execution,
        "execute_live_approved",
        lambda *_args, **_kwargs: {
            "processed": 1,
            "results": [
                {
                    "status": "deferred_market_closed",
                    "reason": "close_cutoff_reached",
                    "intent_type": "close",
                    "ticket_hash": "nvda-close",
                    "underlying": "NVDA",
                    "submission_window": {"retryable_next_session": True},
                }
            ],
        },
    )
    monkeypatch.setattr(
        execution,
        "send_lathi_alert",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("retryable next-session management must stay silent")
        ),
    )

    result = execution.execute_live_approved_with_recovery(_config(), submit=True, store=store)

    assert result["operator_notification"] == {
        "needed": False,
        "attempted": False,
        "reason": "no_selected_entry_failure",
    }


def test_auto_selected_advisory_safety_cap_stays_silent(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    sent = []
    monkeypatch.setattr(
        execution,
        "send_lathi_alert",
        lambda **kwargs: sent.append(kwargs) or AlertResult(attempted=True, ok=True, mode="spool"),
    )
    candidate = SimpleNamespace(
        candidate_id="cand-baba",
        underlying="BABA",
        structure="put_spread",
        score=9.0,
        rejection_reason="live_risk_underlying_cap:BABA",
    )

    result = execution.notify_live_advisory_risk_block(_config(), store, [candidate])

    assert result == {
        "needed": False,
        "attempted": False,
        "reason": "self_handled_safety_limit",
    }
    assert sent == []
    assert store.latest_event("live_advisory_risk_block_self_handled")["reason"] == "live_risk_underlying_cap:BABA"


def test_auto_selected_drawdown_block_still_notifies(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    sent = []
    monkeypatch.setattr(
        execution,
        "send_lathi_alert",
        lambda **kwargs: sent.append(kwargs) or AlertResult(attempted=True, ok=True, mode="spool"),
    )
    candidate = SimpleNamespace(
        candidate_id="cand-risk",
        underlying="SPY",
        structure="put_spread",
        score=9.0,
        rejection_reason="live_risk_manager_blocked:risk_daily_drawdown_breaker",
    )

    result = execution.notify_live_advisory_risk_block(_config(), store, [candidate])

    assert result["ok"] is True
    assert len(sent) == 1


def test_stale_selection_notifies_once_without_replanning(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "state.db")
    monkeypatch.setattr(execution, "execute_live_approved", lambda *a, **kw: _stale_result())
    monkeypatch.setattr(advisory, "run_live_advisory_plan", lambda *a, **kw: pytest.fail("executor must not plan"))
    sent = []
    def alert(**kw):
        sent.append(kw)
        return AlertResult(attempted=True, ok=True, mode="spool")
    monkeypatch.setattr(execution, "send_lathi_alert", alert)
    first = execution.execute_live_approved_with_recovery(_config(), submit=True, store=store)
    second = execution.execute_live_approved_with_recovery(_config(), submit=True, store=store)
    assert first["recovery"] == {"attempted": False}
    assert first["operator_notification"]["ok"]
    assert second["operator_notification"]["reason"] == "unchanged_selected_entry_failure"
    assert len(sent) == 1


def test_lost_current_selection_is_visible_without_ledger_submission(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "state.db")
    ticket = {"ticket_hash": "selected", "order_id": "id", "plan_id": "plan", "candidate_id": "candidate", "intent_type": "open", "underlying": "NTAP"}
    store.save_live_order_intent(ticket, status="pending_approval")
    config = _config()
    config["live"]["entry_approval_mode"] = "auto_top_plan"
    store.event("live_current_selection", {"trading_date": execution.market_today(config), "ticket_hashes": ["selected"]})
    monkeypatch.setattr(execution, "execute_live_approved", lambda *a, **kw: {"processed": 0, "results": []})
    monkeypatch.setattr(execution, "send_lathi_alert", lambda **kw: AlertResult(attempted=True, ok=True, mode="spool"))
    result = execution.execute_live_approved_with_recovery(config, submit=True, store=store)
    assert result["results"][0]["reason"] == "selected_entry_missing_from_cockpit"
    assert store.live_order_intent("selected")["_ledger_status"] == "pending_approval"
    store.update_live_order_intent_status("selected", "submit_uncertain")
    assert execution._missing_selected_entry(config, store) is None
