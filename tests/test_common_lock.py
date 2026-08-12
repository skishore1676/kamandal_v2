from __future__ import annotations

import os
from pathlib import Path
import subprocess


COMMON = Path("scripts/common.sh").resolve()


def _run_lock(lock_root: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "KAMANDAL_RUNLOCK_ROOT": str(lock_root)}
    return subprocess.run(
        ["/bin/bash", "-c", 'source "$1"; with_lock demo true', "kamandal-lock-test", str(COMMON)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_lock_is_removed_after_success(tmp_path: Path) -> None:
    lock_root = tmp_path / "runlocks"

    completed = _run_lock(lock_root)

    assert completed.returncode == 0
    assert not (lock_root / "demo.lock").exists()


def test_live_owner_is_reported_as_already_running(tmp_path: Path) -> None:
    lock_root = tmp_path / "runlocks"
    lock = lock_root / "demo.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(
        f"pid={os.getpid()}\nscript=pytest\nstarted_at=2026-08-12T00:00:00Z\ntoken=active\n",
        encoding="utf-8",
    )

    completed = _run_lock(lock_root)

    assert completed.returncode == 75
    assert '"status":"already_running"' in completed.stdout
    assert lock.exists()


def test_dead_owner_lock_is_recovered_and_job_runs(tmp_path: Path) -> None:
    lock_root = tmp_path / "runlocks"
    lock = lock_root / "demo.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(
        "pid=99999999\nscript=dead-job\nstarted_at=2026-08-11T00:00:00Z\ntoken=dead\n",
        encoding="utf-8",
    )

    completed = _run_lock(lock_root)

    recovered = list((lock_root / "recovered").glob("demo.*.lock"))
    assert completed.returncode == 0
    assert '"status":"stale_lock_recovered"' in completed.stdout
    assert len(recovered) == 1
    assert not lock.exists()


def test_ownerless_lock_fails_closed(tmp_path: Path) -> None:
    lock_root = tmp_path / "runlocks"
    (lock_root / "demo.lock").mkdir(parents=True)

    completed = _run_lock(lock_root)

    assert completed.returncode == 76
    assert '"status":"unverifiable"' in completed.stdout


def test_reused_live_pid_with_wrong_command_fails_closed(tmp_path: Path) -> None:
    lock_root = tmp_path / "runlocks"
    lock = lock_root / "demo.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(
        f"pid={os.getpid()}\nscript=definitely-not-the-owner\nstarted_at=2026-08-12T00:00:00Z\ntoken=old\n",
        encoding="utf-8",
    )

    completed = _run_lock(lock_root)

    assert completed.returncode == 76
    assert '"status":"unverifiable"' in completed.stdout
    assert lock.exists()
