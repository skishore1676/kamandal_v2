"""Canonical CSA scorecard aggregation and durable report rendering."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


CENTRAL = ZoneInfo("America/Chicago")
CONTRACT_MULTIPLIER = 100.0


@dataclass(frozen=True, slots=True)
class ScorecardWriteResult:
    report: dict[str, Any]
    json_path: Path
    markdown_path: Path
    csv_path: Path


@dataclass(frozen=True, slots=True)
class WeeklyEconomicsWriteResult:
    report: dict[str, Any]
    json_path: Path
    markdown_path: Path
    csv_path: Path


def build_csa_scorecard(sqlite_path: str | Path, *, trading_date: date | str | None = None) -> dict[str, Any]:
    path = Path(sqlite_path)
    day = trading_date.isoformat() if isinstance(trading_date, date) else str(trading_date or datetime.now(UTC).date().isoformat())
    empty = {
        "schema": "kamandal.strategy_experiment_evidence.v1",
        "trading_date": day,
        "schema_ready": False,
        "runs": 0,
        "opportunities": 0,
        "admitted": 0,
        "rejected": 0,
        "actions": {},
        "shadow_intents": {},
        "shadow_fills": {},
        "open_lifecycles": 0,
        "policy_hashes": [],
        "primary_blockers": {},
        "run_errors": [],
        "csa_live_intents": 0,
        "zero_broker_effect": True,
        "unexpected_broker_effects": 0,
        "zero_unexpected_broker_effect": True,
        "evidence_status": "NO_DATA",
        "experiments": [],
        "recommendation_authority": False,
        "execution_authority": False,
    }
    if not path.is_file():
        return empty
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "csa_run_receipts" not in tables:
            return empty
        receipts = _payload_rows(conn, "csa_run_receipts", "date(started_at)=?", (day,))
        opportunities = _payload_rows(conn, "csa_opportunities", "date(observed_at)=?", (day,))
        decisions = _payload_rows(conn, "csa_admission_decisions", "date(decided_at)=?", (day,))
        actions = _rows(conn, "SELECT action_type, disposition, payload FROM csa_actions WHERE date(created_at)=?", (day,))
        intents = _rows(conn, "SELECT status, payload FROM csa_shadow_order_intents WHERE date(created_at)=?", (day,))
        intent_catalog = _rows(conn, "SELECT status, payload FROM csa_shadow_order_intents", ())
        fills = _rows(conn, "SELECT status, payload FROM csa_shadow_fills WHERE date(filled_at)=?", (day,))
        open_lifecycles = conn.execute("SELECT COUNT(*) FROM csa_lifecycles WHERE status IN ('proposed','open')").fetchone()[0]
        live_intents: list[dict[str, Any]] = []
        if "live_order_intents" in tables:
            live_intents = _rows(
                conn,
                "SELECT status, payload FROM live_order_intents "
                "WHERE date(created_at)=? AND (payload LIKE '%csa_policy_hash%' OR ticket_hash LIKE 'csa:%')",
                (day,),
            )
    blockers = Counter(str(item.get("primary_blocker") or "") for item in decisions if item.get("primary_blocker"))
    policy_hashes = sorted(
        {
            str(item.get("policy_hash") or "")
            for item in (*opportunities, *decisions)
            if item.get("policy_hash")
        }
    )
    run_errors = [
        str(error)
        for receipt in receipts
        for error in ((receipt.get("result") or {}).get("errors") or [])
    ]
    decoded_live_intents = _decoded_payload_rows(live_intents)
    unexpected_broker_effects = sum(
        not bool(item.get("stage_authorized"))
        or str(item.get("csa_stage") or "") not in {"pilot_live", "live"}
        for item in decoded_live_intents
    )
    zero_broker_effect = not decoded_live_intents
    zero_unexpected_broker_effect = unexpected_broker_effects == 0
    evidence_status = "RED" if run_errors or not zero_unexpected_broker_effect else ("COLLECTING" if receipts else "NO_DATA")
    experiments = _experiment_rows(
        receipts,
        opportunities,
        decisions,
        _decoded_payload_rows(intents),
        _decoded_payload_rows(intent_catalog),
        _decoded_payload_rows(fills),
        decoded_live_intents,
    )
    return {
        **empty,
        "schema_ready": True,
        "runs": len(receipts),
        "opportunities": len(opportunities),
        "admitted": sum(bool(item.get("admitted")) for item in decisions),
        "rejected": sum(not bool(item.get("admitted")) for item in decisions),
        "actions": dict(sorted(Counter(str(item["action_type"]) for item in actions).items())),
        "shadow_intents": dict(sorted(Counter(str(item["status"]) for item in intents).items())),
        "shadow_fills": dict(sorted(Counter(str(item["status"]) for item in fills).items())),
        "open_lifecycles": int(open_lifecycles),
        "policy_hashes": policy_hashes,
        "primary_blockers": dict(sorted(blockers.items())),
        "run_errors": run_errors,
        "csa_live_intents": len(decoded_live_intents),
        "zero_broker_effect": zero_broker_effect,
        "unexpected_broker_effects": unexpected_broker_effects,
        "zero_unexpected_broker_effect": zero_unexpected_broker_effect,
        "evidence_status": evidence_status,
        "experiments": experiments,
    }


def write_csa_scorecard(
    sqlite_path: str | Path,
    *,
    output_dir: str | Path,
    trading_date: date | str | None = None,
) -> ScorecardWriteResult:
    report = build_csa_scorecard(sqlite_path, trading_date=trading_date)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    day = report["trading_date"]
    json_path = target / f"csa1_scorecard_{day}.json"
    markdown_path = target / f"csa1_scorecard_{day}.md"
    csv_path = target / f"csa1_scorecard_{day}.csv"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_csa_scorecard(report), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        for key in (
            "runs",
            "opportunities",
            "admitted",
            "rejected",
            "open_lifecycles",
            "csa_live_intents",
            "zero_broker_effect",
            "unexpected_broker_effects",
            "zero_unexpected_broker_effect",
        ):
            writer.writerow([key, report[key]])
    return ScorecardWriteResult(report, json_path, markdown_path, csv_path)


def render_csa_scorecard(report: dict[str, Any]) -> str:
    verdict = str(report.get("evidence_status") or "NO_DATA")
    lines = [
        f"# Strategy Experiment Scorecard - {report['trading_date']}",
        "",
        f"Verdict: **{verdict}**",
        "",
        f"- Runs: {report['runs']}",
        f"- Opportunities: {report['opportunities']}",
        f"- Admitted / rejected decisions: {report['admitted']} / {report['rejected']}",
        f"- Open lifecycles: {report['open_lifecycles']}",
        f"- CSA live intents: {report['csa_live_intents']}",
        f"- Zero broker effect: {report['zero_broker_effect']}",
        f"- Unexpected broker effects: {report['unexpected_broker_effects']}",
        f"- Shadow fills: {json.dumps(report['shadow_fills'], sort_keys=True)}",
        f"- Primary blockers: {json.dumps(report['primary_blockers'], sort_keys=True)}",
        f"- Run errors: {json.dumps(report['run_errors'], sort_keys=True)}",
        "",
    ]
    return "\n".join(lines)


def build_csa_weekly_economics(
    sqlite_path: str | Path,
    *,
    through_date: date | str | None = None,
) -> dict[str, Any]:
    """Aggregate app-owned CSA lifecycle cashflows without making a recommendation."""

    path = Path(sqlite_path)
    through = _as_date(through_date)
    period_start = through - timedelta(days=through.weekday())
    base = {
        "schema": "kamandal.strategy_weekly_economics.v1",
        "scope": "csa_strategy_promotion_lane",
        "period_start": period_start.isoformat(),
        "through": through.isoformat(),
        "market_timezone": str(CENTRAL),
        "contract_multiplier": int(CONTRACT_MULTIPLIER),
        "schema_ready": False,
        "economic_rows": [],
        "totals": {
            "opened_in_period": 0,
            "closed_in_period": 0,
            "active_open": 0,
            "realized_pnl_usd": 0.0,
            "open_unrealized_pnl_usd": None,
            "total_pnl_usd": None,
        },
        "limitations": [
            "Shadow fills use Kamandal's conservative quote-based fill model.",
            "Commissions and fees are not included.",
            "Open P&L is reportable only from a same-day natural-close mark.",
            "Small samples are descriptive evidence, not proof of durable alpha.",
        ],
        "recommendation_authority": False,
        "sheet_write_authority": False,
        "execution_authority": False,
        "alpha_claim_authority": False,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    if not path.is_file():
        return _with_receipt(base, status="no_data")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "csa_lifecycles" not in tables:
            return _with_receipt(base, status="no_data")
        lifecycles = _payload_rows(conn, "csa_lifecycles", "1=1", ())

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for lifecycle in lifecycles:
        metadata = dict(lifecycle.get("metadata") or {})
        playbook_id = str(metadata.get("playbook_id") or "")
        if not playbook_id:
            continue
        opened = _central_date(lifecycle.get("opened_at"))
        updated = _central_date(lifecycle.get("updated_at"))
        status = str(lifecycle.get("status") or "")
        active = status in {"open", "proposed", "pending_live_submission"}
        if opened is None or opened > through:
            continue
        if not active and (updated is None or updated < period_start or updated > through):
            continue
        execution_mode = str(metadata.get("execution_mode") or "shadow")
        policy = dict(metadata.get("policy") or {})
        stage = str(policy.get("stage") or ("shadow" if execution_mode == "shadow" else "live"))
        grouped.setdefault((playbook_id, stage, execution_mode), []).append(lifecycle)

    rows = [
        _economic_row(key, values, period_start=period_start, through=through)
        for key, values in sorted(grouped.items())
    ]
    realized = round(sum(float(row["realized_pnl_usd"]) for row in rows), 2)
    open_marks_complete = all(row["open_unrealized_pnl_usd"] is not None for row in rows)
    open_unrealized = (
        round(sum(float(row["open_unrealized_pnl_usd"] or 0.0) for row in rows), 2)
        if open_marks_complete
        else None
    )
    total = round(realized + open_unrealized, 2) if open_unrealized is not None else None
    report = {
        **base,
        "schema_ready": True,
        "economic_rows": rows,
        "totals": {
            "opened_in_period": sum(int(row["opened_in_period"]) for row in rows),
            "closed_in_period": sum(int(row["closed_in_period"]) for row in rows),
            "active_open": sum(int(row["active_open"]) for row in rows),
            "realized_pnl_usd": realized,
            "open_unrealized_pnl_usd": open_unrealized,
            "total_pnl_usd": total,
        },
    }
    return _with_receipt(report, status="ok" if rows else "no_data")


def write_csa_weekly_economics(
    sqlite_path: str | Path,
    *,
    output_dir: str | Path,
    through_date: date | str | None = None,
) -> WeeklyEconomicsWriteResult:
    report = build_csa_weekly_economics(sqlite_path, through_date=through_date)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    through = report["through"]
    json_path = target / f"csa1_weekly_economics_{through}.json"
    markdown_path = target / f"csa1_weekly_economics_{through}.md"
    csv_path = target / f"csa1_weekly_economics_{through}.csv"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_csa_weekly_economics(report), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "playbook_id",
                "stage",
                "execution_mode",
                "opened_in_period",
                "closed_in_period",
                "active_open",
                "wins",
                "losses",
                "realized_pnl_usd",
                "open_unrealized_pnl_usd",
                "total_pnl_usd",
                "realized_return_on_bpr_pct",
                "economic_status",
            ),
        )
        writer.writeheader()
        for row in report["economic_rows"]:
            writer.writerow({key: row.get(key) for key in writer.fieldnames})
    return WeeklyEconomicsWriteResult(report, json_path, markdown_path, csv_path)


def render_csa_weekly_economics(report: dict[str, Any]) -> str:
    lines = [
        f"# CSA Strategy Economics - week of {report['period_start']} through {report['through']}",
        "",
        "This packet reports app-owned economics only. It cannot recommend or apply a stage change.",
        "",
        "| Playbook | Stage | Closed | Open | Realized P&L | Open P&L | Return on closed BPR | Evidence |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report.get("economic_rows") or []:
        open_pnl = row.get("open_unrealized_pnl_usd")
        return_pct = row.get("realized_return_on_bpr_pct")
        return_label = f"{return_pct:+.2f}%" if return_pct is not None else "Unavailable"
        lines.append(
            f"| `{row['playbook_id']}` | {row['stage']} | {row['closed_in_period']} | {row['active_open']} | "
            f"{_money(row['realized_pnl_usd'])} | {_money(open_pnl) if open_pnl is not None else 'Unavailable'} | "
            f"{return_label} | {row['economic_status']} |"
        )
    if not report.get("economic_rows"):
        lines.append("| No strategy economics yet | — | 0 | 0 | $0.00 | Unavailable | Unavailable | no data |")
    lines.extend(["", "Limitations:", "", *[f"- {item}" for item in report.get("limitations") or []], ""])
    return "\n".join(lines)


def _experiment_rows(
    receipts: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    intent_catalog: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    live_intents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    playbook_ids = sorted(
        {
            str(playbook_id)
            for receipt in receipts
            for playbook_id in ((receipt.get("result") or {}).get("playbook_ids") or [])
            if playbook_id
        }
        | {str(row.get("playbook_id") or "") for row in opportunities if row.get("playbook_id")}
    )
    rows: list[dict[str, Any]] = []
    for playbook_id in playbook_ids:
        playbook_opportunities = [row for row in opportunities if row.get("playbook_id") == playbook_id]
        opportunity_ids = {str(row.get("opportunity_id") or "") for row in playbook_opportunities}
        playbook_decisions = [row for row in decisions if str(row.get("opportunity_id") or "") in opportunity_ids]
        policy_hashes = sorted({str(row.get("policy_hash") or "") for row in playbook_opportunities if row.get("policy_hash")})
        # Ticket snapshots carry playbook identity; older rows remain visible in
        # aggregate totals but are not guessed into a cohort.
        playbook_intents = [row for row in intents if (row.get("metadata") or {}).get("playbook_id") == playbook_id]
        catalog_rows = [row for row in intent_catalog if (row.get("metadata") or {}).get("playbook_id") == playbook_id]
        ticket_ids = {str(row.get("ticket_id") or "") for row in catalog_rows}
        playbook_fills = [row for row in fills if str(row.get("ticket_id") or "") in ticket_ids]
        playbook_live_intents = [
            row for row in live_intents if str(row.get("csa_playbook_id") or "") == playbook_id
        ]
        stage_observations = [
            str(((receipt.get("result") or {}).get("playbook_stages") or {}).get(playbook_id) or "")
            for receipt in sorted(receipts, key=lambda item: str(item.get("started_at") or ""))
        ]
        stage = next((item for item in reversed(stage_observations) if item), "shadow")
        live_statuses = Counter(str(row.get("status") or "unknown") for row in playbook_live_intents)
        live_working = sum(
            str(row.get("status") or "")
            in {"stage_approved_pending_submit", "submitted", "repriced", "partially_filled"}
            for row in playbook_live_intents
        )
        unexpected = sum(
            not bool(row.get("stage_authorized"))
            or str(row.get("csa_stage") or "") not in {"pilot_live", "live"}
            for row in playbook_live_intents
        )
        rows.append(
            {
                "experiment_id": playbook_id,
                "playbook_id": playbook_id,
                "stage": stage,
                "policy_hashes": policy_hashes,
                "opportunities": len(playbook_opportunities),
                "admitted": sum(bool(row.get("admitted")) for row in playbook_decisions),
                "rejected": sum(not bool(row.get("admitted")) for row in playbook_decisions),
                "intents": dict(sorted(Counter(str(row.get("status") or "unknown") for row in playbook_intents).items())),
                "working_intents_current": sum(str(row.get("status") or "") in {"proposed", "working"} for row in catalog_rows),
                "fills": dict(sorted(Counter(str(row.get("status") or "unknown") for row in playbook_fills).items())),
                "live_intents": dict(sorted(live_statuses.items())),
                "live_working_intents_current": live_working,
                "unexpected_broker_effects": unexpected,
                "primary_blockers": dict(
                    sorted(
                        Counter(
                            str(row.get("primary_blocker") or "")
                            for row in playbook_decisions
                            if row.get("primary_blocker")
                        ).items()
                    )
                ),
            }
        )
    return rows


def _decoded_payload_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decoded: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        decoded.append({**payload, "status": row.get("status") or payload.get("status")})
    return decoded


def _payload_rows(conn: sqlite3.Connection, table: str, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    rows = _rows(conn, f"SELECT payload FROM {table} WHERE {where}", params)
    result = []
    for row in rows:
        try:
            result.append(json.loads(row["payload"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result.append({})
    return result


def _rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _economic_row(
    key: tuple[str, str, str],
    lifecycles: list[dict[str, Any]],
    *,
    period_start: date,
    through: date,
) -> dict[str, Any]:
    playbook_id, stage, execution_mode = key
    opened_in_period = 0
    closed_in_period: list[dict[str, Any]] = []
    active_open: list[dict[str, Any]] = []
    unresolved_entries = 0
    adjustments = 0
    quality_issues: list[str] = []
    policy_hashes: set[str] = set()
    observation_days: set[str] = set()
    for lifecycle in lifecycles:
        opened = _central_date(lifecycle.get("opened_at"))
        updated = _central_date(lifecycle.get("updated_at"))
        status = str(lifecycle.get("status") or "")
        metadata = dict(lifecycle.get("metadata") or {})
        if opened and period_start <= opened <= through:
            opened_in_period += 1
            observation_days.add(opened.isoformat())
        if updated and period_start <= updated <= through:
            observation_days.add(updated.isoformat())
        if lifecycle.get("policy_hash"):
            policy_hashes.add(str(lifecycle["policy_hash"]))
        adjustments += int(metadata.get("adjustment_count") or 0)
        if status == "closed" and updated and period_start <= updated <= through:
            closed_in_period.append(lifecycle)
        elif status == "open":
            active_open.append(lifecycle)
        elif status in {"proposed", "pending_live_submission"}:
            unresolved_entries += 1
    if len(policy_hashes) > 1:
        quality_issues.append("multiple_policy_hashes_in_stage_cohort")

    realized_values: list[float] = []
    closed_bpr = 0.0
    for lifecycle in closed_in_period:
        cashflows = list(lifecycle.get("cashflow_ledger") or [])
        metadata = dict(lifecycle.get("metadata") or {})
        if not cashflows:
            quality_issues.append("closed_lifecycle_missing_cashflows")
            continue
        realized_values.append(round(sum(float(item.get("amount") or 0.0) for item in cashflows) * CONTRACT_MULTIPLIER, 2))
        bpr = _positive_float(metadata.get("bpr"))
        if bpr is None:
            quality_issues.append("closed_lifecycle_missing_bpr")
        else:
            closed_bpr += bpr

    open_values: list[float] = []
    open_bpr = 0.0
    marked_open = 0
    for lifecycle in active_open:
        metadata = dict(lifecycle.get("metadata") or {})
        bpr = _positive_float(metadata.get("bpr"))
        if bpr is None:
            quality_issues.append("open_lifecycle_missing_bpr")
        else:
            open_bpr += bpr
        mark_date = _central_date(metadata.get("last_marked_at"))
        mark_pnl = _float_or_none(metadata.get("mark_pnl_price"))
        if mark_date == through and mark_pnl is not None:
            marked_open += 1
            open_values.append(round(mark_pnl * CONTRACT_MULTIPLIER, 2))
        else:
            quality_issues.append("open_lifecycle_missing_same_day_mark")

    realized = round(sum(realized_values), 2)
    open_complete = marked_open == len(active_open)
    open_unrealized = round(sum(open_values), 2) if open_complete else None
    total = round(realized + open_unrealized, 2) if open_unrealized is not None else None
    wins = sum(value > 0 for value in realized_values)
    losses = sum(value < 0 for value in realized_values)
    breakeven = sum(value == 0 for value in realized_values)
    closed_count = len(realized_values)
    realized_return = round(100.0 * realized / closed_bpr, 4) if closed_bpr > 0 else None
    total_bpr = closed_bpr + open_bpr
    total_return = round(100.0 * total / total_bpr, 4) if total is not None and total_bpr > 0 else None
    if not closed_count and not active_open:
        economic_status = "no_fills"
    elif not closed_count:
        economic_status = "open_only" if open_complete else "partial"
    elif quality_issues or not open_complete:
        economic_status = "partial"
    else:
        economic_status = "observed"
    return {
        "playbook_id": playbook_id,
        "stage": stage,
        "execution_mode": execution_mode,
        "policy_hashes": sorted(policy_hashes),
        "observation_days": sorted(observation_days),
        "opened_in_period": opened_in_period,
        "completed_entries": len(closed_in_period) + len(active_open),
        "closed_in_period": len(closed_in_period),
        "economically_complete_closed": closed_count,
        "active_open": len(active_open),
        "unresolved_entries": unresolved_entries,
        "marked_open": marked_open,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate_pct": round(100.0 * wins / closed_count, 2) if closed_count else None,
        "realized_pnl_usd": realized,
        "open_unrealized_pnl_usd": open_unrealized,
        "total_pnl_usd": total,
        "closed_bpr_usd": round(closed_bpr, 2),
        "open_bpr_usd": round(open_bpr, 2),
        "realized_return_on_bpr_pct": realized_return,
        "total_return_on_bpr_pct": total_return,
        "largest_win_usd": max(realized_values) if realized_values else None,
        "largest_loss_usd": min(realized_values) if realized_values else None,
        "adjustment_count": adjustments,
        "economic_status": economic_status,
        "quality_issues": sorted(set(quality_issues)),
        "commissions_included": False,
        "fill_basis": "quote_model" if execution_mode == "shadow" else "broker_fill",
    }


def _as_date(value: date | str | None) -> date:
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value))
    return datetime.now(CENTRAL).date()


def _central_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(CENTRAL).date()


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any) -> float | None:
    parsed = _float_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _with_receipt(report: dict[str, Any], *, status: str) -> dict[str, Any]:
    stable = {key: value for key, value in report.items() if key not in {"generated_at", "receipt"}}
    digest = hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return {**report, "receipt": {"status": status, "sha256": digest}}


def _money(value: float) -> str:
    amount = float(value)
    if amount > 0:
        return f"+${amount:,.2f}"
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    return "$0.00"
