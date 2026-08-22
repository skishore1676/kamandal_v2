from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg, Plan, Playbook, PortfolioState, PreflightResult
from kamandal_v2.live.advisory import render_live_plan_rows
from kamandal_v2.live.execution import _entry_reprice_due, _fallback_submission_gate, _project_fallback_daily_plan, _repriced_open_ticket
from kamandal_v2.live.execution import _fallback_basket_cap_allows
from kamandal_v2.live.execution import _sync_live_orders_locked
from kamandal_v2.live.orders import APPROVE_LIVE, build_open_ticket, ticket_hash
from kamandal_v2.live.plan_fallback import PlanFallbackCoordinator, fallback_enabled, register_rank_one_attempt
from kamandal_v2.live.pricing import candidate_entry_limit_price, entry_campaign, entry_campaign_policy, entry_price_metadata, normalize_campaign_entry_metadata
from kamandal_v2.market.public import PublicAdapter
from kamandal_v2.planner.candidate_builder import _entry_economic_bounds
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.strategy_lanes.models import LaneId, LegEffect, LegSide, LifecycleState, StrategyTicket, TicketLeg
from kamandal_v2.strategy_lanes.store import CsaStore


def _credit_candidate() -> Candidate:
    return Candidate(
        candidate_id="credit-candidate",
        idea_id="credit-idea",
        underlying="TSLA",
        playbook_id="call_spread_default",
        structure="call_spread",
        legs=[
            OptionLeg("short_call", "sell", "call", 200, "2026-10-16", 1, 2.00, 1.95, 2.05, 0.25, 0, 0, 0, 1000),
            OptionLeg("long_call", "buy", "call", 205, "2026-10-16", 1, 1.00, 0.95, 1.05, 0.15, 0, 0, 0, 1000),
        ],
        net_credit=1.0,
        estimated_bpr=400,
        greeks=Greeks(),
        liquidity_score=1.0,
        score=1.0,
        entry_credit_floor=0.80,
        entry_debit_ceiling=1.20,
        entry_economic_bound_source="fixture_playbook_boundary",
    )


def _legacy_config() -> dict:
    return {
        "live": {
            "entry_pricing": {
                "mode": "improved_mid",
                "improvement_pct_of_spread": 10,
                "min_improvement": 0.01,
                "max_improvement": 0.10,
                "apply_to_credit": True,
                "apply_to_debit": True,
            },
            "entry_reprice": {
                "improvement_multipliers": [0.5, 0.0],
            },
        },
    }


def _campaign_config(**overrides: object) -> dict:
    config = _legacy_config()
    config["live"]["entry_pricing"]["campaign"] = {
        "enabled": True,
        "initial_improvement_multiplier": 0.50,
        "allowance_pct_of_midpoint": 5.0,
        "allowance_max_fraction_of_improvement": 0.50,
        "absolute_allowance_cap": 0.25,
        "valid_tick": 0.01,
        "absurd_bid_ask_pct": 3.0,
        **overrides,
    }
    return config


def test_characterize_legacy_credit_pricing_and_metadata() -> None:
    candidate = _credit_candidate()

    assert candidate_entry_limit_price(candidate, _legacy_config()) == "-1.02"
    assert entry_price_metadata(candidate, _legacy_config()) == {
        "mode": "improved_mid",
        "base_mid_limit": 1.0,
        "improved_limit": 1.02,
        "aggregate_bid_ask_spread": 0.2,
        "max_bid_ask_pct": 0.1,
        "aggregate_spread_to_mid_pct": 0.2,
        "execution_liquidity_tier": "normal",
        "improvement": 0.02,
        "improvement_pct_of_spread": 10.0,
        "raw_improvement_pct_of_spread": 10.0,
        "max_improvement_cap": 0.1,
        "tier_max_improvement": 0.1,
        "premium_max_improvement": 0.4,
        "max_improvement_pct_of_premium": 40.0,
        "min_open_interest": 1000,
        "side": "credit",
    }


def test_characterize_legacy_reprice_lineage_and_prices() -> None:
    ticket = {
        "ticket_hash": "legacy-root",
        "order_id": "legacy-order",
        "limit_price": "-2.25",
        "reprice_attempt": 0,
        "created_at": "2026-08-21T14:00:00Z",
        "submit_payload": {"orderId": "legacy-order", "limitPrice": "-2.25"},
        "preflight": {"raw": {"entry_pricing": {"base_mid_limit": 2.15, "improved_limit": 2.25}}},
    }

    first = _repriced_open_ticket(ticket, _legacy_config())
    second = _repriced_open_ticket(first, _legacy_config())

    assert first["limit_price"] == "-2.20"
    assert second["limit_price"] == "-2.15"
    assert first["parent_ticket_hash"] == ticket["ticket_hash"]
    assert second["parent_ticket_hash"] == first["ticket_hash"]
    assert first["preflight"]["raw"]["entry_pricing"] == ticket["preflight"]["raw"]["entry_pricing"]


def test_characterize_live_approval_is_rank_one_only(tmp_path) -> None:
    before = PortfolioState(account_size=10_000, buying_power=10_000, bpr_used=0, positions_count=0, greeks=Greeks())
    candidate = _credit_candidate()
    plans = [
        Plan("rank-one", 1, "eligible", [candidate], 2.0, 400, 4, 9_600, before, before),
        Plan("rank-two", 2, "eligible", [candidate], 1.0, 400, 4, 9_600, before, before),
    ]
    result = type("PlanRunResult", (), {"plans": plans, "metrics": {}})()
    rows = render_live_plan_rows(result, {"live": {"entry_approval_mode": "auto_top_plan"}}, store=LocalStore(tmp_path / "state.db"))

    assert len(rows) == 2
    assert dict(zip(DAILY_PLAN_HEADER, rows[0], strict=False))["operator_action"] == APPROVE_LIVE
    assert dict(zip(DAILY_PLAN_HEADER, rows[1], strict=False))["operator_action"] == ""


def test_characterize_ticket_hash_is_deterministic() -> None:
    ticket = {"order_id": "order", "plan_id": "plan", "candidate_id": "candidate", "limit_price": "-1.02"}
    assert ticket_hash(ticket) == ticket_hash(json.loads(json.dumps(ticket, sort_keys=True)))


def test_campaign_credit_is_midpoint_centered_and_bounded() -> None:
    campaign = entry_campaign(_credit_candidate(), _campaign_config())

    assert campaign.prices == ("-1.01", "-1.00", "-0.99")
    assert campaign.metadata["allowance_binding_cap"] == "improvement_fraction"
    assert campaign.metadata["prices"] == list(campaign.prices)


def test_campaign_debit_mirrors_credit_geometry() -> None:
    candidate = _credit_candidate()
    candidate.net_credit = -1.0
    campaign = entry_campaign(candidate, _campaign_config())

    assert campaign.prices == ("0.99", "1.00", "1.01")
    assert candidate_entry_limit_price(candidate, _campaign_config()) == "0.99"


@pytest.mark.parametrize("legacy_value", ["0.05", "0.06", "60"])
def test_debit_campaign_uses_sheet_money_cap_not_mixed_legacy_units(legacy_value: str) -> None:
    playbook = Playbook.from_row(
        {
            "playbook_id": "calendar_live",
            "enabled": "TRUE",
            "strategy_family": "earnings_calendar",
            "structure": "call_calendar",
            "max_debit_pct_bpr": legacy_value,
            "live_max_bpr_per_order": "1200",
        }
    )

    floor, ceiling, source = _entry_economic_bounds(
        playbook,
        structure=playbook.structure,
        width=0,
        net_credit=-4.00,
    )
    candidate = _credit_candidate()
    candidate.net_credit = -4.00
    candidate.entry_credit_floor = floor
    candidate.entry_debit_ceiling = ceiling
    candidate.entry_economic_bound_source = source

    assert floor is None
    assert ceiling == 12.0
    assert source == "playbook.live_max_bpr_per_order"
    assert entry_campaign(candidate, _campaign_config()).prices


def test_credit_campaign_preserves_jade_lizard_no_upside_risk_floor() -> None:
    playbook = Playbook.from_row(
        {
            "playbook_id": "jade_lizard_live",
            "enabled": "TRUE",
            "strategy_family": "jade_lizard",
            "structure": "jade_lizard",
        }
    )

    floor, ceiling, source = _entry_economic_bounds(
        playbook,
        structure=playbook.structure,
        width=3.0,
        net_credit=3.20,
    )

    assert floor == 3.0
    assert ceiling is None
    assert source == "jade_lizard.call_width"


def test_campaign_preserves_full_improvement_but_starts_at_half() -> None:
    candidate = _credit_candidate()
    candidate.net_credit = 1.43
    for leg in candidate.legs:
        leg.mid = 1.75
        leg.bid = 0.0
        leg.ask = 3.5
    config = _campaign_config()
    config["live"]["entry_pricing"]["max_improvement"] = 0.50
    config["live"]["entry_pricing"]["max_improvement_by_liquidity_tier"] = {
        "tight": 0.35,
        "normal": 0.35,
        "wide": 0.35,
        "very_wide": 0.35,
        "extreme": 0.35,
    }
    config["live"]["entry_pricing"]["mode"] = "liquidity_adjusted_mid"

    campaign = entry_campaign(candidate, config)

    assert campaign.improvement == 0.35
    assert campaign.prices[0] == "-1.61"
    assert campaign.prices[1] == "-1.43"
    assert campaign.prices[2] == "-1.36"


def test_campaign_skips_terminal_attempt_when_absolute_cap_is_missing() -> None:
    campaign = entry_campaign(_credit_candidate(), _campaign_config(absolute_allowance_cap=0.0))

    assert campaign.prices == ()
    assert campaign.metadata["skip_reason"] == "absolute_allowance_cap_not_configured"
    with pytest.raises(ValueError, match="absolute_allowance_cap_not_configured"):
        candidate_entry_limit_price(_credit_candidate(), _campaign_config(absolute_allowance_cap=0.0))

    adapter = PublicAdapter({**_campaign_config(absolute_allowance_cap=0.0), "broker": {"public": {"secret_token": "test", "account_id": "acct"}}})
    blocked = adapter.preflight(_credit_candidate())
    assert blocked.ok is False
    assert blocked.raw["entry_pricing"]["campaign"]["skip_reason"] == "absolute_allowance_cap_not_configured"


def test_campaign_skips_absurd_or_stale_quotes() -> None:
    candidate = _credit_candidate()
    candidate.reasons.append("quote_stale")
    stale = entry_campaign(candidate, _campaign_config())
    assert stale.metadata["skip_reason"] == "quote_stale_or_unstable"

    candidate.reasons.clear()
    candidate.legs[0].ask = 20.0
    wide = entry_campaign(candidate, _campaign_config())
    assert wide.metadata["skip_reason"] == "absurdly_wide_quote"


def test_campaign_tick_rounding_never_widens_terminal_allowance() -> None:
    candidate = _credit_candidate()
    candidate.net_credit = 1.43
    campaign = entry_campaign(
        candidate,
        _campaign_config(
            allowance_pct_of_midpoint=4.9,
            allowance_max_fraction_of_improvement=10.0,
            valid_tick=0.05,
        ),
    )

    assert campaign.prices == ("-1.50", "-1.45", "-1.40")
    assert abs(float(campaign.prices[1])) - abs(float(campaign.prices[2])) <= campaign.allowance + 1e-9


def test_campaign_ticket_and_public_payload_start_at_frozen_first_price() -> None:
    candidate = _credit_candidate()
    config = _campaign_config()
    metadata = entry_price_metadata(candidate, config)
    campaign_prices = metadata["campaign"]["prices"]
    candidate.preflight = PreflightResult(
        ok=True,
        bpr=400,
        message="ok",
        raw={"request": {"limitPrice": campaign_prices[0]}, "entry_pricing": metadata},
    )
    plan = type("Plan", (), {"plan_id": "campaign-plan", "plan_rank": 1})()
    ticket = build_open_ticket(plan, candidate)

    assert ticket["limit_price"] == "-1.01"
    assert PublicAdapter(config)._order_payload(candidate)["limitPrice"] == "-1.01"


def test_campaign_replacements_use_persisted_prices_across_restart(tmp_path) -> None:
    candidate = _credit_candidate()
    config = _campaign_config()
    metadata = entry_price_metadata(candidate, config)
    candidate.preflight = PreflightResult(
        ok=True,
        bpr=400,
        message="ok",
        raw={"request": {"limitPrice": metadata["campaign"]["prices"][0]}, "entry_pricing": metadata},
    )
    plan = type("Plan", (), {"plan_id": "campaign-plan", "plan_rank": 1})()
    root = build_open_ticket(plan, candidate)
    first = _repriced_open_ticket(root, config)
    second = _repriced_open_ticket(first, config)
    store = LocalStore(tmp_path / "restart.db")
    store.save_live_order_intent(root, status="repriced")
    store.save_live_order_intent(first, status="repriced")
    store.save_live_order_intent(second, status="submitted")
    reloaded = store.live_order_intent(first["ticket_hash"])
    resumed = _repriced_open_ticket(reloaded, config)

    assert root["limit_price"] == "-1.01"
    assert first["limit_price"] == "-1.00"
    assert second["limit_price"] == "-0.99"
    assert resumed["limit_price"] == second["limit_price"]
    assert resumed["parent_ticket_hash"] == first["ticket_hash"]
    assert resumed["preflight"]["raw"]["entry_pricing"]["campaign"]["prices"] == metadata["campaign"]["prices"]


def test_campaign_reprice_schedule_uses_only_the_three_stored_prices(tmp_path) -> None:
    candidate = _credit_candidate()
    config = _campaign_config()
    metadata = entry_price_metadata(candidate, config)
    config["live"]["entry_reprice"] = {"enabled": True, "after_minutes": 5, "max_reprices": 2}
    store = LocalStore(tmp_path / "schedule.db")
    root = {
        "ticket_hash": "campaign-root",
        "intent_type": "open",
        "reprice_attempt": 0,
        "created_at": "2026-08-21T14:00:00Z",
        "preflight": {"raw": {"entry_pricing": metadata}},
    }
    midpoint = {**root, "ticket_hash": "campaign-midpoint", "reprice_attempt": 1}
    terminal = {**root, "ticket_hash": "campaign-terminal", "reprice_attempt": 2}
    status = {"status": "NEW", "createdAt": "2026-08-21T14:00:00Z"}

    assert _entry_reprice_due(store, root, status, config) is True
    assert _entry_reprice_due(store, midpoint, status, config) is True
    assert _entry_reprice_due(store, terminal, status, config) is False


def test_campaign_fails_closed_without_authoritative_economic_bound() -> None:
    candidate = _credit_candidate()
    candidate.entry_credit_floor = None
    candidate.entry_debit_ceiling = None

    campaign = entry_campaign(candidate, _campaign_config())

    assert campaign.metadata["skip_reason"] == "economic_bound_missing"


def test_public_nickel_retry_freezes_the_complete_ladder_for_replacements(monkeypatch) -> None:
    candidate = _credit_candidate()
    candidate.net_credit = 1.43
    for leg in candidate.legs:
        leg.mid = 1.75
        leg.bid = 0.0
        leg.ask = 3.5
    config = _campaign_config(allowance_max_fraction_of_improvement=10.0)
    config["live"]["entry_pricing"].update(
        {
            "mode": "liquidity_adjusted_mid",
            "max_improvement": 0.50,
            "max_improvement_by_liquidity_tier": {
                "tight": 0.35,
                "normal": 0.35,
                "wide": 0.35,
                "very_wide": 0.35,
                "extreme": 0.35,
            },
        }
    )
    config["broker"] = {"public": {"secret_token": "fixture", "account_id": "acct"}}
    adapter = PublicAdapter(config)
    calls = []

    def fake_post(_path, payload):
        calls.append(dict(payload))
        if len(calls) == 1:
            raise RuntimeError("limit price must be in increments of $0.05")
        return {"buyingPowerRequirement": 400}

    monkeypatch.setattr(adapter, "_post", fake_post)
    preflight = adapter.preflight(candidate)
    metadata = (preflight.raw or {})["entry_pricing"]
    prices = metadata["campaign"]["prices"]
    assert preflight.ok is True
    assert len(prices) == 3
    assert all(abs(round(abs(float(price)) / 0.05) - abs(float(price)) / 0.05) < 1e-9 for price in prices)
    assert calls[1]["limitPrice"] == prices[0]

    candidate.preflight = preflight
    plan = type("Plan", (), {"plan_id": "nickel-plan", "plan_rank": 1})()
    root = build_open_ticket(plan, candidate)
    first = _repriced_open_ticket(root, config)
    second = _repriced_open_ticket(first, config)
    assert [root["limit_price"], first["limit_price"], second["limit_price"]] == list(prices)
    assert all(abs(round(abs(float(price)) / 0.05) - abs(float(price)) / 0.05) < 1e-9 for price in [root["limit_price"], first["limit_price"], second["limit_price"]])


@pytest.mark.parametrize(
    ("net_credit", "raw_prices", "expected"),
    [
        (1.43, ["-1.50", "-1.43", "-1.36"], ["-1.50", "-1.45", "-1.40"]),
        (-1.43, ["1.35", "1.43", "1.49"], ["1.35", "1.40", "1.45"]),
    ],
)
def test_nickel_normalization_never_crosses_midpoint_or_widens_terminal_bound(
    net_credit: float,
    raw_prices: list[str],
    expected: list[str],
) -> None:
    candidate = _credit_candidate()
    candidate.net_credit = net_credit
    metadata = {
        "campaign": {
            "enabled": True,
            "side": "credit" if net_credit > 0 else "debit",
            "midpoint": 1.43,
            "allowance": 0.07,
            "economic_bound": 0.80 if net_credit > 0 else 1.50,
            "prices": raw_prices,
        }
    }

    normalized = normalize_campaign_entry_metadata(candidate, metadata, valid_tick=0.05)

    assert normalized["campaign"]["prices"] == expected
    midpoint = abs(float(expected[1]))
    terminal = abs(float(expected[2]))
    if net_credit > 0:
        assert midpoint >= 1.43
        assert terminal >= abs(float(raw_prices[2]))
    else:
        assert midpoint <= 1.43
        assert terminal <= abs(float(raw_prices[2]))


@pytest.mark.parametrize(
    ("net_credit", "raw_prices", "expected"),
    [
        (1.43, ["-1.50", "-1.43", "-1.42"], ["-1.50", "-1.45"]),
        (-1.43, ["1.35", "1.43", "1.44"], ["1.35", "1.40"]),
    ],
)
def test_nickel_normalization_preserves_midpoint_when_terminal_stage_collapses(
    net_credit: float,
    raw_prices: list[str],
    expected: list[str],
) -> None:
    candidate = _credit_candidate()
    candidate.net_credit = net_credit
    metadata = {
        "campaign": {
            "enabled": True,
            "side": "credit" if net_credit > 0 else "debit",
            "midpoint": 1.43,
            "prices": raw_prices,
        }
    }

    normalized = normalize_campaign_entry_metadata(candidate, metadata, valid_tick=0.05)

    assert normalized["campaign"]["prices"] == expected


def test_configured_tick_keeps_debit_midpoint_on_operator_side() -> None:
    candidate = _credit_candidate()
    candidate.net_credit = -1.43
    candidate.entry_debit_ceiling = 2.00
    for leg in candidate.legs:
        leg.mid = 1.75
        leg.bid = 0.0
        leg.ask = 3.5
    config = _campaign_config(
        valid_tick=0.05,
        allowance_max_fraction_of_improvement=10.0,
        absolute_allowance_cap=0.25,
    )
    config["live"]["entry_pricing"].update(
        {
            "mode": "liquidity_adjusted_mid",
            "max_improvement": 0.50,
            "max_improvement_by_liquidity_tier": {
                "tight": 0.35,
                "normal": 0.35,
                "wide": 0.35,
                "very_wide": 0.35,
                "extreme": 0.35,
            },
        }
    )

    campaign = entry_campaign(candidate, config)

    assert campaign.midpoint == 1.40
    assert abs(float(campaign.prices[1])) <= 1.43


def test_fallback_inline_submit_requires_canonical_live_confirmation(monkeypatch) -> None:
    monkeypatch.delenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", raising=False)
    config = {"runtime": {"mode": "live", "trading_enabled": True}, "live": {}}

    reason = _fallback_submission_gate(config, [{"underlying": "TSLA"}], gate={"risk_manager": {}})

    assert reason.startswith("blocked_live_submit_gate:")


def test_fallback_inline_submit_rechecks_stage_and_cluster_gates(monkeypatch) -> None:
    monkeypatch.setenv("KAMANDAL_LIVE_SUBMIT_CONFIRM", "I_UNDERSTAND_THIS_SUBMITS_REAL_ORDERS")
    monkeypatch.setattr("kamandal_v2.live.execution.load_daily_policy_snapshot", lambda _config: object())
    config = {"runtime": {"mode": "live", "trading_enabled": True}, "live": {}}
    ticket = {"underlying": "TSLA"}

    monkeypatch.setattr(
        "kamandal_v2.live.execution._stage_ticket_authorization",
        lambda _ticket, _snapshot: (False, "blocked_stage_authorization_policy_changed"),
    )
    assert _fallback_submission_gate(config, [ticket], gate={"risk_manager": {}}) == "blocked_stage_authorization_policy_changed"

    monkeypatch.setattr(
        "kamandal_v2.live.execution._stage_ticket_authorization",
        lambda _ticket, _snapshot: (True, "stage_authorization_current"),
    )
    reason = _fallback_submission_gate(
        config,
        [ticket],
        gate={"risk_manager": {"clusters_at_cap": {"mega_cap_tech": ["TSLA", "NVDA"]}}},
    )
    assert reason == "blocked_risk_cluster_cap:mega_cap_tech"
    reason = _fallback_submission_gate(
        config,
        [ticket],
        gate={"risk_manager": {"underlyings_at_cap": {"TSLA": 1}}},
    )
    assert reason == "blocked_risk_underlying_cap:TSLA"


def test_repriced_root_uses_terminal_leaf_for_fallback(tmp_path) -> None:
    store, ticket = _registered_fallback(tmp_path)
    child = _fallback_ticket("rank-one-child")
    child["parent_ticket_hash"] = ticket["ticket_hash"]
    child["limit_price"] = "-0.95"
    store.save_live_order_intent(child, status="cancelled")
    store.update_live_order_intent_status(ticket["ticket_hash"], "repriced")
    coordinator = PlanFallbackCoordinator(store, {"live": {"plan_fallback": {"enabled": True, "max_attempts": 2}}})

    decision = coordinator.advance("campaign-one", replan=lambda _context: _validated_rank_two(store))

    assert decision.status == "fallback_ready"
    assert decision.reason == "zero_fill_terminal"


def test_terminal_partial_fill_is_adopted_into_typed_lifecycle_before_replan(tmp_path, monkeypatch) -> None:
    database = tmp_path / "partial.db"
    store = LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    lifecycle = LifecycleState(
        lifecycle_id="partial-life",
        opportunity_id="partial-opportunity",
        lane=LaneId.CALL_VERTICAL,
        version=1,
        status="pending_live_submission",
        active_legs=(),
        cashflow_ledger=(),
        opened_at="2026-08-21T14:00:00Z",
        updated_at="2026-08-21T14:00:00Z",
        policy_hash="policy-hash",
        metadata={"execution_mode": "live"},
    )
    CsaStore(database).save_lifecycle(lifecycle)
    strategy_ticket = StrategyTicket(
        ticket_id="typed-ticket",
        action_id="typed-action",
        lifecycle_id=lifecycle.lifecycle_id,
        lifecycle_version=1,
        lane=LaneId.CALL_VERTICAL,
        underlying="TSLA",
        order_kind="credit",
        limit_price=1.0,
        legs=(TicketLeg("TSLA", LegSide.SELL, LegEffect.OPEN, 1, "call", "2026-10-16", 200.0, "short_call"),),
        policy_hash="policy-hash",
        created_at="2026-08-21T14:00:00Z",
        metadata={"action_type": "open"},
    )
    ticket = {
        "ticket_hash": "typed-live-ticket",
        "order_id": "typed-order",
        "plan_id": "typed-plan",
        "candidate_id": "typed-candidate",
        "idea_id": "typed-idea",
        "intent_type": "open",
        "underlying": "TSLA",
        "playbook_id": "call_spread_default",
        "structure": "call_spread",
        "quantity": 2,
        "legs": [{"role": "short_call", "side": "sell", "option_type": "call", "strike": 200, "expiration": "2026-10-16", "quantity": 2, "mid": 1.0, "bid": 0.95, "ask": 1.05}],
        "csa_lifecycle_id": lifecycle.lifecycle_id,
        "csa_strategy_ticket": strategy_ticket.to_dict(),
    }
    store.save_live_order_intent(ticket, status="submitted")
    response = {"status": "CANCELED", "filledQuantity": 1, "averagePrice": 0.95, "updatedAt": "2026-08-21T14:05:00Z"}
    class FakeAdapter:
        def get_order(self, _order_id):
            return dict(response)

    monkeypatch.setattr("kamandal_v2.live.execution.broker_adapter", lambda _config: FakeAdapter())
    synced = _sync_live_orders_locked({"live": {}}, store=store, manage_entries=False)

    typed = CsaStore(database, read_only=True).lifecycle(lifecycle.lifecycle_id)
    assert synced["orders"][0]["partial_fill_preserved"] is True
    assert synced["orders"][0]["csa_lifecycle_projection"]["status"] == "open"
    assert typed is not None and typed.version == 2 and typed.status == "open"
    assert typed.metadata["filled_quantity"] == 1.0
    assert store.live_order_intent(ticket["ticket_hash"])["_ledger_status"] == "partially_filled_terminal"


def _fallback_ticket(ticket_hash_value: str, candidate_id: str = "rank-one-candidate") -> dict:
    return {
        "ticket_hash": ticket_hash_value,
        "order_id": f"order-{ticket_hash_value}",
        "plan_id": "rank-one-plan",
        "plan_rank": 1,
        "candidate_id": candidate_id,
        "idea_id": f"idea-{candidate_id}",
        "intent_type": "open",
        "underlying": "TSLA",
        "playbook_id": "call_spread_default",
        "structure": "call_spread",
        "quantity": 1,
        "limit_price": "-1.00",
        "created_at": "2026-08-21T14:00:00Z",
        "legs": [],
        "submit_payload": {"orderId": f"order-{ticket_hash_value}", "limitPrice": "-1.00", "legs": []},
    }


def _registered_fallback(tmp_path, status: str = "submitted"):
    store = LocalStore(tmp_path / "fallback.db")
    ticket = _fallback_ticket("rank-one-ticket")
    store.save_live_order_intent(ticket, status=status)
    plan = type("Plan", (), {"plan_id": "rank-one-plan", "plan_rank": 1, "to_dict": lambda self: {"plan_id": self.plan_id, "plan_rank": self.plan_rank}})()
    register_rank_one_attempt(
        store,
        campaign_id="campaign-one",
        plan=plan,
        tickets=[ticket],
        plan_run_id="run-one",
        idea_paths=[str(tmp_path / "ideas.yaml")],
    )
    return store, ticket


def _validated_rank_two(store: LocalStore) -> dict:
    ticket = _fallback_ticket("rank-two-ticket", "rank-two-candidate")
    ticket["plan_id"] = "rank-two-plan"
    store.save_live_order_intent(ticket, status="pending_approval")
    return {
        "plan_id": "rank-two-plan",
        "candidate_ids": ["rank-two-candidate"],
        "tickets": [ticket],
        "validation": {key: True for key in ("fresh_session", "fresh_quotes", "risk_valid", "bpr_valid", "concentration_valid", "overlap_valid", "broker_preflight_valid")},
    }


def test_fallback_projection_replaces_only_live_lane_before_submission(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    row = {column: "" for column in DAILY_PLAN_HEADER}
    row.update(
        {
            "plan_date": "2026-08-21",
            "plan_rank": 1,
            "plan_id": "rank-two-plan",
            "plan_status": "eligible",
            "mode": "live_advisory",
            "plan_detail_json": json.dumps({"lane": "live_advisory"}),
        }
    )
    decision = SimpleNamespace(
        daily_plan_rows=(tuple(row[column] for column in DAILY_PLAN_HEADER),),
        attempt=2,
        campaign_id="campaign-one",
        reason="zero_fill_terminal",
        plan_id="rank-two-plan",
    )
    captured = {}

    def write(_config, rows, header, *, replace_lanes):  # noqa: ANN001
        captured.update(rows=rows, header=header, replace_lanes=replace_lanes)
        return len(rows)

    monkeypatch.setattr("kamandal_v2.live.execution.write_daily_plan", write)
    receipt = _project_fallback_daily_plan({}, LocalStore(tmp_path / "projection.db"), decision)
    projected = dict(zip(DAILY_PLAN_HEADER, captured["rows"][0], strict=False))
    detail = json.loads(projected["plan_detail_json"])

    assert receipt["ok"] is True
    assert captured["header"] == DAILY_PLAN_HEADER
    assert captured["replace_lanes"] == {"live_advisory"}
    assert projected["operator_action"] == APPROVE_LIVE
    assert projected["operator_notes"] == "automatic Plan 2 after zero_fill_terminal"
    assert detail["fallback_campaign_id"] == "campaign-one"
    assert detail["fallback_attempt"] == 2


def test_fallback_blocks_working_orders_and_is_idempotent_after_zero_fill(tmp_path) -> None:
    store, ticket = _registered_fallback(tmp_path)
    coordinator = PlanFallbackCoordinator(store, {"live": {"plan_fallback": {"enabled": True, "max_attempts": 2}}})
    calls = []

    blocked = coordinator.advance("campaign-one", replan=lambda _context: calls.append("unexpected") or _validated_rank_two(store))
    assert blocked.status == "blocked_unresolved"
    assert calls == []

    store.update_live_order_intent_status(ticket["ticket_hash"], "cancelled")
    ready = coordinator.advance("campaign-one", replan=lambda context: calls.append(context["reason"]) or _validated_rank_two(store))
    replay = coordinator.advance("campaign-one", replan=lambda _context: calls.append("duplicate") or _validated_rank_two(store))

    assert ready.status == "fallback_ready"
    assert ready.attempt == 2
    assert calls == ["zero_fill_terminal"]
    assert replay.status == "fallback_ready"
    assert replay.reason == "idempotent_replay"


def test_fallback_partial_fill_replans_from_actual_positions(tmp_path) -> None:
    store, ticket = _registered_fallback(tmp_path)
    store.update_live_order_intent_status_with_payload(ticket["ticket_hash"], "partially_filled_terminal", {"filled_quantity": 0.5})
    store.save_live_position_group("group-filled", {"underlying": "TSLA", "candidate": {"candidate_id": ticket["candidate_id"]}}, status="open")
    coordinator = PlanFallbackCoordinator(store, {"live": {"plan_fallback": {"enabled": True, "max_attempts": 2}}})
    observed = []

    decision = coordinator.advance(
        "campaign-one",
        replan=lambda context: observed.append(context) or _validated_rank_two(store),
    )

    assert decision.status == "fallback_ready"
    assert observed[0]["reason"] == "partial_fill_terminal"
    assert observed[0]["attempted_candidate_ids"] == [ticket["candidate_id"]]
    assert observed[0]["actual_portfolio_groups"][0]["group_id"] == "group-filled"


def test_fallback_never_releases_fully_filled_rank_one(tmp_path) -> None:
    store, ticket = _registered_fallback(tmp_path, status="filled")
    coordinator = PlanFallbackCoordinator(store, {"live": {"plan_fallback": {"enabled": True, "max_attempts": 2}}})

    decision = coordinator.advance("campaign-one", replan=lambda _context: (_ for _ in ()).throw(AssertionError("must not replan")))

    assert decision.status == "complete"
    assert decision.reason == "rank_one_filled"


def test_fallback_rejects_unvalidated_second_plan(tmp_path) -> None:
    store, ticket = _registered_fallback(tmp_path)
    store.update_live_order_intent_status(ticket["ticket_hash"], "expired")
    store.record_live_order_status(ticket["order_id"], "CANCELLED", {"status": "CANCELLED"}, ticket_hash=ticket["ticket_hash"])
    coordinator = PlanFallbackCoordinator(store, {"live": {"plan_fallback": {"enabled": True, "max_attempts": 2}}})
    invalid = {"plan_id": "rank-two-plan", "candidate_ids": ["rank-two-candidate"], "tickets": [_fallback_ticket("rank-two-ticket")], "validation": {"fresh_session": True}}

    decision = coordinator.advance("campaign-one", replan=lambda _context: invalid)

    assert decision.status == "terminal_no_valid_plan"
    assert decision.reason.startswith("fresh_validation_failed:")


def test_fallback_counts_terminal_rank_one_attempt_against_basket_cap(tmp_path) -> None:
    store, ticket = _registered_fallback(tmp_path)
    store.update_live_order_intent_status(ticket["ticket_hash"], "cancelled")
    store.record_live_order_status(ticket["order_id"], "CANCELLED", {"status": "CANCELLED"}, ticket_hash=ticket["ticket_hash"])
    config = {"live": {"max_live_baskets_per_day": 1, "plan_fallback": {"enabled": True, "max_attempts": 2}}}

    assert _fallback_basket_cap_allows(config, store, "campaign-one", "rank-two-plan") is False


def test_integrated_credit_replay_reaches_one_fresh_rank_two_attempt(tmp_path) -> None:
    config = _campaign_config()
    config["live"]["plan_fallback"] = {"enabled": True, "max_attempts": 2, "auto_submit": False}
    candidate = _credit_candidate()
    metadata = entry_price_metadata(candidate, config)
    candidate.preflight = PreflightResult(
        ok=True,
        bpr=400,
        message="fixture-preflight",
        raw={"request": {"limitPrice": metadata["campaign"]["prices"][0]}, "entry_pricing": metadata},
    )
    root = build_open_ticket(type("Plan", (), {"plan_id": "rank-one-plan", "plan_rank": 1})(), candidate)
    store = LocalStore(tmp_path / "integrated-replay.db")
    store.save_live_order_intent(root, status="submitted")
    plan = type("Plan", (), {"plan_id": "rank-one-plan", "plan_rank": 1, "to_dict": lambda self: {"plan_id": self.plan_id, "plan_rank": self.plan_rank}})()
    register_rank_one_attempt(store, campaign_id="integrated-campaign", plan=plan, tickets=[root], plan_run_id="run-integrated")
    coordinator = PlanFallbackCoordinator(store, config)

    working = coordinator.advance("integrated-campaign", replan=lambda _context: (_ for _ in ()).throw(AssertionError("working rank one must block")))
    store.update_live_order_intent_status(root["ticket_hash"], "expired")
    store.record_live_order_status(root["order_id"], "CANCELLED", {"status": "CANCELLED"}, ticket_hash=root["ticket_hash"])
    second = _fallback_ticket("integrated-rank-two-ticket", "integrated-rank-two-candidate")
    second["plan_id"] = "rank-two-plan"
    store.save_live_order_intent(second, status="pending_approval")
    first_terminal = coordinator.advance("integrated-campaign", replan=lambda context: {
        "plan_id": "rank-two-plan",
        "candidate_ids": ["integrated-rank-two-candidate"],
        "tickets": [second],
        "validation": {key: True for key in ("fresh_session", "fresh_quotes", "risk_valid", "bpr_valid", "concentration_valid", "overlap_valid", "broker_preflight_valid")},
        "reason_from_context": context["reason"],
    })
    replay = coordinator.advance("integrated-campaign", replan=lambda _context: (_ for _ in ()).throw(AssertionError("rank two must be exactly once")))

    assert root["limit_price"] == "-1.01"
    assert working.status == "blocked_unresolved"
    assert first_terminal.status == "fallback_ready"
    assert first_terminal.attempt == 2
    assert replay.status == "fallback_ready"
    assert replay.reason == "idempotent_replay"
    assert store.latest_event("live_plan_attempt:integrated-campaign")["ticket_hashes"] == [second["ticket_hash"]]


def test_operator_approved_configuration_activates_campaign_and_fallback(monkeypatch) -> None:
    monkeypatch.delenv("KAMANDAL_ENTRY_CAMPAIGN_ENABLED", raising=False)
    monkeypatch.delenv("KAMANDAL_LIVE_PLAN_FALLBACK_ENABLED", raising=False)
    from kamandal_v2.config import load_control

    control = load_control()

    assert control["live"]["entry_pricing"]["campaign"]["enabled"] is True
    assert control["live"]["entry_pricing"]["campaign"]["absolute_allowance_cap"] == 0.10
    assert control["live"]["entry_pricing"]["campaign"]["valid_tick"] == 0.01
    assert control["live"]["plan_fallback"]["enabled"] is True
    assert control["live"]["plan_fallback"]["max_attempts"] == 2


def test_missing_activation_keys_remain_fail_closed() -> None:
    assert entry_campaign_policy({"live": {"entry_pricing": {"mode": "liquidity_adjusted_mid"}}}).enabled is False
    assert fallback_enabled({"live": {}}) is False
