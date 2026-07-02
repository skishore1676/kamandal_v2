import json

from kamandal_v2.domain.models import PortfolioState
from kamandal_v2.live.operator_review import (
    apply_operator_review_decision,
    create_operator_review_request,
    parse_operator_review_decision,
    send_pending_operator_review_requests,
    send_operator_review_message,
)
from kamandal_v2.live.reconciliation import reconcile_live_positions
from kamandal_v2.stores.sqlite import LocalStore


def _group() -> dict:
    return {
        "group_id": "live_group_amzn",
        "underlying": "AMZN",
        "playbook_id": "call_spread_default",
        "structure": "call_spread",
        "candidate": {
            "candidate_id": "cand",
            "idea_id": "idea",
            "underlying": "AMZN",
            "playbook_id": "call_spread_default",
            "structure": "call_spread",
            "net_credit": 1.0,
            "legs": [
                {
                    "role": "short_call",
                    "side": "sell",
                    "option_type": "call",
                    "strike": 200,
                    "expiration": "2026-07-17",
                    "quantity": 1,
                    "mid": 2.0,
                    "bid": 1.95,
                    "ask": 2.05,
                    "delta": 0.25,
                    "gamma": 0.0,
                    "theta": 0.0,
                    "vega": 0.0,
                    "open_interest": 500,
                },
                {
                    "role": "long_call",
                    "side": "buy",
                    "option_type": "call",
                    "strike": 205,
                    "expiration": "2026-07-17",
                    "quantity": 1,
                    "mid": 1.0,
                    "bid": 0.95,
                    "ask": 1.05,
                    "delta": 0.15,
                    "gamma": 0.0,
                    "theta": 0.0,
                    "vega": 0.0,
                    "open_interest": 500,
                },
            ],
        },
    }


def _put_group(group_id: str, *, long_strike: int, short_strike: int, net_credit: float = 2.5) -> dict:
    return {
        "group_id": group_id,
        "underlying": "MRVL",
        "playbook_id": "put_spread_high_ivr",
        "structure": "put_spread",
        "candidate": {
            "candidate_id": group_id,
            "idea_id": group_id,
            "underlying": "MRVL",
            "playbook_id": "put_spread_high_ivr",
            "structure": "put_spread",
            "net_credit": net_credit,
            "legs": [
                {
                    "role": "long_put",
                    "side": "buy",
                    "option_type": "put",
                    "strike": long_strike,
                    "expiration": "2026-07-17",
                    "quantity": 1,
                    "mid": 11.0,
                    "bid": 10.9,
                    "ask": 11.1,
                    "delta": -0.16,
                    "gamma": 0.0,
                    "theta": -0.3,
                    "vega": 0.0,
                    "open_interest": 500,
                },
                {
                    "role": "short_put",
                    "side": "sell",
                    "option_type": "put",
                    "strike": short_strike,
                    "expiration": "2026-07-17",
                    "quantity": 1,
                    "mid": 13.5,
                    "bid": 13.4,
                    "ask": 13.6,
                    "delta": -0.2,
                    "gamma": 0.0,
                    "theta": -0.34,
                    "vega": 0.0,
                    "open_interest": 500,
                },
            ],
        },
    }


def _close_ticket(group: dict, ticket_hash: str = "ticket-close-filled") -> dict:
    return {
        "ticket_hash": ticket_hash,
        "order_id": f"order-{ticket_hash}",
        "plan_id": "plan-close",
        "candidate_id": str((group.get("candidate") or {}).get("candidate_id") or group["group_id"]),
        "idea_id": str((group.get("candidate") or {}).get("idea_id") or group["group_id"]),
        "group_id": group["group_id"],
        "intent_type": "close",
        "underlying": group["underlying"],
        "structure": group["structure"],
        "limit_price": "1.00",
    }


def _config() -> dict:
    return {
        "live": {
            "reconciliation": {
                "enabled": True,
                "broker_flat_confirmations_required": 2,
                "auto_retire_ghost_after_confirmations": True,
                "auto_local_repair_enabled": True,
                "block_management_on_open_issues": True,
            },
            "operator_review": {"enabled": True, "target": "123", "account": "default", "use_inline_buttons": True, "text_fallback": True},
            "telegram_approval": {"target": "123"},
        },
        "broker": {"active": "public"},
    }


def test_operator_review_parser_accepts_button_and_text() -> None:
    assert parse_operator_review_decision("callback_data: kamandal:review:or_123:hold") == {
        "request_id": "or_123",
        "action": "hold",
        "note": "",
    }
    assert parse_operator_review_decision("kamandal review or_123 dismiss false positive") == {
        "request_id": "or_123",
        "action": "dismiss",
        "note": "false positive",
    }


def test_send_operator_review_uses_lathi_telegram_ask(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    request = create_operator_review_request(
        _config(),
        request_type="live_reconciliation",
        subject_id="issue_1",
        title="Ghost position",
        summary="Local AMZN position is not at broker.",
        allowed_actions=["retire_local", "hold"],
        payload={"issue_id": "issue_1"},
        store=store,
    )
    calls = []

    def fake_run(command, **_kwargs):  # noqa: ANN001
        calls.append(command)
        return type("Result", (), {"returncode": 0, "stdout": '{"network_call_performed": true, "live_send_requested": true}', "stderr": ""})()

    monkeypatch.setattr("kamandal_v2.live.operator_review.subprocess.run", fake_run)
    result = send_operator_review_message(_config(), request, store=store)

    assert result["status"] == "sent"
    assert result["transport"] == "lathi"
    assert calls[0][0].endswith("lathi-bus") or calls[0][0:2] == ["python3", "-m"]
    assert "telegram-ask" in calls[0]
    assert "--live" in calls[0]
    assert "--option" in calls[0]
    options = [calls[0][index + 1] for index, part in enumerate(calls[0]) if part == "--option"]
    assert any(f"kamandal:review:{request['request_id']}:retire_local" in option for option in options)


def test_expired_sent_operator_reviews_do_not_consume_pending_cap(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    config = _config()
    config["live"]["operator_review"]["max_pending_requests"] = 1
    old_request = {
        "request_id": "or_old",
        "request_type": "live_reconciliation",
        "subject_id": "old_issue",
        "title": "Old review",
        "summary": "Expired review",
        "allowed_actions": ["hold"],
        "payload": {"issue_id": "old_issue"},
        "status": "sent",
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-01T00:30:00Z",
    }
    store.save_operator_review_request(old_request)

    request = create_operator_review_request(
        config,
        request_type="live_reconciliation",
        subject_id="new_issue",
        title="New review",
        summary="This should fit after the stale sent request expires.",
        allowed_actions=["hold"],
        payload={"issue_id": "new_issue"},
        store=store,
    )

    assert request["request_id"] != "or_old"
    assert store.operator_review_request("or_old")["_ledger_status"] == "expired"


def test_send_pending_operator_reviews_expires_sent_requests(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.save_operator_review_request(
        {
            "request_id": "or_old_sent",
            "request_type": "live_reconciliation",
            "subject_id": "old_issue",
            "title": "Old review",
            "summary": "Expired review",
            "allowed_actions": ["hold"],
            "payload": {"issue_id": "old_issue"},
            "status": "sent",
            "created_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-01-01T00:30:00Z",
        }
    )

    result = send_pending_operator_review_requests(_config(), store=store)

    assert result["skipped"] == [{"request_id": "or_old_sent", "reason": "expired"}]
    assert store.operator_review_request("or_old_sent")["_ledger_status"] == "expired"


def test_reconcile_auto_retires_ghost_after_two_flat_confirmations(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.save_live_position_group("live_group_amzn", _group(), status="open")
    store.save_live_position("live_group_amzn", "live_group_amzn", _group(), status="open")

    class Broker:
        def broker_positions(self):
            return []

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config: Broker())

    first = reconcile_live_positions(_config(), store=store)
    assert first["issues"][0]["issue_type"] == "ghost_local_position"
    assert store.live_position_group("live_group_amzn") is not None

    second = reconcile_live_positions(_config(), store=store)
    assert second["issues"] == []
    assert not store.open_live_position_groups()
    retired = store.live_reconciliation_issue(first["issues"][0]["issue_id"])
    assert retired["status"] == "retired"
    assert retired["observed_count"] == 2


def test_reconcile_suppresses_close_filled_ghost_review_until_confirmation(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    group = _group()
    store.save_live_position_group("live_group_amzn", group, status="open")
    store.save_live_position("live_group_amzn", "live_group_amzn", group, status="open")
    store.save_live_order_intent(_close_ticket(group), status="close_filled")

    class Broker:
        def broker_positions(self):
            return []

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config: Broker())

    first = reconcile_live_positions(_config(), store=store, send_review=True)

    assert first["issues"] == []
    open_issues = store.open_live_reconciliation_issues(group_id="live_group_amzn")
    assert len(open_issues) == 1
    assert open_issues[0]["decision"]["tier"] == "pending_confirmation"
    assert open_issues[0]["decision"]["reason"] == "close_filled_waiting_for_broker_flat_confirmation"
    assert store.operator_review_requests_by_status({"pending", "sent"}) == []
    assert store.live_position_group("live_group_amzn") is not None

    store.save_operator_review_request(
        {
            "request_id": f"or_{open_issues[0]['issue_id']}",
            "request_type": "live_reconciliation",
            "subject_id": open_issues[0]["issue_id"],
            "title": "Old review",
            "summary": "This was created before close-filled ghost suppression.",
            "allowed_actions": ["retire_local", "hold"],
            "payload": {"issue_id": open_issues[0]["issue_id"]},
            "status": "sent",
            "created_at": "2099-01-01T00:00:00Z",
            "expires_at": "2099-01-01T00:30:00Z",
        }
    )

    second = reconcile_live_positions(_config(), store=store, send_review=True)

    assert second["issues"] == []
    assert not store.open_live_position_groups()
    retired = store.live_reconciliation_issue(open_issues[0]["issue_id"])
    assert retired["status"] == "retired"
    assert retired["observed_count"] == 2
    request = store.operator_review_request(f"or_{open_issues[0]['issue_id']}")
    assert request["_ledger_status"] == "expired"
    assert request["expiration_reason"] == "superseded_by_auto_reconciliation"


def test_reconcile_send_review_failure_is_nonfatal(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.save_live_position_group("live_group_amzn", _group(), status="open")
    store.save_live_position("live_group_amzn", "live_group_amzn", _group(), status="open")
    config = _config()
    config["live"]["operator_review"]["max_pending_requests"] = 1
    store.save_operator_review_request(
        {
            "request_id": "or_current",
            "request_type": "live_reconciliation",
            "subject_id": "current_issue",
            "title": "Current review",
            "summary": "Still pending",
            "allowed_actions": ["hold"],
            "payload": {"issue_id": "current_issue"},
            "status": "sent",
            "created_at": "2099-01-01T00:00:00Z",
            "expires_at": "2099-01-01T00:30:00Z",
        }
    )

    class Broker:
        def broker_positions(self):
            return []

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config: Broker())

    result = reconcile_live_positions(config, store=store, send_review=True)

    assert result["status"] == "ok"
    assert result["issues"][0]["issue_type"] == "ghost_local_position"


def test_reconcile_aggregates_duplicate_local_occ_legs(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    first = _put_group("live_group_mrvl_1", long_strike=240, short_strike=250)
    second = _put_group("live_group_mrvl_2", long_strike=240, short_strike=250)
    for group in (first, second):
        store.save_live_position_group(group["group_id"], group, status="open")
        store.save_live_position(group["group_id"], group["group_id"], group, status="open")
    stale_issue = {
        "issue_id": "recon_stale_mrvl_qty",
        "issue_type": "quantity_mismatch",
        "subject_id": "MRVL260717P00250000",
        "group_id": "live_group_mrvl_1",
        "underlying": "MRVL",
        "status": "open",
    }
    store.save_live_reconciliation_issue(stale_issue)

    class Broker:
        def broker_positions(self):
            return [
                {
                    "asset_type": "option",
                    "occ_symbol": "MRVL260717P00240000",
                    "underlying": "MRVL",
                    "quantity": 2.0,
                },
                {
                    "asset_type": "option",
                    "occ_symbol": "MRVL260717P00250000",
                    "underlying": "MRVL",
                    "quantity": -2.0,
                },
            ]

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config: Broker())

    result = reconcile_live_positions(_config(), store=store)

    assert result["issues"] == []
    assert store.live_reconciliation_issue("recon_stale_mrvl_qty")["status"] == "resolved"
    assert store.open_live_reconciliation_issues(underlying="MRVL") == []


def test_reconcile_auto_retires_closed_duplicate_group_when_aggregate_matches_broker(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    closed = _put_group("live_group_mrvl_closed", long_strike=240, short_strike=250)
    open_group = _put_group("live_group_mrvl_open", long_strike=240, short_strike=250)
    for group in (closed, open_group):
        store.save_live_position_group(group["group_id"], group, status="open")
        store.save_live_position(group["group_id"], group["group_id"], group, status="open")
    store.save_live_order_intent(_close_ticket(closed), status="close_filled")

    class Broker:
        def broker_positions(self):
            return [
                {
                    "asset_type": "option",
                    "occ_symbol": "MRVL260717P00240000",
                    "underlying": "MRVL",
                    "quantity": 1.0,
                },
                {
                    "asset_type": "option",
                    "occ_symbol": "MRVL260717P00250000",
                    "underlying": "MRVL",
                    "quantity": -1.0,
                },
            ]

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config: Broker())

    result = reconcile_live_positions(_config(), store=store, send_review=True)

    assert result["issues"] == []
    open_ids = {group["group_id"] for group in store.open_live_position_groups()}
    assert open_ids == {"live_group_mrvl_open"}
    retired_issues = store.open_live_reconciliation_issues(underlying="MRVL")
    assert retired_issues == []
    closed_group = store.live_position_group("live_group_mrvl_closed")
    assert closed_group["closed_status"] == "reconciled_retired"
    assert closed_group["close_reason"] == "close_filled_ticket_reconciles_broker_aggregate"


def test_reconcile_does_not_auto_retire_duplicate_group_without_filled_close(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    first = _put_group("live_group_mrvl_1", long_strike=240, short_strike=250)
    second = _put_group("live_group_mrvl_2", long_strike=240, short_strike=250)
    for group in (first, second):
        store.save_live_position_group(group["group_id"], group, status="open")
        store.save_live_position(group["group_id"], group["group_id"], group, status="open")

    class Broker:
        def broker_positions(self):
            return [
                {
                    "asset_type": "option",
                    "occ_symbol": "MRVL260717P00240000",
                    "underlying": "MRVL",
                    "quantity": 1.0,
                },
                {
                    "asset_type": "option",
                    "occ_symbol": "MRVL260717P00250000",
                    "underlying": "MRVL",
                    "quantity": -1.0,
                },
            ]

    monkeypatch.setattr("kamandal_v2.live.reconciliation.broker_adapter", lambda _config: Broker())

    result = reconcile_live_positions(_config(), store=store)

    assert len(result["issues"]) == 2
    assert {group["group_id"] for group in store.open_live_position_groups()} == {"live_group_mrvl_1", "live_group_mrvl_2"}
    decisions = [issue["decision"] for issue in result["issues"]]
    assert {decision["tier"] for decision in decisions} == {"human_review"}
    assert {decision["reason"] for decision in decisions} == {"quantity_mismatch_requires_review"}


def test_live_portfolio_state_exposes_group_count_and_underlying_bpr(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    first = _put_group("live_group_mrvl_1", long_strike=240, short_strike=250, net_credit=2.5)
    second = _put_group("live_group_mrvl_2", long_strike=240, short_strike=250, net_credit=2.75)
    for group in (first, second):
        store.save_live_position_group(group["group_id"], group, status="open")
        store.save_live_position(group["group_id"], group["group_id"], group, status="open")
    base = PortfolioState(account_size=10_000, buying_power=5_000, bpr_used=5_000, positions_count=4)

    portfolio = store.live_portfolio_state(base)

    assert portfolio.positions_count == 2
    assert portfolio.bpr_used == 5_000
    assert portfolio.per_underlying_bpr == {"MRVL": 1475.0}
    assert round(portfolio.greeks.delta, 4) == 0.08
    assert round(portfolio.greeks.theta, 4) == 0.08


def test_reconciliation_review_retire_local_closes_group(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")
    store.save_live_position_group("live_group_amzn", _group(), status="open")
    store.save_live_position("live_group_amzn", "live_group_amzn", _group(), status="open")
    issue = {
        "issue_id": "recon_test",
        "issue_type": "ghost_local_position",
        "subject_id": "live_group_amzn",
        "group_id": "live_group_amzn",
        "underlying": "AMZN",
        "status": "open",
    }
    store.save_live_reconciliation_issue(issue)
    request = create_operator_review_request(
        _config(),
        request_type="live_reconciliation",
        subject_id="recon_test",
        title="Ghost position",
        summary="Local AMZN position is not at broker.",
        allowed_actions=["retire_local", "hold"],
        payload={"issue_id": "recon_test"},
        store=store,
    )

    result = apply_operator_review_decision(_config(), request["request_id"], "retire_local", store=store)

    assert result["result"]["issue_status"] == "retired"
    assert not store.open_live_position_groups()
