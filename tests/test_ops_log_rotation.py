from __future__ import annotations

import gzip
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kamandal_v2.ops.log_rotation import rotate_log_directories


def _write(path: Path, content: bytes, *, mtime: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        ts = mtime.timestamp()
        import os

        os.utime(path, (ts, ts))


def test_rotate_archives_stale_top_level_logs_without_deleting_content(tmp_path: Path) -> None:
    repo_root = tmp_path
    logs_dir = repo_root / "data" / "logs"
    old_log = logs_dir / "cron_market_shadow.log"
    stale_mtime = datetime.now(timezone.utc) - timedelta(days=40)
    _write(old_log, b"legacy cron output\n" * 1000, mtime=stale_mtime)

    now = datetime.now(timezone.utc)
    result = rotate_log_directories(repo_root=repo_root, max_age_days=14, now=now)

    assert str(old_log.relative_to(repo_root)) in result.archived
    # original path still exists (launchd's fd stays valid) but is truncated
    assert old_log.exists()
    assert old_log.stat().st_size == 0
    # content was preserved in the archive, compressed
    archive_files = list((logs_dir / "archive").glob("cron_market_shadow.log.*.gz"))
    assert len(archive_files) == 1
    with gzip.open(archive_files[0], "rb") as fh:
        assert fh.read() == b"legacy cron output\n" * 1000


def test_rotate_skips_recent_top_level_logs(tmp_path: Path) -> None:
    repo_root = tmp_path
    recent_log = repo_root / "data" / "logs" / "cron_recent.log"
    _write(recent_log, b"still fresh\n", mtime=datetime.now(timezone.utc) - timedelta(days=1))

    result = rotate_log_directories(repo_root=repo_root, max_age_days=14)

    assert str(recent_log.relative_to(repo_root)) in result.skipped
    assert recent_log.stat().st_size == len(b"still fresh\n")


def test_rotate_caps_oversized_live_launchd_logs_regardless_of_age(tmp_path: Path) -> None:
    repo_root = tmp_path
    live_log = repo_root / "data" / "logs" / "launchd" / "com.kamandal.v2.live_management.out.log"
    _write(live_log, b"x" * 200, mtime=datetime.now(timezone.utc))  # fresh, but over the tiny cap below

    result = rotate_log_directories(repo_root=repo_root, max_age_days=14, max_bytes_per_file=100)

    assert str(live_log.relative_to(repo_root)) in result.archived
    assert live_log.stat().st_size == 0


def test_rotate_leaves_small_live_launchd_logs_alone(tmp_path: Path) -> None:
    repo_root = tmp_path
    live_log = repo_root / "data" / "logs" / "launchd" / "com.kamandal.v2.iv.out.log"
    _write(live_log, b"small\n", mtime=datetime.now(timezone.utc))

    result = rotate_log_directories(repo_root=repo_root, max_age_days=14, max_bytes_per_file=100)

    assert str(live_log.relative_to(repo_root)) in result.skipped
    assert live_log.read_bytes() == b"small\n"


def test_rotate_on_missing_logs_dir_is_a_noop(tmp_path: Path) -> None:
    result = rotate_log_directories(repo_root=tmp_path)

    assert result.archived == []
    assert result.skipped == []
