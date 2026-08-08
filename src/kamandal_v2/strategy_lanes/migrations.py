"""Explicit, additive CSA SQLite migration with backup and integrity receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kamandal_v2.paths import resolve_path


CSA_MIGRATION_ID = "001_csa1_foundation"

CSA_TABLES = (
    "csa_schema_migrations",
    "csa_scan_runs",
    "csa_opportunities",
    "csa_admission_decisions",
    "csa_lifecycles",
    "csa_actions",
    "csa_adjustments",
    "csa_shadow_order_intents",
    "csa_shadow_fills",
    "csa_run_receipts",
)

_CSA_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS csa_schema_migrations (
    migration_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csa_scan_runs (
    id TEXT PRIMARY KEY,
    lane TEXT NOT NULL,
    status TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csa_opportunities (
    id TEXT PRIMARY KEY,
    scan_run_id TEXT,
    lane TEXT NOT NULL,
    source_mode TEXT NOT NULL,
    underlying TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csa_admission_decisions (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    admitted INTEGER NOT NULL,
    primary_blocker TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csa_lifecycles (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    lane TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csa_actions (
    id TEXT PRIMARY KEY,
    lifecycle_id TEXT NOT NULL,
    lifecycle_version INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    disposition TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csa_adjustments (
    id TEXT PRIMARY KEY,
    lifecycle_id TEXT NOT NULL,
    lifecycle_version INTEGER NOT NULL,
    action_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csa_shadow_order_intents (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    lifecycle_id TEXT NOT NULL,
    lifecycle_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    filled_at TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csa_shadow_fills (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    lifecycle_id TEXT NOT NULL,
    status TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csa_run_receipts (
    id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_csa_opportunity_run ON csa_opportunities(scan_run_id);
CREATE INDEX IF NOT EXISTS idx_csa_decision_opportunity ON csa_admission_decisions(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_csa_action_lifecycle ON csa_actions(lifecycle_id, lifecycle_version);
CREATE INDEX IF NOT EXISTS idx_csa_intent_lifecycle ON csa_shadow_order_intents(lifecycle_id, lifecycle_version);
"""


@dataclass(frozen=True, slots=True)
class MigrationReceipt:
    migration_id: str
    dry_run: bool
    database_path: str
    backup_path: str
    backup_sha256: str
    before_tables: tuple[str, ...]
    after_tables: tuple[str, ...]
    added_tables: tuple[str, ...]
    integrity_check: str
    applied_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def migrate_csa_database(
    sqlite_path: str | Path,
    *,
    dry_run: bool,
    backup_dir: str | Path | None = None,
) -> MigrationReceipt:
    database_path = resolve_path(sqlite_path)
    if not database_path.exists():
        raise FileNotFoundError(f"CSA migration requires an existing Kamandal database: {database_path}")

    before_tables = _table_names(database_path)
    applied_at = datetime.now(UTC).isoformat()
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="kamandal-csa-migration-") as temp_dir:
            working_path = Path(temp_dir) / database_path.name
            _sqlite_backup(database_path, working_path)
            backup_sha256 = _sha256(working_path)
            _apply_migration(working_path, applied_at=applied_at, dry_run=True)
            after_tables = _table_names(working_path)
            integrity = _integrity_check(working_path)
        backup_path = ""
    else:
        target_dir = resolve_path(backup_dir) if backup_dir else database_path.parent / "backups"
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = target_dir / f"{database_path.name}.pre-{CSA_MIGRATION_ID}.{stamp}.bak"
        _sqlite_backup(database_path, backup)
        backup_sha256 = _sha256(backup)
        backup_path = str(backup)
        _apply_migration(database_path, applied_at=applied_at, dry_run=False)
        after_tables = _table_names(database_path)
        integrity = _integrity_check(database_path)

    if integrity != "ok":
        raise RuntimeError(f"CSA migration integrity_check failed: {integrity}")
    missing = sorted(set(CSA_TABLES) - set(after_tables))
    if missing:
        raise RuntimeError(f"CSA migration missing tables: {', '.join(missing)}")
    lost = sorted(set(before_tables) - set(after_tables))
    if lost:
        raise RuntimeError(f"CSA migration removed baseline tables: {', '.join(lost)}")
    return MigrationReceipt(
        migration_id=CSA_MIGRATION_ID,
        dry_run=dry_run,
        database_path=str(database_path),
        backup_path=backup_path,
        backup_sha256=backup_sha256,
        before_tables=before_tables,
        after_tables=after_tables,
        added_tables=tuple(sorted(set(after_tables) - set(before_tables))),
        integrity_check=integrity,
        applied_at=applied_at,
    )


def csa_schema_ready(sqlite_path: str | Path) -> bool:
    database_path = resolve_path(sqlite_path)
    if not database_path.exists():
        return False
    return set(CSA_TABLES).issubset(_table_names(database_path))


def _apply_migration(path: Path, *, applied_at: str, dry_run: bool) -> None:
    payload = json.dumps({"migration_id": CSA_MIGRATION_ID, "dry_run": dry_run}, sort_keys=True)
    with sqlite3.connect(path) as conn:
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + _CSA_SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO csa_schema_migrations(migration_id, applied_at, payload) VALUES (?, ?, ?)",
                (CSA_MIGRATION_ID, applied_at, payload),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)


def _table_names(path: Path) -> tuple[str, ...]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return tuple(str(row[0]) for row in rows)


def _integrity_check(path: Path) -> str:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0] if row else "missing")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
