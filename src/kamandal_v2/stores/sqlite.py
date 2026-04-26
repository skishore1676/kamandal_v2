"""SQLite-backed local store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import Candidate, ChainSnapshot, Idea, Plan, PortfolioState, PreflightResult
from kamandal_v2.paths import resolve_path


class LocalStore:
    def __init__(self, sqlite_path: str | Path = "data/kamandal_v2.db") -> None:
        self.sqlite_path = resolve_path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
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
                """
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

    def save_account_snapshot(self, snapshot_id: str, portfolio: PortfolioState) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO account_snapshots VALUES (?, ?)",
                (snapshot_id, json.dumps(portfolio.to_dict(), sort_keys=True)),
            )

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

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (event_type, payload) VALUES (?, ?)",
                (event_type, json.dumps(payload, sort_keys=True)),
            )

