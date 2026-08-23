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
    assert JOB_SCHEDULES["live-reconciliation"].fixed_times[-1] == time(14, 10)
    assert JOB_SCHEDULES["unified-planning"].fixed_times[-1] == time(14, 15)
    assert JOB_SCHEDULES["daily-report"].fixed_times[-1] == time(15, 25)
    assert JOB_SCHEDULES["daily-report"].fixed_times[-1] > JOB_SCHEDULES["unified-lifecycle-management"].window_end
    lifecycle = JOB_SCHEDULES["unified-lifecycle-management"]
    assert lifecycle.cadence_minutes == 5
    assert lifecycle.window_start == time(8, 30)
    assert lifecycle.window_end == time(15, 15)

    config = load_control()
    allowed = submission_window(
        config,
        {"underlying": "AAPL"},
        close=False,
        now=datetime(2026, 7, 30, 14, 20, tzinfo=CENTRAL),
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
    assert weekday_one_times("x_bookmarks") == {(8, 15)}
    assert weekday_one_times("live_reconciliation") == {(8, 35), (10, 30), (12, 30), (14, 10)}
    assert weekday_one_times("unified_planning") == {
        (8, 50),
        (9, 25),
        (11, 55),
        (14, 15),
    }
    assert {(8, 30), (14, 35)}.issubset(weekday_one_times("live_approved_orders"))
    assert {(8, 30), (9, 45), (14, 40), (15, 15)}.issubset(weekday_one_times("unified_lifecycle_management"))
    rendered = {path.name for path in launchd_dir.glob("*.plist")}
    assert not any("csa_" in item or "live_advisory" in item or "live_management" in item for item in rendered)
    assert DISABLED_BY_DEFAULT == set()


def test_current_idea_filter_keeps_refined_correspondents_and_current_day_files(tmp_path: Path) -> None:
    source = tmp_path / "active"
    current_root = tmp_path / "current"
    source.mkdir()
    today = datetime.now(CENTRAL).date().isoformat()
    (source / f"x_bookmarks_imported_{today}.yaml").write_text("ideas: []\n", encoding="utf-8")
    (source / "correspondent_greg_harmon.yaml").write_text("ideas: []\n", encoding="utf-8")
    (source / "x_bookmarks_imported_2000-01-01.yaml").write_text("ideas: []\n", encoding="utf-8")
    (source / "undated_manual.yaml").write_text("ideas: []\n", encoding="utf-8")
    command = (
        f"source {REPO_ROOT / 'scripts/common.sh'}; "
        f"prepare_current_ideas_dir {source} test_lane; "
        'find "$CURRENT_IDEAS_DIR" -maxdepth 1 -type l -exec basename {} \\; | sort'
    )

    result = subprocess.run(
        ["bash", "-c", command],
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, "KAMANDAL_CURRENT_IDEAS_ROOT": str(current_root)},
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines()[-2:] == [
        "correspondent_greg_harmon.yaml",
        f"x_bookmarks_imported_{today}.yaml",
    ]


def test_installer_can_render_only_unified_ownership_jobs(tmp_path: Path) -> None:
    launchd_dir = tmp_path / "LaunchAgents"
    env = {
        **os.environ,
        "KAMANDAL_REPO_ROOT": str(REPO_ROOT),
        "KAMANDAL_LAUNCHD_DIR": str(launchd_dir),
        "KAMANDAL_LAUNCHD_LOG_DIR": str(tmp_path / "logs"),
    }

    subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/launchd/install_kamandal_launchd.sh"), "render-unified"],
        check=True,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    rendered = {path.name for path in launchd_dir.glob("*.plist")}
    assert rendered == {
        "com.kamandal.v2.unified_planning.plist",
        "com.kamandal.v2.unified_lifecycle_management.plist",
    }


def test_retired_competing_entrypoints_fail_closed() -> None:
    for script, unified_owner in (
        ("run_live_advisory.sh", "run_unified_planning.sh"),
        ("run_csa_shadow_scan.sh", "run_unified_planning.sh"),
        ("run_csa_live_scan.sh", "run_unified_planning.sh"),
        ("run_live_management.sh", "run_unified_lifecycle_management.sh"),
        ("run_csa_shadow_management.sh", "run_unified_lifecycle_management.sh"),
        ("run_csa_live_management.sh", "run_unified_lifecycle_management.sh"),
    ):
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / script)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 64
        assert unified_owner in result.stderr
