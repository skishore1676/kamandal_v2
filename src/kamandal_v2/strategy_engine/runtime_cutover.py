"""Protected, reversible Phase 9 cutover primitives.

These functions are intentionally unusable without explicit expected hashes
and an apply flag.  They never submit broker orders or trigger trading jobs.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from kamandal_v2.config import load_control
from kamandal_v2.sheets import GoogleSheetClient, pull_sheet_tables
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.cutover import (
    CutoverManifest,
    SheetMappingManifest,
    build_cutover_manifest,
    build_sheet_mapping_manifest,
    materialize_sheet_mapping,
    planned_lifecycle_adoptions,
)
from kamandal_v2.strategy_engine.policy import compile_playbook_policies
from kamandal_v2.strategy_lanes.migrations import csa_schema_ready, migrate_csa_database
from kamandal_v2.strategy_lanes.store import CsaStore


NONTERMINAL_INTENT_STATUSES = {
    "pending_approval",
    "pending_close_approval",
    "approved_close_pending_submit",
    "stage_approved_pending_submit",
    "submitted",
    "repriced",
    "partially_filled",
    "replace_cancel_pending",
    "replace_waiting_cancel",
}


@dataclass(frozen=True, slots=True)
class RuntimeCutoverInspection:
    database_sha256: str
    database_integrity: str
    database_manifest: CutoverManifest
    open_group_count: int
    nonterminal_intent_count: int
    sheet_snapshot_hash: str
    sheet_manifest: SheetMappingManifest
    target_policy_count: int
    target_policy_errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return (
            self.database_integrity == "ok"
            and self.database_manifest.ready
            and self.nonterminal_intent_count == 0
            and self.sheet_manifest.ready
            and not self.target_policy_errors
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "database_sha256": self.database_sha256,
            "database_integrity": self.database_integrity,
            "database_manifest": self.database_manifest.to_dict(),
            "open_group_count": self.open_group_count,
            "nonterminal_intent_count": self.nonterminal_intent_count,
            "sheet_snapshot_hash": self.sheet_snapshot_hash,
            "sheet_manifest": self.sheet_manifest.to_dict(),
            "target_policy_count": self.target_policy_count,
            "target_policy_errors": list(self.target_policy_errors),
        }


@dataclass(frozen=True, slots=True)
class RuntimeCutoverReceipt:
    database_path: str
    database_backup_path: str
    database_backup_sha256: str
    database_integrity_before: str
    database_integrity_after: str
    adopted_lifecycle_ids: tuple[str, ...]
    sheet_backup_path: str
    sheet_snapshot_hash_before: str
    sheet_snapshot_hash_after: str
    sheet_updated_cells: int
    sheet_added_rows: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_runtime_cutover(
    sqlite_path: str | Path,
    *,
    config: dict[str, Any] | None = None,
) -> RuntimeCutoverInspection:
    active_config = config or load_control()
    tables = pull_sheet_tables(active_config)
    client = GoogleSheetClient.from_config(active_config)
    title = _playbooks_title(active_config)
    values = client.read_tab_values(title)
    header, rows = _matrix_to_rows(values)
    if rows != tables.get("playbooks"):
        raise ValueError("playbooks Sheet changed between exact matrix and table reads")
    earnings_row = default_earnings_calendar_row(rows)
    sheet_manifest = build_sheet_mapping_manifest(header, rows, earnings_calendar_row=earnings_row)
    _target_header, target_rows = materialize_sheet_mapping(sheet_manifest, rows)
    target_compilation = compile_playbook_policies(target_rows)
    store = LocalStore(sqlite_path, read_only=True)
    database_manifest = build_cutover_manifest(store, playbook_rows=target_rows)
    nonterminal = store.live_order_intents_by_status(NONTERMINAL_INTENT_STATUSES)
    return RuntimeCutoverInspection(
        database_sha256=file_sha256(Path(sqlite_path)),
        database_integrity=_integrity_check(Path(sqlite_path), read_only=True),
        database_manifest=database_manifest,
        open_group_count=len(store.open_live_position_groups()),
        nonterminal_intent_count=len(nonterminal),
        sheet_snapshot_hash=sheet_values_hash(values),
        sheet_manifest=sheet_manifest,
        target_policy_count=len(target_compilation.policies),
        target_policy_errors=target_compilation.errors,
    )


def apply_runtime_cutover(
    sqlite_path: str | Path,
    *,
    backup_dir: str | Path,
    expected_database_sha256: str,
    expected_sheet_snapshot_hash: str,
    allow_apply: bool,
    config: dict[str, Any] | None = None,
) -> RuntimeCutoverReceipt:
    if not allow_apply:
        raise PermissionError("runtime cutover requires allow_apply=True after protected authorization")
    active_config = config or load_control()
    inspection = inspect_runtime_cutover(sqlite_path, config=active_config)
    if not inspection.ready:
        raise ValueError("runtime cutover inspection is blocked")
    if inspection.database_sha256 != expected_database_sha256:
        raise ValueError("runtime database changed after review")
    if inspection.sheet_snapshot_hash != expected_sheet_snapshot_hash:
        raise ValueError("operator Sheet changed after review")

    backup_root = Path(backup_dir).resolve()
    backup_root.mkdir(parents=True, exist_ok=False)
    database = Path(sqlite_path).resolve()
    database_backup = backup_root / "kamandal_v2.pre-unified-cutover.db"
    sheet_backup = backup_root / "playbooks.pre-unified-cutover.json"

    tables = pull_sheet_tables(active_config)
    client = GoogleSheetClient.from_config(active_config)
    title = _playbooks_title(active_config)
    sheet_values = client.read_tab_values(title)
    sheet_rows, sheet_cols = client.tab_dimensions(title)
    header, current_rows = _matrix_to_rows(sheet_values)
    earnings_row = default_earnings_calendar_row(current_rows)
    sheet_manifest = build_sheet_mapping_manifest(header, current_rows, earnings_calendar_row=earnings_row)
    target_header, target_rows = materialize_sheet_mapping(sheet_manifest, current_rows)
    if current_rows != tables.get("playbooks"):
        raise ValueError("playbooks Sheet changed before cutover apply")

    _sqlite_backup(database, database_backup)
    sheet_backup.write_text(
        json.dumps(
            {
                "title": title,
                "values": sheet_values,
                "row_count": sheet_rows,
                "col_count": sheet_cols,
                "snapshot_hash": expected_sheet_snapshot_hash,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    adopted_ids: tuple[str, ...] = ()
    try:
        if not csa_schema_ready(database):
            migrate_csa_database(database, dry_run=False, backup_dir=backup_root / "schema")
        store = LocalStore(database)
        lifecycles = planned_lifecycle_adoptions(store, playbook_rows=target_rows)
        csa_store = CsaStore(database)
        for lifecycle in lifecycles:
            csa_store.save_lifecycle(lifecycle)
        adopted_ids = tuple(lifecycle.lifecycle_id for lifecycle in lifecycles)
        _verify_adoptions(store, csa_store, adopted_ids)
        integrity_after = _integrity_check(database)
        if integrity_after != "ok":
            raise RuntimeError(f"post-cutover database integrity failed: {integrity_after}")
        sheet_hash_after = _apply_sheet_manifest(
            client,
            title=title,
            source_header=header,
            source_rows=current_rows,
            target_header=target_header,
            target_rows=target_rows,
            manifest=sheet_manifest,
        )
    except Exception:
        _restore_sqlite_backup(database_backup, database)
        _restore_sheet_manifest(
            client,
            title=title,
            source_header=header,
            source_rows=current_rows,
            manifest=sheet_manifest,
            original_row_count=sheet_rows,
            original_col_count=sheet_cols,
        )
        raise

    return RuntimeCutoverReceipt(
        database_path=str(database),
        database_backup_path=str(database_backup),
        database_backup_sha256=file_sha256(database_backup),
        database_integrity_before=inspection.database_integrity,
        database_integrity_after=integrity_after,
        adopted_lifecycle_ids=adopted_ids,
        sheet_backup_path=str(sheet_backup),
        sheet_snapshot_hash_before=expected_sheet_snapshot_hash,
        sheet_snapshot_hash_after=sheet_hash_after,
        sheet_updated_cells=len(sheet_manifest.header_additions) + len(sheet_manifest.cell_mappings),
        sheet_added_rows=len(sheet_manifest.row_additions),
    )


def default_earnings_calendar_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    existing = [
        dict(row) for row in rows
        if str(row.get("strategy_family") or "").strip().lower() == "earnings_calendar"
    ]
    if len(existing) > 1:
        raise ValueError("multiple earnings_calendar rows require explicit operator review")
    if existing:
        return existing[0]
    template = next(
        (dict(row) for row in rows if str(row.get("playbook_id") or "") == "call_calendar_low_iv"),
        None,
    )
    if template is None:
        raise ValueError("call_calendar_low_iv template is unavailable")
    template.update(
        {
            "playbook_id": "earnings_calendar_directional",
            "enabled": "TRUE",
            "csa_stage": "live",
            "mode": "live",
            "strategy_family": "earnings_calendar",
            # The capability replaces this nominal call shape with a put
            # calendar for bearish ideas before candidate validation.
            "structure": "call_calendar",
            "source_mode": "idea",
            "variant": "directional_event",
            "applicable_direction": "bullish,bearish",
            "applicable_thesis_tags": "",
            "applicable_horizon_min": "1",
            "applicable_horizon_max": "14",
            "iv_percentile_min": "0",
            "iv_percentile_max": "100",
            "earnings_blackout_days": "0",
            "dte_min": "5",
            "dte_max": "7",
            "long_dte_min": "45",
            "long_dte_max": "60",
            "short_delta_min": "0.45",
            "short_delta_max": "0.55",
            "long_delta_min": "0.45",
            "long_delta_max": "0.55",
            "profit_target_pct": "25",
            "max_loss_multiple": "1",
            "exit_dte_min": "0",
            "half_time_exit": "FALSE",
            "avoid_earnings": "FALSE",
            "exit_pre_event_days": "",
            "max_contracts": "1",
            "priority": "6",
            "rationale": "Direction-aware paired earnings calendar: calls bullish, puts bearish; enter before and close after the confirmed event.",
            "notes": "Live event strategy. Both legs enter and exit as one complex package.",
            "management_policy_json": json.dumps(
                {
                    "lifecycle": {
                        "close_only": True,
                        "fill": {"max_attempts": 4, "price_increment": 0.05},
                    }
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "event_timing": "confirmed_bmo_or_amc_final_pre_event_session",
            "event_near_expiry_after_days": "1",
            "paired_order_required": "TRUE",
            "post_event_exit": "first_eligible_post_event_session",
        }
    )
    return template


def sheet_values_hash(values: list[list[Any]]) -> str:
    canonical = json.dumps(values, sort_keys=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _apply_sheet_manifest(
    client: GoogleSheetClient,
    *,
    title: str,
    source_header: tuple[str, ...],
    source_rows: list[dict[str, Any]],
    target_header: tuple[str, ...],
    target_rows: list[dict[str, Any]],
    manifest: SheetMappingManifest,
) -> str:
    physical_rows, physical_cols = client.tab_dimensions(title)
    client.resize_tab(
        title,
        rows=max(physical_rows, len(target_rows) + 1),
        cols=max(physical_cols, len(target_header)),
    )
    updates: list[dict[str, Any]] = []
    if manifest.header_additions:
        start = len(source_header) + 1
        updates.append(
            {
                "range": f"{_column_letter(start)}1:{_column_letter(len(target_header))}1",
                "values": [list(manifest.header_additions)],
            }
        )
    for change in manifest.cell_mappings:
        column = target_header.index(change.column) + 1
        updates.append(
            {
                "range": f"{_column_letter(column)}{change.sheet_row}",
                "values": [[change.new_value]],
            }
        )
    for addition in manifest.row_additions:
        values = [addition.values.get(column, "") for column in target_header]
        updates.append(
            {
                "range": f"A{addition.start_row}:{_column_letter(len(target_header))}{addition.end_row}",
                "values": [values],
            }
        )
    client.batch_update_tab(title, updates)
    readback = client.read_tab_values(title)
    observed_header, observed_rows = _matrix_to_rows(readback)
    if observed_header != target_header:
        raise RuntimeError("playbooks Sheet header readback mismatch")
    if _normalized_rows(observed_rows, target_header) != _normalized_rows(target_rows, target_header):
        raise RuntimeError("playbooks Sheet value readback mismatch")
    return sheet_values_hash(readback)


def _restore_sheet_manifest(
    client: GoogleSheetClient,
    *,
    title: str,
    source_header: tuple[str, ...],
    source_rows: list[dict[str, Any]],
    manifest: SheetMappingManifest,
    original_row_count: int,
    original_col_count: int,
) -> None:
    """Undo only cells this cutover owns, preserving unrelated formulas."""
    updates: list[dict[str, Any]] = []
    for change in manifest.cell_mappings:
        if change.column in source_header:
            column = source_header.index(change.column) + 1
            updates.append(
                {
                    "range": f"{_column_letter(column)}{change.sheet_row}",
                    "values": [[change.old_value]],
                }
            )
    client.batch_update_tab(title, updates)
    clears: list[str] = []
    for addition in manifest.row_additions:
        clears.append(
            f"A{addition.start_row}:{_column_letter(len(manifest.target_header))}{addition.end_row}"
        )
    if manifest.header_additions and original_col_count >= len(source_header):
        clears.append(
            f"{_column_letter(len(source_header) + 1)}1:"
            f"{_column_letter(len(manifest.target_header))}{len(source_rows) + 1}"
        )
    client.batch_clear_tab(title, clears)
    client.resize_tab(title, rows=original_row_count, cols=original_col_count)
    observed = client.read_tab_values(title)
    observed_header, observed_rows = _matrix_to_rows(observed)
    if observed_header != source_header:
        raise RuntimeError("playbooks Sheet rollback header mismatch")
    if _normalized_rows(observed_rows, source_header) != _normalized_rows(source_rows, source_header):
        raise RuntimeError("playbooks Sheet rollback value mismatch")


def _verify_adoptions(store: LocalStore, csa_store: CsaStore, lifecycle_ids: tuple[str, ...]) -> None:
    groups = store.open_live_position_groups()
    expected = {f"adopt:{group.get('group_id')}" for group in groups}
    if set(lifecycle_ids) != expected:
        raise RuntimeError("cutover lifecycle ownership set does not match open groups")
    for lifecycle_id in lifecycle_ids:
        lifecycle = csa_store.lifecycle(lifecycle_id)
        if lifecycle is None or lifecycle.status != "open":
            raise RuntimeError(f"cutover lifecycle missing or not open: {lifecycle_id}")
        if str(lifecycle.metadata.get("execution_mode") or "") != "live":
            raise RuntimeError(f"cutover lifecycle is not live-owned: {lifecycle_id}")
        if not lifecycle.active_legs or not lifecycle.metadata.get("underlying"):
            raise RuntimeError(f"cutover lifecycle ownership metadata is incomplete: {lifecycle_id}")
        if not lifecycle.cashflow_ledger:
            raise RuntimeError(f"cutover lifecycle entry economics are missing: {lifecycle_id}")


def _matrix_to_rows(values: list[list[Any]]) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    if not values:
        raise ValueError("playbooks Sheet is empty")
    header = tuple(str(cell).strip() for cell in values[0] if str(cell).strip())
    rows: list[dict[str, Any]] = []
    for raw in values[1:]:
        padded = list(raw) + [""] * (len(header) - len(raw))
        if not any(str(value).strip() for value in padded[: len(header)]):
            continue
        rows.append({column: str(padded[index]).strip() for index, column in enumerate(header)})
    return header, rows


def _normalized_rows(rows: list[dict[str, Any]], header: tuple[str, ...]) -> list[list[str]]:
    return [[_normalized_cell(row.get(column, "")) for column in header] for row in rows]


def _normalized_cell(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value if value is not None else "").strip()


def _integrity_check(path: Path, *, read_only: bool = False) -> str:
    uri = f"file:{path}?mode=ro" if read_only else str(path)
    with sqlite3.connect(uri, uri=read_only) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def _sqlite_backup(source: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError(f"cutover database backup already exists: {target}")
    with sqlite3.connect(source) as source_connection, sqlite3.connect(target) as target_connection:
        source_connection.backup(target_connection)
    if _integrity_check(target, read_only=True) != "ok":
        raise RuntimeError("cutover database backup integrity failed")


def _restore_sqlite_backup(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    with sqlite3.connect(source) as source_connection, sqlite3.connect(target) as target_connection:
        source_connection.backup(target_connection)


def _playbooks_title(config: dict[str, Any]) -> str:
    return str(((config.get("google_sheets") or {}).get("tabs") or {}).get("playbooks") or "playbooks")


def _column_letter(index: int) -> str:
    if index < 1:
        raise ValueError("column index must be positive")
    result = ""
    current = index
    while current:
        current, remainder = divmod(current - 1, 26)
        result = chr(65 + remainder) + result
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or apply the protected unified runtime cutover.")
    parser.add_argument("action", choices=("inspect", "apply"))
    parser.add_argument("--database", default="data/kamandal_v2.db")
    parser.add_argument("--backup-dir")
    parser.add_argument("--expected-database-sha256")
    parser.add_argument("--expected-sheet-snapshot-hash")
    parser.add_argument("--allow-apply", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "inspect":
        result = inspect_runtime_cutover(args.database)
    else:
        required = {
            "--backup-dir": args.backup_dir,
            "--expected-database-sha256": args.expected_database_sha256,
            "--expected-sheet-snapshot-hash": args.expected_sheet_snapshot_hash,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            parser.error("apply requires " + ", ".join(missing))
        result = apply_runtime_cutover(
            args.database,
            backup_dir=args.backup_dir,
            expected_database_sha256=args.expected_database_sha256,
            expected_sheet_snapshot_hash=args.expected_sheet_snapshot_hash,
            allow_apply=args.allow_apply,
        )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
