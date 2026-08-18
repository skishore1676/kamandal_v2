"""SQLite-backed local store."""

from __future__ import annotations

import json
import sqlite3
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import Candidate, ChainSnapshot, Greeks, Idea, Plan, PortfolioState, PreflightResult
from kamandal_v2.paths import resolve_path


class LocalStore:
    def __init__(
        self,
        sqlite_path: str | Path = "data/kamandal_v2.db",
        *,
        read_only: bool = False,
    ) -> None:
        self.sqlite_path = resolve_path(sqlite_path)
        self.read_only = read_only
        if not read_only:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            conn = sqlite3.connect(f"file:{self.sqlite_path}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ideas (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chain_snapshots (
                    id TEXT PRIMARY KEY,
                    underlying TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_snapshots (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY,
                    plan_run_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    plan_run_id TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preflight_results (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovery_observations (
                    identity TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    source_profile TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    exclusion_reason TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovery_candidates (
                    symbol TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    mention_count INTEGER NOT NULL,
                    source_profiles_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    exclusion_reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS universe_review_commits (
                    id TEXT PRIMARY KEY,
                    committed_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_fills (
                    id TEXT PRIMARY KEY,
                    plan_run_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    idea_id TEXT,
                    underlying TEXT NOT NULL,
                    playbook_id TEXT,
                    structure TEXT NOT NULL,
                    net_credit REAL,
                    estimated_bpr REAL,
                    delta REAL,
                    gamma REAL,
                    theta REAL,
                    vega REAL,
                    status TEXT NOT NULL DEFAULT 'open',
                    opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    closed_at TEXT,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_marks (
                    id TEXT PRIMARY KEY,
                    marked_at TEXT NOT NULL,
                    position_count INTEGER NOT NULL,
                    mid_pnl REAL NOT NULL,
                    natural_pnl REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_order_intents (
                    ticket_hash TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    idea_id TEXT,
                    intent_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_order_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ticket_hash TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    submit INTEGER NOT NULL,
                    ok INTEGER NOT NULL,
                    request_payload TEXT NOT NULL,
                    response_payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_order_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ticket_hash TEXT,
                    order_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_positions (
                    id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    order_id TEXT,
                    plan_id TEXT,
                    candidate_id TEXT,
                    idea_id TEXT,
                    underlying TEXT NOT NULL,
                    playbook_id TEXT,
                    structure TEXT NOT NULL,
                    status TEXT NOT NULL,
                    opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    closed_at TEXT,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_position_groups (
                    group_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    closed_at TEXT,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_management_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    group_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_position_marks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    group_id TEXT NOT NULL,
                    underlying TEXT,
                    entry_kind TEXT,
                    pnl REAL NOT NULL,
                    target_profit REAL NOT NULL,
                    target_progress_pct REAL NOT NULL,
                    quote_fresh INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_approval_requests (
                    request_id TEXT PRIMARY KEY,
                    ticket_hash TEXT NOT NULL,
                    plan_id TEXT,
                    candidate_id TEXT,
                    idea_id TEXT,
                    underlying TEXT,
                    structure TEXT,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operator_review_requests (
                    request_id TEXT PRIMARY KEY,
                    request_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_reconciliation_issues (
                    issue_id TEXT PRIMARY KEY,
                    issue_type TEXT NOT NULL,
                    group_id TEXT,
                    underlying TEXT,
                    status TEXT NOT NULL,
                    observed_count INTEGER NOT NULL DEFAULT 1,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT,
                    payload TEXT NOT NULL
                );
                """
            )
            self._ensure_shadow_fill_columns(conn)
            self._backfill_shadow_fill_columns(conn)

    def record_discovery_evidence(
        self,
        *,
        symbol: str,
        source_profile: str,
        source_record_id: str,
        exclusion_reason: str,
        evidence_ref: str,
        observed_at: str | None = None,
    ) -> bool:
        """Persist one replay-safe, non-tradable discovery observation.

        The identity deliberately excludes free-form source text and credentials.
        Replaying the same source-profile/record/symbol does not increase the
        candidate count; a new source record updates last-seen and diversity.
        """
        normalized_symbol = str(symbol or "").strip().upper()
        profile = str(source_profile or "").strip()
        record_id = str(source_record_id or "").strip()
        reason = str(exclusion_reason or "outside_enabled_universe").strip()
        reference = str(evidence_ref or "").strip()
        if not normalized_symbol or not profile or not record_id:
            raise ValueError("discovery evidence requires symbol, source_profile, and source_record_id")
        seen_at = observed_at or datetime.now(UTC).isoformat()
        identity = hashlib.sha256(f"{profile}|{record_id}|{normalized_symbol}".encode("utf-8")).hexdigest()
        with self._connect() as conn:
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO discovery_observations
                (identity, symbol, source_profile, source_record_id, observed_at, exclusion_reason, evidence_ref)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (identity, normalized_symbol, profile, record_id, seen_at, reason, reference),
            ).rowcount > 0
            if not inserted:
                return False
            existing = conn.execute(
                "SELECT first_seen_at, mention_count, source_profiles_json, evidence_refs_json FROM discovery_candidates WHERE symbol=?",
                (normalized_symbol,),
            ).fetchone()
            profiles = {profile}
            references = {reference} if reference else set()
            first_seen = seen_at
            mentions = 1
            if existing:
                first_seen = str(existing["first_seen_at"])
                mentions = int(existing["mention_count"]) + 1
                profiles.update(json.loads(existing["source_profiles_json"]))
                references.update(json.loads(existing["evidence_refs_json"]))
            conn.execute(
                """
                INSERT INTO discovery_candidates
                (symbol, first_seen_at, last_seen_at, mention_count, source_profiles_json, evidence_refs_json, exclusion_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    mention_count=excluded.mention_count,
                    source_profiles_json=excluded.source_profiles_json,
                    evidence_refs_json=excluded.evidence_refs_json,
                    exclusion_reason=excluded.exclusion_reason
                """,
                (normalized_symbol, first_seen, seen_at, mentions, json.dumps(sorted(profiles)), json.dumps(sorted(references)), reason),
            )
        return True

    def discovery_candidates(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, first_seen_at, last_seen_at, mention_count, source_profiles_json, evidence_refs_json, exclusion_reason FROM discovery_candidates ORDER BY mention_count DESC, symbol"
            ).fetchall()
        return [
            {
                **dict(row),
                "source_profiles": json.loads(row["source_profiles_json"]),
                "evidence_refs": json.loads(row["evidence_refs_json"]),
            }
            for row in rows
        ]

    def record_universe_review_commit(self, *, review_id: str, committed_at: str, payload: dict[str, Any] | None = None) -> None:
        """Record the committed review boundary used by discovery ranking.

        A proposal run is not a review: this record is deliberately written only
        by the eventual operator-approved review workflow.  Keeping the boundary
        separate prevents an automatic daily proposer from erasing the weekly
        evidence interval it is meant to summarize.
        """
        if not str(review_id).strip() or not str(committed_at).strip():
            raise ValueError("universe review commit requires review_id and committed_at")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO universe_review_commits (id, committed_at, payload) VALUES (?, ?, ?)",
                (str(review_id), str(committed_at), json.dumps(payload or {}, sort_keys=True)),
            )

    def latest_universe_review_commit_at(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT committed_at FROM universe_review_commits ORDER BY committed_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return str(row["committed_at"]) if row is not None else None

    def _ensure_shadow_fill_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(shadow_fills)").fetchall()}
        columns = {
            "idea_id": "TEXT",
            "playbook_id": "TEXT",
            "net_credit": "REAL",
            "estimated_bpr": "REAL",
            "delta": "REAL",
            "gamma": "REAL",
            "theta": "REAL",
            "vega": "REAL",
            "close_reason": "TEXT",
            "close_pnl": "REAL",
            "close_payload": "TEXT",
        }
        for name, column_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE shadow_fills ADD COLUMN {name} {column_type}")

    def _backfill_shadow_fill_columns(self, conn: sqlite3.Connection) -> None:
        candidate_rows = conn.execute("SELECT id, payload FROM candidates").fetchall()
        candidate_ideas = {
            str(row["id"]): str((json.loads(row["payload"]) or {}).get("idea_id") or "")
            for row in candidate_rows
        }
        rows = conn.execute("SELECT id, candidate_id, payload FROM shadow_fills").fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            greeks = payload.get("greeks") or {}
            idea_id = str(payload.get("idea_id") or candidate_ideas.get(str(row["candidate_id"]), "") or "")
            conn.execute(
                """
                UPDATE shadow_fills
                SET idea_id = COALESCE(NULLIF(idea_id, ''), ?),
                    playbook_id = COALESCE(NULLIF(playbook_id, ''), ?),
                    net_credit = COALESCE(net_credit, ?),
                    estimated_bpr = COALESCE(estimated_bpr, ?),
                    delta = COALESCE(delta, ?),
                    gamma = COALESCE(gamma, ?),
                    theta = COALESCE(theta, ?),
                    vega = COALESCE(vega, ?)
                WHERE id = ?
                """,
                (
                    idea_id,
                    payload.get("playbook_id"),
                    payload.get("net_credit"),
                    payload.get("estimated_bpr"),
                    greeks.get("delta"),
                    greeks.get("gamma"),
                    greeks.get("theta"),
                    greeks.get("vega"),
                    row["id"],
                ),
            )

    def save_ideas(self, ideas: list[Idea]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO ideas VALUES (?, ?, ?)",
                [(idea.idea_id, idea.operator_status, json.dumps(idea.to_dict(), sort_keys=True)) for idea in ideas],
            )

    def save_chain_snapshot(self, snapshot: ChainSnapshot) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO chain_snapshots VALUES (?, ?, ?)",
                (snapshot.chain_snapshot_id, snapshot.underlying, json.dumps(snapshot.to_dict(), sort_keys=True)),
            )

    def save_account_snapshot(
        self,
        snapshot_id: str,
        portfolio: PortfolioState,
        *,
        mode: str = "live",
    ) -> None:
        book_mode = _account_book_mode(mode)
        payload = portfolio.to_dict()
        payload["_book_mode"] = book_mode
        payload["_source_snapshot_id"] = snapshot_id
        storage_id = f"{book_mode}:{snapshot_id}"
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO account_snapshots VALUES (?, ?)",
                (storage_id, json.dumps(payload, sort_keys=True)),
            )

    def latest_account_snapshot(self, *, mode: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, payload FROM account_snapshots ORDER BY rowid DESC",
            ).fetchall()
        wanted = _account_book_mode(mode) if mode is not None else None
        for row in rows:
            payload = json.loads(row["payload"])
            if wanted is not None and payload.get("_book_mode") != wanted:
                continue
            payload["_snapshot_id"] = row["id"]
            return payload
        return None

    def recent_account_snapshots(
        self,
        *,
        limit: int = 1000,
        mode: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, payload FROM account_snapshots ORDER BY rowid DESC",
            ).fetchall()
        wanted = _account_book_mode(mode) if mode is not None else None
        snapshots = []
        for row in rows:
            payload = json.loads(row["payload"])
            if wanted is not None and payload.get("_book_mode") != wanted:
                continue
            payload["_snapshot_id"] = row["id"]
            snapshots.append(payload)
            if len(snapshots) >= int(limit):
                break
        return snapshots

    def save_candidates(self, plan_run_id: str, candidates: list[Candidate]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO candidates VALUES (?, ?, ?)",
                [(candidate.candidate_id, plan_run_id, json.dumps(candidate.to_dict(), sort_keys=True)) for candidate in candidates],
            )

    def save_plans(self, plan_run_id: str, plans: list[Plan]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO plans VALUES (?, ?, ?, ?)",
                [(plan.plan_id, plan_run_id, plan.plan_rank, json.dumps(plan.to_dict(), sort_keys=True)) for plan in plans],
            )

    def save_preflight_result(self, result_id: str, result: PreflightResult) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO preflight_results VALUES (?, ?)",
                (result_id, json.dumps(result.to_dict(), sort_keys=True)),
            )

    def save_shadow_fills(self, plan_run_id: str, plan: Plan) -> None:
        rows = []
        for candidate in plan.candidates:
            fill_id = f"{plan_run_id}:{candidate.candidate_id}"
            payload = {
                "fill_id": fill_id,
                "plan_run_id": plan_run_id,
                "plan_id": plan.plan_id,
                "candidate_id": candidate.candidate_id,
                "idea_id": candidate.idea_id,
                "underlying": candidate.underlying,
                "playbook_id": candidate.playbook_id,
                "structure": candidate.structure,
                "net_credit": candidate.net_credit,
                "estimated_bpr": candidate.estimated_bpr,
                "greeks": candidate.greeks.to_dict(),
                "legs": [leg.to_dict() for leg in candidate.legs],
            }
            rows.append((
                fill_id,
                plan_run_id,
                plan.plan_id,
                candidate.candidate_id,
                candidate.idea_id,
                candidate.underlying,
                candidate.playbook_id,
                candidate.structure,
                candidate.net_credit,
                candidate.estimated_bpr,
                candidate.greeks.delta,
                candidate.greeks.gamma,
                candidate.greeks.theta,
                candidate.greeks.vega,
                "open",
                json.dumps(payload, sort_keys=True),
            ))
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO shadow_fills
                (id, plan_run_id, plan_id, candidate_id, idea_id, underlying, playbook_id, structure,
                 net_credit, estimated_bpr, delta, gamma, theta, vega, status, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def open_shadow_candidate_ids(self) -> set[str]:
        with self._connect() as conn:
            typed = _typed_shadow_lifecycles(conn, statuses={"proposed", "open"})
            if typed is not None:
                return {
                    str((item.get("metadata") or {}).get("candidate_id") or "")
                    for item in typed
                    if str((item.get("metadata") or {}).get("candidate_id") or "")
                }
            rows = conn.execute("SELECT candidate_id FROM shadow_fills WHERE status = 'open'").fetchall()
        return {str(row["candidate_id"]) for row in rows}

    def open_shadow_idea_ids(self) -> set[str]:
        with self._connect() as conn:
            typed = _typed_shadow_lifecycles(conn, statuses={"proposed", "open"})
            if typed is not None:
                return {
                    str(((item.get("metadata") or {}).get("source_identity") or {}).get("idea_id") or "")
                    for item in typed
                    if str(((item.get("metadata") or {}).get("source_identity") or {}).get("idea_id") or "")
                }
            rows = conn.execute("SELECT idea_id FROM shadow_fills WHERE status = 'open' AND idea_id IS NOT NULL AND idea_id != ''").fetchall()
            if rows:
                return {str(row["idea_id"]) for row in rows}
            rows = conn.execute("SELECT payload FROM shadow_fills WHERE status = 'open'").fetchall()
            candidate_rows = conn.execute("SELECT id, payload FROM candidates").fetchall()
        candidate_ideas = {
            str(row["id"]): str((json.loads(row["payload"]) or {}).get("idea_id") or "")
            for row in candidate_rows
        }
        idea_ids: set[str] = set()
        for row in rows:
            payload = json.loads(row["payload"])
            idea_id = str(payload.get("idea_id") or "")
            if not idea_id:
                idea_id = candidate_ideas.get(str(payload.get("candidate_id") or ""), "")
            if idea_id:
                idea_ids.add(idea_id)
        return idea_ids

    def shadow_idea_ids_opened_since(self, opened_since: str) -> set[str]:
        with self._connect() as conn:
            typed = _typed_shadow_lifecycles(conn, opened_since=opened_since)
            if typed is not None:
                return {
                    str(((item.get("metadata") or {}).get("source_identity") or {}).get("idea_id") or "")
                    for item in typed
                    if str(((item.get("metadata") or {}).get("source_identity") or {}).get("idea_id") or "")
                }
            rows = conn.execute(
                """
                SELECT idea_id, candidate_id, payload
                FROM shadow_fills
                WHERE opened_at >= ?
                """,
                (opened_since,),
            ).fetchall()
            candidate_rows = conn.execute("SELECT id, payload FROM candidates").fetchall()
        candidate_ideas = {
            str(row["id"]): str((json.loads(row["payload"]) or {}).get("idea_id") or "")
            for row in candidate_rows
        }
        idea_ids: set[str] = set()
        for row in rows:
            payload = json.loads(row["payload"])
            idea_id = str(row["idea_id"] or payload.get("idea_id") or "")
            if not idea_id:
                idea_id = candidate_ideas.get(str(row["candidate_id"] or payload.get("candidate_id") or ""), "")
            if idea_id:
                idea_ids.add(idea_id)
        return idea_ids

    def shadow_portfolio_state(self, base: PortfolioState) -> PortfolioState:
        with self._connect() as conn:
            typed = _typed_shadow_lifecycles(conn, statuses={"open"})
            if typed is None:
                rows = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT underlying, estimated_bpr, delta, gamma, theta, vega
                        FROM shadow_fills
                        WHERE status = 'open'
                        """
                    ).fetchall()
                ]
            else:
                candidate_payloads = {
                    str(row["id"]): json.loads(row["payload"])
                    for row in conn.execute("SELECT id, payload FROM candidates").fetchall()
                }
                rows = [_typed_shadow_portfolio_row(item, candidate_payloads) for item in typed]
        bpr_used = round(sum(float(row.get("estimated_bpr") or 0.0) for row in rows), 2)
        greeks = Greeks()
        per_underlying_bpr: dict[str, float] = {}
        for row in rows:
            greeks = greeks + Greeks(
                delta=float(row.get("delta") or 0.0),
                gamma=float(row.get("gamma") or 0.0),
                theta=float(row.get("theta") or 0.0),
                vega=float(row.get("vega") or 0.0),
            )
            underlying = str(row.get("underlying") or "")
            if underlying:
                per_underlying_bpr[underlying] = round(
                    per_underlying_bpr.get(underlying, 0.0) + float(row.get("estimated_bpr") or 0.0),
                    2,
                )
        return PortfolioState(
            account_size=base.account_size,
            buying_power=round(max(base.buying_power - bpr_used, 0.0), 2),
            bpr_used=bpr_used,
            positions_count=len(rows),
            greeks=greeks,
            per_underlying_bpr=per_underlying_bpr,
        )

    def live_portfolio_state(self, base: PortfolioState) -> PortfolioState:
        groups = self.open_live_position_groups()
        if not groups:
            return base
        per_underlying_bpr: dict[str, float] = {}
        greeks = Greeks()
        for group in groups:
            candidate = group.get("candidate") or {}
            underlying = str(group.get("underlying") or candidate.get("underlying") or "")
            bpr = _live_group_bpr(group)
            if underlying:
                per_underlying_bpr[underlying] = round(per_underlying_bpr.get(underlying, 0.0) + bpr, 2)
            greeks = greeks + _candidate_greeks(candidate)
        return PortfolioState(
            account_size=base.account_size,
            buying_power=base.buying_power,
            bpr_used=base.bpr_used,
            positions_count=len(groups),
            greeks=greeks if any((greeks.delta, greeks.gamma, greeks.theta, greeks.vega)) else base.greeks,
            per_underlying_bpr=per_underlying_bpr,
        )

    def save_shadow_mark(self, mark_id: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO shadow_marks
                (id, marked_at, position_count, mid_pnl, natural_pnl, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    mark_id,
                    str(payload.get("marked_at") or ""),
                    int(payload.get("position_count") or 0),
                    float(payload.get("total_mid_pnl") or 0.0),
                    float(payload.get("total_natural_pnl") or 0.0),
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def close_shadow_fill(self, fill_id: str, *, reason: str, pnl: float, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE shadow_fills
                SET status = 'closed',
                    closed_at = CURRENT_TIMESTAMP,
                    close_reason = ?,
                    close_pnl = ?,
                    close_payload = ?
                WHERE id = ? AND status = 'open'
                """,
                (reason, float(pnl), json.dumps(payload, sort_keys=True), fill_id),
            )

    def save_live_order_intent(self, ticket: dict[str, Any], *, status: str = "pending_approval") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO live_order_intents
                (ticket_hash, order_id, plan_id, candidate_id, idea_id, intent_type, status, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(NULLIF(?, ''), CURRENT_TIMESTAMP), CURRENT_TIMESTAMP, ?)
                """,
                (
                    str(ticket["ticket_hash"]),
                    str(ticket["order_id"]),
                    str(ticket["plan_id"]),
                    str(ticket["candidate_id"]),
                    str(ticket.get("idea_id") or ""),
                    str(ticket["intent_type"]),
                    status,
                    str(ticket.get("created_at") or ""),
                    json.dumps(ticket, sort_keys=True),
                ),
            )

    def live_order_intent(self, ticket_hash: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, payload FROM live_order_intents WHERE ticket_hash = ?",
                (ticket_hash,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["_ledger_status"] = row["status"]
        return payload

    def live_order_intents_by_status(self, statuses: set[str]) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT status, payload FROM live_order_intents WHERE status IN ({placeholders})",
                tuple(sorted(statuses)),
            ).fetchall()
        tickets = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["_ledger_status"] = row["status"]
            tickets.append(payload)
        return tickets

    def live_order_intents_by_type(self, intent_type: str, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        query = "SELECT status, created_at, updated_at, payload FROM live_order_intents WHERE intent_type = ?"
        params: list[Any] = [intent_type]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(sorted(statuses))
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        tickets = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["_ledger_status"] = row["status"]
            payload["_ledger_created_at"] = row["created_at"]
            payload["_ledger_updated_at"] = row["updated_at"]
            tickets.append(payload)
        return tickets

    def live_order_attempts_for_ticket_hashes(self, ticket_hashes: set[str]) -> list[dict[str, Any]]:
        hashes = {ticket_hash for ticket_hash in ticket_hashes if ticket_hash}
        if not hashes:
            return []
        placeholders = ",".join("?" for _ in hashes)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT created_at, ticket_hash, order_id, action, submit, ok, request_payload, response_payload
                FROM live_order_attempts
                WHERE ticket_hash IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                """,
                tuple(sorted(hashes)),
            ).fetchall()
        attempts = []
        for row in rows:
            attempts.append({
                "created_at": row["created_at"],
                "ticket_hash": row["ticket_hash"],
                "order_id": row["order_id"],
                "action": row["action"],
                "submit": bool(row["submit"]),
                "ok": bool(row["ok"]),
                "request_payload": json.loads(row["request_payload"]),
                "response_payload": json.loads(row["response_payload"]),
            })
        return attempts

    def live_order_child_intents(self, parent_ticket_hash: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, payload FROM live_order_intents").fetchall()
        children = []
        for row in rows:
            payload = json.loads(row["payload"])
            if str(payload.get("parent_ticket_hash") or "") != parent_ticket_hash:
                continue
            payload["_ledger_status"] = row["status"]
            children.append(payload)
        return children

    def live_close_order_intents_for_group(self, group_id: str, *, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        if not group_id:
            return []
        query = "SELECT status, created_at, updated_at, payload FROM live_order_intents WHERE intent_type = 'close'"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(sorted(statuses))
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        tickets = []
        for row in rows:
            payload = json.loads(row["payload"])
            if str(payload.get("group_id") or "") != group_id:
                continue
            payload["_ledger_status"] = row["status"]
            payload["_ledger_created_at"] = row["created_at"]
            payload["_ledger_updated_at"] = row["updated_at"]
            tickets.append(payload)
        tickets.sort(key=lambda item: (str(item.get("_ledger_updated_at") or ""), str(item.get("created_at") or "")), reverse=True)
        return tickets

    def live_entry_plan_ids_since(self, created_since: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT plan_id FROM live_order_intents
                WHERE intent_type = 'open'
                  AND created_at >= ?
                  AND status IN ('submitted', 'repriced', 'filled', 'manual_fill_recorded')
                """,
                (created_since,),
            ).fetchall()
        return {str(row["plan_id"]) for row in rows if row["plan_id"]}

    def update_live_order_intent_status(self, ticket_hash: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE live_order_intents SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE ticket_hash = ?",
                (status, ticket_hash),
            )

    def transition_live_order_intent_status(
        self,
        ticket_hash: str,
        *,
        expected_statuses: set[str],
        status: str,
    ) -> bool:
        """Atomically claim a ledger transition and report whether this caller won."""

        if not expected_statuses:
            return False
        placeholders = ",".join("?" for _ in expected_statuses)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE live_order_intents
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE ticket_hash = ? AND status IN ({placeholders})
                """,
                (status, ticket_hash, *sorted(expected_statuses)),
            )
        return cursor.rowcount == 1

    def update_live_order_intent_status_with_payload(
        self,
        ticket_hash: str,
        status: str,
        payload_updates: dict[str, Any] | None = None,
    ) -> None:
        existing = self.live_order_intent(ticket_hash)
        if not existing:
            return
        payload = {key: value for key, value in existing.items() if not key.startswith("_ledger_")}
        if payload_updates:
            payload.update(payload_updates)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE live_order_intents
                SET status = ?, updated_at = CURRENT_TIMESTAMP, payload = ?
                WHERE ticket_hash = ?
                """,
                (status, json.dumps(payload, sort_keys=True), ticket_hash),
            )

    def record_live_order_attempt(
        self,
        ticket: dict[str, Any],
        *,
        action: str,
        submit: bool,
        ok: bool,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_order_attempts
                (ticket_hash, order_id, action, submit, ok, request_payload, response_payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(ticket["ticket_hash"]),
                    str(ticket["order_id"]),
                    action,
                    int(submit),
                    int(ok),
                    json.dumps(request_payload, sort_keys=True),
                    json.dumps(response_payload, sort_keys=True),
                ),
            )

    def record_live_order_status(self, order_id: str, status: str, payload: dict[str, Any], *, ticket_hash: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_order_status (ticket_hash, order_id, status, payload)
                VALUES (?, ?, ?, ?)
                """,
                (ticket_hash, order_id, status, json.dumps(payload, sort_keys=True)),
            )

    def live_order_status_history(self, order_ids: set[str]) -> list[dict[str, Any]]:
        """Return persisted broker observations for a bounded set of orders."""

        normalized = sorted(str(order_id) for order_id in order_ids if str(order_id))
        if not normalized:
            return []
        placeholders = ", ".join("?" for _ in normalized)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, created_at, ticket_hash, order_id, status, payload
                FROM live_order_status
                WHERE order_id IN ({placeholders})
                ORDER BY id ASC
                """,
                normalized,
            ).fetchall()
        history = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload.update(
                {
                    "_status_id": int(row["id"]),
                    "_observed_at": row["created_at"],
                    "_ticket_hash": row["ticket_hash"],
                    "_order_id": row["order_id"],
                    "_broker_status": row["status"],
                }
            )
            history.append(payload)
        return history

    def save_live_position_group(self, group_id: str, payload: dict[str, Any], *, status: str = "open") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_position_groups
                (group_id, status, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    closed_at = CASE WHEN excluded.status = 'open' THEN NULL ELSE live_position_groups.closed_at END
                """,
                (group_id, status, json.dumps(payload, sort_keys=True)),
            )

    def save_live_position(self, position_id: str, group_id: str, payload: dict[str, Any], *, status: str = "open") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_positions
                (id, group_id, order_id, plan_id, candidate_id, idea_id, underlying, playbook_id, structure, status, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    group_id = excluded.group_id,
                    order_id = excluded.order_id,
                    plan_id = excluded.plan_id,
                    candidate_id = excluded.candidate_id,
                    idea_id = excluded.idea_id,
                    underlying = excluded.underlying,
                    playbook_id = excluded.playbook_id,
                    structure = excluded.structure,
                    status = excluded.status,
                    payload = excluded.payload,
                    closed_at = CASE WHEN excluded.status = 'open' THEN NULL ELSE live_positions.closed_at END
                """,
                (
                    position_id,
                    group_id,
                    payload.get("order_id"),
                    payload.get("plan_id"),
                    payload.get("candidate_id"),
                    payload.get("idea_id"),
                    payload.get("underlying"),
                    payload.get("playbook_id"),
                    payload.get("structure"),
                    status,
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def open_live_idea_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT idea_id FROM live_positions WHERE status = 'open' AND idea_id IS NOT NULL AND idea_id != ''"
            ).fetchall()
        return {str(row["idea_id"]) for row in rows}

    def live_idea_ids_opened_since(self, opened_since: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT idea_id FROM live_positions
                WHERE opened_at >= ? AND idea_id IS NOT NULL AND idea_id != ''
                """,
                (opened_since,),
            ).fetchall()
        return {str(row["idea_id"]) for row in rows}

    def latest_live_position_mark(self, group_id: str) -> dict[str, Any] | None:
        if not group_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM live_position_marks WHERE group_id = ? ORDER BY id DESC LIMIT 1",
                (group_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload"])

    def latest_canonical_live_lifecycle_mark(self, group_id: str) -> dict[str, Any] | None:
        """Project the canonical lifecycle mark for a reconciled live group.

        During convergence, adopted lifecycle rows retain the pre-cutover live
        group id in metadata.  Reporting should prefer their five-minute mark
        over the retired manager's older ``live_position_marks`` projection.
        """

        if not group_id:
            return None
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT payload FROM csa_lifecycles "
                    "WHERE lower(status) IN ('open','pending_live_submission') "
                    "ORDER BY updated_at DESC, rowid DESC"
                ).fetchall()
        except sqlite3.OperationalError:
            return None
        for row in rows:
            payload = json.loads(row["payload"])
            metadata = payload.get("metadata") or {}
            if str(metadata.get("execution_mode") or "") != "live":
                continue
            owner_ids = {
                str(metadata.get("legacy_source_id") or ""),
                str(metadata.get("live_group_id") or ""),
                str(metadata.get("group_id") or ""),
            }
            if group_id not in owner_ids:
                continue
            marked_at = str(metadata.get("last_marked_at") or "")
            if not marked_at:
                return None
            compiled = metadata.get("compiled_management_policy") or {}
            fields = compiled.get("resolved_fields") if isinstance(compiled, dict) else {}
            fields = fields if isinstance(fields, dict) else {}
            target_pct = _float(fields.get("profit_target_pct"), 0.0)
            profit_pct = _float(metadata.get("mark_profit_pct"), 0.0)
            entry_price = abs(_float(metadata.get("active_cost_basis"), 0.0))
            if entry_price <= 0:
                cashflows = payload.get("cashflow_ledger") or []
                if cashflows:
                    entry_price = abs(_float(cashflows[0].get("amount"), 0.0))
            multiplier = max(_float(metadata.get("contract_multiplier"), 100.0), 1.0)
            pnl = _float(metadata.get("mark_pnl_price"), 0.0) * multiplier
            target_profit = entry_price * (target_pct / 100.0) * multiplier
            target_progress = (pnl / target_profit * 100.0) if target_profit > 0 else 0.0
            return {
                "underlying": str(metadata.get("underlying") or ""),
                "pnl_mid": pnl,
                "pnl_natural": pnl,
                "target_profit": target_profit,
                "target_progress_pct": target_progress,
                "trigger_progress_pct": target_progress,
                "profit_pct": profit_pct,
                "marked_at": marked_at,
                "quote_fresh": True,
                "mark_source": "canonical_lifecycle",
                "loss_watch": False,
                "max_loss_watch": False,
            }
        return None

    def live_position_mark_stats(self, group_id: str) -> dict[str, Any]:
        if not group_id:
            return {"mfe": None, "mae": None, "marks": 0}
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT max(pnl) AS mfe, min(pnl) AS mae, count(*) AS marks
                FROM live_position_marks
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
        return {
            "mfe": row["mfe"] if row and row["mfe"] is not None else None,
            "mae": row["mae"] if row and row["mae"] is not None else None,
            "marks": int(row["marks"] or 0) if row else 0,
        }

    def open_live_position_groups(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            group_rows = conn.execute("SELECT group_id, opened_at, payload FROM live_position_groups WHERE status = 'open'").fetchall()
            position_rows = conn.execute("SELECT group_id, opened_at, payload FROM live_positions WHERE status = 'open'").fetchall()
        positions_by_group: dict[str, list[dict[str, Any]]] = {}
        for row in position_rows:
            payload = json.loads(row["payload"])
            payload["opened_at"] = row["opened_at"]
            positions_by_group.setdefault(str(row["group_id"]), []).append(payload)
        groups = []
        for row in group_rows:
            payload = json.loads(row["payload"])
            payload["group_id"] = str(row["group_id"])
            payload["opened_at"] = row["opened_at"]
            payload["positions"] = positions_by_group.get(str(row["group_id"]), [])
            groups.append(payload)
        return groups

    def closed_live_position_groups(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Non-open groups, most recently closed first (closed_at falls back to opened_at)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT group_id, status, opened_at, closed_at, payload
                FROM live_position_groups
                WHERE status != 'open'
                ORDER BY COALESCE(closed_at, opened_at) DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        groups = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["group_id"] = payload.get("group_id") or str(row["group_id"])
            payload["_status"] = row["status"]
            payload["_opened_at"] = row["opened_at"]
            payload["_closed_at"] = row["closed_at"]
            groups.append(payload)
        return groups

    def count_live_position_groups_opened_since(self, opened_since: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM live_position_groups WHERE opened_at >= ?",
                (opened_since,),
            ).fetchone()
        return int(row["n"] or 0) if row else 0

    def live_position_group(self, group_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT group_id, opened_at, payload FROM live_position_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["opened_at"] = row["opened_at"]
        return payload

    def record_live_management_decision(self, group_id: str, action: str, reason: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_management_decisions (group_id, action, reason, payload)
                VALUES (?, ?, ?, ?)
                """,
                (group_id, action, reason, json.dumps(payload, sort_keys=True)),
            )

    def latest_live_management_decision(self, group_id: str) -> dict[str, Any] | None:
        if not group_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT created_at, action, reason, payload
                FROM live_management_decisions
                WHERE group_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (group_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["_created_at"] = row["created_at"]
        payload["_action"] = row["action"]
        payload["_reason"] = row["reason"]
        return payload

    def record_live_position_mark(self, group_id: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_position_marks
                (group_id, underlying, entry_kind, pnl, target_profit, target_progress_pct, quote_fresh, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    payload.get("underlying"),
                    payload.get("entry_kind"),
                    float(payload.get("pnl_mid") or 0.0),
                    float(payload.get("target_profit") or 0.0),
                    float(payload.get("target_progress_pct") or 0.0),
                    int(bool(payload.get("quote_fresh"))),
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def live_loss_watch_observations(self, group_id: str, *, window_minutes: int) -> dict[str, Any]:
        modifier = f"-{max(int(window_minutes), 1)} minutes"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, payload
                FROM live_position_marks
                WHERE group_id = ?
                  AND created_at >= datetime('now', ?)
                ORDER BY created_at ASC, id ASC
                """,
                (group_id, modifier),
            ).fetchall()
        observed = []
        for row in rows:
            payload = json.loads(row["payload"])
            if bool(payload.get("max_loss_watch")):
                observed.append({"created_at": row["created_at"], "payload": payload})
        return {
            "count": len(observed),
            "window_minutes": max(int(window_minutes), 1),
            "first_seen_at": observed[0]["created_at"] if observed else "",
            "latest_seen_at": observed[-1]["created_at"] if observed else "",
        }

    def close_live_position_group(self, group_id: str, *, status: str, reason: str, payload: dict[str, Any]) -> None:
        close_payload = dict(payload)
        close_payload["close_reason"] = reason
        close_payload["closed_status"] = status
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE live_position_groups
                SET status = ?,
                    closed_at = CURRENT_TIMESTAMP,
                    payload = ?
                WHERE group_id = ? AND status = 'open'
                """,
                (status, json.dumps(close_payload, sort_keys=True), group_id),
            )
            conn.execute(
                """
                UPDATE live_positions
                SET status = ?,
                    closed_at = CURRENT_TIMESTAMP,
                    payload = ?
                WHERE group_id = ? AND status = 'open'
                """,
                (status, json.dumps(close_payload, sort_keys=True), group_id),
            )

    def save_live_approval_request(self, request: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO live_approval_requests
                (request_id, ticket_hash, plan_id, candidate_id, idea_id, underlying, structure,
                 status, expires_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                """,
                (
                    str(request["request_id"]),
                    str(request["ticket_hash"]),
                    str(request.get("plan_id") or ""),
                    str(request.get("candidate_id") or ""),
                    str(request.get("idea_id") or ""),
                    str(request.get("underlying") or ""),
                    str(request.get("structure") or ""),
                    str(request.get("status") or "pending"),
                    str(request["expires_at"]),
                    json.dumps(request, sort_keys=True),
                ),
            )

    def live_approval_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, payload FROM live_approval_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["_ledger_status"] = row["status"]
        return payload

    def live_approval_requests_by_status(self, statuses: set[str]) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT status, payload FROM live_approval_requests WHERE status IN ({placeholders})",
                tuple(sorted(statuses)),
            ).fetchall()
        requests = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["_ledger_status"] = row["status"]
            requests.append(payload)
        return requests

    def update_live_approval_request_status(self, request_id: str, status: str, payload_updates: dict[str, Any] | None = None) -> None:
        existing = self.live_approval_request(request_id)
        payload = dict(existing or {})
        payload.pop("_ledger_status", None)
        if payload_updates:
            payload.update(payload_updates)
        payload["status"] = status
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE live_approval_requests
                SET status = ?, updated_at = CURRENT_TIMESTAMP, payload = ?
                WHERE request_id = ?
                """,
                (status, json.dumps(payload, sort_keys=True), request_id),
            )

    def save_operator_review_request(self, request: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO operator_review_requests
                (request_id, request_type, subject_id, status, expires_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                """,
                (
                    str(request["request_id"]),
                    str(request["request_type"]),
                    str(request.get("subject_id") or ""),
                    str(request.get("status") or "pending"),
                    str(request["expires_at"]),
                    json.dumps(request, sort_keys=True),
                ),
            )

    def operator_review_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, payload FROM operator_review_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["_ledger_status"] = row["status"]
        return payload

    def operator_review_requests_by_status(self, statuses: set[str]) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT status, payload FROM operator_review_requests WHERE status IN ({placeholders})",
                tuple(sorted(statuses)),
            ).fetchall()
        requests = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["_ledger_status"] = row["status"]
            requests.append(payload)
        return requests

    def update_operator_review_request_status(
        self,
        request_id: str,
        status: str,
        payload_updates: dict[str, Any] | None = None,
    ) -> None:
        existing = self.operator_review_request(request_id)
        payload = dict(existing or {})
        payload.pop("_ledger_status", None)
        if payload_updates:
            payload.update(payload_updates)
        payload["status"] = status
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE operator_review_requests
                SET status = ?, updated_at = CURRENT_TIMESTAMP, payload = ?
                WHERE request_id = ?
                """,
                (status, json.dumps(payload, sort_keys=True), request_id),
            )

    def save_live_reconciliation_issue(self, issue: dict[str, Any]) -> None:
        existing = self.live_reconciliation_issue(str(issue["issue_id"]))
        observed_count = int((existing or {}).get("observed_count") or 0) + 1 if existing else int(issue.get("observed_count") or 1)
        issue = dict(issue)
        issue["observed_count"] = observed_count
        status = str(issue.get("status") or (existing or {}).get("status") or "open")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO live_reconciliation_issues
                (issue_id, issue_type, group_id, underlying, status, observed_count, last_seen_at, resolved_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
                """,
                (
                    str(issue["issue_id"]),
                    str(issue["issue_type"]),
                    str(issue.get("group_id") or ""),
                    str(issue.get("underlying") or ""),
                    status,
                    observed_count,
                    issue.get("resolved_at"),
                    json.dumps(issue, sort_keys=True),
                ),
            )

    def live_reconciliation_issue(self, issue_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status, observed_count, last_seen_at, payload
                FROM live_reconciliation_issues
                WHERE issue_id = ?
                """,
                (issue_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["status"] = row["status"]
        payload["observed_count"] = int(row["observed_count"] or payload.get("observed_count") or 0)
        payload["last_seen_at"] = row["last_seen_at"]
        return payload

    def open_live_reconciliation_issues(self, *, group_id: str = "", underlying: str = "") -> list[dict[str, Any]]:
        query = "SELECT status, observed_count, last_seen_at, payload FROM live_reconciliation_issues WHERE status IN ('open', 'held')"
        params: list[Any] = []
        if group_id:
            query += " AND group_id = ?"
            params.append(group_id)
        if underlying:
            query += " AND underlying = ?"
            params.append(underlying)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        issues = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["status"] = row["status"]
            payload["observed_count"] = int(row["observed_count"] or payload.get("observed_count") or 0)
            payload["last_seen_at"] = row["last_seen_at"]
            issues.append(payload)
        return issues

    def update_live_reconciliation_issue_status(
        self,
        issue_id: str,
        status: str,
        payload_updates: dict[str, Any] | None = None,
    ) -> None:
        existing = self.live_reconciliation_issue(issue_id)
        payload = dict(existing or {})
        if payload_updates:
            payload.update(payload_updates)
        payload["status"] = status
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE live_reconciliation_issues
                SET status = ?,
                    resolved_at = CASE WHEN ? IN ('resolved', 'dismissed', 'retired', 'adopted') THEN CURRENT_TIMESTAMP ELSE resolved_at END,
                    payload = ?
                WHERE issue_id = ?
                """,
                (status, status, json.dumps(payload, sort_keys=True), issue_id),
            )

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (event_type, payload) VALUES (?, ?)",
                (event_type, json.dumps(payload, sort_keys=True)),
            )

    def latest_event(self, event_type: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT created_at, payload FROM events WHERE event_type = ? ORDER BY id DESC LIMIT 1",
                (event_type,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload"]))
        return {**payload, "_created_at": str(row["created_at"])}


def _typed_shadow_lifecycles(
    conn: sqlite3.Connection,
    *,
    statuses: set[str] | None = None,
    opened_since: str = "",
) -> list[dict[str, Any]] | None:
    """Return canonical shadow lifecycle rows, or None for pre-migration stores.

    Once a database has any typed shadow lifecycle, the legacy shadow_fills
    table is historical evidence only.  Returning an empty list is therefore
    meaningful and must not fall back to stale legacy rows.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'csa_lifecycles'"
    ).fetchone()
    if table is None:
        return None
    has_typed_shadow = conn.execute(
        """
        SELECT 1
        FROM csa_lifecycles
        WHERE json_extract(payload, '$.metadata.execution_mode') = 'shadow'
        LIMIT 1
        """
    ).fetchone()
    if has_typed_shadow is None:
        return None
    query = "SELECT payload FROM csa_lifecycles WHERE json_extract(payload, '$.metadata.execution_mode') = 'shadow'"
    params: list[Any] = []
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        query += f" AND status IN ({placeholders})"
        params.extend(sorted(statuses))
    if opened_since:
        query += " AND opened_at >= ?"
        params.append(opened_since)
    return [json.loads(row["payload"]) for row in conn.execute(query, tuple(params)).fetchall()]


def _typed_shadow_portfolio_row(
    lifecycle: dict[str, Any],
    candidate_payloads: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    metadata = lifecycle.get("metadata") or {}
    candidate = candidate_payloads.get(str(metadata.get("candidate_id") or ""), {})
    greeks = metadata.get("greeks") if isinstance(metadata.get("greeks"), dict) else candidate.get("greeks") or {}
    estimated_bpr = metadata.get("bpr")
    if estimated_bpr in (None, ""):
        estimated_bpr = candidate.get("estimated_bpr") or 0.0
    return {
        "underlying": str(metadata.get("underlying") or candidate.get("underlying") or ""),
        "estimated_bpr": float(estimated_bpr or 0.0),
        "delta": float(greeks.get("delta") or 0.0),
        "gamma": float(greeks.get("gamma") or 0.0),
        "theta": float(greeks.get("theta") or 0.0),
        "vega": float(greeks.get("vega") or 0.0),
    }


def _candidate_greeks(candidate: dict[str, Any]) -> Greeks:
    raw = candidate.get("greeks")
    if isinstance(raw, dict) and any(raw.get(key) not in (None, "") for key in ("delta", "gamma", "theta", "vega")):
        return Greeks(
            delta=float(raw.get("delta") or 0.0),
            gamma=float(raw.get("gamma") or 0.0),
            theta=float(raw.get("theta") or 0.0),
            vega=float(raw.get("vega") or 0.0),
        )
    greeks = Greeks()
    for leg in candidate.get("legs") or []:
        sign = -1.0 if str(leg.get("side") or "").lower() == "sell" else 1.0
        qty = float(leg.get("quantity") or 1.0)
        greeks = greeks + Greeks(
            delta=sign * qty * float(leg.get("delta") or 0.0),
            gamma=sign * qty * float(leg.get("gamma") or 0.0),
            theta=sign * qty * float(leg.get("theta") or 0.0),
            vega=sign * qty * float(leg.get("vega") or 0.0),
        )
    return greeks


def _live_group_bpr(group: dict[str, Any]) -> float:
    candidate = group.get("candidate") or {}
    for raw in (candidate.get("estimated_bpr"), group.get("estimated_bpr")):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return round(value, 2)
    structure = str(group.get("structure") or candidate.get("structure") or "")
    legs = list(candidate.get("legs") or [])
    net_credit = _float(candidate.get("net_credit"), _float(group.get("net_credit"), 0.0))
    if structure in {"put_spread", "call_spread"} and len(legs) == 2:
        strikes = [_float(leg.get("strike"), 0.0) for leg in legs]
        width = abs(strikes[0] - strikes[1])
        return round(max(width * 100.0 - max(net_credit, 0.0) * 100.0, 1.0), 2)
    if structure in {"long_call", "long_put", "call_calendar", "put_calendar", "put_diagonal", "call_diagonal"}:
        return round(max(abs(net_credit) * 100.0, 1.0), 2)
    return round(max(abs(net_credit) * 100.0, 0.0), 2)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _account_book_mode(value: str | None) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {"live", "shadow"}:
        raise ValueError(f"account snapshot mode must be live or shadow, got {value!r}")
    return mode
