"""Read-only inventory for the protected unified-engine cutover."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.strategy_engine.lifecycle import adopt_legacy_position
from kamandal_v2.strategy_lanes.models import LaneId
from kamandal_v2.strategy_lanes.migrations import csa_schema_ready, migrate_csa_database
from kamandal_v2.strategy_lanes.store import CsaStore


@dataclass(frozen=True, slots=True)
class CutoverDecision:
    subject: str
    decision: str
    reason: str
    lifecycle_id: str = ""


@dataclass(frozen=True, slots=True)
class CutoverManifest:
    decisions: tuple[CutoverDecision, ...]

    @property
    def ready(self) -> bool:
        return not any(item.decision == "block" for item in self.decisions)

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "decisions": [asdict(item) for item in self.decisions]}


@dataclass(frozen=True, slots=True)
class FixtureApplyReceipt:
    database_path: str
    backup_path: str
    backup_sha256: str
    created_lifecycle_ids: tuple[str, ...]
    integrity_check: str


@dataclass(frozen=True, slots=True)
class CopiedCutoverReceipt:
    """A production-shaped rehearsal which can mutate only a newly made copy."""

    source_database_path: str
    work_database_path: str
    source_sha256: str
    manifest: CutoverManifest
    apply_receipt: FixtureApplyReceipt | None
    verify_integrity_check: str
    rollback_verified: bool


_UNIFIED_PLAYBOOK_COLUMNS = (
    "mode",
    "management_delta_target",
    "management_delta_max",
    "tested_side_confirmations",
    "rearm_inside_confirmations",
    "filled_side_adjustment_limit",
    "dte_action",
    "dte_action_threshold",
    "duration_roll_limit",
    "inversion_enabled",
    "event_timing",
    "event_near_expiry_after_days",
    "paired_order_required",
    "post_event_exit",
)


@dataclass(frozen=True, slots=True)
class SheetCellMapping:
    sheet_row: int
    playbook_id: str
    column: str
    old_value: Any
    new_value: Any


@dataclass(frozen=True, slots=True)
class SheetRowAddition:
    start_row: int
    end_row: int
    values: dict[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class SheetMappingManifest:
    source_header: tuple[str, ...]
    target_header: tuple[str, ...]
    header_additions: tuple[str, ...]
    cell_mappings: tuple[SheetCellMapping, ...]
    row_additions: tuple[SheetRowAddition, ...]
    validation_preservation: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "source_header": list(self.source_header),
            "target_header": list(self.target_header),
            "header_additions": list(self.header_additions),
            "cell_mappings": [asdict(item) for item in self.cell_mappings],
            "row_additions": [asdict(item) for item in self.row_additions],
            "validation_preservation": list(self.validation_preservation),
            "blockers": list(self.blockers),
        }


def build_cutover_manifest(store: LocalStore) -> CutoverManifest:
    """Inventory baseline groups without mutating the database or scheduler."""
    decisions: list[CutoverDecision] = []
    for group in sorted(store.open_live_position_groups(), key=lambda item: str(item.get("group_id") or "")):
        group_id = str(group.get("group_id") or "")
        try:
            lifecycle = adopt_legacy_position(_adoption_payload(group), lifecycle_id=f"adopt:{group_id}", adopted_at=str(group.get("opened_at") or ""))
        except (ValueError, KeyError, TypeError) as exc:
            decisions.append(CutoverDecision(group_id, "block", str(exc)))
        else:
            decisions.append(CutoverDecision(group_id, "create", "legacy position maps exactly", lifecycle.lifecycle_id))
    return CutoverManifest(tuple(decisions))


def unified_schedule_manifest() -> dict[str, tuple[str, ...]]:
    """Describe, but never render or load, the target single-owner topology."""
    retain = (
        "x-bookmarks", "youtube", "my-ideas", "live-reconciliation",
        "live-approved-orders", "live-health-report", "scheduled-job-health",
        "daily-report", "earnings", "iv", "iv-afternoon", "weekly-reviewer",
    )
    retire = (
        "universe-proposer", "live-advisory", "live-management", "csa-policy-snapshot",
        "csa-shadow-scan", "csa-live-scan", "csa-shadow-management",
        "csa-live-management", "csa-shadow-scorecard",
    )
    add = ("unified-planning", "unified-lifecycle-management")
    return {"retain": retain, "retire": retire, "add": add}


def build_sheet_mapping_manifest(
    header: list[str] | tuple[str, ...],
    rows: list[dict[str, Any]],
    *,
    earnings_calendar_row: dict[str, Any] | None = None,
) -> SheetMappingManifest:
    """Describe the exact Phase 9 Sheet edit without touching a Sheet client.

    Existing column order is preserved and unified fields are appended.  The
    returned ranges are deliberately bounded to the supplied snapshot; a
    protected Phase 9 runner must reread and compare this manifest before any
    external write.
    """
    source_header = tuple(str(item) for item in header if str(item))
    missing_identity = {"playbook_id", "enabled", "strategy_family", "structure"} - set(source_header)
    if missing_identity:
        raise ValueError(f"playbook mapping header is missing: {', '.join(sorted(missing_identity))}")
    additions = tuple(column for column in _UNIFIED_PLAYBOOK_COLUMNS if column not in source_header)
    target_header = source_header + additions
    changes: list[SheetCellMapping] = []
    blockers: list[str] = []
    for index, raw_row in enumerate(rows, start=2):
        row = dict(raw_row)
        playbook_id = str(row.get("playbook_id") or "").strip()
        if not playbook_id:
            blockers.append(f"row {index}: playbook_id is required")
            continue
        mode = _legacy_mode(row.get("mode"), row.get("csa_stage"))
        if mode is None:
            blockers.append(f"{playbook_id}: unsupported csa_stage={row.get('csa_stage')!r}")
            continue
        _append_cell_change(changes, index, playbook_id, "mode", row.get("mode", ""), mode)
        structure = str(row.get("structure") or "").strip().lower()
        family = str(row.get("strategy_family") or "").strip().lower()
        if structure in {"short_strangle", "strangle"} or family == "short_strangle":
            _append_strangle_mapping(changes, index, playbook_id, row)
        if structure in {"call_calendar", "put_calendar"} and family != "earnings_calendar":
            _remove_unused_generic_calendar_event_expiration(changes, blockers, index, playbook_id, row)

    additions_rows: list[SheetRowAddition] = []
    if earnings_calendar_row is None:
        blockers.append("earnings_calendar: reviewed direction and approved row values are required before Phase 9")
    else:
        proposed = _validated_earnings_calendar_row(earnings_calendar_row, target_header)
        additions_rows.append(
            SheetRowAddition(
                start_row=len(rows) + 2,
                end_row=len(rows) + 2,
                values=proposed,
                reason="separate event-aware earnings calendar; exact Phase 9 live/enabled target, rendered locally only",
            )
        )
    return SheetMappingManifest(
        source_header=source_header,
        target_header=target_header,
        header_additions=additions,
        cell_mappings=tuple(changes),
        row_additions=tuple(additions_rows),
        validation_preservation=(
            "copy existing playbooks header order, formatting, formulas, and validations unchanged",
            "extend existing column validation only through the bounded final addition row",
            "apply only listed cell ranges after exact pre-write readback; do not clear or replace the tab",
            "read back every listed cell and the appended earnings-calendar row after write",
        ),
        blockers=tuple(blockers),
    )


def _legacy_mode(explicit_mode: Any, legacy_stage: Any) -> str | None:
    explicit = str(explicit_mode or "").strip().lower()
    if explicit in {"live", "shadow"}:
        return explicit
    if explicit:
        return None
    stage = str(legacy_stage or "baseline").strip().lower() or "baseline"
    if stage == "shadow":
        return "shadow"
    if stage in {"baseline", "pilot_live", "live"}:
        return "live"
    return None


def _append_cell_change(
    changes: list[SheetCellMapping], sheet_row: int, playbook_id: str, column: str, old_value: Any, new_value: Any
) -> None:
    if old_value != new_value:
        changes.append(SheetCellMapping(sheet_row, playbook_id, column, old_value, new_value))


def _append_strangle_mapping(
    changes: list[SheetCellMapping], sheet_row: int, playbook_id: str, row: dict[str, Any]
) -> None:
    # These are the reviewed initial controls.  Existing JSON fields remain
    # historical evidence; the explicit columns become the compiled policy.
    for column, value in (
        ("management_delta_target", 0.30),
        ("management_delta_max", 0.40),
        ("tested_side_confirmations", 2),
        ("rearm_inside_confirmations", 2),
        ("filled_side_adjustment_limit", 2),
        ("dte_action", "close"),
        ("dte_action_threshold", 21),
        ("duration_roll_limit", 0),
        ("inversion_enabled", "FALSE"),
    ):
        _append_cell_change(changes, sheet_row, playbook_id, column, row.get(column, ""), value)


def _remove_unused_generic_calendar_event_expiration(
    changes: list[SheetCellMapping], blockers: list[str], sheet_row: int, playbook_id: str, row: dict[str, Any]
) -> None:
    raw = row.get("management_policy_json")
    if raw in (None, ""):
        return
    try:
        policy = json.loads(str(raw))
    except json.JSONDecodeError:
        blockers.append(f"{playbook_id}: management_policy_json is not valid JSON")
        return
    if not isinstance(policy, dict) or "event_expiration" not in policy:
        return
    policy = dict(policy)
    policy.pop("event_expiration")
    _append_cell_change(
        changes,
        sheet_row,
        playbook_id,
        "management_policy_json",
        raw,
        json.dumps(policy, sort_keys=True, separators=(",", ":")),
    )


def _validated_earnings_calendar_row(values: dict[str, Any], header: tuple[str, ...]) -> dict[str, Any]:
    row = {column: "" for column in header}
    row.update(values)
    required = {
        "playbook_id", "strategy_family", "structure", "applicable_direction", "dte_min", "dte_max",
        "long_dte_min", "long_dte_max", "event_timing", "event_near_expiry_after_days",
        "paired_order_required", "post_event_exit",
    }
    missing = sorted(field for field in required if row.get(field) in (None, ""))
    if missing:
        raise ValueError(f"earnings calendar mapping missing: {', '.join(missing)}")
    if str(row["strategy_family"]).strip().lower() != "earnings_calendar":
        raise ValueError("earnings calendar mapping must use strategy_family=earnings_calendar")
    if str(row["structure"]).strip().lower() not in {"call_calendar", "put_calendar"}:
        raise ValueError("earnings calendar mapping must use a calendar structure")
    if str(row["mode"] or "").strip().lower() != "live":
        raise ValueError("earnings calendar mapping must target mode=live")
    if str(row["enabled"] or "").strip().lower() not in {"true", "1", "yes", "on"}:
        raise ValueError("earnings calendar mapping must target enabled=TRUE")
    # This manifest is intentionally effect-free.  The protected Phase 9
    # transaction boundary, not a substituted disabled/shadow row, keeps the
    # reviewed target from reaching the operator Sheet before authorization.
    row["enabled"] = "TRUE"
    row["mode"] = "live"
    return row


def apply_cutover_fixture(
    sqlite_path: str | Path,
    *,
    backup_dir: str | Path,
    allow_fixture_apply: bool = False,
) -> FixtureApplyReceipt:
    """Apply adoption only to an explicit fixture database, with a restore point.

    This helper is deliberately unsuitable for production invocation: callers
    must provide a concrete local test database and its backup directory.  The
    Phase 9 runner will have a separate protected operation, not this helper.
    """
    if not allow_fixture_apply:
        raise PermissionError("cutover fixture apply requires allow_fixture_apply=True; Phase 9 has a separate protected operation")
    database = Path(sqlite_path).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"fixture database does not exist: {database}")
    store = LocalStore(database)
    manifest = build_cutover_manifest(store)
    if not manifest.ready:
        blockers = "; ".join(f"{item.subject}: {item.reason}" for item in manifest.decisions if item.decision == "block")
        raise ValueError(f"cutover fixture blocked before mutation: {blockers}")
    backup_root = Path(backup_dir).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / f"{database.name}.pre-unified-cutover.bak"
    if not backup.exists():
        shutil.copy2(database, backup)
    if not csa_schema_ready(database):
        migrate_csa_database(database, dry_run=False, backup_dir=backup_root)
    csa_store = CsaStore(database)
    created: list[str] = []
    groups = {str(group.get("group_id") or ""): group for group in store.open_live_position_groups()}
    for decision in manifest.decisions:
        if decision.decision != "create":
            continue
        lifecycle = adopt_legacy_position(
            _adoption_payload(groups[decision.subject]),
            lifecycle_id=decision.lifecycle_id,
            adopted_at=str(groups[decision.subject].get("opened_at") or ""),
        )
        csa_store.save_lifecycle(lifecycle)
        created.append(lifecycle.lifecycle_id)
    integrity = _integrity_check(database)
    if integrity != "ok":
        raise RuntimeError(f"cutover fixture integrity failed: {integrity}")
    return FixtureApplyReceipt(str(database), str(backup), _sha256(backup), tuple(created), integrity)


def restore_cutover_fixture(receipt: FixtureApplyReceipt) -> None:
    database = Path(receipt.database_path)
    backup = Path(receipt.backup_path)
    if _sha256(backup) != receipt.backup_sha256:
        raise ValueError("cutover fixture backup checksum mismatch")
    shutil.copy2(backup, database)
    integrity = _integrity_check(database)
    if integrity != "ok":
        raise RuntimeError(f"restored fixture integrity failed: {integrity}")


def rehearse_cutover_on_copy(
    source_database: str | Path,
    *,
    work_database: str | Path,
    backup_dir: str | Path,
    apply: bool,
    verify_rollback: bool = True,
) -> CopiedCutoverReceipt:
    """Inventory and optionally migrate a fresh copy, never the source DB.

    Phase 9 can supply an authorized runtime source path to this same runner,
    but the runner refuses an in-place target and an already-existing work
    path.  That makes the local rehearsal and the eventual protected command
    share migration logic without giving a source audit write authority.
    """
    source = Path(source_database).resolve()
    work = Path(work_database).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"cutover source database does not exist: {source}")
    if source == work:
        raise ValueError("cutover work database must be a distinct copy, never the source database")
    if work.exists():
        raise FileExistsError(f"cutover work database already exists: {work}")
    work.parent.mkdir(parents=True, exist_ok=True)
    source_sha = _sha256(source)
    manifest = build_cutover_manifest(LocalStore(source))
    if not manifest.ready:
        return CopiedCutoverReceipt(str(source), str(work), source_sha, manifest, None, _integrity_check(source), False)
    if not apply:
        return CopiedCutoverReceipt(str(source), str(work), source_sha, manifest, None, _integrity_check(source), False)
    shutil.copy2(source, work)
    receipt = apply_cutover_fixture(work, backup_dir=backup_dir, allow_fixture_apply=True)
    verified = _integrity_check(work)
    rollback_verified = False
    if verify_rollback:
        restore_cutover_fixture(receipt)
        rollback_verified = _sha256(work) == source_sha and _integrity_check(work) == "ok"
        if not rollback_verified:
            raise RuntimeError("cutover copy rollback did not restore exact source bytes")
    return CopiedCutoverReceipt(str(source), str(work), source_sha, manifest, receipt, verified, rollback_verified)


def _adoption_payload(group: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(group.get("candidate") or {})
    structure = str(candidate.get("structure") or group.get("structure") or "").lower()
    family = str(candidate.get("strategy_family") or group.get("strategy_family") or "").lower()
    lanes = {
        "short_strangle": LaneId.SHORT_STRANGLE,
        "strangle": LaneId.SHORT_STRANGLE,
        "call_spread": LaneId.CALL_VERTICAL,
        "call_diagonal": LaneId.DIRECTIONAL_DIAGONAL,
        "put_diagonal": LaneId.DIRECTIONAL_DIAGONAL,
        "short_put": LaneId.GENERIC_CLOSE_ONLY,
        "long_call": LaneId.GENERIC_CLOSE_ONLY,
        "long_put": LaneId.GENERIC_CLOSE_ONLY,
        "put_spread": LaneId.GENERIC_CLOSE_ONLY,
        "iron_condor": LaneId.GENERIC_CLOSE_ONLY,
        "jade_lizard": LaneId.GENERIC_CLOSE_ONLY,
        "call_calendar": LaneId.GENERIC_CLOSE_ONLY,
        "put_calendar": LaneId.GENERIC_CLOSE_ONLY,
    }
    if family == "earnings_calendar" and structure in {"call_calendar", "put_calendar"}:
        lane = LaneId.EARNINGS_CALENDAR
    else:
        lane = lanes.get(structure)
    if lane is None:
        raise ValueError(f"legacy adoption blocked: unsupported structure {structure or '<missing>'}")
    return {
        "group_id": group.get("group_id"),
        "opportunity_id": str(candidate.get("idea_id") or group.get("group_id") or ""),
        "lane": lane.value,
        "active_legs": candidate.get("legs") or (),
        "cashflow_ledger": group.get("cashflow_ledger") or (),
        "policy_hash": str(group.get("policy_hash") or "policy-at-adoption"),
        "compiled_management_policy": group.get("compiled_management_policy") or group.get("policy_at_adoption"),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _integrity_check(path: Path) -> str:
    import sqlite3

    with sqlite3.connect(path) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
