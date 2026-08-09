"""Canonical CSA scorecard aggregation and durable report rendering."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ScorecardWriteResult:
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
        csa_live_intents = 0
        if "live_order_intents" in tables:
            csa_live_intents = conn.execute(
                "SELECT COUNT(*) FROM live_order_intents WHERE payload LIKE '%csa_policy_hash%' OR ticket_hash LIKE 'csa:%'"
            ).fetchone()[0]
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
    zero_broker_effect = int(csa_live_intents) == 0
    evidence_status = "RED" if run_errors or not zero_broker_effect else ("COLLECTING" if receipts else "NO_DATA")
    experiments = _experiment_rows(
        receipts,
        opportunities,
        decisions,
        _decoded_payload_rows(intents),
        _decoded_payload_rows(intent_catalog),
        _decoded_payload_rows(fills),
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
        "csa_live_intents": int(csa_live_intents),
        "zero_broker_effect": zero_broker_effect,
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
        for key in ("runs", "opportunities", "admitted", "rejected", "open_lifecycles", "csa_live_intents", "zero_broker_effect"):
            writer.writerow([key, report[key]])
    return ScorecardWriteResult(report, json_path, markdown_path, csv_path)


def render_csa_scorecard(report: dict[str, Any]) -> str:
    verdict = str(report.get("evidence_status") or "NO_DATA")
    lines = [
        f"# CSA-1 Shadow Scorecard - {report['trading_date']}",
        "",
        f"Verdict: **{verdict}**",
        "",
        f"- Runs: {report['runs']}",
        f"- Opportunities: {report['opportunities']}",
        f"- Admitted / rejected decisions: {report['admitted']} / {report['rejected']}",
        f"- Open lifecycles: {report['open_lifecycles']}",
        f"- CSA live intents: {report['csa_live_intents']}",
        f"- Zero broker effect: {report['zero_broker_effect']}",
        f"- Shadow fills: {json.dumps(report['shadow_fills'], sort_keys=True)}",
        f"- Primary blockers: {json.dumps(report['primary_blockers'], sort_keys=True)}",
        f"- Run errors: {json.dumps(report['run_errors'], sort_keys=True)}",
        "",
    ]
    return "\n".join(lines)


def _experiment_rows(
    receipts: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    intent_catalog: list[dict[str, Any]],
    fills: list[dict[str, Any]],
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
        rows.append(
            {
                "experiment_id": playbook_id,
                "playbook_id": playbook_id,
                "stage": "shadow",
                "policy_hashes": policy_hashes,
                "opportunities": len(playbook_opportunities),
                "admitted": sum(bool(row.get("admitted")) for row in playbook_decisions),
                "rejected": sum(not bool(row.get("admitted")) for row in playbook_decisions),
                "intents": dict(sorted(Counter(str(row.get("status") or "unknown") for row in playbook_intents).items())),
                "working_intents_current": sum(str(row.get("status") or "") in {"proposed", "working"} for row in catalog_rows),
                "fills": dict(sorted(Counter(str(row.get("status") or "unknown") for row in playbook_fills).items())),
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
