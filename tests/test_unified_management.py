from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import subprocess

from kamandal_v2.domain.models import OptionLeg
from kamandal_v2.events.earnings import EarningsSnapshot, EarningsStore
from kamandal_v2.strategy_engine.management import run_unified_lifecycle_management
from kamandal_v2.strategy_lanes.management_runtime import _half_time_state, _pre_event_exit_state
from kamandal_v2.strategy_lanes.models import CsaStage, LaneId, LifecycleState, SourceMode
from kamandal_v2.strategy_lanes.policy import CsaPolicy


def test_scheduled_manager_imports_only_generic_lifecycle_owners() -> None:
    source = Path("src/kamandal_v2/strategy_engine/management.py").read_text(encoding="utf-8")

    assert "run_csa_live_management" not in source
    assert "run_csa_shadow_management" not in source
    assert "run_live_lifecycle_management" in source
    assert "run_shadow_lifecycle_management" in source


def test_unified_management_runs_live_before_shadow_and_isolates_failure() -> None:
    calls: list[str] = []

    def typed_live():
        calls.append("live_lifecycle")
        raise RuntimeError("fixture live lifecycle failure")

    def typed_shadow():
        calls.append("shadow_lifecycle")
        return {"ok": True, "managed": 2}

    receipt = run_unified_lifecycle_management(
        {},
        sqlite_path="fixture.db",
        provider="fixture",
        live_lifecycle_manager=typed_live,
        shadow_lifecycle_manager=typed_shadow,
    )

    assert calls == ["live_lifecycle", "shadow_lifecycle"]
    assert receipt.ok is False
    assert receipt.branches[0].error == "RuntimeError: fixture live lifecycle failure"
    assert receipt.branches[1].result == {"ok": True, "managed": 2}


def test_unified_management_can_finish_live_effect_boundary_before_shadow() -> None:
    calls: list[str] = []

    receipt = run_unified_lifecycle_management(
        {},
        sqlite_path="fixture.db",
        provider="fixture",
        branch="live",
        live_lifecycle_manager=lambda: calls.append("live") or {"ok": True},
        shadow_lifecycle_manager=lambda: calls.append("shadow") or {"ok": True},
    )

    assert calls == ["live"]
    assert [branch.branch for branch in receipt.branches] == ["live_lifecycle"]
    assert receipt.ok is True


def test_unified_management_retries_only_transient_sqlite_lock(monkeypatch) -> None:  # noqa: ANN001
    calls = 0
    sleeps: list[float] = []

    def typed_live():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "ok": False,
                "errors": ["fixture: OperationalError: database is locked"],
            }
        return {"ok": True, "errors": []}

    monkeypatch.setattr("kamandal_v2.strategy_engine.management.time.sleep", sleeps.append)
    receipt = run_unified_lifecycle_management(
        {},
        sqlite_path="fixture.db",
        provider="fixture",
        branch="live",
        live_lifecycle_manager=typed_live,
        shadow_lifecycle_manager=lambda: {"ok": True},
    )

    assert calls == 2
    assert sleeps == [1.0]
    assert receipt.ok is True


def test_unified_management_does_not_retry_mixed_or_nonlock_failures(monkeypatch) -> None:  # noqa: ANN001
    calls = 0

    def typed_live():
        nonlocal calls
        calls += 1
        return {
            "ok": False,
            "errors": [
                "fixture: OperationalError: database is locked",
                "fixture: RuntimeError: quote unavailable",
            ],
        }

    monkeypatch.setattr(
        "kamandal_v2.strategy_engine.management.time.sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("unexpected retry")),
    )
    receipt = run_unified_lifecycle_management(
        {},
        sqlite_path="fixture.db",
        provider="fixture",
        branch="live",
        live_lifecycle_manager=typed_live,
        shadow_lifecycle_manager=lambda: {"ok": True},
    )

    assert calls == 1
    assert receipt.ok is False


def test_scheduled_management_completes_live_cycle_before_shadow() -> None:
    source = Path("scripts/run_unified_lifecycle_management.sh").read_text(encoding="utf-8")

    pre_sync = source.index('"$KAMANDAL_BIN" sync-live-orders')
    live = source.index("--branch live")
    close = source.index('"$KAMANDAL_BIN" execute-live-approved-closes --submit-auto')
    post_sync = source.index('"$KAMANDAL_BIN" sync-live-orders', pre_sync + 1)
    cleanup = source.index('"$KAMANDAL_BIN" cleanup-live-approvals')
    shadow = source.index("--branch shadow")

    assert pre_sync < live < close < post_sync < cleanup < shadow
    assert 'return "$live_status"' in source
    assert "KAMANDAL_LIFECYCLE_TERMINAL=" in source

    entry_source = Path("scripts/run_live_approved_orders.sh").read_text(encoding="utf-8")
    assert '"$KAMANDAL_BIN" sync-live-orders --read-only' in entry_source
    assert "execute-live-approved-closes" not in entry_source
    assert "cleanup-live-approvals" not in entry_source


def _run_scheduled_manager(tmp_path, failures: dict[str, int]):  # noqa: ANN001
    fake = tmp_path / "kamandal"
    fake.write_text(
        """#!/bin/bash
set -u
command="$1"
shift
step="$command"
if [[ "$command" == "unified-lifecycle-management" ]]; then
  while (( $# )); do
    if [[ "$1" == "--branch" ]]; then
      step="${command}:$2"
      break
    fi
    shift
  done
elif [[ "$command" == "sync-live-orders" ]]; then
  count_file="${FAKE_SYNC_COUNT_FILE}"
  count=0
  [[ -f "$count_file" ]] && count="$(<"$count_file")"
  count=$((count + 1))
  printf '%s' "$count" > "$count_file"
  step="${command}:$count"
fi
printf '{"step":"%s"}\n' "$step"
case "$step" in
  sync-live-orders:1) exit "${FAIL_PRE_SYNC:-0}" ;;
  unified-lifecycle-management:live) exit "${FAIL_LIVE:-0}" ;;
  execute-live-approved-closes) exit "${FAIL_CLOSE:-0}" ;;
  sync-live-orders:2) exit "${FAIL_POST_SYNC:-0}" ;;
  cleanup-live-approvals) exit "${FAIL_CLEANUP:-0}" ;;
  unified-lifecycle-management:shadow) exit "${FAIL_SHADOW:-0}" ;;
esac
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "KAMANDAL_BIN": str(fake),
        "KAMANDAL_FORCE_RUN": "1",
        "KAMANDAL_RUNLOCK_ROOT": str(tmp_path / "locks"),
        "FAKE_SYNC_COUNT_FILE": str(tmp_path / "sync-count"),
        **{name: str(code) for name, code in failures.items()},
    }
    return subprocess.run(
        ["bash", "scripts/run_unified_lifecycle_management.sh"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _terminal_receipt(stdout: str) -> dict:
    line = next(line for line in stdout.splitlines() if line.startswith("KAMANDAL_LIFECYCLE_TERMINAL="))
    return json.loads(line.split("=", 1)[1])


def test_scheduled_manager_shadow_failure_is_non_terminal(tmp_path) -> None:  # noqa: ANN001
    completed = _run_scheduled_manager(tmp_path, {"FAIL_SHADOW": 9})

    assert completed.returncode == 0
    assert _terminal_receipt(completed.stdout) == {
        "status": "succeeded",
        "exit_code": 0,
        "steps": {
            "pre_sync": 0,
            "live_lifecycle": 0,
            "execute_live_closes": 0,
            "post_sync": 0,
            "cleanup_live_approvals": 0,
            "shadow_lifecycle": 9,
        },
    }


def test_scheduled_manager_live_failure_remains_terminal_after_shadow_success(tmp_path) -> None:  # noqa: ANN001
    completed = _run_scheduled_manager(tmp_path, {"FAIL_LIVE": 7})

    assert completed.returncode == 7
    receipt = _terminal_receipt(completed.stdout)
    assert receipt["status"] == "failed"
    assert receipt["exit_code"] == 7
    assert receipt["steps"]["live_lifecycle"] == 7
    assert receipt["steps"]["shadow_lifecycle"] == 0


def test_scheduled_manager_pre_sync_failure_skips_live_effects(tmp_path) -> None:  # noqa: ANN001
    completed = _run_scheduled_manager(tmp_path, {"FAIL_PRE_SYNC": 6})

    assert completed.returncode == 6
    assert _terminal_receipt(completed.stdout)["steps"] == {
        "pre_sync": 6,
        "live_lifecycle": -1,
        "execute_live_closes": -1,
        "post_sync": -1,
        "cleanup_live_approvals": -1,
        "shadow_lifecycle": 0,
    }


def _policy(**fields) -> CsaPolicy:  # noqa: ANN003
    return CsaPolicy(
        playbook_id="fixture-playbook",
        lane=LaneId.CALL_VERTICAL,
        stage=CsaStage.SHADOW,
        source_mode=SourceMode.IDEA,
        management={"lifecycle": {"fill": {"max_attempts": 2, "price_increment": 0.05}}},
        resolved_fields=fields,
        policy_hash="fixture-policy",
        source="fixture",
        read_at="2026-08-01T14:30:00Z",
    )


def _lifecycle() -> LifecycleState:
    return LifecycleState(
        lifecycle_id="fixture-lifecycle",
        opportunity_id="fixture-opportunity",
        lane=LaneId.CALL_VERTICAL,
        version=2,
        status="open",
        active_legs=(),
        cashflow_ledger=(
            {
                "ticket_id": "entry",
                "fill_id": "entry-fill",
                "amount": 1.0,
                "filled_at": "2026-08-03T14:30:00Z",
            },
        ),
        opened_at="2026-08-01T14:30:00Z",
        updated_at="2026-08-03T14:30:00Z",
        policy_hash="fixture-policy",
    )


def _leg(expiration: str) -> OptionLeg:
    return OptionLeg("short_call", "sell", "call", 100, expiration, 1, 1.0, 0.95, 1.05, 0.3, 0, 0, 0, 100)


def test_half_time_uses_completed_entry_fill_date_and_sheet_switch() -> None:
    lifecycle = _lifecycle()
    policy = _policy(half_time_exit="TRUE")
    leg = _leg("2026-09-02")

    before = _half_time_state(lifecycle, policy, (leg,), remaining_dtes=[16])
    due = _half_time_state(lifecycle, policy, (leg,), remaining_dtes=[15])
    disabled = _half_time_state(lifecycle, _policy(half_time_exit="FALSE"), (leg,), remaining_dtes=[15])

    assert before == {"entry_dte": 30, "remaining_dte": 16, "threshold": 15, "due": False}
    assert due["due"] is True
    assert disabled["due"] is False


def test_pre_event_exit_uses_latest_captured_earnings_and_sheet_days(tmp_path) -> None:  # noqa: ANN001
    database = tmp_path / "kamandal.db"
    EarningsStore(database).save(
        EarningsSnapshot(
            symbol="XYZ",
            fetched_date="2026-08-17",
            next_earnings_date="2026-08-20",
            source="fixture",
            confirmed=True,
        )
    )

    due = _pre_event_exit_state(
        _policy(exit_pre_event_days="3"),
        sqlite_path=str(database),
        underlying="XYZ",
        observed_date=date(2026, 8, 17),
    )
    not_due = _pre_event_exit_state(
        _policy(exit_pre_event_days="2"),
        sqlite_path=str(database),
        underlying="XYZ",
        observed_date=date(2026, 8, 17),
    )

    assert due == {"due": True, "days_to_event": 3, "event_date": "2026-08-20"}
    assert not_due["due"] is False
