"""Go-live quality audit for ideas, plans, and shadow outcomes."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kamandal_v2.paths import resolve_path


DATE_RE = re.compile(r"(20\d{2})-?(\d{2})-?(\d{2})")


@dataclass(frozen=True)
class AuditResult:
    output_dir: Path
    selected_dates: list[str]
    files: dict[str, Path]
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "selected_dates": self.selected_dates,
            "files": {key: str(value) for key, value in self.files.items()},
            "verdict": self.verdict,
        }


def build_go_live_audit_report(
    *,
    sqlite_path: str | Path = "data/kamandal_v2.db",
    output_dir: str | Path = "data/reports/go_live_audit",
    dates: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
) -> AuditResult:
    db_path = resolve_path(sqlite_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Kamandal database not found: {db_path}")
    out_root = resolve_path(output_dir)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        selected_dates = _selected_dates(conn, dates=dates, since=since, until=until)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = out_root / f"{stamp}_{'_'.join(selected_dates)}"
        out_dir.mkdir(parents=True, exist_ok=True)

        context = _load_context(conn, selected_dates)
        files = {
            "daily_summary_csv": out_dir / "daily_summary.csv",
            "ideas_csv": out_dir / "ideas.csv",
            "candidates_csv": out_dir / "candidates.csv",
            "plans_csv": out_dir / "plans.csv",
            "shadow_outcomes_csv": out_dir / "shadow_outcomes.csv",
            "verdict_md": out_dir / "verdict.md",
            "audit_json": out_dir / "audit.json",
        }
        _write_csv(files["daily_summary_csv"], _daily_summary_rows(context), DAILY_SUMMARY_FIELDS)
        _write_csv(files["ideas_csv"], _idea_rows(context), IDEA_FIELDS)
        _write_csv(files["candidates_csv"], _candidate_rows(context), CANDIDATE_FIELDS)
        _write_csv(files["plans_csv"], _plan_rows(context), PLAN_FIELDS)
        _write_csv(files["shadow_outcomes_csv"], _shadow_outcome_rows(context), SHADOW_OUTCOME_FIELDS)
        verdict = _markdown_verdict(context)
        files["verdict_md"].write_text(verdict, encoding="utf-8")
        files["audit_json"].write_text(
            json.dumps(
                {
                    "selected_dates": selected_dates,
                    "files": {key: str(value) for key, value in files.items()},
                    "daily_summary": _daily_summary_rows(context),
                    "verdict": verdict,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    finally:
        conn.close()
    return AuditResult(output_dir=out_dir, selected_dates=selected_dates, files=files, verdict=verdict)


DAILY_SUMMARY_FIELDS = [
    "date",
    "plan_runs",
    "plans",
    "auto_approved",
    "daily_plan_skipped",
    "ideas",
    "candidates",
    "eligible_candidates",
    "rejected_candidates",
    "shadow_opened",
    "shadow_closed",
    "closed_pnl",
    "portfolio_positions",
    "portfolio_bpr_used_pct",
    "no_plan_diagnosis",
    "top_rejection_reasons",
    "machine_verdict",
    "suman_review",
]

IDEA_FIELDS = [
    "date",
    "idea_id",
    "source",
    "underlying",
    "direction",
    "horizon_days",
    "confidence",
    "extraction_confidence",
    "thesis_tags",
    "mentioned_strategy",
    "quote_evidence",
    "notes",
    "machine_verdict",
    "suman_review",
]

CANDIDATE_FIELDS = [
    "date",
    "plan_run_id",
    "candidate_id",
    "idea_id",
    "underlying",
    "playbook_id",
    "structure",
    "eligible",
    "rejection_reason",
    "net_credit",
    "estimated_bpr",
    "delta",
    "theta",
    "gamma",
    "liquidity_score",
    "preflight_ok",
    "machine_verdict",
    "suman_review",
]

PLAN_FIELDS = [
    "date",
    "plan_run_id",
    "plan_id",
    "rank",
    "trade_count",
    "score",
    "bpr_utilization_pct",
    "buying_power_after",
    "candidate_summary",
    "machine_verdict",
    "suman_review",
]

SHADOW_OUTCOME_FIELDS = [
    "date",
    "fill_id",
    "plan_run_id",
    "idea_id",
    "underlying",
    "playbook_id",
    "structure",
    "status",
    "opened_at",
    "closed_at",
    "net_credit",
    "estimated_bpr",
    "delta",
    "theta",
    "gamma",
    "close_reason",
    "close_pnl",
    "machine_verdict",
    "suman_review",
]


def _selected_dates(conn: sqlite3.Connection, *, dates: list[str] | None, since: str | None, until: str | None) -> list[str]:
    if dates:
        return _normalize_dates(dates)
    if since or until:
        return _date_range(conn, since=since, until=until)
    return _select_representative_dates(conn)


def _date_range(conn: sqlite3.Connection, *, since: str | None, until: str | None) -> list[str]:
    summary = _all_daily_summary(conn)
    if not summary:
        raise RuntimeError("no Kamandal shadow evidence found in database")
    start = _parse_day(_date_from_text(since or "") or min(row["date"] for row in summary))
    end = _parse_day(_date_from_text(until or "") or max(row["date"] for row in summary))
    if start > end:
        raise ValueError(f"invalid audit range: since {start.date()} is after until {end.date()}")
    selected = [row["date"] for row in summary if start <= _parse_day(row["date"]) <= end]
    if not selected:
        raise RuntimeError(f"no Kamandal evidence found between {start.date()} and {end.date()}")
    return sorted(dict.fromkeys(selected))


def _select_representative_dates(conn: sqlite3.Connection) -> list[str]:
    summary = _all_daily_summary(conn)
    if not summary:
        raise RuntimeError("no Kamandal shadow evidence found in database")
    max_day = max(_parse_day(row["date"]) for row in summary)
    cutoff = max_day - timedelta(days=14)
    recent = [row for row in summary if _parse_day(row["date"]) >= cutoff and row["plan_runs"] > 0]
    if not recent:
        recent = summary
    active = sorted(recent, key=lambda row: (row["shadow_opened"], row["plans"], row["plan_runs"], _date_ordinal(row["date"])), reverse=True)[0]["date"]
    quiet_pool = [row for row in recent if row["date"] != active and (row["plans"] == 0 or row["shadow_opened"] == 0)]
    if not quiet_pool:
        quiet_pool = [row for row in recent if row["date"] != active]
    quiet = None
    if quiet_pool:
        quiet = sorted(
            quiet_pool,
            key=lambda row: (
                row["shadow_opened"],
                row["plans"],
                -row["plan_runs"],
                -_date_ordinal(row["date"]),
            ),
        )[0]["date"]
    return sorted([date for date in [active, quiet] if date])[:2]


def _all_daily_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    dates = set()
    if _table_exists(conn, "events"):
        dates.update(
            str(row[0])
            for row in conn.execute("SELECT DISTINCT date(created_at) FROM events WHERE created_at IS NOT NULL").fetchall()
            if row[0]
        )
    if _table_exists(conn, "shadow_fills"):
        dates.update(
            str(row[0])
            for row in conn.execute("SELECT DISTINCT date(opened_at) FROM shadow_fills WHERE opened_at IS NOT NULL").fetchall()
            if row[0]
        )
    context = _load_context(conn, sorted(dates))
    return _daily_summary_rows(context)


def _load_context(conn: sqlite3.Connection, dates: list[str]) -> dict[str, Any]:
    selected = set(dates)
    events = _events_by_date(conn, selected)
    portfolio_snapshots = _portfolio_snapshots_by_date(conn, selected)
    plans = _plans_by_date(conn, selected)
    candidates = _candidates_by_date(conn, selected)
    fills = _fills_by_date(conn, selected)
    idea_usage_dates = _idea_usage_dates(candidates, fills)
    idea_ids = {payload.get("idea_id") for rows in candidates.values() for payload in rows}
    idea_ids.update(payload.get("idea_id") for rows in fills.values() for payload in rows)
    ideas = _ideas_by_date(conn, selected, idea_ids, idea_usage_dates)
    return {
        "dates": dates,
        "events": events,
        "portfolio_snapshots": portfolio_snapshots,
        "plans": plans,
        "candidates": candidates,
        "fills": fills,
        "ideas": ideas,
    }


def _idea_usage_dates(
    candidates: dict[str, list[dict[str, Any]]],
    fills: dict[str, list[dict[str, Any]]],
) -> dict[str, set[str]]:
    usage: dict[str, set[str]] = defaultdict(set)
    for day, rows in candidates.items():
        for row in rows:
            idea_id = str(row.get("idea_id") or "")
            if idea_id:
                usage[idea_id].add(day)
    for day, rows in fills.items():
        for row in rows:
            idea_id = str(row.get("_idea_id") or row.get("idea_id") or "")
            if idea_id:
                usage[idea_id].add(day)
    return usage


def _events_by_date(conn: sqlite3.Connection, dates: set[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not _table_exists(conn, "events"):
        return out
    for row in conn.execute("SELECT created_at, event_type, payload FROM events ORDER BY created_at").fetchall():
        day = str(row["created_at"] or "")[:10]
        if day not in dates:
            continue
        payload = _loads(row["payload"])
        payload["_created_at"] = row["created_at"]
        payload["_event_type"] = row["event_type"]
        out[day].append(payload)
    return out


def _portfolio_snapshots_by_date(conn: sqlite3.Connection, dates: set[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not _table_exists(conn, "account_snapshots"):
        return out
    for row in conn.execute("SELECT id, payload FROM account_snapshots ORDER BY id").fetchall():
        run_id = str(row["id"] or "")
        day = _date_from_run_id(run_id)
        if day not in dates:
            continue
        payload = _loads(row["payload"])
        payload["_plan_run_id"] = run_id
        out[day].append(payload)
    return out


def _plans_by_date(conn: sqlite3.Connection, dates: set[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not _table_exists(conn, "plans"):
        return out
    for row in conn.execute("SELECT id, plan_run_id, rank, payload FROM plans ORDER BY plan_run_id, rank").fetchall():
        day = _date_from_run_id(str(row["plan_run_id"]))
        if day not in dates:
            continue
        payload = _loads(row["payload"])
        payload["_id"] = row["id"]
        payload["_plan_run_id"] = row["plan_run_id"]
        payload["_rank"] = row["rank"]
        out[day].append(payload)
    return out


def _candidates_by_date(conn: sqlite3.Connection, dates: set[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not _table_exists(conn, "candidates"):
        return out
    for row in conn.execute("SELECT id, plan_run_id, payload FROM candidates ORDER BY plan_run_id, id").fetchall():
        day = _date_from_run_id(str(row["plan_run_id"]))
        if day not in dates:
            continue
        payload = _loads(row["payload"])
        payload["_id"] = row["id"]
        payload["_plan_run_id"] = row["plan_run_id"]
        out[day].append(payload)
    return out


def _fills_by_date(conn: sqlite3.Connection, dates: set[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not _table_exists(conn, "shadow_fills"):
        return out
    for row in conn.execute("SELECT * FROM shadow_fills ORDER BY opened_at, id").fetchall():
        day = str(row["opened_at"] or "")[:10]
        if day not in dates:
            continue
        payload = _loads(row["payload"])
        close_payload = _loads(row["close_payload"])
        payload.update(
            {
                "_fill_id": row["id"],
                "_plan_run_id": row["plan_run_id"],
                "_candidate_id": row["candidate_id"],
                "_idea_id": row["idea_id"] or payload.get("idea_id"),
                "_underlying": row["underlying"],
                "_playbook_id": row["playbook_id"] or payload.get("playbook_id"),
                "_structure": row["structure"],
                "_status": row["status"],
                "_opened_at": row["opened_at"],
                "_closed_at": row["closed_at"],
                "_net_credit": row["net_credit"],
                "_estimated_bpr": row["estimated_bpr"],
                "_delta": row["delta"],
                "_gamma": row["gamma"],
                "_theta": row["theta"],
                "_vega": row["vega"],
                "_close_reason": row["close_reason"],
                "_close_pnl": row["close_pnl"],
                "_close_payload": close_payload,
            }
        )
        out[day].append(payload)
    return out


def _ideas_by_date(
    conn: sqlite3.Connection,
    dates: set[str],
    idea_ids: set[Any],
    idea_usage_dates: dict[str, set[str]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not _table_exists(conn, "ideas"):
        return out
    ids = {str(item) for item in idea_ids if item}
    for row in conn.execute("SELECT id, status, payload FROM ideas ORDER BY id").fetchall():
        payload = _loads(row["payload"])
        payload["_id"] = row["id"]
        payload["_status"] = row["status"]
        idea_id = str(payload.get("idea_id") or row["id"])
        usage_days = sorted(idea_usage_dates.get(idea_id, set()) & dates)
        day = _date_from_text(idea_id + " " + str(payload.get("source") or ""))
        if day in dates:
            usage_days.append(day)
        usage_days = sorted(dict.fromkeys(usage_days))
        if not usage_days and idea_id not in ids:
            continue
        if not usage_days:
            usage_days = [_infer_date_for_idea(idea_id, dates, ids)]
        for usage_day in usage_days:
            if usage_day in dates:
                stamped = dict(payload)
                stamped["_audit_date"] = usage_day
                out[usage_day].append(stamped)
    return out


def _daily_summary_rows(context: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for day in context["dates"]:
        events = context["events"].get(day, [])
        plans = context["plans"].get(day, [])
        candidates = context["candidates"].get(day, [])
        fills = context["fills"].get(day, [])
        portfolio_summary = _portfolio_summary(context["portfolio_snapshots"].get(day, []))
        rejection_counts = Counter(_candidate_rejection(candidate) for candidate in candidates if _candidate_rejection(candidate))
        closed_pnl = sum(float(fill.get("_close_pnl") or 0.0) for fill in fills if fill.get("_close_pnl") is not None)
        rows.append(
            {
                "date": day,
                "plan_runs": _count_events(events, "plan_run_completed"),
                "plans": len(plans),
                "auto_approved": _count_events(events, "shadow_plan_auto_approved"),
                "daily_plan_skipped": _count_events(events, "daily_plan_write_skipped"),
                "ideas": len(context["ideas"].get(day, [])),
                "candidates": len(candidates),
                "eligible_candidates": sum(1 for candidate in candidates if not _candidate_rejection(candidate)),
                "rejected_candidates": sum(1 for candidate in candidates if _candidate_rejection(candidate)),
                "shadow_opened": len(fills),
                "shadow_closed": sum(1 for fill in fills if fill.get("_status") == "closed"),
                "closed_pnl": round(closed_pnl, 2),
                "portfolio_positions": portfolio_summary["positions_count"],
                "portfolio_bpr_used_pct": portfolio_summary["bpr_used_pct"],
                "no_plan_diagnosis": _no_plan_diagnosis(plans, candidates, fills, portfolio_summary, events),
                "top_rejection_reasons": _format_counter(rejection_counts, limit=4),
                "machine_verdict": _daily_verdict(plans, candidates, fills, rejection_counts),
                "suman_review": "",
            }
        )
    return rows


def _portfolio_summary(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    if not snapshots:
        return {"positions_count": "", "bpr_used_pct": ""}
    positions = [_float_or_none(snapshot.get("positions_count")) for snapshot in snapshots]
    bpr_pcts = [_float_or_none(snapshot.get("bpr_used_pct")) for snapshot in snapshots]
    positions = [item for item in positions if item is not None]
    bpr_pcts = [item for item in bpr_pcts if item is not None]
    return {
        "positions_count": int(max(positions)) if positions else "",
        "bpr_used_pct": round(max(bpr_pcts), 2) if bpr_pcts else "",
    }


def _no_plan_diagnosis(
    plans: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    portfolio_summary: dict[str, Any],
    events: list[dict[str, Any]],
) -> str:
    if plans or fills:
        return ""
    eligible = [candidate for candidate in candidates if not _candidate_rejection(candidate)]
    if not eligible:
        return ""
    skipped = [event for event in events if event.get("_event_type") == "daily_plan_write_skipped"]
    reasons = sorted({str(event.get("reason") or "") for event in skipped if event.get("reason")})
    positions = portfolio_summary.get("positions_count")
    bpr_pct = portfolio_summary.get("bpr_used_pct")
    parts = []
    if reasons:
        parts.append("sheet_skipped=" + "|".join(reasons))
    if positions != "":
        parts.append(f"portfolio_positions={positions}")
    if bpr_pct != "":
        parts.append(f"bpr_used_pct={bpr_pct}")
    parts.append(f"eligible_candidates={len(eligible)}")
    if positions not in ("", 0, 0.0):
        parts.append("likely_position_capacity_or_portfolio_constraint")
    else:
        parts.append("inspect_plan_constraints")
    return "; ".join(parts)


def _idea_rows(context: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for day in context["dates"]:
        for idea in context["ideas"].get(day, []):
            rows.append(
                {
                    "date": day,
                    "idea_id": idea.get("idea_id") or idea.get("_id"),
                    "source": idea.get("source"),
                    "underlying": idea.get("underlying"),
                    "direction": idea.get("direction"),
                    "horizon_days": idea.get("horizon_days"),
                    "confidence": idea.get("confidence"),
                    "extraction_confidence": idea.get("extraction_confidence"),
                    "thesis_tags": "|".join(idea.get("thesis_tags") or []),
                    "mentioned_strategy": idea.get("mentioned_strategy") or idea.get("strategy_hint") or "",
                    "quote_evidence": idea.get("quote_evidence") or "",
                    "notes": idea.get("notes") or idea.get("extraction_notes") or "",
                    "machine_verdict": _idea_verdict(idea),
                    "suman_review": "",
                }
            )
    return rows


def _candidate_rows(context: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for day in context["dates"]:
        for candidate in context["candidates"].get(day, []):
            rejection = _candidate_rejection(candidate)
            greeks = candidate.get("greeks") or {}
            preflight = candidate.get("preflight") or {}
            rows.append(
                {
                    "date": day,
                    "plan_run_id": candidate.get("_plan_run_id"),
                    "candidate_id": candidate.get("candidate_id") or candidate.get("_id"),
                    "idea_id": candidate.get("idea_id"),
                    "underlying": candidate.get("underlying"),
                    "playbook_id": candidate.get("playbook_id"),
                    "structure": candidate.get("structure"),
                    "eligible": "yes" if not rejection else "no",
                    "rejection_reason": rejection,
                    "net_credit": candidate.get("net_credit"),
                    "estimated_bpr": candidate.get("estimated_bpr"),
                    "delta": greeks.get("delta"),
                    "theta": greeks.get("theta"),
                    "gamma": greeks.get("gamma"),
                    "liquidity_score": candidate.get("liquidity_score"),
                    "preflight_ok": preflight.get("ok") if preflight else "",
                    "machine_verdict": _candidate_verdict(candidate),
                    "suman_review": "",
                }
            )
    return rows


def _plan_rows(context: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for day in context["dates"]:
        for plan in context["plans"].get(day, []):
            candidates = plan.get("candidates") or []
            rows.append(
                {
                    "date": day,
                    "plan_run_id": plan.get("_plan_run_id"),
                    "plan_id": plan.get("plan_id") or plan.get("_id"),
                    "rank": plan.get("rank") or plan.get("_rank"),
                    "trade_count": len(candidates),
                    "score": plan.get("score"),
                    "bpr_utilization_pct": plan.get("bpr_utilization_pct"),
                    "buying_power_after": plan.get("buying_power_after"),
                    "candidate_summary": "; ".join(
                        f"{c.get('underlying')} {c.get('playbook_id')} {c.get('net_credit')}" for c in candidates
                    ),
                    "machine_verdict": _plan_verdict(plan),
                    "suman_review": "",
                }
            )
    return rows


def _shadow_outcome_rows(context: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for day in context["dates"]:
        for fill in context["fills"].get(day, []):
            rows.append(
                {
                    "date": day,
                    "fill_id": fill.get("_fill_id"),
                    "plan_run_id": fill.get("_plan_run_id"),
                    "idea_id": fill.get("_idea_id"),
                    "underlying": fill.get("_underlying"),
                    "playbook_id": fill.get("_playbook_id"),
                    "structure": fill.get("_structure"),
                    "status": fill.get("_status"),
                    "opened_at": fill.get("_opened_at"),
                    "closed_at": fill.get("_closed_at"),
                    "net_credit": fill.get("_net_credit"),
                    "estimated_bpr": fill.get("_estimated_bpr"),
                    "delta": fill.get("_delta"),
                    "theta": fill.get("_theta"),
                    "gamma": fill.get("_gamma"),
                    "close_reason": fill.get("_close_reason"),
                    "close_pnl": fill.get("_close_pnl"),
                    "machine_verdict": _shadow_verdict(fill),
                    "suman_review": "",
                }
            )
    return rows


def _markdown_verdict(context: dict[str, Any]) -> str:
    daily = _daily_summary_rows(context)
    ideas = _idea_rows(context)
    candidates = _candidate_rows(context)
    outcomes = _shadow_outcome_rows(context)
    low_conf = sum(1 for row in ideas if "weak_input" in row["machine_verdict"])
    eligible = sum(1 for row in candidates if row["eligible"] == "yes")
    rejected = len(candidates) - eligible
    opened = len(outcomes)
    closed = [row for row in outcomes if row["status"] == "closed"]
    closed_pnl = sum(float(row["close_pnl"] or 0.0) for row in closed)
    lines = [
        "# Kamandal Go-Live Audit",
        "",
        f"Selected dates: {', '.join(context['dates'])}",
        "",
        "## Verdict",
        "",
        _overall_verdict(len(ideas), low_conf, eligible, rejected, opened, closed_pnl),
        "",
        "## Daily Summary",
        "",
        "| Date | Plan runs | Plans | Auto-approved | Ideas | Candidates | Eligible | Shadow opened | Closed PnL | No-plan diagnosis | Verdict |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in daily:
        lines.append(
            f"| {row['date']} | {row['plan_runs']} | {row['plans']} | {row['auto_approved']} | {row['ideas']} | "
            f"{row['candidates']} | {row['eligible_candidates']} | {row['shadow_opened']} | {row['closed_pnl']} | "
            f"{row['no_plan_diagnosis']} | {row['machine_verdict']} |"
        )
    lines.extend(
        [
            "",
            "## Calibration Questions For Suman",
            "",
            "1. Do the `weak_input` rows match what you would personally ignore?",
            "2. Do the `good_plan_candidate` rows feel like trades you would want surfaced?",
            "3. Are the top rejection reasons real risk filters, missing playbooks, or over-strict plumbing?",
            "4. Would any opened shadow trade have been unacceptable live, even at one contract?",
            "",
            "## Files",
            "",
            "- `daily_summary.csv` gives the day-level read.",
            "- `ideas.csv` is the source-quality review surface.",
            "- `candidates.csv` is the planner/matcher review surface.",
            "- `plans.csv` is the plan-level review surface.",
            "- `shadow_outcomes.csv` is the management/PnL review surface.",
        ]
    )
    return "\n".join(lines) + "\n"


def _overall_verdict(idea_count: int, low_conf: int, eligible: int, rejected: int, opened: int, closed_pnl: float) -> str:
    if idea_count == 0:
        return "RED: no idea evidence for the sampled days; do not use this sample for go-live."
    low_ratio = low_conf / max(idea_count, 1)
    if opened == 0 and eligible == 0:
        return "YELLOW/RED: input exists, but the planner produced no live-like candidates. Review rejections before go-live."
    if low_ratio > 0.5:
        return "YELLOW: too many low-confidence ideas. Planner may be working, but source quality needs pruning."
    if eligible and opened:
        pnl_note = "positive" if closed_pnl >= 0 else "negative"
        return f"YELLOW/GREEN: pipeline produced eligible candidates and shadow positions. Closed PnL sample is {pnl_note}; review trade quality row by row before scaling."
    if rejected > eligible * 3:
        return "YELLOW: candidate rejection load is high. Good for safety, but inspect whether gates are blocking useful setups."
    return "YELLOW: enough evidence to review, not enough to declare go-live quality without human calibration."


def _daily_verdict(plans: list[dict[str, Any]], candidates: list[dict[str, Any]], fills: list[dict[str, Any]], rejection_counts: Counter[str]) -> str:
    eligible = sum(1 for candidate in candidates if not _candidate_rejection(candidate))
    if fills:
        return "planned_and_shadow_traded"
    if plans:
        return "planned_but_not_opened"
    if eligible:
        return "eligible_candidates_no_plan"
    if candidates:
        top = rejection_counts.most_common(1)[0][0] if rejection_counts else "unknown"
        return f"rejected_before_plan:{top}"
    return "no_planner_evidence"


def _idea_verdict(idea: dict[str, Any]) -> str:
    confidence = str(idea.get("extraction_confidence") or idea.get("confidence") or "").lower()
    direction = str(idea.get("direction") or "").lower()
    evidence = str(idea.get("quote_evidence") or "")
    tags = set(idea.get("thesis_tags") or [])
    if confidence == "low" or len(evidence.strip()) < 20:
        return "weak_input:low_confidence_or_sparse_evidence"
    if direction in {"", "unknown"}:
        return "weak_input:missing_direction"
    if direction == "neutral" and not ({"range_bound", "theta_harvest", "vol_contraction"} & tags):
        return "review_input:neutral_without_vol_or_range_tag"
    return "reviewable_input"


def _candidate_verdict(candidate: dict[str, Any]) -> str:
    rejection = _candidate_rejection(candidate)
    if rejection:
        if rejection.startswith("matched_") or "playbook" in rejection:
            return "coverage_review:" + rejection
        if "preflight" in rejection:
            return "data_or_broker_blocked:" + rejection
        return "risk_or_quality_rejected:" + rejection
    preflight = candidate.get("preflight") or {}
    if preflight and preflight.get("ok") is False:
        return "data_or_broker_blocked:preflight_failed"
    return "good_plan_candidate"


def _plan_verdict(plan: dict[str, Any]) -> str:
    candidates = plan.get("candidates") or []
    if not candidates:
        return "bad_plan:no_candidates"
    if len(candidates) > 5:
        return "review_plan:too_many_trades"
    return "review_plan:ranked_bundle"


def _shadow_verdict(fill: dict[str, Any]) -> str:
    if fill.get("_status") == "closed":
        pnl = float(fill.get("_close_pnl") or 0.0)
        return "closed_profit" if pnl >= 0 else "closed_loss"
    return "open_shadow_position_review"


def _candidate_rejection(candidate: dict[str, Any]) -> str:
    return str(candidate.get("rejection_reason") or "").strip()


def _count_events(events: list[dict[str, Any]], event_type: str) -> int:
    return sum(1 for event in events if event.get("_event_type") == event_type)


def _format_counter(counter: Counter[str], *, limit: int) -> str:
    return "; ".join(f"{key}:{value}" for key, value in counter.most_common(limit))


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _normalize_dates(dates: list[str]) -> list[str]:
    normalized = []
    for value in dates:
        day = _date_from_text(value)
        if not day:
            raise ValueError(f"invalid audit date: {value}")
        normalized.append(day)
    return sorted(dict.fromkeys(normalized))


def _parse_day(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def _date_ordinal(value: str) -> int:
    return _parse_day(value).toordinal()


def _date_from_run_id(run_id: str) -> str:
    match = re.search(r"run_(\d{4})(\d{2})(\d{2})T", run_id)
    if not match:
        return _date_from_text(run_id)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _date_from_text(text: str) -> str:
    match = DATE_RE.search(text)
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _infer_date_for_idea(_idea_id: str, dates: set[str], _ids: set[str]) -> str:
    return sorted(dates)[0] if len(dates) == 1 else ""


def _loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row and row[0])
