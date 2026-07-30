from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

from kamandal_v2.config import load_control
from kamandal_v2.domain.models import PortfolioState
from kamandal_v2.live.health import entry_health_gate, run_live_health
from kamandal_v2.live.risk_manager import (
    BREAKER_ACCOUNT_SNAPSHOT_STALE,
    BREAKER_CONSECUTIVE_LOSSES,
    BREAKER_DAILY_NEW_POSITIONS,
    BREAKER_WEEKLY_DRAWDOWN,
    REASON_CLUSTER_AT_CAP,
    REASON_UNDERLYING_AT_CAP,
    cluster_capped_symbols,
    evaluate_entry_risk,
    underlying_capped_symbols,
)
from kamandal_v2.stores.sqlite import LocalStore

NOW = datetime(2026, 7, 2, 15, 0, 0, tzinfo=UTC)


def _enabled_config(**overrides) -> dict:
    settings = {
        "enabled": True,
        "max_daily_drawdown_pct": 3.0,
        "max_weekly_drawdown_pct": 5.0,
        "consecutive_loss_limit": 3,
        "cooldown_days": 2,
        "max_new_positions_per_day": 3,
        "max_positions_per_cluster": 2,
        "correlation_clusters": {"semis": ["NVDA", "AMD", "MRVL"]},
    }
    settings.update(overrides)
    return {"risk_manager": settings}


def _snapshot(store: LocalStore, stamp: datetime, account_size: float) -> None:
    store.save_account_snapshot(
        f"run_{stamp.strftime('%Y%m%dT%H%M%S')}Z",
        PortfolioState(account_size=account_size, buying_power=account_size, bpr_used=0.0, positions_count=0),
    )


def _open_group(store: LocalStore, group_id: str, underlying: str = "AAPL") -> None:
    store.save_live_position_group(group_id, {"group_id": group_id, "underlying": underlying})


def _closed_losing_group(store: LocalStore, group_id: str, pnl: float) -> None:
    _open_group(store, group_id)
    store.record_live_position_mark(group_id, {"underlying": "AAPL", "pnl_mid": pnl})
    store.close_live_position_group(group_id, status="closed", reason="test", payload={"group_id": group_id})


def test_disabled_by_default(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _snapshot(store, NOW - timedelta(days=1), 10_000)
    _snapshot(store, NOW, 8_000)

    decision = evaluate_entry_risk(store, {}, now=NOW)

    assert decision.enabled is False
    assert decision.blocked is False
    assert entry_health_gate(store, {})["blocked"] is False


def test_weekly_drawdown_breaker_blocks(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _snapshot(store, NOW - timedelta(days=5), 10_000)
    _snapshot(store, NOW - timedelta(hours=1), 9_300)

    decision = evaluate_entry_risk(store, _enabled_config(max_daily_drawdown_pct=None), now=NOW)

    assert decision.blocked is True
    assert BREAKER_WEEKLY_DRAWDOWN in decision.reason_codes()


def test_drawdown_within_limit_allows(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _snapshot(store, NOW - timedelta(days=5), 10_000)
    _snapshot(store, NOW - timedelta(hours=1), 9_700)

    decision = evaluate_entry_risk(store, _enabled_config(), now=NOW)

    assert decision.blocked is False
    assert decision.reasons == []


def test_stale_account_snapshot_blocks_entries_when_configured(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _snapshot(store, NOW - timedelta(hours=4), 10_000)

    decision = evaluate_entry_risk(
        store,
        _enabled_config(max_account_snapshot_age_minutes=180),
        now=NOW,
    )

    assert decision.blocked is True
    assert BREAKER_ACCOUNT_SNAPSHOT_STALE in decision.reason_codes()
    reason = next(item for item in decision.reasons if item["code"] == BREAKER_ACCOUNT_SNAPSHOT_STALE)
    assert reason["age_minutes"] == 240.0
    assert reason["max_age_minutes"] == 180


def test_missing_account_snapshot_blocks_entries_when_configured(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")

    decision = evaluate_entry_risk(
        store,
        _enabled_config(max_account_snapshot_age_minutes=180),
        now=NOW,
    )

    assert decision.blocked is True
    assert BREAKER_ACCOUNT_SNAPSHOT_STALE in decision.reason_codes()


def test_consecutive_losses_trigger_cooldown(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    for index in range(3):
        _closed_losing_group(store, f"group_loss_{index}", pnl=-40.0)

    # closed_at is CURRENT_TIMESTAMP, so evaluate close to real now
    decision = evaluate_entry_risk(store, _enabled_config(max_new_positions_per_day=10), now=datetime.now(UTC))

    assert decision.blocked is True
    assert decision.reason_codes() == [BREAKER_CONSECUTIVE_LOSSES]


def test_win_breaks_loss_streak(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _closed_losing_group(store, "group_loss_old_0", pnl=-40.0)
    _closed_losing_group(store, "group_loss_old_1", pnl=-40.0)
    _closed_losing_group(store, "group_win", pnl=55.0)

    decision = evaluate_entry_risk(store, _enabled_config(max_new_positions_per_day=10), now=datetime.now(UTC))

    assert decision.blocked is False


def test_loss_cooldown_expires(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    for index in range(3):
        _closed_losing_group(store, f"group_loss_{index}", pnl=-40.0)

    later = datetime.now(UTC) + timedelta(days=3)
    decision = evaluate_entry_risk(store, _enabled_config(cooldown_days=2, max_new_positions_per_day=10), now=later)

    assert decision.blocked is False


def test_daily_new_position_cap(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    for index in range(3):
        _open_group(store, f"group_today_{index}")

    decision = evaluate_entry_risk(store, _enabled_config(), now=datetime.now(UTC))

    assert decision.blocked is True
    assert BREAKER_DAILY_NEW_POSITIONS in decision.reason_codes()


def test_daily_new_position_cap_uses_configured_market_day(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    for index in range(4):
        _open_group(store, f"group_{index}")
    with sqlite3.connect(tmp_path / "kamandal_v2.db") as conn:
        conn.execute("UPDATE live_position_groups SET opened_at = ? WHERE group_id = ?", ("2026-07-02 02:00:00", "group_0"))
        conn.execute("UPDATE live_position_groups SET opened_at = ? WHERE group_id = ?", ("2026-07-02 06:00:00", "group_1"))
        conn.execute("UPDATE live_position_groups SET opened_at = ? WHERE group_id = ?", ("2026-07-02 07:00:00", "group_2"))
        conn.execute("UPDATE live_position_groups SET opened_at = ? WHERE group_id = ?", ("2026-07-02 08:00:00", "group_3"))

    decision = evaluate_entry_risk(
        store,
        {"runtime": {"market_timezone": "America/Chicago"}, "risk_manager": {"enabled": True, "max_new_positions_per_day": 3}},
        now=datetime(2026, 7, 2, 15, 0, 0, tzinfo=UTC),
    )

    reason = next(item for item in decision.reasons if item["code"] == BREAKER_DAILY_NEW_POSITIONS)
    assert reason["opened_today"] == 3
    assert reason["market_day_start"] == "2026-07-02 05:00:00"


def test_cluster_cap_reports_without_global_block(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _open_group(store, "group_nvda", underlying="NVDA")
    _open_group(store, "group_amd", underlying="AMD")

    config = _enabled_config(max_new_positions_per_day=10)
    decision = evaluate_entry_risk(store, config, now=datetime.now(UTC))

    assert decision.blocked is False
    assert REASON_CLUSTER_AT_CAP in decision.reason_codes()
    assert decision.clusters_at_cap == {"semis": ["AMD", "MRVL", "NVDA"]}
    assert cluster_capped_symbols(decision) == {"NVDA", "AMD", "MRVL"}

    gate = entry_health_gate(store, config)
    assert gate["blocked"] is False
    assert gate["risk_manager"]["clusters_at_cap"] == {"semis": ["AMD", "MRVL", "NVDA"]}


def test_per_cluster_limits_override_legacy_fallback(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _open_group(store, "group_nvda", underlying="NVDA")
    _open_group(store, "group_amd", underlying="AMD")
    _open_group(store, "group_aapl", underlying="AAPL")
    _open_group(store, "group_msft", underlying="MSFT")
    _open_group(store, "group_meta", underlying="META")

    config = _enabled_config(
        max_new_positions_per_day=10,
        max_positions_per_cluster=1,
        max_positions_by_cluster={"semis": 3, "megacap_tech": 4},
        correlation_clusters={
            "semis": ["NVDA", "AMD", "MRVL"],
            "megacap_tech": ["AAPL", "MSFT", "META", "GOOGL"],
        },
    )
    decision = evaluate_entry_risk(store, config, now=datetime.now(UTC))

    assert decision.clusters_at_cap == {}
    _open_group(store, "group_mrvl", underlying="MRVL")
    _open_group(store, "group_googl", underlying="GOOGL")
    decision = evaluate_entry_risk(store, config, now=datetime.now(UTC))

    assert decision.clusters_at_cap == {
        "megacap_tech": ["AAPL", "GOOGL", "META", "MSFT"],
        "semis": ["AMD", "MRVL", "NVDA"],
    }
    reasons = [item for item in decision.reasons if item["code"] == REASON_CLUSTER_AT_CAP]
    assert {item["cluster"]: item["max_positions"] for item in reasons} == {
        "megacap_tech": 4,
        "semis": 3,
    }


def test_underlying_cap_blocks_only_that_symbol_without_global_block(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    _open_group(store, "group_baba_1", underlying="BABA")
    _open_group(store, "group_baba_2", underlying="BABA")

    config = _enabled_config(
        max_new_positions_per_day=10,
        max_positions_per_underlying=2,
        max_positions_per_cluster=None,
    )
    decision = evaluate_entry_risk(store, config, now=datetime.now(UTC))

    assert decision.blocked is False
    assert REASON_UNDERLYING_AT_CAP in decision.reason_codes()
    assert decision.underlyings_at_cap == {"BABA": 2}
    assert underlying_capped_symbols(decision) == {"BABA"}
    gate = entry_health_gate(store, config)
    assert gate["blocked"] is False
    assert gate["risk_manager"]["underlyings_at_cap"] == {"BABA": 2}


def test_breaker_blocks_through_entry_health_gate(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "kamandal_v2.db")
    now = datetime.now(UTC)
    _snapshot(store, now - timedelta(days=5), 10_000)
    _snapshot(store, now - timedelta(hours=1), 9_000)

    config = _enabled_config(max_daily_drawdown_pct=None)
    report = run_live_health(store, config)
    gate = entry_health_gate(store, config)

    assert report["overall"] == "RED"
    assert BREAKER_WEEKLY_DRAWDOWN in report["reasons"]
    assert gate["blocked"] is True


def test_env_overrides_wire_risk_manager(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KAMANDAL_RISK_MANAGER_ENABLED", "true")
    monkeypatch.setenv("KAMANDAL_RISK_MAX_ACCOUNT_SNAPSHOT_AGE_MINUTES", "90")
    monkeypatch.setenv("KAMANDAL_RISK_MAX_WEEKLY_DRAWDOWN_PCT", "7.5")
    monkeypatch.setenv("KAMANDAL_RISK_CONSECUTIVE_LOSS_LIMIT", "4")
    monkeypatch.setenv("KAMANDAL_RISK_MAX_NEW_POSITIONS_PER_DAY", "4")
    monkeypatch.setenv("KAMANDAL_RISK_MAX_POSITIONS_PER_UNDERLYING", "2")

    config = load_control()

    assert config["risk_manager"]["enabled"] is True
    assert config["risk_manager"]["max_weekly_drawdown_pct"] == 7.5
    assert config["risk_manager"]["consecutive_loss_limit"] == 4
    assert config["risk_manager"]["max_account_snapshot_age_minutes"] == 90
    assert config["risk_manager"]["max_positions_per_underlying"] == 2
    # yaml defaults still present for knobs without env overrides
    assert config["risk_manager"]["max_new_positions_per_day"] == 4


def test_control_defaults_keep_global_bpr_and_define_entry_concentration_caps() -> None:
    config = load_control()

    assert config["portfolio"]["target_max_bpr_utilization_pct"] == 55
    assert config["portfolio"]["hard_max_bpr_utilization_pct"] == 55
    assert config["risk_manager"]["max_positions_per_underlying"] == 2
    assert config["risk_manager"]["max_positions_by_cluster"] == {
        "megacap_tech": 4,
        "semis": 3,
        "broad_index": 2,
        "crypto_adjacent": 2,
    }
