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
# App-owned equity-option contract multiplier (see live/position_management.CONTRACT_MULTIPLIER).
_STANDARD_OPTION_MULTIPLIER = 100.0


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
    # Source freshness is the most recent genuine broker observation backing this
    # export: the account snapshot readback and each open group's persisted FILLED
    # order observation. A FILLED order confirms the position at fill time; it is
    # not a continuous broker re-poll, so per-position broker_as_of carries the
    # position-level freshness the Kernel should judge staleness against.
    observed_candidates: list[datetime] = [account_observed] if account_observed else []
    for exposure in exposures:
        broker_as_of = exposure.get("broker_as_of")
        if broker_as_of:
            observed_candidates.append(datetime.fromisoformat(broker_as_of))
    source_observed = max(observed_candidates) if observed_candidates else None
    account_size = _number(account_payload.get("account_size"))
    bpr_used = _number(account_payload.get("bpr_used"))
    bpr_used_pct = round((bpr_used / account_size) * 100.0, 2) if account_size and bpr_used is not None else None
    assigned_capital, assigned_capital_provenance = _assigned_capital(config, account_payload)
    correlation_clusters = _correlation_clusters(config)
    identity = _account_identity(config_path.parent, config, broker_provider)
    basis = (
        "public_nlv_option_bp_difference" if broker_provider == "public"
        else "tastytrade_maintenance_requirement" if broker_provider == "tastytrade"
        else "unknown"
    )
    gaps = [
        "Account topology relative to other family apps is not verified.",
        "Broker position freshness reflects the last persisted FILLED order observation, not a continuous broker re-poll.",
        "BPR and worst-case loss are computed only for defined-risk unit vertical spreads; other structures stay null with an explicit basis.",
    ]
    if not identity["verified"]:
        gaps.append("Stable account identity is unavailable from app-owned config/cache state.")
    if source_observed is None:
        gaps.append("No account or broker-fill observation is persisted; source_observed_at is null.")
    if assigned_capital is None:
        gaps.append("Assigned capital is not configured and no broker account snapshot is available.")
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
        "assigned_capital": assigned_capital,
        "assigned_capital_provenance": assigned_capital_provenance,
        "correlation_clusters": correlation_clusters,
        "adapter_gaps": gaps,
        "account_snapshot": {
            "account_size": account_size,
            "buying_power": _number(account_payload.get("buying_power")),
            "bpr_used": bpr_used,
            "bpr_used_pct": bpr_used_pct,
            "bpr_used_pct_basis": "bpr_used_over_account_size" if bpr_used_pct is not None else "unknown",
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
    explicit_multiplier = _common_explicit_multiplier(candidate_legs, broker_legs)
    # Every broker leg above is a validated standard OPTION instrument, so the
    # app-owned equity-option multiplier applies unless a leg persists an explicit,
    # non-conflicting override.
    multiplier = explicit_multiplier if explicit_multiplier is not None else _STANDARD_OPTION_MULTIPLIER
    multiplier_provenance = (
        "producer_status_export" if explicit_multiplier is not None
        else "app_standard_option_contract"
    )
    multiplier_provenance_detail = (
        "explicit identical multiplier persisted on every candidate and broker leg"
        if explicit_multiplier is not None
        else "app standard equity-option multiplier constant"
    )
    structure = str(candidate.get("structure") or payload.get("structure") or "").strip().lower() or None
    risk = _structured_vertical_risk(candidate_legs, _number(candidate.get("net_credit")), multiplier, quantity, structure)
    mark_payload = _object(mark["payload"], label=f"mark for {group_id}") if mark else {}
    greeks, direction = _strategy_greeks(mark_payload.get("legs"))
    underlying = str(payload.get("underlying") or candidate.get("underlying") or "").upper()
    if not underlying:
        raise ValueError(f"open group {group_id} lacks an underlying")
    cluster = _cluster_for(config, underlying)
    mark_at = _sqlite_utc(mark["created_at"]) if mark and bool(mark["quote_fresh"]) else None
    position_at = _sqlite_utc(group["opened_at"])
    broker_at = _sqlite_utc(status_row["created_at"])
    return {
        "id": group_id,
        "group_id": group_id,
        "broker_group_id": _safe_fingerprint("broker-order", broker_order_id),
        "broker_position_ids": [_safe_fingerprint("broker-option", symbol) for symbol in symbols],
        "broker_identity_provenance": "persisted_filled_order_and_option_contract_fingerprints",
        "status": "open",
        "underlying": underlying,
        "structure": structure,
        "cluster": cluster or None,
        "direction": direction,
        "strategy_quantity": quantity,
        "contract_multiplier": multiplier,
        "multiplier_provenance": multiplier_provenance,
        "multiplier_provenance_detail": multiplier_provenance_detail,
        "estimated_bpr": risk["estimated_bpr"],
        "bpr_basis": risk["bpr_basis"],
        "bpr_basis_detail": risk["bpr_basis_detail"],
        "worst_case_loss_usd": risk["worst_case_loss_usd"],
        "worst_case_loss_basis": risk["worst_case_loss_basis"],
        "planned_stop_loss_usd": None,
        "planned_stop_loss_basis": "planned_stop_multiple_not_persisted_with_live_group",
        "greeks": greeks,
        "greek_metadata": ({"scope": "strategy_unit_current_mark", "units": "per_share_option_greeks",
                            "signedness": "producer_computed_from_leg_side",
                            "contract_multiplier_applied": False} if greeks else None),
        "opened_at": _sqlite_utc(group["opened_at"]),
        "position_as_of": position_at,
        "mark_as_of": mark_at,
        "broker_as_of": broker_at,
        "broker_observation_basis": "persisted_filled_order_status_fill_confirmation",
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


def _structured_vertical_risk(
    legs: list[Any], net_credit: float | None, multiplier: float | None,
    strategy_quantity: float | None, structure: str | None,
) -> dict[str, Any]:
    """Structure-aware BPR/worst-case loss for defined-risk unit vertical spreads.

    Reuses the app's own defined-risk vertical economics: for a credit spread the
    max loss is (width - credit) * multiplier; for a debit spread it is the net
    debit paid. Broker margin (BPR) on a defined-risk vertical equals that max
    loss. Anything that is not an unambiguous unit vertical stays explicitly null.
    """
    unknown = {
        "estimated_bpr": None, "worst_case_loss_usd": None,
        "bpr_basis": "unknown",
        "bpr_basis_detail": "unknown_or_unsupported_structure",
        "worst_case_loss_basis": "unknown_or_unsupported_structure",
    }
    if multiplier is None or multiplier <= 0 or net_credit is None or net_credit == 0:
        return unknown
    if strategy_quantity is None or strategy_quantity <= 0:
        return unknown
    geometry = _vertical_geometry(legs)
    if geometry is None:
        return unknown
    width_dollars = geometry["width"] * multiplier
    per_unit_entry = abs(net_credit) * multiplier
    if net_credit > 0:  # credit vertical
        per_unit_max_loss = width_dollars - per_unit_entry
        detail = "app_defined_risk_credit_vertical_width_minus_credit_times_multiplier"
        if per_unit_max_loss <= 0:
            return {**unknown, "worst_case_loss_basis": "credit_at_or_above_width_unresolvable",
                    "bpr_basis_detail": "credit_at_or_above_width_unresolvable"}
    else:  # debit vertical
        per_unit_max_loss = per_unit_entry
        detail = "app_defined_risk_debit_vertical_net_debit_times_multiplier"
        if per_unit_max_loss <= 0:
            return unknown
    total = round(per_unit_max_loss * strategy_quantity, 2)
    return {"estimated_bpr": total, "worst_case_loss_usd": total,
            "bpr_basis": "defined_risk_max_loss", "bpr_basis_detail": detail,
            "worst_case_loss_basis": detail}


def _vertical_geometry(legs: list[Any]) -> dict[str, Any] | None:
    if not isinstance(legs, list) or len(legs) != 2:
        return None
    option_types: set[str] = set()
    sides: list[str] = []
    strikes: list[float] = []
    expirations: set[str] = set()
    for leg in legs:
        if not isinstance(leg, dict):
            return None
        quantity = _number(leg.get("quantity"))
        if quantity is None:
            quantity = 1.0
        if quantity != 1.0:  # net_credit is per single spread unit; only unit legs stay unambiguous.
            return None
        option_type = str(leg.get("option_type") or "").lower()
        side = str(leg.get("side") or "").lower()
        strike = _number(leg.get("strike"))
        expiration = str(leg.get("expiration") or "").strip()
        if option_type not in {"put", "call"} or side not in {"buy", "sell"} or strike is None or not expiration:
            return None
        option_types.add(option_type)
        sides.append(side)
        strikes.append(strike)
        expirations.add(expiration)
    if len(option_types) != 1 or set(sides) != {"buy", "sell"} or len(expirations) != 1 or len(set(strikes)) != 2:
        return None
    return {"width": abs(strikes[0] - strikes[1]), "option_type": next(iter(option_types))}


def _assigned_capital(config: dict[str, Any], account_payload: dict[str, Any]) -> tuple[float | None, str]:
    for path in (("family_risk", "assigned_capital"), ("portfolio", "assigned_capital"),
                 ("risk_manager", "assigned_capital")):
        node: Any = config
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        value = _number(node)
        if value is not None:
            return value, f"configured:{'.'.join(path)}"
    size = _number(account_payload.get("account_size"))
    if size is not None:
        return size, "broker_account_net_liquidation_snapshot"
    return None, "no assigned-capital config or broker account snapshot available"


def _correlation_clusters(config: dict[str, Any]) -> dict[str, list[str]]:
    clusters = ((config.get("risk_manager") or {}).get("correlation_clusters") or {})
    result: dict[str, list[str]] = {}
    for name, members in clusters.items():
        symbols = sorted({str(item).upper() for item in (members or []) if str(item).strip()})
        if symbols:
            result[str(name)] = symbols
    return result


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
