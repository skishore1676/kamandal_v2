from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from kamandal_v2.config import load_control
from kamandal_v2.domain.models import ChainSnapshot, OptionQuote, Playbook, PortfolioState, utc_now
from kamandal_v2.intelligence.observed_packages import normalize_observed_package_output
from kamandal_v2.seed import build_seed_tables, seed_headers
from kamandal_v2.planner.observed_package_candidates import build_observed_package_candidates
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.planning import run_unified_books
from kamandal_v2.strategy_engine.policy import PolicyError, compile_playbook_policy
from kamandal_v2.strategy_lanes.migrations import migrate_csa_database
from kamandal_v2.strategy_lanes.management_runtime import run_shadow_lifecycle_management
from kamandal_v2.strategy_lanes.policy import PolicyError as CsaPolicyError, compile_csa_policy
from kamandal_v2.strategy_lanes.store import CsaStore


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "mike_observed_packages"


def _batch(fixture_index: int = 0):  # noqa: ANN202
    manifest = json.loads((FIXTURE_ROOT / "ground-truth.json").read_text(encoding="utf-8"))
    fixture = manifest["fixtures"][fixture_index]
    return normalize_observed_package_output(
        fixture["expected_extraction"],
        source_profile="mike_butler",
        canonical_post_id=fixture["post_id"],
        published_at=fixture["published_at"],
        image_sha256=tuple(image["sha256"] for image in fixture["images"]),
        prompt_sha256="1" * 64,
    )


def _observed_calendar_row() -> dict[str, object]:
    headers = seed_headers()["playbooks"]
    for raw in build_seed_tables(load_control())["playbooks"]:
        row = dict(zip(headers, [*raw, *[""] * len(headers)]))
        if row.get("playbook_id") == "call_calendar":
            row.update(
                {
                    "playbook_id": "mike_call_calendar_shadow",
                    "csa_stage": "shadow",
                    "mode": "shadow",
                    "source_mode": "observed_package",
                    "source_profiles": "mike_butler",
                    "max_bid_ask_pct": 0.5,
                    "min_option_oi": 10,
                    "sizing_method": "fixed_contracts",
                    "sizing_value": 1,
                    "max_contracts": 1,
                    "score_weight_credit": 1,
                    "score_weight_pop": 1,
                    "score_weight_liquidity": 1,
                    "score_weight_spread": 1,
                    "management_policy_json": json.dumps(
                        {
                            "lifecycle": {
                                "fill": {"max_attempts": 2, "price_increment": 0.10},
                                "close_only": True,
                                "profit_target_pct": 25,
                                "max_loss_multiple": 1.5,
                                "exit_dte_min": 14,
                            }
                        }
                    ),
                }
            )
            return row
    raise AssertionError("call calendar seed row missing")


class _Market:
    def __init__(self, *, captured_at: str | None = None, price_shift: float = 0.0) -> None:
        self.captured_at = captured_at or utc_now()
        self.price_shift = price_shift

    def account_state(self) -> PortfolioState:
        raise AssertionError("shadow observed-package planning must not read broker account state")

    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        assert underlying == "ADSK"
        return ChainSnapshot(
            chain_snapshot_id="adsk-exact-chain",
            underlying="ADSK",
            captured_at=self.captured_at,
            underlying_price=290,
            quotes=[
                OptionQuote("ADSK", "2026-08-28", "call", 290, 2.0 + self.price_shift, 2.2 + self.price_shift, 0.5, 0.02, -0.15, 0.08, 0.4, 500, 100),
                OptionQuote("ADSK", "2026-09-04", "call", 290, 3.4 + 2 * self.price_shift, 3.6 + 2 * self.price_shift, 0.55, 0.02, -0.08, 0.12, 0.4, 600, 100),
            ],
            source="fixture_exact",
        )

    def iv_percentile(self, _underlying: str) -> float:
        return 30

    def iv_rank(self, _underlying: str) -> float:
        return 30

    def iv_abs(self, _underlying: str) -> float:
        return 0.4

    def event_status(self, _underlying: str) -> str:
        return "unknown"


class _WidePackageMarket(_Market):
    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        snapshot = super().chain_snapshot(underlying)
        snapshot.quotes[1].bid = 2.1
        snapshot.quotes[1].ask = 2.3
        return snapshot


class _ProfitableExitMarket(_Market):
    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        snapshot = super().chain_snapshot(underlying)
        snapshot.quotes[0].bid = 0.9
        snapshot.quotes[0].ask = 1.0
        snapshot.quotes[1].bid = 3.5
        snapshot.quotes[1].ask = 3.6
        return snapshot


def _migrated_store(tmp_path: Path) -> LocalStore:
    database = tmp_path / "kamandal.db"
    store = LocalStore(database)
    migrate_csa_database(database, dry_run=False, backup_dir=tmp_path / "backups")
    return store


def test_observed_package_policy_is_shadow_only_and_profile_explicit() -> None:
    row = _observed_calendar_row()
    policy = compile_playbook_policy(row)
    assert policy.source_mode == "observed_package"
    assert policy.mode.value == "shadow"

    live = dict(row, mode="live", csa_stage="live")
    with pytest.raises(PolicyError, match="shadow-only"):
        compile_playbook_policy(live)
    missing_profile = dict(row, source_profiles="")
    with pytest.raises(PolicyError, match="requires source_profiles"):
        compile_playbook_policy(missing_profile)
    with pytest.raises(CsaPolicyError, match="shadow-only"):
        compile_csa_policy(live, source="google_sheet", read_at=utc_now())


def test_exact_package_enters_existing_planner_and_shadow_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kamandal_v2.strategy_engine import planning

    store = _migrated_store(tmp_path)
    monkeypatch.setattr(planning, "_market_provider", lambda *_args, **_kwargs: _Market())
    result = run_unified_books(
        load_control(),
        universe_rows=[],
        playbook_rows=[_observed_calendar_row()],
        idea_paths=[],
        provider="fixture",
        store=store,
        audit_root=tmp_path / "audit",
        observed_package_batches=(_batch(),),
    )

    assert result.compilation.ok
    assert result.live.policy_ids == ()
    assert result.shadow.errors == ()
    assert result.shadow.result is not None
    assert len(result.shadow.result.candidates) == 1
    candidate = result.shadow.result.candidates[0]
    assert [(leg.side, leg.expiration, leg.strike) for leg in candidate.legs] == [
        ("sell", "2026-08-28", 290),
        ("buy", "2026-09-04", 290),
    ]
    assert candidate.net_credit == -1.4
    assert candidate.metadata["observational_entry_mark"] == -1.4
    assert candidate.preflight.raw == {"source": "observed_package_shadow_local", "broker_effects": False}
    assert result.shadow.result.plans
    assert len(result.shadow.handoffs) == 1

    evidence = store.observed_package_evidence(source_profile="mike_butler")
    assert len(evidence) == 1
    lifecycle = CsaStore(store.sqlite_path).lifecycle(result.shadow.handoffs[0]["lifecycle_id"])
    assert lifecycle is not None
    assert lifecycle.metadata["source_identity"]["package_signature"] == candidate.metadata["package_signature"]
    assert lifecycle.metadata["observational_entry_mark"] == -1.4
    assert lifecycle.metadata["broker_effects"] is False


def test_stale_chain_parks_before_candidate_or_fill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kamandal_v2.strategy_engine import planning

    store = _migrated_store(tmp_path)
    control = load_control()
    control.setdefault("runtime", {})["observed_at"] = "2026-08-28T15:05:00Z"
    control["live"]["option_submission"]["quote_max_age_minutes"] = 1
    monkeypatch.setattr(planning, "_market_provider", lambda *_args, **_kwargs: _Market(captured_at="2026-08-28T15:00:00Z"))
    result = run_unified_books(
        control,
        universe_rows=[],
        playbook_rows=[_observed_calendar_row()],
        idea_paths=[],
        provider="fixture",
        store=store,
        audit_root=tmp_path / "audit",
        observed_package_batches=(_batch(),),
    )
    assert result.shadow.result is not None
    assert result.shadow.result.candidates == []
    assert result.shadow.handoffs == ()


def test_first_observational_midpoint_and_candidate_identity_are_replay_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kamandal_v2.strategy_engine import planning

    store = _migrated_store(tmp_path)
    markets = iter((_Market(price_shift=0.0), _Market(price_shift=0.5)))
    monkeypatch.setattr(planning, "_market_provider", lambda *_args, **_kwargs: next(markets))
    first = run_unified_books(
        load_control(),
        universe_rows=[],
        playbook_rows=[_observed_calendar_row()],
        idea_paths=[],
        provider="fixture",
        store=store,
        audit_root=tmp_path / "audit-first",
        observed_package_batches=(_batch(),),
    )
    second = run_unified_books(
        load_control(),
        universe_rows=[],
        playbook_rows=[_observed_calendar_row()],
        idea_paths=[],
        provider="fixture",
        store=store,
        audit_root=tmp_path / "audit-second",
        observed_package_batches=(_batch(),),
    )
    first_candidate = first.shadow.result.candidates[0]
    second_candidate = second.shadow.result.candidates[0]
    assert first_candidate.candidate_id == second_candidate.candidate_id
    assert first_candidate.net_credit != second_candidate.net_credit
    assert first_candidate.metadata["observational_entry_mark"] == -1.4
    assert second_candidate.metadata["observational_entry_mark"] == -1.4


def test_package_wide_spread_rejects_calendar_even_when_each_leg_is_acceptable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kamandal_v2.strategy_engine import planning

    store = _migrated_store(tmp_path)
    monkeypatch.setattr(planning, "_market_provider", lambda *_args, **_kwargs: _WidePackageMarket())
    result = run_unified_books(
        load_control(),
        universe_rows=[],
        playbook_rows=[_observed_calendar_row()],
        idea_paths=[],
        provider="fixture",
        store=store,
        audit_root=tmp_path / "audit",
        observed_package_batches=(_batch(),),
    )
    candidate = result.shadow.result.candidates[0]
    assert candidate.rejection_reason.startswith("package_bid_ask_pct_above_max")
    assert result.shadow.result.plans == []
    with sqlite3.connect(store.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observed_package_first_marks").fetchone()[0] == 0

    monkeypatch.setattr(planning, "_market_provider", lambda *_args, **_kwargs: _Market())
    valid = run_unified_books(
        load_control(),
        universe_rows=[],
        playbook_rows=[_observed_calendar_row()],
        idea_paths=[],
        provider="fixture",
        store=store,
        audit_root=tmp_path / "audit-valid",
        observed_package_batches=(_batch(),),
    )
    assert valid.shadow.result.candidates[0].metadata["observational_entry_mark"] == -1.4


def test_no_shadow_policy_records_one_not_authorized_receipt(tmp_path: Path) -> None:
    store = _migrated_store(tmp_path)
    result = run_unified_books(
        load_control(),
        universe_rows=[],
        playbook_rows=[],
        idea_paths=[],
        provider="fixture",
        store=store,
        audit_root=tmp_path / "audit",
        observed_package_batches=(_batch(),),
    )
    assert result.shadow.policy_ids == ()
    with sqlite3.connect(store.sqlite_path) as conn:
        rows = conn.execute(
            "SELECT payload FROM events WHERE event_type = 'observed_package_planner_receipt'"
        ).fetchall()
    receipts = [json.loads(row[0]) for row in rows]
    assert len(receipts) == 1
    assert receipts[0]["status"] == "not_authorized"
    assert receipts[0]["blocker"] == "no_matching_observed_package_policy"


def test_exact_package_reaches_unified_management_and_closes_with_lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kamandal_v2.strategy_engine import planning

    store = _migrated_store(tmp_path)
    monkeypatch.setattr(planning, "_market_provider", lambda *_args, **_kwargs: _Market())
    first = run_unified_books(
        load_control(),
        universe_rows=[],
        playbook_rows=[_observed_calendar_row()],
        idea_paths=[],
        provider="fixture",
        store=store,
        audit_root=tmp_path / "audit-1",
        observed_package_batches=(_batch(),),
    )
    lifecycle_id = first.shadow.handoffs[0]["lifecycle_id"]
    for index in (2, 3):
        run_unified_books(
            load_control(),
            universe_rows=[],
            playbook_rows=[_observed_calendar_row()],
            idea_paths=[],
            provider="fixture",
            store=store,
            audit_root=tmp_path / f"audit-{index}",
        )
    opened = CsaStore(store.sqlite_path).lifecycle(lifecycle_id)
    assert opened is not None and opened.status == "open"
    signature = opened.metadata["source_identity"]["package_signature"]
    first_mark = opened.metadata["observational_entry_mark"]

    observed_at = utc_now()
    management = run_shadow_lifecycle_management(
        load_control(),
        sqlite_path=str(store.sqlite_path),
        provider="fixture",
        tables={"universe": [], "playbooks": [], "daily_plan": []},
        market=_ProfitableExitMarket(captured_at=observed_at),
        observed_at=observed_at,
    )
    assert management.ok
    assert management.selected_actions == {"close": 1}
    closed = CsaStore(store.sqlite_path).lifecycle(lifecycle_id)
    assert closed is not None and closed.status == "closed"
    assert closed.metadata["source_identity"]["package_signature"] == signature
    assert closed.metadata["observational_entry_mark"] == first_mark
    assert closed.metadata["realized_pnl_usd"] > 0
    assert closed.metadata["broker_effects"] is False


def test_economic_rejection_keeps_first_actionable_source_mark(tmp_path: Path) -> None:
    package = _batch(2).packages[0]
    playbook = Playbook(
        playbook_id="mike_call_diagonal_shadow",
        enabled=True,
        strategy_family="call_diagonal",
        structure="call_diagonal",
        variant="source_exact",
        leg_count=2,
        profiles=[],
        max_bid_ask_pct=0.5,
        min_option_oi=10,
        live_max_bpr_per_order=100,
        max_debit_to_width_ratio=1.0,
    )
    policy = SimpleNamespace(
        playbook_id=playbook.playbook_id,
        source_mode="observed_package",
        structure="call_diagonal",
        fields={"source_profiles": "mike_butler"},
    )
    store = LocalStore(tmp_path / "state.db")

    class DiagonalMarket:
        def __init__(self, far_mid: float) -> None:
            self.far_mid = far_mid

        def chain_snapshot(self, underlying: str) -> ChainSnapshot:
            assert underlying == "UPS"
            return ChainSnapshot(
                chain_snapshot_id=f"ups-{self.far_mid}",
                underlying="UPS",
                captured_at=utc_now(),
                underlying_price=105,
                quotes=[
                    OptionQuote("UPS", "2026-10-16", "call", 110, 2.0, 2.1, 0.3, 0.01, -0.05, 0.05, 0.3, 500),
                    OptionQuote("UPS", "2026-12-18", "call", 100, self.far_mid - 0.05, self.far_mid + 0.05, 0.6, 0.02, -0.03, 0.10, 0.35, 500),
                ],
                source="fixture_exact",
            )

    rejected = build_observed_package_candidates(
        (package,),
        policies=(policy,),
        playbooks=[playbook],
        market=DiagonalMarket(6.0),
        store=store,
        config=load_control(),
    )[0]
    assert rejected.rejection_reason.startswith("diagonal_debit_bpr_above_max")
    assert rejected.metadata["observational_entry_mark"] == -3.95

    eligible = build_observed_package_candidates(
        (package,),
        policies=(policy,),
        playbooks=[playbook],
        market=DiagonalMarket(2.5),
        store=store,
        config=load_control(),
    )[0]
    assert eligible.rejection_reason == ""
    assert eligible.net_credit == -0.45
    assert eligible.metadata["observational_entry_mark"] == -3.95


def test_close_post_stays_passive_and_cannot_create_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kamandal_v2.strategy_engine import planning

    batch = _batch()
    package = batch.packages[0]
    object.__setattr__(package, "action", "close")
    store = _migrated_store(tmp_path)
    monkeypatch.setattr(planning, "_market_provider", lambda *_args, **_kwargs: _Market())
    result = run_unified_books(
        load_control(),
        universe_rows=[],
        playbook_rows=[_observed_calendar_row()],
        idea_paths=[],
        provider="fixture",
        store=store,
        audit_root=tmp_path / "audit",
        observed_package_batches=(batch,),
    )
    assert result.shadow.result is not None
    assert result.shadow.result.candidates == []
    assert len(store.observed_package_evidence(source_profile="mike_butler")) == 1
