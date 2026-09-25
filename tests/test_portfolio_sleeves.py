from __future__ import annotations

import pytest

from kamandal_v2.domain.models import PortfolioState, PreflightResult
from kamandal_v2.intelligence.trade_sources import compile_trade_source_policies
from kamandal_v2.live.execution import (
    _execute_ticket, _fresh_sheet_entry_blocker, _fresh_source_route_blocker, _replace_live_order_atomically,
)
from kamandal_v2.portfolio_sleeves import (
    CURRENT_IDEA, GURU_EXACT, compile_sleeve_policy, live_sleeve_usage,
    occupied_source_opportunities, sleeve_entry_blocker,
)
from kamandal_v2.stores.sqlite import LocalStore


ROWS = [
    {"lane": "current_idea", "max_bpr_pct": "40"},
    {"lane": "guru_exact", "max_bpr_pct": "40"},
    {"lane": "portfolio_total", "max_bpr_pct": "80"},
]


def test_sheet_caps_are_strict_and_existing_excess_only_blocks_its_lane() -> None:
    policy = compile_sleeve_policy(ROWS)
    with pytest.raises(ValueError, match="missing guru_exact"):
        compile_sleeve_policy([ROWS[0], ROWS[2]])
    with pytest.raises(ValueError, match="duplicate"):
        compile_sleeve_policy([*ROWS, ROWS[2]])

    class Store:
        def open_live_position_groups(self):
            return [{"sleeve_id": CURRENT_IDEA, "candidate": {"estimated_bpr": 5480}}]

        def live_order_intents_by_type(self, *_args, **_kwargs):
            return []

    usage = live_sleeve_usage(Store(), PortfolioState(10_000, 4520, 5480, 1))
    assert sleeve_entry_blocker(policy, usage, [(CURRENT_IDEA, 100)]) == "sleeve_bpr_cap:current_idea"
    assert sleeve_entry_blocker(policy, usage, [(GURU_EXACT, 2519)]) == ""
    assert sleeve_entry_blocker(policy, usage, [(GURU_EXACT, 2521)]) == "sleeve_bpr_cap:portfolio_total"


def test_pending_replacement_is_one_commitment_and_legacy_book_is_current(tmp_path) -> None:
    store = LocalStore(tmp_path / "sleeves.db")
    store.save_live_position_group("old", {"candidate": {"estimated_bpr": 3000}}, status="open")
    for revision, bpr in (("first", 1100), ("replacement", 1200)):
        store.save_live_order_intent({
            "ticket_hash": revision, "order_id": revision, "plan_id": "same", "candidate_id": "same",
            "intent_type": "open", "sleeve_id": GURU_EXACT, "entry_risk_budget": bpr,
        }, status="submitted")
    usage = live_sleeve_usage(store, PortfolioState(10_000, 6500, 3500, 1))
    assert usage.current_idea_bpr == 3500  # Broker residual is charged conservatively.
    assert usage.guru_exact_bpr == 1200
    assert usage.pending_bpr == 1200
    assert usage.portfolio_total_bpr == 4700


def test_staged_guru_ticket_obeys_fresh_off_switch(monkeypatch, tmp_path) -> None:
    ticket = {
        "ticket_hash": "this", "order_id": "this", "plan_id": "p", "candidate_id": "c",
        "intent_type": "open", "sleeve_id": GURU_EXACT, "source_id": "greg_harmon",
        "source_output_kind": "exact_package", "structure": "short_strangle",
        "source_opportunity_id": "op-1", "source_package_signature": "sig-1", "entry_risk_budget": 1000,
    }
    store = LocalStore(tmp_path / "sleeves.db")
    store.save_live_order_intent(ticket, status="stage_approved_pending_submit")
    monkeypatch.setattr("kamandal_v2.live.execution.pull_trade_sources", lambda _config: [
        {"source_id": "greg_harmon", "output_kind": "exact_package", "mode": "off", "live_structures": "short_strangle"},
    ])
    monkeypatch.setattr("kamandal_v2.live.execution.pull_portfolio_sleeves", lambda _config: ROWS)

    class Adapter:
        def account_state(self):
            return PortfolioState(10_000, 10_000, 0, 0)

    assert compile_trade_source_policies([{"source_id": "greg_harmon", "output_kind": "exact_package", "mode": "off"}]).ok
    assert _fresh_sheet_entry_blocker({}, Adapter(), store, ticket, preflight_bpr=1000) == "entry_source_route_not_live"


def test_one_source_opportunity_cannot_open_again_through_the_other_route(tmp_path) -> None:
    store = LocalStore(tmp_path / "sleeves.db")
    exact = {
        "ticket_hash": "exact", "order_id": "exact", "plan_id": "p2", "candidate_id": "c2",
        "intent_type": "open", "sleeve_id": GURU_EXACT, "source_id": "mike_butler",
        "source_output_kind": "exact_package", "source_opportunity_id": "opp-1",
        "source_package_signature": "sig-1", "structure": "call_calendar", "entry_risk_budget": 500,
    }
    idea = {**exact, "ticket_hash": "idea", "order_id": "idea", "plan_id": "p1",
            "candidate_id": "c1", "sleeve_id": CURRENT_IDEA, "source_output_kind": "idea"}
    store.save_live_order_intent(idea, status="submitted")
    assert ("mike_butler", "opp-1") in occupied_source_opportunities(store)
    assert ("mike_butler", "opp-1") not in occupied_source_opportunities(store, exclude_ticket_hash="idea")

    class Adapter:
        def account_state(self):
            return PortfolioState(10_000, 10_000, 0, 0)

    assert _fresh_sheet_entry_blocker({}, Adapter(), store, exact, preflight_bpr=500) == "source_opportunity_already_open_or_pending"


def test_source_switch_does_not_cancel_legacy_working_planner_order(monkeypatch) -> None:
    def unexpected_sheet_read(_config):
        raise AssertionError("ordinary planner order has no source switch")

    monkeypatch.setattr("kamandal_v2.live.execution.pull_trade_sources", unexpected_sheet_read)
    assert _fresh_source_route_blocker({"portfolio": {"sleeves_source": "sheet"}}, {"intent_type": "open"}) == ""
    assert _fresh_source_route_blocker({"portfolio": {"sleeves_source": "sheet"}}, {"intent_type": "open", "source_id": ""}) == ""


def test_current_lane_entry_does_not_depend_on_guru_route_sheet(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("kamandal_v2.live.execution.pull_portfolio_sleeves", lambda _config: ROWS)

    def broken_guru_routes(_config):
        raise ValueError("guru route typo")

    monkeypatch.setattr("kamandal_v2.live.execution.pull_trade_sources", broken_guru_routes)

    class Adapter:
        def account_state(self):
            return PortfolioState(10_000, 10_000, 0, 0)

    ticket = {"ticket_hash": "current", "intent_type": "open", "sleeve_id": CURRENT_IDEA,
              "source_id": "", "entry_risk_budget": 500}
    assert _fresh_sheet_entry_blocker({}, Adapter(), LocalStore(tmp_path / "current.db"), ticket, preflight_bpr=500) == ""


def test_atomic_exact_reprice_cannot_raise_bpr_above_approved_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kamandal_v2.live.execution._fresh_source_route_blocker", lambda *_args: "")
    class Adapter:
        replacements = 0

        def preflight_ticket(self, _ticket):
            return PreflightResult(True, 600, "ok", {"broker_bpr_provided": True})

        def replace_order(self, *_args):
            self.replacements += 1
            return {"orderId": "new"}

    adapter = Adapter()
    old = {"ticket_hash": "old", "order_id": "old", "intent_type": "open", "structure": "call_calendar",
           "source_output_kind": "exact_package", "entry_risk_budget": 500}
    new = {**old, "ticket_hash": "new", "order_id": "new"}
    result = _replace_live_order_atomically(adapter, LocalStore(tmp_path / "atomic.db"), {}, old, new,
                                            broker_status={"status": "WORKING"}, close=False)
    assert result["reprice_status"] == "deferred_preflight_failed"
    assert result["reprice_message"] == "fresh_preflight_exceeds_approved_risk_budget"
    assert adapter.replacements == 0


def test_off_switch_blocks_entry_post_but_does_not_block_exit(monkeypatch, tmp_path) -> None:
    store = LocalStore(tmp_path / "sleeves.db")
    entry = {
        "ticket_hash": "entry", "order_id": "entry", "plan_id": "p", "candidate_id": "c",
        "intent_type": "open", "sleeve_id": GURU_EXACT, "source_id": "mike_butler",
        "source_output_kind": "exact_package", "source_opportunity_id": "op-1",
        "source_package_signature": "sig-1", "structure": "short_strangle", "entry_risk_budget": 500,
    }
    store.save_live_order_intent(entry, status="stage_approved_pending_submit")
    monkeypatch.setattr("kamandal_v2.live.execution.submission_window", lambda *_args, **_kwargs: {"allowed": True})
    monkeypatch.setattr("kamandal_v2.live.execution._ticket_fresh", lambda *_args: True)
    monkeypatch.setattr("kamandal_v2.live.execution._preflight_ticket_with_entry_risk", lambda *_args: PreflightResult(True, 500, "ok", {"broker_bpr_provided": True}))
    monkeypatch.setattr("kamandal_v2.live.execution.pull_trade_sources", lambda _config: [
        {"source_id": "mike_butler", "output_kind": "exact_package", "mode": "off", "live_structures": "short_strangle"},
    ])
    monkeypatch.setattr("kamandal_v2.live.execution.pull_portfolio_sleeves", lambda _config: ROWS)

    class Adapter:
        posts = 0

        def account_state(self):
            return PortfolioState(10_000, 10_000, 0, 0)

        def place_order_ticket(self, _ticket):
            self.posts += 1
            return {"orderId": "fake-broker-id"}

    adapter = Adapter()
    config = {"portfolio": {"sleeves_source": "sheet"}}
    result = _execute_ticket(config, adapter, store, entry, submit=True, close=False)
    assert result["status"] == "blocked"
    assert result["failure_code"] == "entry_source_route_not_live"
    assert adapter.posts == 0
    assert store.live_order_intent("entry")["_ledger_status"] == "blocked_source_route"

    exit_ticket = {**entry, "ticket_hash": "exit", "order_id": "exit", "intent_type": "close", "csa_lifecycle_id": "existing"}
    store.save_live_order_intent(exit_ticket, status="pending_close_approval")
    exit_result = _execute_ticket(config, adapter, store, exit_ticket, submit=True, close=True)
    assert exit_result["status"] == "submitted"
    assert adapter.posts == 1
