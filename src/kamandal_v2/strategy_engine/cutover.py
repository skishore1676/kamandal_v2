"""Read-only inventory for the protected unified-engine cutover."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
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
    add = ("unified-planning", "unified-lifecycle-management", "unified-lifecycle-history")
    return {"retain": retain, "retire": retire, "add": add}


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


def _adoption_payload(group: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(group.get("candidate") or {})
    structure = str(candidate.get("structure") or group.get("structure") or "").lower()
    lanes = {
        "short_strangle": LaneId.SHORT_STRANGLE,
        "strangle": LaneId.SHORT_STRANGLE,
        "call_spread": LaneId.CALL_VERTICAL,
        "call_diagonal": LaneId.DIRECTIONAL_DIAGONAL,
        "put_diagonal": LaneId.DIRECTIONAL_DIAGONAL,
        "call_calendar": LaneId.EARNINGS_CALENDAR,
        "put_calendar": LaneId.EARNINGS_CALENDAR,
    }
    if structure not in lanes:
        raise ValueError(f"legacy adoption blocked: unsupported structure {structure or '<missing>'}")
    return {
        "group_id": group.get("group_id"),
        "opportunity_id": str(candidate.get("idea_id") or group.get("group_id") or ""),
        "lane": lanes[structure].value,
        "active_legs": candidate.get("legs") or (),
        "cashflow_ledger": group.get("cashflow_ledger") or (),
        "policy_hash": str(group.get("policy_hash") or "policy-at-adoption"),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _integrity_check(path: Path) -> str:
    import sqlite3

    with sqlite3.connect(path) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
