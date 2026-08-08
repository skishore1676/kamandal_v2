from __future__ import annotations

import os
import plistlib
import subprocess
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from kamandal_v2.config import load_control
from kamandal_v2.live.option_sessions import submission_window
from kamandal_v2.ops.launchd_registry import DISABLED_BY_DEFAULT, JOB_SCHEDULES


CENTRAL = ZoneInfo("America/Chicago")
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_late_day_sequence_preserves_dependency_order_and_submission_window() -> None:
    assert JOB_SCHEDULES["iv-afternoon"].fixed_times[-1] == time(13, 45)
    assert JOB_SCHEDULES["youtube"].fixed_times[-1] == time(14, 0)
    assert JOB_SCHEDULES["live-reconciliation"].fixed_times[-1] == time(14, 20)
    assert JOB_SCHEDULES["live-advisory"].fixed_times[-1] == time(14, 30)
    assert time(14, 50) in JOB_SCHEDULES["live-management"].fixed_times

    config = load_control()
    allowed = submission_window(
        config,
        {"underlying": "AAPL"},
        close=False,
        now=datetime(2026, 7, 30, 14, 35, tzinfo=CENTRAL),
    )
    cutoff = submission_window(
        config,
        {"underlying": "AAPL"},
        close=False,
        now=datetime(2026, 7, 30, 14, 40, tzinfo=CENTRAL),
    )

    assert allowed["allowed"] is True
    assert allowed["submission_cutoff_at"].startswith("2026-07-30T14:40:00")
    assert cutoff["allowed"] is False
    assert cutoff["reason"] == "entry_cutoff_reached"


def test_live_vertical_caps_are_750() -> None:
    by_structure = load_control()["live"]["max_bpr_per_order_by_structure"]

    assert by_structure["put_spread"] == 750
    assert by_structure["call_spread"] == 750
    assert by_structure["iron_condor"] == 500
    assert by_structure["jade_lizard"] == 500


def test_installer_renders_registry_schedule(tmp_path: Path) -> None:
    launchd_dir = tmp_path / "LaunchAgents"
    log_dir = tmp_path / "logs"
    env = {
        **os.environ,
        "KAMANDAL_REPO_ROOT": str(REPO_ROOT),
        "KAMANDAL_LAUNCHD_DIR": str(launchd_dir),
        "KAMANDAL_LAUNCHD_LOG_DIR": str(log_dir),
    }

    subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/launchd/install_kamandal_launchd.sh"), "render"],
        check=True,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    def weekday_one_times(suffix: str) -> set[tuple[int, int]]:
        payload = plistlib.loads((launchd_dir / f"com.kamandal.v2.{suffix}.plist").read_bytes())
        return {
            (int(entry["Hour"]), int(entry["Minute"]))
            for entry in payload["StartCalendarInterval"]
            if int(entry["Weekday"]) == 1
        }

    assert weekday_one_times("iv_afternoon") == {(13, 45)}
    assert weekday_one_times("youtube") == {(9, 15), (11, 45), (14, 0)}
    assert weekday_one_times("live_reconciliation") == {(8, 35), (10, 30), (12, 30), (14, 20)}
    assert weekday_one_times("live_advisory") == {(9, 25), (11, 55), (14, 30)}
    assert (14, 35) in weekday_one_times("live_approved_orders")
    assert {(14, 45), (14, 50), (15, 5)}.issubset(weekday_one_times("live_management"))
    assert weekday_one_times("csa_shadow_scan") == {(9, 35), (12, 5), (14, 35)}
    assert (14, 45) in weekday_one_times("csa_shadow_management")
    assert weekday_one_times("csa_shadow_scorecard") == {(15, 25)}
    for suffix in ("csa_shadow_scan", "csa_shadow_management", "csa_shadow_scorecard"):
        payload = plistlib.loads((launchd_dir / f"com.kamandal.v2.{suffix}.plist").read_bytes())
        assert payload["Disabled"] is True
    assert DISABLED_BY_DEFAULT == {"csa-shadow-scan", "csa-shadow-management", "csa-shadow-scorecard"}
