from dataclasses import replace

import pytest

from kamandal_v2.domain.models import ChainSnapshot, OptionQuote, Playbook, PreflightResult, UniverseEntry
from kamandal_v2.intelligence.observed_packages import ObservedLegEvidence, observed_package_batch_from_dict
from kamandal_v2.intelligence.trade_sources import compile_trade_source_policies
from kamandal_v2.planner.observed_package_candidates import build_observed_package_candidates
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.policy import compile_playbook_policy
from tests.test_observed_package_planning import _batch
from tests.test_unified_policy import _strangle_row


NOW = "2026-09-08T14:00:00Z"
EXPIRY = "2026-10-16"


def _package(source="mike_butler"):
    return replace(
        _batch().packages[0], source_profile=source, symbol="XYZ", structure="short_strangle",
        package_signature="exact-strangle-signature", opportunity_group_id="exact-strangle-opportunity",
        source_published_at="2026-09-08T13:00:00Z", source_valid_until="2026-09-09T13:00:00Z",
        legs=(ObservedLegEvidence(1, EXPIRY, "90", "put", "STO", "sell", "open"),
              ObservedLegEvidence(1, EXPIRY, "110", "call", "STO", "sell", "open")),
    )


class Market:
    iv = 60
    event = "clear"
    bpr = 1800
    bpr_provided = True
    calls = 0

    def chain_snapshot(self, symbol):
        return ChainSnapshot("exact-quotes", symbol, NOW, 100, [
            OptionQuote(symbol, EXPIRY, "put", 90, .99, 1.01, -.18, .01, -.02, .03, .3, 500),
            OptionQuote(symbol, EXPIRY, "call", 110, .99, 1.01, .18, .01, -.02, .03, .3, 500),
        ], "fixture")

    def iv_percentile(self, _symbol): return 60
    def iv_rank(self, _symbol): return self.iv
    def iv_abs(self, _symbol): return .3
    def event_status(self, _symbol): return self.event

    def preflight(self, candidate):
        assert candidate.execution_venue == "tasty_primary"
        assert [(leg.strike, leg.expiration) for leg in candidate.legs] == [(90, EXPIRY), (110, EXPIRY)]
        self.calls += 1
        return PreflightResult(True, self.bpr, "native dry-run", {"broker_bpr_provided": self.bpr_provided, "bpr_source": "tastytrade_dry_run"})


def _build(tmp_path, *, package=None, source_mode="live", book="live", enabled=True, market=None, row_changes=None):
    package = package or _package()
    row = _strangle_row(
        mode="live", csa_stage="pilot_live", accepted_inputs="market_scan,exact_package",
        dte_min=35, dte_max=50, iv_rank_min=50, iv_rank_max=100,
        execution_venue="tasty_primary", live_max_bpr_per_order=2500,
        universe_expansion_enabled=True, underlying_price_min=20, underlying_price_max=250,
    )
    row.update(row_changes or {})
    policy = compile_playbook_policy(row)
    source = compile_trade_source_policies([{
        "source_id": package.source_profile, "output_kind": "exact_package", "mode": source_mode,
        "live_structures": "short_strangle" if source_mode == "live" else "",
    }])
    assert source.ok
    return build_observed_package_candidates(
        (package,), policies=(policy,), playbooks=[Playbook.from_row(row)], market=market or Market(),
        store=LocalStore(tmp_path / "state.db"), config={"runtime": {"mode": book, "observed_at": NOW}},
        trade_source_policies=source.by_key(), universe=[UniverseEntry("XYZ", enabled)], mode=book,
    )


@pytest.mark.parametrize("source", ["mike_butler", "greg_harmon"])
def test_exact_strangle_keeps_contracts_and_uses_native_bpr(tmp_path, source):
    market = Market()
    candidate, = _build(tmp_path, package=_package(source), market=market)
    assert candidate.eligible
    assert candidate.estimated_bpr == 1800
    assert [(leg.role, leg.quantity) for leg in candidate.legs] == [("short_put", 1), ("short_call", 1)]
    assert "live_max_bpr_per_order=2500.0" in candidate.reasons
    assert candidate.metadata["source_profile"] == source
    assert market.calls == 1


@pytest.mark.parametrize("source_mode", ["off", "observe", "shadow"])
def test_source_ceiling_never_leaks_into_live(tmp_path, source_mode):
    market = Market()
    assert _build(tmp_path, source_mode=source_mode, market=market) == []
    assert market.calls == 0


def test_live_strangle_is_not_duplicated_in_shadow(tmp_path):
    assert _build(tmp_path, book="shadow") == []
    candidate, = _build(tmp_path, book="shadow", source_mode="shadow")
    assert candidate.eligible
    assert candidate.preflight.raw["broker_effects"] is False


def test_disabled_universe_blocks_exact_trade(tmp_path):
    market = Market()
    candidate, = _build(tmp_path, market=market, enabled=False)
    assert candidate.rejection_reason == "universe_symbol_not_enabled"
    assert market.calls == 0


@pytest.mark.parametrize("iv,event,reason", [(20, "clear", "strangle_entry_outside_configured_ranges"), (60, "earnings_soon", "event_status_blocked")])
def test_exact_trade_does_not_bypass_strategy_admission(tmp_path, iv, event, reason):
    market = Market()
    market.iv, market.event = iv, event
    candidate, = _build(tmp_path, market=market)
    assert candidate.rejection_reason.startswith(reason)
    assert market.calls == 0


@pytest.mark.parametrize("mutator", [
    lambda p: replace(p, source_valid_until="2026-09-08T13:30:00Z"),
    lambda p: replace(p, source_valid_until=None),
    lambda p: replace(p, source_published_at="2026-09-09T13:00:00Z"),
    lambda p: replace(p, action="adjust"),
    lambda p: replace(p, legs=(p.legs[0], replace(p.legs[1], strike="90"))),
])
def test_stale_incomplete_or_nonopening_exact_packages_are_parked(tmp_path, mutator):
    market = Market()
    assert _build(tmp_path, package=mutator(_package()), market=market) == []
    assert market.calls == 0


def test_live_source_freshness_survives_feed_roundtrip():
    batch = replace(_batch(), packages=(_package(),))
    restored = observed_package_batch_from_dict(batch.to_dict()).packages[0]
    assert restored.source_published_at == _package().source_published_at
    assert restored.source_valid_until == _package().source_valid_until


def test_exact_strangle_never_resizes_source_quantity(tmp_path):
    package = _package()
    package = replace(package, legs=tuple(replace(leg, quantity=2) for leg in package.legs))
    market = Market()
    candidate, = _build(tmp_path, package=package, market=market)
    assert candidate.rejection_reason == "exact_strangle_quantity_above_policy"
    assert [leg.quantity for leg in candidate.legs] == [2, 2]
    assert market.calls == 0


def test_final_live_gate_accepts_tasty_bpr_and_rejects_missing_or_increased_bpr(tmp_path):
    from kamandal_v2.live.advisory import _preflight_bpr_incomplete
    from kamandal_v2.live.execution import _fresh_entry_preflight_blocker
    candidate, = _build(tmp_path)
    assert not _preflight_bpr_incomplete(candidate)
    ticket = {"intent_type": "open", "structure": "short_strangle", "entry_risk_budget": 1800}
    assert _fresh_entry_preflight_blocker(ticket, candidate.preflight) == ""
    increased = replace(candidate.preflight, bpr=1900)
    assert _fresh_entry_preflight_blocker(ticket, increased) == "fresh_preflight_exceeds_approved_risk_budget"
    missing = replace(candidate.preflight, raw={})
    assert _fresh_entry_preflight_blocker(ticket, missing) == "live_preflight_bpr_incomplete"
    candidate.preflight = missing
    assert _preflight_bpr_incomplete(candidate)


def test_exact_source_reaches_normal_live_lifecycle_and_one_canary_reservation(tmp_path, monkeypatch):
    from kamandal_v2.config import load_control
    from kamandal_v2.domain.models import PortfolioState
    from kamandal_v2.strategy_engine import planning
    from kamandal_v2.strategy_lanes.daily_policy import DailyPolicySnapshot, policy_tables_hash
    from kamandal_v2.strategy_lanes.operator_policy import OperatorPolicyBundle
    from kamandal_v2.strategy_lanes.store import CsaStore
    from tests.test_observed_package_planning import _migrated_store

    row = _strangle_row(
        mode="live", csa_stage="pilot_live", accepted_inputs="exact_package",
        dte_min=35, dte_max=50, iv_rank_min=50, iv_rank_max=100,
        execution_venue="tasty_primary", live_max_bpr_per_order=2500, leg_count=2,
        profit_target_pct=40,
    )
    sources = [
        {"source_id": source, "output_kind": output, "mode": "live" if output == "exact_package" else "off",
         "live_structures": "short_strangle" if output == "exact_package" else ""}
        for source in ("mike_butler", "greg_harmon") for output in ("idea", "exact_package")
    ]
    universe = [{"symbol": "XYZ", "enabled": "TRUE", "notes": ""}]
    tables = {"universe": universe, "playbooks": [row], "trade_sources": sources}
    snapshot = DailyPolicySnapshot(NOW[:10], NOW, policy_tables_hash(tables), tables, tmp_path / "policy.json",
                                   OperatorPolicyBundle((), (), (), NOW, source="fixture"))
    control = load_control()
    control["runtime"]["observed_at"] = NOW
    control["live"]["max_bpr_per_order"] = 2500
    control["risk_manager"]["enabled"] = False  # risk-manager behavior has its own suite; broker effects remain impossible here
    market = Market()
    market.account_state = lambda: PortfolioState(100000, 100000, 0, 0)
    monkeypatch.setattr(planning, "_market_provider", lambda *_args, **_kwargs: market)
    store = _migrated_store(tmp_path)
    args = dict(universe_rows=universe, playbook_rows=[row], idea_paths=[], provider="fixture", store=store,
                audit_root=tmp_path / "audit", daily_policy_snapshot=snapshot, trade_source_rows=sources,
                observed_package_batches=(replace(_batch(), packages=(_package(),)),), register_plan_attempt=False)
    result = planning.run_unified_books(control, **args)
    assert result.compilation.ok
    assert result.live.errors == ()
    assert len(result.live.handoffs) == 1
    lifecycle = CsaStore(store.sqlite_path).lifecycle(result.live.handoffs[0]["lifecycle_id"])
    assert lifecycle.status == "pending_live_submission"
    assert lifecycle.metadata["execution_venue"] == "tasty_primary"
    assert lifecycle.metadata["source_identity"]["package_signature"] == _package().package_signature
    assert lifecycle.metadata["compiled_management_policy"]["resolved_fields"]["profit_target_pct"] == 40
    assert not result.shadow.result.candidates
    replay = planning.run_unified_books(control, **args)
    assert not replay.live.result.plans
    assert replay.live.result.candidates[0].rejection_reason == "pilot_live_canary_already_reserved"
