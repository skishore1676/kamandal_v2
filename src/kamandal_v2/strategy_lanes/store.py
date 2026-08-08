"""SQLite persistence boundary for the CSA shadow overlay."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from kamandal_v2.paths import resolve_path
from kamandal_v2.strategy_lanes.migrations import CSA_TABLES, csa_schema_ready
from kamandal_v2.strategy_lanes.models import AdmissionDecision, CsaAction, LifecycleState, ShadowFill, StrategyOpportunity, StrategyTicket


class CsaStore:
    """Access only explicitly migrated CSA tables; never auto-migrate runtime DBs."""

    def __init__(self, sqlite_path: str | Path = "data/kamandal_v2.db", *, read_only: bool = False) -> None:
        self.sqlite_path = resolve_path(sqlite_path)
        self.read_only = read_only
        if not csa_schema_ready(self.sqlite_path):
            raise RuntimeError("CSA schema is not ready; run the explicit CSA migration first")

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            conn = sqlite3.connect(f"file:{self.sqlite_path}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def schema_tables(self) -> tuple[str, ...]:
        with self._connect() as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return tuple(str(row["name"]) for row in rows if str(row["name"]) in CSA_TABLES)

    def save_scan_run(self, payload: dict[str, Any]) -> None:
        self._require_writable()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO csa_scan_runs
                (id, lane, status, policy_hash, started_at, completed_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload["id"]),
                    str(payload.get("lane") or "all"),
                    str(payload.get("status") or "completed"),
                    str(payload.get("policy_hash") or "multiple"),
                    str(payload["started_at"]),
                    payload.get("completed_at"),
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def save_run_receipt(self, payload: dict[str, Any]) -> None:
        self._require_writable()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO csa_run_receipts
                (id, command, status, started_at, completed_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload["id"]),
                    str(payload["command"]),
                    str(payload.get("status") or "completed"),
                    str(payload["started_at"]),
                    payload.get("completed_at"),
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def save_opportunity(self, opportunity: StrategyOpportunity, *, scan_run_id: str = "") -> None:
        self._require_writable()
        payload = opportunity.to_dict()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO csa_opportunities
                (id, scan_run_id, lane, source_mode, underlying, observed_at, policy_hash, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    opportunity.opportunity_id,
                    scan_run_id or None,
                    opportunity.lane.value,
                    opportunity.source_mode.value,
                    opportunity.underlying,
                    opportunity.observed_at,
                    opportunity.policy_hash,
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def save_admission_decision(self, decision: AdmissionDecision) -> None:
        self._require_writable()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO csa_admission_decisions
                (id, opportunity_id, admitted, primary_blocker, policy_hash, decided_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.opportunity_id,
                    int(decision.admitted),
                    decision.primary_blocker,
                    decision.policy_hash,
                    decision.decided_at,
                    json.dumps(decision.to_dict(), sort_keys=True),
                ),
            )

    def save_lifecycle(self, lifecycle: LifecycleState) -> None:
        self._require_writable()
        with self._connect() as conn:
            current = conn.execute("SELECT version FROM csa_lifecycles WHERE id = ?", (lifecycle.lifecycle_id,)).fetchone()
            if current and int(current["version"]) > lifecycle.version:
                raise ValueError("cannot overwrite a newer CSA lifecycle version")
            conn.execute(
                """
                INSERT OR REPLACE INTO csa_lifecycles
                (id, opportunity_id, lane, version, status, opened_at, updated_at, policy_hash, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lifecycle.lifecycle_id,
                    lifecycle.opportunity_id,
                    lifecycle.lane.value,
                    lifecycle.version,
                    lifecycle.status,
                    lifecycle.opened_at,
                    lifecycle.updated_at,
                    lifecycle.policy_hash,
                    json.dumps(lifecycle.to_dict(), sort_keys=True),
                ),
            )

    def lifecycle(self, lifecycle_id: str) -> LifecycleState | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM csa_lifecycles WHERE id = ?", (lifecycle_id,)).fetchone()
        if row is None:
            return None
        return _lifecycle_from_payload(json.loads(row["payload"]))

    def open_lifecycles(self) -> list[LifecycleState]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM csa_lifecycles WHERE status IN ('proposed', 'open') ORDER BY opened_at, id"
            ).fetchall()
        return [_lifecycle_from_payload(json.loads(row["payload"])) for row in rows]

    def save_action(self, action: CsaAction) -> None:
        self._require_writable()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO csa_actions
                (id, lifecycle_id, lifecycle_version, action_type, disposition, idempotency_key, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    action.lifecycle_id,
                    action.lifecycle_version,
                    action.action_type.value,
                    action.disposition.value,
                    action.idempotency_key,
                    action.proposed_at,
                    json.dumps(action.to_dict(), sort_keys=True),
                ),
            )

    def save_shadow_order_intent(self, ticket: StrategyTicket, *, status: str = "proposed") -> None:
        self._require_writable()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO csa_shadow_order_intents
                (id, action_id, lifecycle_id, lifecycle_version, status, idempotency_key, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket.ticket_id,
                    ticket.action_id,
                    ticket.lifecycle_id,
                    ticket.lifecycle_version,
                    status,
                    ticket.idempotency_key,
                    ticket.created_at,
                    json.dumps(ticket.to_dict(), sort_keys=True),
                ),
            )

    def save_shadow_fill(self, fill: ShadowFill) -> None:
        self._require_writable()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO csa_shadow_fills
                (id, ticket_id, lifecycle_id, status, filled_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.ticket_id,
                    fill.lifecycle_id,
                    fill.status,
                    fill.filled_at,
                    json.dumps(fill.to_dict(), sort_keys=True),
                ),
            )
            conn.execute(
                "UPDATE csa_shadow_order_intents SET status = ?, filled_at = ? WHERE id = ?",
                (fill.status, fill.filled_at if fill.status == "filled" else None, fill.ticket_id),
            )

    def rows(self, table: str) -> list[dict[str, Any]]:
        if table not in CSA_TABLES:
            raise ValueError(f"not a CSA table: {table}")
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        return [dict(row) for row in rows]

    def _require_writable(self) -> None:
        if self.read_only:
            raise RuntimeError("CSA store is read-only")


def _lifecycle_from_payload(payload: dict[str, Any]) -> LifecycleState:
    from kamandal_v2.strategy_lanes.models import LaneId

    return LifecycleState(
        lifecycle_id=str(payload["lifecycle_id"]),
        opportunity_id=str(payload["opportunity_id"]),
        lane=LaneId(str(payload["lane"])),
        version=int(payload["version"]),
        status=str(payload["status"]),
        active_legs=tuple(payload.get("active_legs") or ()),
        cashflow_ledger=tuple(payload.get("cashflow_ledger") or ()),
        opened_at=str(payload["opened_at"]),
        updated_at=str(payload["updated_at"]),
        policy_hash=str(payload["policy_hash"]),
        metadata=dict(payload.get("metadata") or {}),
    )
