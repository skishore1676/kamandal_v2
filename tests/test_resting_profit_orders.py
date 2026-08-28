from __future__ import annotations

from dataclasses import replace

import pytest

from kamandal_v2.domain.models import ChainSnapshot, OptionLeg, OptionQuote
from kamandal_v2.live.execution import (
    REPLACE_CANCEL_PENDING,
    REPLACE_WAITING_CANCEL,
    _close_expire_due,
    _close_reprice_due,
    _advance_staged_replacement,
    stage_live_management_replacement,
)
from kamandal_v2.live.orders import build_csa_live_ticket
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.action_arbiter import arbitrate_actions
from kamandal_v2.strategy_lanes.lane_common import propose_action
from kamandal_v2.strategy_lanes.management_runtime import (
    _management_ticket,
    _resting_profit_proposal,
    _target_close_price,
    resting_profit_policy,
    run_live_lifecycle_management,
    run_shadow_lifecycle_management,
)
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.strategy_lanes.models import ActionType, CsaStage, LaneId, LifecycleState, SourceMode
from kamandal_v2.strategy_lanes.observations import PackageObservation
from kamandal_v2.strategy_lanes.policy import CsaPolicy
from kamandal_v2.strategy_lanes.shadow_execution import ShadowExecutionAdapter
from kamandal_v2.strategy_lanes.store import CsaStore


NOW = "2026-08-28T16:00:00Z"


def _policy(*, target: float = 50.0) -> CsaPolicy:
    return CsaPolicy(
        playbook_id="vertical",
        lane=LaneId.CALL_VERTICAL,
        stage=CsaStage.SHADOW,
        source_mode=SourceMode.IDEA,
        management={"lifecycle": {"fill": {"max_attempts": 0, "price_increment": 0.05}}},
        resolved_fields={"profit_target_pct": target, "max_bid_ask_pct": 0.25},
        policy_hash="policy",
        source="fixture",
        read_at=NOW,
    )


def _lifecycle(*, entry: float = 1.5, cumulative: float | None = None) -> LifecycleState:
    return LifecycleState(
        lifecycle_id="life",
        opportunity_id="opp",
        lane=LaneId.CALL_VERTICAL,
        version=3,
        status="open",
        active_legs=(
            {"role": "short_call", "side": "sell", "option_type": "call", "strike": 100.0, "expiration": "2026-10-16", "quantity": 1},
            {"role": "long_call", "side": "buy", "option_type": "call", "strike": 105.0, "expiration": "2026-10-16", "quantity": 1},
        ),
        cashflow_ledger=({"amount": entry, "filled_at": "2026-08-20T15:00:00Z"},),
        opened_at="2026-08-20T15:00:00Z",
        updated_at=NOW,
        policy_hash="policy",
        metadata={"underlying": "XYZ", "cumulative_cashflow": entry if cumulative is None else cumulative},
    )


def _legs() -> tuple[OptionLeg, ...]:
    return (
        OptionLeg("short_call", "sell", "call", 100, "2026-10-16", 1, 2.0, 1.95, 2.05, 0.3, 0, 0, 0, 100),
        OptionLeg("long_call", "buy", "call", 105, "2026-10-16", 1, 1.4, 1.35, 1.45, 0.2, 0, 0, 0, 100),
    )


def _observation(*, actionable: bool = True, midpoint_pnl: float = 0.20) -> PackageObservation:
    return PackageObservation(
        observation_id="obs",
        lifecycle_id="life",
        lifecycle_version=3,
        mode="shadow",
        underlying="XYZ",
        observed_at=NOW,
        snapshot_id="snapshot",
        snapshot_captured_at=NOW,
        quote_source="fixture",
        midpoint_liquidation=-1.30,
        natural_liquidation=-1.35,
        midpoint_pnl=midpoint_pnl,
        natural_pnl=midpoint_pnl - 0.05,
        profit_pct=midpoint_pnl / 1.5 * 100,
        loss_multiple=0.0,
        max_leg_bid_ask_pct=0.05,
        package_bid_ask_pct=0.05,
        max_bid_ask_pct=0.25,
        quote_fresh=actionable,
        pricing_complete=actionable,
        quote_actionable=actionable,
        quote_blockers=() if actionable else ("stale_snapshot",),
    )


def test_resting_profit_policy_is_disabled_by_default_and_mode_scoped() -> None:
    assert resting_profit_policy({}, "live").enabled is False
    assert resting_profit_policy({}, "shadow").enabled is False
    config = {"live": {"resting_profit": {"live_enabled": True, "shadow_enabled": False, "arm_progress_pct": 25}}}
    assert resting_profit_policy(config, "live").enabled is True
    assert resting_profit_policy(config, "shadow").enabled is False


def test_target_close_price_preserves_original_target_dollars_after_adjustment() -> None:
    assert _target_close_price(_lifecycle(entry=1.50), _policy(target=50)) == pytest.approx(-0.75)
    assert _target_close_price(_lifecycle(entry=-4.00), _policy(target=25)) == pytest.approx(5.00)
    assert _target_close_price(_lifecycle(entry=1.50, cumulative=2.10), _policy(target=50)) == pytest.approx(-1.35)


def test_resting_proposal_requires_actionable_arm_progress_and_is_day_stable() -> None:
    lifecycle = _lifecycle()
    policy = resting_profit_policy(
        {"live": {"resting_profit": {"shadow_enabled": True, "arm_progress_pct": 25}}},
        "shadow",
    )
    below = _resting_profit_proposal(lifecycle, _policy(), _observation(midpoint_pnl=0.18), policy, proposed_at=NOW)
    blocked = _resting_profit_proposal(lifecycle, _policy(), _observation(actionable=False, midpoint_pnl=0.30), policy, proposed_at=NOW)
    armed = _resting_profit_proposal(lifecycle, _policy(), _observation(midpoint_pnl=0.20), policy, proposed_at=NOW)
    replay = _resting_profit_proposal(lifecycle, _policy(), _observation(midpoint_pnl=0.20), policy, proposed_at="2026-08-28T19:00:00Z")

    assert below is None
    assert blocked is None
    assert armed is not None
    assert armed.action_id == replay.action_id
    assert armed.reason_codes == ("profit_target_resting",)


@pytest.mark.parametrize(
    ("reason_class", "action_type"),
    [
        ("hard_emergency", ActionType.CLOSE),
        ("mandatory_event_exit", ActionType.CLOSE),
        ("executable_profit", ActionType.CLOSE),
        ("time_decision", ActionType.CLOSE),
        ("adverse_price_loss", ActionType.CLOSE),
        ("lane_adjustment", ActionType.ADJUST),
    ],
)
def test_higher_priority_management_supersedes_resting_profit(reason_class, action_type) -> None:  # noqa: ANN001
    lifecycle = _lifecycle()
    resting = propose_action(lifecycle, ActionType.CLOSE, "profit_target_resting", arbiter_class="resting_profit", proposed_at=NOW)
    higher = propose_action(lifecycle, action_type, "higher_priority", arbiter_class=reason_class, proposed_at=NOW)

    assert arbitrate_actions((resting, higher)).selected.reason_codes == ("higher_priority",)


def test_resting_ticket_uses_exact_target_and_never_concedes_in_shadow() -> None:
    lifecycle = _lifecycle()
    action = propose_action(
        lifecycle,
        ActionType.CLOSE,
        "profit_target_resting",
        arbiter_class="resting_profit",
        proposed_at=NOW,
        payload={"resting_order_day": "2026-08-28", "arm_progress_pct": 25.0},
    )
    observation = _observation(midpoint_pnl=0.20)
    ticket = _management_ticket(
        arbitrate_actions((action,)).selected,
        lifecycle,
        _policy(),
        _legs(),
        {"midpoint_liquidation": -1.30, "natural_liquidation": -1.35},
        "XYZ",
        NOW,
        observation=observation,
        config={},
    )
    live = build_csa_live_ticket(ticket)
    fill = ShadowExecutionAdapter().simulate_fill(
        ticket,
        {
            ticket.legs[0].instrument_id: {"bid": 1.95, "ask": 2.05, "fresh": True},
            ticket.legs[1].instrument_id: {"bid": 1.20, "ask": 1.30, "fresh": True},
        },
        {"max_attempts": 0, "price_increment": 0.50},
        observed_at=NOW,
        attempt=4,
    )

    assert ticket.order_kind == "debit"
    assert ticket.limit_price == pytest.approx(0.75)
    assert live["limit_price"] == "0.75"
    assert live["time_in_force"] == "DAY"
    assert live["resting_profit_order"] is True
    assert live["execution_envelope"]["initial"] == "strategy_target"
    assert live["execution_envelope"]["boundary"] == "strategy_target"
    assert fill.status == "working"
    assert fill.working_price == pytest.approx(0.75)


def test_debit_target_becomes_favorably_rounded_close_credit() -> None:
    lifecycle = _lifecycle(entry=-4.01)
    action = propose_action(
        lifecycle,
        ActionType.CLOSE,
        "profit_target_resting",
        arbiter_class="resting_profit",
        proposed_at=NOW,
        payload={"resting_order_day": "2026-08-28", "arm_progress_pct": 25.0},
    )
    ticket = _management_ticket(
        arbitrate_actions((action,)).selected,
        lifecycle,
        _policy(target=25),
        _legs(),
        {"midpoint_liquidation": 4.50, "natural_liquidation": 4.40},
        "XYZ",
        NOW,
        observation=_observation(midpoint_pnl=0.25),
        config={},
    )
    live = build_csa_live_ticket(ticket)

    assert ticket.order_kind == "credit"
    assert ticket.limit_price == pytest.approx(5.0125)
    assert live["limit_price"] == "-5.05"


def test_resting_live_order_skips_generic_reprice_and_early_expiry(tmp_path) -> None:
    store = LocalStore(tmp_path / "state.db")
    ticket = {"intent_type": "close", "resting_profit_order": True, "created_at": "2026-08-28T13:00:00Z"}
    broker = {"status": "WORKING", "enteredTime": "2026-08-28T13:00:00Z"}
    config = {"live": {"exit_reprice": {"enabled": True, "after_minutes": 1, "expire_after_minutes": 1, "max_reprices": 5}}}

    assert _close_reprice_due(store, ticket, broker, config) is False
    assert _close_expire_due(store, ticket, broker, config) is False


def test_management_supersession_persists_child_before_broker_cancel(tmp_path) -> None:
    store = LocalStore(tmp_path / "state.db")
    parent = {
        "ticket_hash": "parent",
        "order_id": "parent-order",
        "plan_id": "life",
        "candidate_id": "parent-candidate",
        "intent_type": "close",
        "underlying": "XYZ",
        "created_at": NOW,
        "resting_profit_order": True,
    }
    child = {
        "ticket_hash": "child",
        "order_id": "child-order",
        "plan_id": "life",
        "candidate_id": "child-candidate",
        "intent_type": "close",
        "underlying": "XYZ",
        "created_at": NOW,
    }
    store.save_live_order_intent(parent, status="submitted")

    stage_live_management_replacement(store, parent, child)

    assert store.live_order_intent("parent")["_ledger_status"] == REPLACE_CANCEL_PENDING
    staged = store.live_order_intent("child")
    assert staged["_ledger_status"] == REPLACE_WAITING_CANCEL
    assert staged["parent_ticket_hash"] == "parent"


def test_parent_fill_during_cancel_aborts_child_and_projects_once(tmp_path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "state.db")
    parent = {
        "ticket_hash": "parent",
        "order_id": "parent-order",
        "plan_id": "life",
        "candidate_id": "parent-candidate",
        "intent_type": "close",
        "underlying": "XYZ",
        "created_at": NOW,
        "resting_profit_order": True,
        "csa_lifecycle_id": "life",
    }
    child = {
        "ticket_hash": "child",
        "order_id": "child-order",
        "plan_id": "life",
        "candidate_id": "child-candidate",
        "intent_type": "adjust",
        "underlying": "XYZ",
        "created_at": NOW,
    }
    store.save_live_order_intent(parent, status="submitted")
    stage_live_management_replacement(store, parent, child)
    projected: list[str] = []
    monkeypatch.setattr(
        "kamandal_v2.live.execution._adopt_csa_live_fill",
        lambda *_args, **_kwargs: projected.append("life") or {"status": "closed"},
    )

    result = _advance_staged_replacement(
        object(),
        store,
        store.live_order_intent("parent"),
        {"status": "FILLED", "filledQuantity": 1},
    )

    assert result["reprice_status"] == "aborted_parent_filled"
    assert result["csa_lifecycle_projection"] == {"status": "closed"}
    assert projected == ["life"]
    assert store.live_order_intent("parent")["_ledger_status"] == "close_filled"
    assert store.live_order_intent("child")["_ledger_status"] == "replace_aborted_parent_filled"


def test_parent_partial_fill_aborts_child_and_requires_reconciliation(tmp_path) -> None:
    store = LocalStore(tmp_path / "state.db")
    parent = {
        "ticket_hash": "parent",
        "order_id": "parent-order",
        "plan_id": "life",
        "candidate_id": "parent-candidate",
        "intent_type": "close",
        "underlying": "XYZ",
        "created_at": NOW,
        "resting_profit_order": True,
    }
    child = {
        "ticket_hash": "child",
        "order_id": "child-order",
        "plan_id": "life",
        "candidate_id": "child-candidate",
        "intent_type": "close",
        "underlying": "XYZ",
        "created_at": NOW,
    }
    store.save_live_order_intent(parent, status="submitted")
    stage_live_management_replacement(store, parent, child)

    result = _advance_staged_replacement(
        object(),
        store,
        store.live_order_intent("parent"),
        {"status": "PARTIALLY_FILLED", "filledQuantity": 0.5},
    )

    assert result["reprice_status"] == "aborted_parent_partial_fill"
    assert result["needs_position_reconciliation"] is True
    assert store.live_order_intent("parent")["_ledger_status"] == "partially_filled"
    assert store.live_order_intent("child")["_ledger_status"] == "replace_aborted_parent_partial_fill"


def _runtime_lifecycle(mode: str) -> LifecycleState:
    frozen = {
        "playbook_id": "vertical",
        "lane": "call_vertical",
        "stage": "live" if mode == "live" else "shadow",
        "source_mode": "idea",
        "management": {"lifecycle": {"fill": {"max_attempts": 2, "price_increment": 0.05}}},
        "resolved_fields": {
            "profit_target_pct": 50,
            "max_loss_multiple": 2,
            "exit_dte_min": 21,
            "half_time_exit": False,
            "max_bid_ask_pct": 0.25,
        },
        "policy_hash": "policy",
        "source": "fixture",
        "read_at": NOW,
    }
    return replace(
        _lifecycle(),
        metadata={
            **_lifecycle().metadata,
            "execution_mode": mode,
            "compiled_management_policy": frozen,
        },
    )


class _RuntimeMarket:
    def __init__(self, captured_at: str = NOW) -> None:
        self.captured_at = captured_at

    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        assert underlying == "XYZ"
        return ChainSnapshot(
            chain_snapshot_id="runtime-snapshot",
            underlying="XYZ",
            captured_at=self.captured_at,
            underlying_price=101.0,
            quotes=[
                OptionQuote("XYZ", "2026-10-16", "call", 100, 2.00, 2.10, 0.30, 0, 0, 0, 0.2, 500),
                OptionQuote("XYZ", "2026-10-16", "call", 105, 0.70, 0.80, 0.20, 0, 0, 0, 0.2, 500),
            ],
            source="fixture",
        )


def _runtime_store(tmp_path, mode: str) -> tuple[str, CsaStore]:  # noqa: ANN001
    database = tmp_path / f"{mode}.db"
    LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    store = CsaStore(database)
    store.save_lifecycle(_runtime_lifecycle(mode))
    return str(database), store


def test_canonical_live_manager_stages_one_exact_target_and_default_is_inert(tmp_path) -> None:
    disabled_db, _disabled = _runtime_store(tmp_path, "live-disabled")
    # Normalize fixture mode after using a distinct database name.
    disabled_store = CsaStore(disabled_db)
    disabled_store.save_lifecycle(_runtime_lifecycle("live"))
    disabled = run_live_lifecycle_management(
        {}, sqlite_path=disabled_db, market=_RuntimeMarket(), observed_at=NOW
    )
    assert disabled.ok
    assert LocalStore(disabled_db, read_only=True).live_order_intents_by_status({"stage_approved_pending_submit"}) == []

    database, _store = _runtime_store(tmp_path, "live")
    config = {"live": {"resting_profit": {"live_enabled": True, "arm_progress_pct": 25}}}
    first = run_live_lifecycle_management(
        config, sqlite_path=database, market=_RuntimeMarket(), observed_at=NOW
    )
    second = run_live_lifecycle_management(
        config, sqlite_path=database, market=_RuntimeMarket(), observed_at="2026-08-28T19:00:00Z"
    )
    tickets = LocalStore(database, read_only=True).live_order_intents_by_status({"stage_approved_pending_submit"})

    assert first.ok and second.ok
    assert first.selected_actions == {"close": 1}
    assert len(tickets) == 1
    assert tickets[0]["resting_profit_order"] is True
    assert tickets[0]["limit_price"] == "0.75"


def test_canonical_shadow_manager_keeps_one_day_target_then_rearms_next_day(tmp_path) -> None:
    database, store = _runtime_store(tmp_path, "shadow")
    config = {"live": {"resting_profit": {"shadow_enabled": True, "arm_progress_pct": 25}}}

    first = run_shadow_lifecycle_management(
        config, sqlite_path=database, market=_RuntimeMarket(), observed_at=NOW
    )
    same_day = run_shadow_lifecycle_management(
        config, sqlite_path=database, market=_RuntimeMarket(), observed_at="2026-08-28T19:00:00Z"
    )
    next_day = run_shadow_lifecycle_management(
        config,
        sqlite_path=database,
        market=_RuntimeMarket("2026-08-29T16:00:00Z"),
        observed_at="2026-08-29T16:00:00Z",
    )
    intents = store.rows("csa_shadow_order_intents")

    assert first.ok and same_day.ok and next_day.ok
    assert len(intents) == 2
    assert [row["status"] for row in intents] == ["expired_eod", "working"]
