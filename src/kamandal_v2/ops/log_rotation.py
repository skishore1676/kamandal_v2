"""Log rotation for Kamandal's launchd job output logs.

data/logs/ has two generations of log files:

- `data/logs/launchd/<label>.{out,err}.log` — the current location every
  active launchd job writes to (StandardOutPath/StandardErrorPath).
- `data/logs/*.log` at the top level (`com.kamandal.v2.*.log`,
  `cron_*.log`) — a retired cron-era location. No currently-registered
  launchd job writes there any more (verified against `launchctl list` and
  the plists under ~/Library/LaunchAgents); they are pure historical debris.

Neither generation had any rotation, so data/logs/ grew to 123MB unrotated
(mostly the retired top-level files, which never shrink on their own).

This module implements a conservative, additive rotation: it never deletes a
byte of current log content in its first pass over a file. Instead it moves
(or gzips) old/oversized logs into an archive/ subdirectory and leaves an
empty file behind for the writer (launchd re-opens StandardOutPath lazily on
next write, so truncating in place is also safe once a copy is archived).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import gzip
import os
from pathlib import Path
import shutil


DEFAULT_MAX_AGE_DAYS = 14
DEFAULT_MAX_BYTES_PER_FILE = 10 * 1024 * 1024  # 10 MiB


@dataclass
class RotationResult:
    archived: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    bytes_before: int = 0
    bytes_after: int = 0

    def to_dict(self) -> dict:
        return {
            "archived": self.archived,
            "skipped": self.skipped,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
        }


def rotate_log_directories(
    *,
    repo_root: Path,
    max_age_days: int | None = None,
    max_bytes_per_file: int | None = None,
    now: datetime | None = None,
) -> RotationResult:
    """Archive stale/oversized log files under data/logs/.

    Rotation candidates are:
      1. Any `*.log` file directly under `data/logs/` (the retired
         cron-era location) whose mtime is older than max_age_days.
      2. Any `*.log` file under `data/logs/launchd/` (the live location)
         that has grown past max_bytes_per_file.

    A rotated file is gzip-compressed into `data/logs/archive/` with a
    timestamp suffix, then truncated to empty in place (the original path
    keeps existing so launchd's already-open StandardOutPath/StandardErrorPath
    handle keeps working — launchd does not need to be restarted). Nothing is
    ever deleted; archives accumulate under data/logs/archive/.
    """
    max_age_days = DEFAULT_MAX_AGE_DAYS if max_age_days is None else max_age_days
    max_bytes_per_file = DEFAULT_MAX_BYTES_PER_FILE if max_bytes_per_file is None else max_bytes_per_file
    now = now or datetime.now(timezone.utc)
    logs_dir = repo_root / "data" / "logs"
    archive_dir = logs_dir / "archive"
    result = RotationResult()
    if not logs_dir.is_dir():
        return result

    cutoff = now - timedelta(days=max_age_days)

    # Legacy top-level *.log files: rotate anything past max_age_days.
    for path in sorted(logs_dir.glob("*.log")):
        if not path.is_file():
            continue
        result.bytes_before += path.stat().st_size
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            _archive_and_truncate(path, archive_dir=archive_dir, now=now)
            result.archived.append(str(path.relative_to(repo_root)))
        else:
            result.skipped.append(str(path.relative_to(repo_root)))

    # Live launchd/*.log files: rotate only if they've grown past the cap,
    # regardless of age, so an active job never gets unrotated forever.
    launchd_dir = logs_dir / "launchd"
    if launchd_dir.is_dir():
        for path in sorted(launchd_dir.glob("*.log")):
            if not path.is_file():
                continue
            size = path.stat().st_size
            result.bytes_before += size
            if size > max_bytes_per_file:
                _archive_and_truncate(path, archive_dir=archive_dir, now=now)
                result.archived.append(str(path.relative_to(repo_root)))
            else:
                result.skipped.append(str(path.relative_to(repo_root)))

    for rel in result.archived:
        result.bytes_after += 0  # rotated files are truncated to 0 bytes
    for rel in result.skipped:
        result.bytes_after += (repo_root / rel).stat().st_size

    return result


def _archive_and_truncate(path: Path, *, archive_dir: Path, now: datetime) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    dest = archive_dir / f"{path.name}.{stamp}.gz"
    # Avoid clobbering an existing archive with the same second-resolution stamp.
    counter = 0
    while dest.exists():
        counter += 1
        dest = archive_dir / f"{path.name}.{stamp}-{counter}.gz"
    with path.open("rb") as src, gzip.open(dest, "wb") as dst:
        shutil.copyfileobj(src, dst)
    # Truncate in place rather than unlink+recreate: launchd's open file
    # descriptor for StandardOutPath/StandardErrorPath keeps writing at its
    # current offset if we replace the inode, which would silently discard
    # future writes into a file no longer referenced by any directory entry.
    # Truncating the existing inode preserves the descriptor's validity.
    os.truncate(path, 0)
