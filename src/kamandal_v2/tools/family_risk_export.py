"""Export app-owned Kamandal live-position facts for family-risk shadow reporting."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


SCHEMA = "family-risk-shadow/v1"
_SNAPSHOT_ID = re.compile(r"(\d{8}T\d{6})Z?$")


def build_export(
    *, db_path: Path, config_path: Path, account_alias: str, now: datetime | None = None
) -> dict[str, Any]:
    generated_at = (now or datetime.now(UTC)).astimezone(UTC)
    config = _read_config(config_path)
    broker_provider = str(((config.get("broker") or {}).get("active") or "unknown")).lower()
    if broker_provider not in {"public", "tastytrade"}:
        broker_provider = "unknown"
    with _read_connection(db_path) as conn:
        conn.execute("BEGIN")
        groups = conn.execute(
            "SELECT group_id, opened_at, payload FROM live_position_groups "
            "WHERE status='open' ORDER BY group_id"
        ).fetchall()
        positions = conn.execute(
            "SELECT group_id, order_id, payload FROM live_positions WHERE status='open' ORDER BY group_id, id"
        ).fetchall()
        latest_account = conn.execute(
            "SELECT id, payload FROM account_snapshots ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        marks = conn.execute(
            """SELECT m.group_id, m.created_at, m.quote_fresh, m.payload
               FROM live_position_marks m
               JOIN (SELECT group_id, max(id) id FROM live_position_marks GROUP BY group_id) x
                 ON x.id=m.id"""
        ).fetchall()
        order_status = conn.execute(
            """SELECT s.order_id, s.created_at, s.status, s.payload
               FROM live_order_status s
               JOIN (SELECT order_id, max(id) id FROM live_order_status GROUP BY order_id) x
                 ON x.id=s.id"""
        ).fetchall()
        conn.execute("COMMIT")

    positions_by_group: dict[str, list[sqlite3.Row]] = {}
    for row in positions:
        positions_by_group.setdefault(str(row["group_id"]), []).append(row)
    marks_by_group = {str(row["group_id"]): row for row in marks}
    status_by_order = {str(row["order_id"]): row for row in order_status}
    exposures = [
        _map_group(row, positions_by_group.get(str(row["group_id"]), []),
                   marks_by_group.get(str(row["group_id"])), status_by_order, config)
        for row in groups
    ]

    account_payload = json.loads(latest_account["payload"]) if latest_account else {}
    account_observed = _snapshot_timestamp(latest_account["id"]) if latest_account else None
    # A mark proves quote freshness, not continued broker position existence.
    # Only an empty broker account snapshot with zero positions can currently
    # prove an empty book. Open groups have no current persisted broker-position
    # observation, so snapshot-wide source freshness must remain unknown.
    positions_count = _number(account_payload.get("positions_count"))
    source_observed = account_observed if not groups and positions_count == 0 else None
    identity = _account_identity(config_path.parent, config, broker_provider)
    basis = (
        "public_nlv_option_bp_difference" if broker_provider == "public"
        else "tastytrade_maintenance_requirement" if broker_provider == "tastytrade"
        else "unknown"
    )
    gaps = [
        "Account topology relative to other family apps is not verified.",
        "Per-position BPR and worst-case loss are not persisted with canonical live groups.",
        "Broker position freshness is limited to the last persisted filled-order observation.",
        "Open groups have no current persisted broker-position observation; source_observed_at remains null.",
    ]
    if not identity["verified"]:
        gaps.append("Stable account identity is unavailable from app-owned config/cache state.")
    if source_observed is None:
        gaps.append("No account or position-mark observation is persisted; source_observed_at is null.")
    return {
        "schema": SCHEMA,
        "source": "kamandal_v2",
        "record_scope": "live_open_positions",
        "source_table": "live_positions",
        "generated_at": generated_at.isoformat(),
        "source_observed_at": source_observed.isoformat() if source_observed else None,
        "account_alias": account_alias,
        "broker_provider": broker_provider,
        "account_identity_verified": identity["verified"],
        "account_fingerprint": identity["fingerprint"],
        "account_group": identity["group"],
        "account_identity_provenance": identity["provenance"],
        "account_topology": "unknown",
        "account_bpr_basis": basis,
        "adapter_gaps": gaps,
        "account_snapshot": {
            "account_size": _number(account_payload.get("account_size")),
            "buying_power": _number(account_payload.get("buying_power")),
            "bpr_used": _number(account_payload.get("bpr_used")),
            "greeks": _allow_greeks(account_payload.get("greeks")),
            "observed_at": account_observed.isoformat() if account_observed else None,
        },
        "live_positions": exposures,
    }


def _map_group(group: sqlite3.Row, positions: list[sqlite3.Row], mark: sqlite3.Row | None,
               statuses: dict[str, sqlite3.Row], config: dict[str, Any]) -> dict[str, Any]:
    group_id = str(group["group_id"])
    payload = _object(group["payload"], label=f"group {group_id}")
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    order_ids = {str(row["order_id"]).strip() for row in positions if str(row["order_id"] or "").strip()}
    if len(order_ids) != 1:
        raise ValueError(f"open group {group_id} has ambiguous persisted broker order references")
    order_id = next(iter(order_ids))
    status_row = statuses.get(order_id)
    if status_row is None or str(status_row["status"]).upper() != "FILLED":
        raise ValueError(f"open group {group_id} lacks one canonical persisted FILLED broker order")
    broker = _object(status_row["payload"], label=f"broker order for {group_id}")
    broker_order_id = str(broker.get("orderId") or "").strip()
    if not broker_order_id or broker_order_id != order_id:
        raise ValueError(f"open group {group_id} has conflicting broker order identity")
    broker_legs = broker.get("legs") if isinstance(broker.get("legs"), list) else []
    symbols: list[str] = []
    for leg in broker_legs:
        instrument = leg.get("instrument") if isinstance(leg, dict) else None
        symbol = str((instrument or {}).get("symbol") or "").strip().upper()
        if not symbol or str((instrument or {}).get("type") or "").upper() != "OPTION":
            raise ValueError(f"open group {group_id} has an ambiguous broker component instrument")
        symbols.append(symbol)
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError(f"open group {group_id} has missing or duplicate broker component instruments")
    quantity = _number(broker.get("filledQuantity")) or _number(broker.get("quantity"))
    if quantity is None or quantity <= 0:
        raise ValueError(f"open group {group_id} has no positive broker-filled strategy quantity")

    candidate_legs = candidate.get("legs") if isinstance(candidate.get("legs"), list) else []
    safe_legs = [_safe_candidate_leg(leg) for leg in candidate_legs]
    multiplier = _common_explicit_multiplier(candidate_legs, broker_legs)
    mark_payload = _object(mark["payload"], label=f"mark for {group_id}") if mark else {}
    greeks, direction = _strategy_greeks(mark_payload.get("legs"))
    underlying = str(payload.get("underlying") or candidate.get("underlying") or "").upper()
    if not underlying:
        raise ValueError(f"open group {group_id} lacks an underlying")
    cluster = _cluster_for(config, underlying)
    mark_at = _sqlite_utc(mark["created_at"]) if mark and bool(mark["quote_fresh"]) else None
    position_at = _sqlite_utc(group["opened_at"])
    return {
        "id": group_id,
        "group_id": group_id,
        "broker_group_id": _safe_fingerprint("broker-order", broker_order_id),
        "broker_position_ids": [_safe_fingerprint("broker-option", symbol) for symbol in symbols],
        "broker_identity_provenance": "persisted_filled_order_and_option_contract_fingerprints",
        "status": "open",
        "underlying": underlying,
        "cluster": cluster or None,
        "direction": direction,
        "strategy_quantity": quantity,
        "contract_multiplier": multiplier,
        "multiplier_provenance": "producer_status_export" if multiplier is not None else None,
        "estimated_bpr": None,
        "bpr_basis": "unknown",
        "worst_case_loss_usd": None,
        "greeks": greeks,
        "greek_metadata": ({"scope": "strategy_unit_current_mark", "units": "per_share_option_greeks",
                            "signedness": "producer_computed_from_leg_side",
                            "contract_multiplier_applied": False} if greeks else None),
        "opened_at": _sqlite_utc(group["opened_at"]),
        "position_as_of": position_at,
        "mark_as_of": mark_at,
        "broker_as_of": None,
        "payload": {"candidate": {"legs": safe_legs}},
    }


def _strategy_greeks(raw_legs: Any) -> tuple[dict[str, float], str]:
    if not isinstance(raw_legs, list) or not raw_legs:
        return {}, "unknown"
    totals = {name: 0.0 for name in ("delta", "gamma", "theta", "vega")}
    for leg in raw_legs:
        if not isinstance(leg, dict) or any(_number(leg.get(name)) is None for name in totals):
            return {}, "unknown"
        side = str(leg.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            return {}, "unknown"
        sign = 1.0 if side == "buy" else -1.0
        quantity = _number(leg.get("quantity")) or 1.0
        for name in totals:
            totals[name] += sign * quantity * float(leg[name])
    direction = "bullish" if totals["delta"] > .05 else "bearish" if totals["delta"] < -.05 else "mixed"
    return {name: round(value, 8) for name, value in totals.items()}, direction


def _safe_candidate_leg(leg: Any) -> dict[str, Any]:
    if not isinstance(leg, dict):
        raise ValueError("candidate leg is not an object")
    allowed = ("side", "quantity", "option_type", "strike", "expiration", "role")
    return {key: leg.get(key) for key in allowed if leg.get(key) is not None}


def _common_explicit_multiplier(candidate_legs: list[Any], broker_legs: list[Any]) -> float | None:
    values = []
    for leg in [*candidate_legs, *broker_legs]:
        if not isinstance(leg, dict):
            continue
        instrument = leg.get("instrument") if isinstance(leg.get("instrument"), dict) else {}
        value = _number(leg.get("contract_multiplier") or leg.get("multiplier")
                        or instrument.get("contractMultiplier") or instrument.get("multiplier"))
        if value is not None:
            values.append(value)
    return values[0] if values and len(values) == len(candidate_legs) + len(broker_legs) and len(set(values)) == 1 else None


def _cluster_for(config: dict[str, Any], symbol: str) -> str:
    clusters = ((config.get("risk_manager") or {}).get("correlation_clusters") or {})
    matches = [str(name) for name, members in clusters.items()
               if symbol in {str(item).upper() for item in (members or [])}]
    if len(matches) > 1:
        raise ValueError(f"underlying {symbol} belongs to multiple configured correlation clusters")
    return matches[0] if matches else ""


def _account_identity(root: Path, config: dict[str, Any], provider: str) -> dict[str, Any]:
    provider_cfg = ((config.get("broker") or {}).get(provider) or {}) if provider != "unknown" else {}
    raw = str(provider_cfg.get("account_id") or "").strip()
    cache_value = str(provider_cfg.get("account_cache_file") or "").strip()
    if not raw and cache_value:
        cache_path = Path(cache_value).expanduser()
        if not cache_path.is_absolute():
            cache_path = (root.parent / cache_path).resolve()
        try:
            raw = str(json.loads(cache_path.read_text(encoding="utf-8")).get("accountId") or "").strip()
        except (OSError, ValueError, TypeError):
            raw = ""
    key = os.environ.get("FAMILY_RISK_IDENTITY_HMAC_KEY", "").encode("utf-8")
    if not raw or not key:
        return {"verified": False, "fingerprint": None, "group": None,
                "provenance": "account identity or operator HMAC key unavailable; identity unknown"}
    digest = hmac.new(key, raw.encode(), hashlib.sha256).hexdigest()
    return {"verified": True, "fingerprint": f"hmac-sha256:{digest}",
            "group": f"{provider}:hmac-sha256:{digest}",
            "provenance": "keyed HMAC-SHA256 of app-owned broker account identity; raw identity and key omitted"}


def _read_config(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Kamandal config must be a mapping")
    return payload


def _object(raw: Any, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {label}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _allow_greeks(raw: Any) -> dict[str, float | None]:
    raw = raw if isinstance(raw, dict) else {}
    return {name: _number(raw.get(name)) for name in ("delta", "gamma", "theta", "vega")}


def _number(raw: Any) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _snapshot_timestamp(raw: Any) -> datetime | None:
    match = _SNAPSHOT_ID.search(str(raw or ""))
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC) if match else None


def _sqlite_utc(raw: Any) -> str:
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)  # SQLite CURRENT_TIMESTAMP is UTC in this app.
    return parsed.astimezone(UTC).isoformat()


def _safe_fingerprint(namespace: str, raw: str) -> str:
    return f"{namespace}:sha256:{hashlib.sha256(raw.encode()).hexdigest()}"


def _read_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write((json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/kamandal_v2.db")
    parser.add_argument("--config", default="config/control.yaml")
    parser.add_argument("--account-alias", default="kamandal-broker")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    atomic_write_json(Path(args.out), build_export(db_path=Path(args.db), config_path=Path(args.config),
                                                   account_alias=args.account_alias))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
