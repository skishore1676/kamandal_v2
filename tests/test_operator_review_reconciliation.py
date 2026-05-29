import json

from kamandal_v2.live.operator_review import (
    apply_operator_review_decision,
    create_operator_review_request,
    parse_operator_review_decision,
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


def _config() -> dict:
    return {
        "live": {
            "reconciliation": {
                "enabled": True,
                "broker_flat_confirmations_required": 2,
                "auto_retire_ghost_after_confirmations": True,
                "block_management_on_open_issues": True,
            },
            "operator_review": {"enabled": True, "target": "123", "use_inline_buttons": True, "text_fallback": True},
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


def test_send_operator_review_uses_presentation_buttons(tmp_path, monkeypatch) -> None:
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
        return type("Result", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

    monkeypatch.setattr("kamandal_v2.live.operator_review.subprocess.run", fake_run)
    result = send_operator_review_message(_config(), request, store=store)

    assert result["status"] == "sent"
    assert "--presentation" in calls[0]
    presentation = json.loads(calls[0][calls[0].index("--presentation") + 1])
    values = [button["value"] for button in presentation["blocks"][0]["buttons"]]
    assert f"kamandal:review:{request['request_id']}:retire_local" in values


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
    assert second["issues"][0]["observed_count"] == 2
    assert not store.open_live_position_groups()


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
