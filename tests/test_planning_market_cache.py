from dataclasses import replace

import pytest

from kamandal_v2.domain.models import Idea, Playbook, UniverseEntry
from kamandal_v2.market.fixture import FixtureMarketDataProvider, FixturePreflightClient
from kamandal_v2.planner.candidate_builder import build_candidates, diagnose_idea_matches
from kamandal_v2.planner.market_cache import PlanningMarketCache


class CountingMarket(FixtureMarketDataProvider):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.snapshot = super().chain_snapshot("TSLA")

    def chain_snapshot(self, underlying):
        self.calls.append(underlying)
        return self.snapshot


def inputs():
    return (
        [Idea("scan", "market_scan", "TSLA", "neutral")],
        [UniverseEntry("TSLA", True)],
        [Playbook.from_row({"playbook_id": "strangle", "enabled": True,
                           "structure": "short_strangle", "iv_rank_min": 30,
                           "iv_rank_max": 90, "universe_expansion_enabled": True,
                           "underlying_price_min": 100, "underlying_price_max": 300,
                           "short_delta_min": .15, "short_delta_max": .30})],
    )


def test_build_and_diagnostics_share_complete_unchanged_snapshot():
    ideas, universe, playbooks = inputs()
    raw = CountingMarket()
    cached = PlanningMarketCache(raw)
    expected = build_candidates(ideas, universe, playbooks, raw, FixturePreflightClient())
    actual = build_candidates(ideas, universe, playbooks, cached, FixturePreflightClient())
    assert actual and [c.to_dict() for c in actual] == [c.to_dict() for c in expected]
    diagnostics = diagnose_idea_matches(ideas, universe, playbooks, cached)
    assert diagnostics[0]["matched_playbooks"] == ["strangle"]
    assert cached.chain_snapshot("TSLA") is raw.snapshot
    assert len({q.expiration for q in raw.snapshot.quotes}) == 3
    assert raw.calls == ["TSLA", "TSLA"]  # baseline plus one cached fetch
    assert cached.metrics() == {"chain_requests": 3, "chain_cache_hits": 2, "chain_symbols_fetched": 1}
    PlanningMarketCache(raw).chain_snapshot("TSLA")
    assert len(raw.calls) == 3  # next invocation must fetch again


@pytest.mark.parametrize("gate", ["iv", "direction", "disabled", "event", "missing_iv"])
def test_independent_rejections_skip_chains_in_both_passes(gate):
    ideas, universe, playbooks = inputs()
    market = CountingMarket()
    if gate == "iv":
        playbooks = [replace(playbooks[0], iv_rank_min=80)]
    elif gate == "direction":
        playbooks = [replace(playbooks[0], applicable_direction=["bullish"])]
    elif gate == "disabled":
        playbooks = [replace(playbooks[0], enabled=False)]
    elif gate == "event":
        market.event_status = lambda _: "earnings"
    else:
        market.iv_rank = lambda _: None
    assert build_candidates(ideas, universe, playbooks, market, FixturePreflightClient()) == []
    diagnostic = diagnose_idea_matches(ideas, universe, playbooks, market)[0]
    assert diagnostic["status"] == "no_playbook_match"
    assert diagnostic["market_context"]["chain_fetch_skipped"]
    assert "strangle_entry_outside_configured_ranges" not in diagnostic["reason_counts"]
    assert market.calls == []


def test_price_gate_still_fetches_and_rejects_and_permissive_iv_still_fetches():
    ideas, universe, playbooks = inputs()
    market = CountingMarket()
    playbooks = [replace(playbooks[0], underlying_price_max=200)]
    assert build_candidates(ideas, universe, playbooks, market, FixturePreflightClient()) == []
    assert market.calls == ["TSLA"]
    diagnostic = diagnose_idea_matches(ideas, universe, playbooks, market)[0]
    assert diagnostic["reason_counts"]["strangle_entry_outside_configured_ranges"] == 1
    # A permissive IV rejection must not become an early hard gate.
    playbooks = [replace(playbooks[0], universe_expansion_enabled=False, iv_rank_min=80)]
    result = build_candidates(ideas, universe, playbooks, market, FixturePreflightClient(), match_gate_mode="permissive")
    assert result
    assert len(market.calls) == 3


def test_failed_fetch_is_retryable():
    raw = CountingMarket()
    cache = PlanningMarketCache(raw)
    def fail(_):
        raise RuntimeError("temporary")
    raw.chain_snapshot = fail
    with pytest.raises(RuntimeError):
        cache.chain_snapshot("TSLA")
    raw.chain_snapshot = lambda _: raw.snapshot
    assert cache.chain_snapshot("TSLA") is raw.snapshot
    assert cache.chain_cache_hits == 0


def test_run_plan_cache_shared_with_supplements_and_reset_between_runs(tmp_path):
    from kamandal_v2.planner.engine import PlanningSourceGroup, run_plan
    from kamandal_v2.stores.audit import AuditWriter
    from kamandal_v2.stores.sqlite import LocalStore

    ideas, universe, playbooks = inputs()
    market = CountingMarket()
    def supplemental(provider, *_):
        assert provider.chain_snapshot("TSLA") is market.snapshot
        return []
    for index in range(2):
        result = run_plan(
            {"runtime": {"mode": "shadow"}}, idea_paths=[],
            universe_override=universe, playbooks_override=playbooks,
            source_groups_factory=lambda *_: [PlanningSourceGroup("scan", ideas, playbooks)],
            supplemental_candidate_factory=supplemental, market_override=market,
            store=LocalStore(tmp_path / f"{index}.db"), audit=AuditWriter(tmp_path / f"audit{index}"),
        )
        assert result.metrics["planning_market"] == {
            "chain_requests": 3, "chain_cache_hits": 2, "chain_symbols_fetched": 1,
        }
    assert market.calls == ["TSLA", "TSLA"]


def test_preflight_capability_remains_uncached():
    market = CountingMarket()
    calls = []
    market.preflight = lambda candidate: calls.append(candidate)
    cache = PlanningMarketCache(market)
    cache.preflight("first")
    cache.preflight("second")
    assert calls == ["first", "second"]
