"""Immutable per-trading-day CSA policy snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kamandal_v2.domain.models import utc_now
from kamandal_v2.sheets import pull_sheet_tables
from kamandal_v2.strategy_lanes.operator_policy import OperatorPolicyBundle, load_csa_operator_policy


SNAPSHOT_SCHEMA = "kamandal.daily_strategy_policy.v1"
DEFAULT_SNAPSHOT_DIR = Path("data/run/strategy_policy")


@dataclass(frozen=True, slots=True)
class DailyPolicySnapshot:
    trading_date: str
    captured_at: str
    snapshot_hash: str
    tables: dict[str, list[dict[str, Any]]]
    path: Path
    policy: OperatorPolicyBundle

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SNAPSHOT_SCHEMA,
            "trading_date": self.trading_date,
            "captured_at": self.captured_at,
            "snapshot_hash": self.snapshot_hash,
            "path": str(self.path),
            "policy_count": len(self.policy.policies),
            "universe_count": len(self.policy.universe),
            "stages": sorted({item.stage.value for item in self.policy.policies}),
            "ok": self.policy.ok,
            "errors": list(self.policy.errors),
        }


def current_trading_date(config: dict[str, Any], *, now: datetime | None = None) -> str:
    timezone_name = str((config.get("runtime") or {}).get("market_timezone") or "America/Chicago")
    current = now or datetime.now(ZoneInfo(timezone_name))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo(timezone_name))
    return current.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def snapshot_path(
    config: dict[str, Any],
    *,
    trading_date: str | None = None,
    snapshot_dir: str | Path | None = None,
) -> Path:
    root = Path(
        snapshot_dir
        or os.environ.get("KAMANDAL_STRATEGY_POLICY_SNAPSHOT_DIR")
        or DEFAULT_SNAPSHOT_DIR
    )
    day = trading_date or current_trading_date(config)
    return root / f"strategy_policy_{day}.json"


def capture_daily_policy_snapshot(
    config: dict[str, Any],
    *,
    trading_date: str | None = None,
    snapshot_dir: str | Path | None = None,
    tables: dict[str, list[dict[str, Any]]] | None = None,
    captured_at: str | None = None,
) -> DailyPolicySnapshot:
    """Capture once; repeated calls return the already-frozen daily state."""

    path = snapshot_path(config, trading_date=trading_date, snapshot_dir=snapshot_dir)
    if path.exists():
        return load_daily_policy_snapshot(
            config,
            trading_date=trading_date,
            snapshot_dir=snapshot_dir,
        )
    day = trading_date or current_trading_date(config)
    observed_at = captured_at or utc_now()
    pulled = tables if tables is not None else pull_sheet_tables(config)
    frozen_tables = {
        "universe": [dict(row) for row in (pulled.get("universe") or [])],
        "playbooks": [dict(row) for row in (pulled.get("playbooks") or [])],
    }
    policy = load_csa_operator_policy(config, tables=frozen_tables, read_at=observed_at)
    if not policy.ok:
        raise ValueError("daily strategy policy is invalid: " + "; ".join(policy.errors))
    digest = policy_tables_hash(frozen_tables)
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "trading_date": day,
        "captured_at": observed_at,
        "snapshot_hash": digest,
        "tables": frozen_tables,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return DailyPolicySnapshot(day, observed_at, digest, frozen_tables, path, policy)


def load_daily_policy_snapshot(
    config: dict[str, Any],
    *,
    trading_date: str | None = None,
    snapshot_dir: str | Path | None = None,
) -> DailyPolicySnapshot:
    day = trading_date or current_trading_date(config)
    path = snapshot_path(config, trading_date=day, snapshot_dir=snapshot_dir)
    if not path.is_file():
        raise FileNotFoundError(f"daily strategy policy snapshot unavailable: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError(f"daily strategy policy schema mismatch: {path}")
    if str(payload.get("trading_date") or "") != day:
        raise ValueError(f"daily strategy policy date mismatch: {path}")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError(f"daily strategy policy tables missing: {path}")
    frozen_tables = {
        "universe": [dict(row) for row in (tables.get("universe") or [])],
        "playbooks": [dict(row) for row in (tables.get("playbooks") or [])],
    }
    digest = policy_tables_hash(frozen_tables)
    if digest != str(payload.get("snapshot_hash") or ""):
        raise ValueError(f"daily strategy policy hash mismatch: {path}")
    captured_at = str(payload.get("captured_at") or "")
    policy = load_csa_operator_policy(config, tables=frozen_tables, read_at=captured_at)
    if not policy.ok:
        raise ValueError("frozen daily strategy policy is invalid: " + "; ".join(policy.errors))
    return DailyPolicySnapshot(day, captured_at, digest, frozen_tables, path, policy)


def policy_tables_hash(tables: dict[str, list[dict[str, Any]]]) -> str:
    canonical = json.dumps(tables, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
