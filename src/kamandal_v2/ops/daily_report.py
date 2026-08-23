"""Kamandal intraday daily report — BHIKsha parity for options book.

Builds a date-scoped report from the local SQLite store and renders
JSON + Markdown + RYG (APP/LIVE/SHADOW) artifacts for Lathi Bus Telegram/Obsidian.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

from kamandal_v2.live.health import run_live_health


@dataclass(slots=True, frozen=True)
class DailyReportWriteResult:
    report: dict[str, Any]
    json_path: Path
    markdown_path: Path
    ryg_markdown_path: Path


def write_daily_report(
    db_path: str | Path,
    *,
    output_dir: str | Path,
    trading_date: date | str | None = None,
    config: dict[str, Any] | None = None,
) -> DailyReportWriteResult:
    report = build_daily_report(db_path, trading_date=trading_date, config=config)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    day = report["trading_date"]
    from kamandal_v2.strategy_lanes.reports import (
        write_csa_experiment_status,
        write_csa_scorecard,
        write_csa_weekly_economics,
    )

    strategy_report_dir = target_dir / "csa1"
    scorecard = write_csa_scorecard(
        db_path,
        output_dir=strategy_report_dir,
        trading_date=day,
    )
    economics = write_csa_weekly_economics(
        db_path,
        output_dir=strategy_report_dir,
        through_date=day,
    )
    experiment_status = write_csa_experiment_status(
        db_path,
        output_dir=strategy_report_dir,
        through_date=day,
    )
    report["strategy_evidence_artifacts"] = {
        "scorecard": str(scorecard.json_path),
        "weekly_economics": str(economics.json_path),
        "experiment_status": str(experiment_status.json_path),
    }
    json_path = target_dir / f"kamandal_daily_report_{day}.json"
    markdown_path = target_dir / f"kamandal_daily_report_{day}.md"
    ryg_markdown_path = target_dir / f"kamandal_daily_report_{day}_ryg.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_daily_report_markdown(report), encoding="utf-8")
    ryg_markdown_path.write_text(render_daily_report_ryg_markdown(report), encoding="utf-8")
    return DailyReportWriteResult(report=report, json_path=json_path, markdown_path=markdown_path, ryg_markdown_path=ryg_markdown_path)


def build_daily_report(
    db_path: str | Path,
    *,
    trading_date: date | str | None = None,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    day = _coerce_day(trading_date)
    path = Path(db_path)
    if not path.exists():
        return _empty_report(day)
    if config is None:
        try:
            from kamandal_v2.config import load_control

            config = load_control()
        except Exception:
            config = {}

    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        events = _load_day_events(conn, day)
        live_intents = _load_live_intents(conn, day)
        live_status = _load_live_status(conn, day)
        positions = _load_live_positions(conn)
        groups = _load_live_groups(conn)
        shadow_fills = _load_shadow_fills(conn, day)
        recon_issues = _load_recon_issues(conn)

    event_counts = Counter(e["event_type"] for e in events)
    live_health = _safe_live_health(path, config, now=now)
    portfolio_books = _portfolio_books(path)
    from kamandal_v2.strategy_lanes.reports import build_csa_scorecard

    csa_shadow = build_csa_scorecard(path, trading_date=day)

    # Idea freshness from DB + filesystem (best-effort)
    idea_freshness = _idea_freshness(path)
    # Sheet cache freshness
    sheet_freshness = _sheet_freshness()

    # Aggregate intent counts for today
    intent_by_status = Counter(i["status"] for i in live_intents)
    intent_by_type = Counter(i["intent_type"] for i in live_intents)
    status_by_status = Counter(s["status"] for s in live_status)

    # Advisory summary from events
    advisory_metrics = _advisory_metrics(events)

    trade_summary = {
        "live_open_groups": len([g for g in groups if g["status"] in ("open", "pending")]),
        "total_groups": len(groups),
        "live_positions": len(positions),
        "historical_group_rows": len(groups),
        "shadow_open": len([f for f in shadow_fills if f["status"] == "open"]),
        "shadow_closed_today": len([f for f in shadow_fills if f["status"] == "closed"]),
        "intents_today": len(live_intents),
        "intents_by_status": dict(intent_by_status),
        "intents_by_type": dict(intent_by_type),
        "fills_today": len([s for s in live_status if s["status"] == "FILLED"]),
        "blocked_preflight_failed_today": intent_by_status.get("blocked_preflight_failed", 0),
        "recon_retired": len([r for r in recon_issues if r["status"] == "retired"]),
        "recon_open": len([r for r in recon_issues if r["status"] not in ("retired", "resolved")]),
    }

    return {
        "trading_date": day.isoformat(),
        "db_path": str(path),
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "total_events": len(events),
        "event_type_counts": dict(sorted(event_counts.items())),
        "live_health": live_health,
        "portfolio_books": portfolio_books,
        "advisory": advisory_metrics,
        "trade_summary": trade_summary,
        "positions": positions[:20],
        "groups": groups[:20],
        "intents": live_intents[:30],
        "recon_issues": recon_issues[:10],
        "idea_freshness": idea_freshness,
        "sheet_freshness": sheet_freshness,
        "csa_shadow": csa_shadow,
        "status": _report_status(live_health, recon_issues, idea_freshness, csa_shadow=csa_shadow),
    }


def _coerce_day(value: date | str | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def _empty_report(day: date) -> dict[str, Any]:
    from kamandal_v2.strategy_lanes.reports import build_csa_scorecard

    return {
        "trading_date": day.isoformat(),
        "db_path": "",
        "generated_at": datetime.now(UTC).isoformat(),
        "total_events": 0,
        "event_type_counts": {},
        "live_health": {"overall": "NO_DATA", "reasons": []},
        "portfolio_books": {"live": {}, "shadow": {}},
        "advisory": {},
        "trade_summary": {},
        "positions": [],
        "groups": [],
        "intents": [],
        "recon_issues": [],
        "idea_freshness": {},
        "sheet_freshness": {},
        "csa_shadow": build_csa_scorecard("", trading_date=day),
        "status": {"level": "NO_DATA", "reason": "db_missing"},
    }


def _load_day_events(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT created_at, event_type, payload FROM events WHERE date(created_at)=? ORDER BY created_at",
        (day.isoformat(),),
    )
    out = []
    for row in cur:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            payload = {}
        out.append({"created_at": row["created_at"], "event_type": row["event_type"], "payload": payload})
    return out


def _load_live_intents(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(
            "SELECT ticket_hash, order_id, intent_type, status, created_at, payload FROM live_order_intents WHERE date(created_at)=? ORDER BY created_at DESC",
            (day.isoformat(),),
        )
    except sqlite3.OperationalError:
        return []
    out = []
    for row in cur:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            payload = {}
        out.append(
            {
                "ticket_hash": row["ticket_hash"],
                "order_id": row["order_id"],
                "intent_type": row["intent_type"],
                "status": row["status"],
                "created_at": row["created_at"],
                "underlying": payload.get("underlying") or payload.get("candidate", {}).get("underlying", ""),
                "payload": payload,
            }
        )
    return out


def _load_live_status(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(
            "SELECT ticket_hash, order_id, status, created_at FROM live_order_status WHERE date(created_at)=? ORDER BY created_at DESC",
            (day.isoformat(),),
        )
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in cur]


def _load_live_positions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(
            "SELECT group_id, underlying, status, payload FROM live_positions "
            "WHERE lower(status) IN ('open','pending') ORDER BY group_id"
        )
    except sqlite3.OperationalError:
        return []
    out = []
    for row in cur:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            payload = {}
        out.append({"group_id": row["group_id"], "underlying": row["underlying"], "status": row["status"], "candidate_id": payload.get("candidate", {}).get("candidate_id", "")})
    return out


def _load_live_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        cur = conn.execute("SELECT group_id, status, payload FROM live_position_groups ORDER BY group_id")
    except sqlite3.OperationalError:
        return []
    out = []
    for row in cur:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            payload = {}
        underlying = payload.get("underlying") or ""
        if not underlying and payload.get("candidate"):
            underlying = payload["candidate"].get("underlying", "")
        out.append({"group_id": row["group_id"], "status": row["status"], "underlying": underlying})
    return out


def _load_shadow_fills(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    try:
        cur = conn.execute("SELECT id, status, underlying FROM shadow_fills WHERE date(opened_at)=? OR date(closed_at)=?", (day.isoformat(), day.isoformat()))
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in cur]


def _load_recon_issues(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        cur = conn.execute("SELECT issue_id, issue_type, underlying, status FROM live_reconciliation_issues ORDER BY last_seen_at DESC LIMIT 20")
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in cur]


def _safe_live_health(db_path: Path, config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    try:
        from kamandal_v2.stores.sqlite import LocalStore

        store = LocalStore(sqlite_path=db_path, read_only=True)
        report = (
            run_live_health(store, config, now=now, allow_mutation=False)
            if now
            else run_live_health(store, config, allow_mutation=False)
        )
        return report
    except Exception as exc:
        return {"overall": "NO_DATA", "reasons": ["live_health_failed"], "error": str(exc), "counts": {}}


def _portfolio_books(db_path: Path) -> dict[str, dict[str, Any]]:
    from kamandal_v2.stores.sqlite import LocalStore

    store = LocalStore(sqlite_path=db_path, read_only=True)
    books: dict[str, dict[str, Any]] = {}
    for mode in ("live", "shadow"):
        snapshot = store.latest_account_snapshot(mode=mode)
        if not snapshot:
            books[mode] = {"snapshot_id": "", "account_size": None, "bpr_used": None, "bpr_used_pct": None}
            continue
        account_size = _optional_number(snapshot.get("account_size"))
        bpr_used = _optional_number(snapshot.get("bpr_used"))
        bpr_pct = _optional_number(snapshot.get("bpr_used_pct"))
        if bpr_pct is None and account_size and bpr_used is not None:
            bpr_pct = bpr_used / account_size * 100.0
        books[mode] = {
            "snapshot_id": str(snapshot.get("_snapshot_id") or ""),
            "account_size": account_size,
            "bpr_used": bpr_used,
            "bpr_used_pct": round(bpr_pct, 2) if bpr_pct is not None else None,
        }
    return books


def _optional_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _idea_freshness(db_path: Path) -> dict[str, Any]:
    # Count active idea files + their mtime
    try:
        from kamandal_v2.paths import resolve_path

        ideas_dir = resolve_path("data/ideas/active")
        if not ideas_dir.exists():
            ideas_dir = db_path.parent / "ideas" / "active"
        files = list(ideas_dir.glob("*.yaml"))
        return {"active_files": len(files), "dir": str(ideas_dir)}
    except Exception:
        return {"active_files": 0}


def _sheet_freshness() -> dict[str, Any]:
    try:
        from kamandal_v2.paths import resolve_path

        cache = resolve_path("data/sheet_cache/latest_pull.json")
        if cache.exists():
            import time

            age_hours = (datetime.now().timestamp() - cache.stat().st_mtime) / 3600
            return {
                "exists": True,
                "age_hours": round(age_hours, 1),
                "path": str(cache),
                "role": "manual_export_cache",
                "health_authoritative": False,
            }
        return {"exists": False, "role": "manual_export_cache", "health_authoritative": False}
    except Exception:
        return {"exists": False}


def _advisory_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    # Summarize last advisory run in today's events
    advisory = [e for e in events if e["event_type"] in ("plan_run_completed", "live_advisory_plan_completed")]
    if not advisory:
        # Also try launchd observation payloads stored as events
        return {"runs": 0}
    last = advisory[-1]["payload"] if advisory else {}
    return {"runs": len(advisory), "last": last}


def _report_status(
    live_health: dict[str, Any],
    recon_issues: list[dict[str, Any]],
    idea_freshness: dict[str, Any],
    *,
    csa_shadow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    overall = str(live_health.get("overall", "UNKNOWN")).upper()
    recon_open = [item for item in recon_issues if item.get("status") not in ("retired", "resolved")]
    idea_count = int(idea_freshness.get("active_files") or 0)
    components = {
        "live": overall,
        "reconciliation": "RED" if recon_open else "GREEN",
        "ideas": "GREEN" if idea_count > 0 else "YELLOW",
    }
    order = {"NO_DATA": 4, "RED": 3, "YELLOW": 2, "UNKNOWN": 1, "GREEN": 0}
    level = max(components.values(), key=lambda item: order.get(item, 1))
    reasons: list[str] = []
    if overall not in {"GREEN", ""}:
        reasons.extend(str(reason) for reason in live_health.get("reasons", [])[:2])
        if not reasons:
            reasons.append("health_" + overall.lower())
    if recon_open:
        reasons.append("open_reconciliation_issues")
    if idea_count == 0:
        reasons.append("no_active_idea_files")
    shadow = csa_shadow or {}
    return {
        "level": level,
        "reason": ",".join(reasons) or "ok",
        "components": components,
        "domains": {
            "current_live_operations": overall,
            "current_shadow_runtime": str(shadow.get("runtime_status") or "NO_DATA"),
            "accumulated_shadow_evidence": str(shadow.get("evidence_status") or "NO_DATA"),
        },
    }


# -- Rendering ---------------------------------------------------------------

def render_daily_report_markdown(report: dict[str, Any]) -> str:
    status = report.get("status") or {}
    summary = report.get("trade_summary") or {}
    health = report.get("live_health") or {}
    advisory = report.get("advisory") or {}
    books = report.get("portfolio_books") or {}
    live_book = books.get("live") or {}
    shadow_book = books.get("shadow") or {}
    lines = [
        f"# Kamandal Daily — {report.get('trading_date')}",
        "",
        f"- status: `{status.get('level','UNKNOWN')}` reason: `{status.get('reason','')}`",
        f"- health: `{health.get('overall','UNKNOWN')}` reasons: `{','.join(health.get('reasons',[])[:4])}`",
        f"- live BPR: `{live_book.get('bpr_used_pct')}` shadow BPR: `{shadow_book.get('bpr_used_pct')}`",
        f"- open groups: `{summary.get('live_open_groups',0)}/{summary.get('total_groups',0)}` positions: `{summary.get('live_positions',0)}`",
        f"- intents today: `{summary.get('intents_today',0)}` by_status `{summary.get('intents_by_status',{})}`",
        f"- fills today: `{summary.get('fills_today',0)}` blocked_preflight_failed: `{summary.get('blocked_preflight_failed_today',0)}`",
        f"- advisory runs: `{advisory.get('runs',0)}`",
        f"- recon open: `{summary.get('recon_open',0)}` retired: `{summary.get('recon_retired',0)}`",
        f"- ideas active files: `{(report.get('idea_freshness') or {}).get('active_files',0)}`",
    ]
    if health.get("counts"):
        lines.append(f"- health counts: `{health['counts']}`")
    if report.get("event_type_counts"):
        lines.append(f"- events: `{report['event_type_counts']}`")
    lines.append(f"- generated_at: `{report.get('generated_at','')}`")

    groups = report.get("groups") or []
    if groups:
        lines.extend(["", "## Open Groups (sample)", "", "| Group | Underlying | Status |", "|---|---|---|"])
        for g in groups[:10]:
            lines.append(f"| `{g['group_id'][:12]}` | {g['underlying']} | {g['status']} |")

    intents = report.get("intents") or []
    if intents:
        lines.extend(["", "## Intents Today", "", "| Time | Type | Underlying | Status | Ticket |", "|---|---|---|---|---|"])
        for i in intents[:15]:
            t = i.get("created_at","")[:16]
            lines.append(f"| {t} | {i['intent_type']} | {i['underlying']} | {i['status']} | `{i['ticket_hash'][:8]}` |")

    recon = report.get("recon_issues") or []
    if recon:
        lines.extend(["", "## Reconciliation", "", "| Issue | Underlying | Status |", "|---|---|---|"])
        for r in recon[:8]:
            lines.append(f"| {r['issue_type']} | {r['underlying']} | {r['status']} |")
    return "\n".join(lines) + "\n"


def _build_ryg_tables(report: dict[str, Any]) -> dict[str, list[tuple[str, str, str, str]]]:
    status = report.get("status") or {}
    health = report.get("live_health") or {}
    summary = report.get("trade_summary") or {}
    level = status.get("level","UNKNOWN")
    health_level = str(health.get("overall","UNKNOWN")).upper()
    counts = health.get("counts") or {}
    freshness = report.get("idea_freshness") or {}
    sheet = report.get("sheet_freshness") or {}
    books = report.get("portfolio_books") or {}
    live_book = books.get("live") or {}
    shadow_book = books.get("shadow") or {}

    def ryg_for_level(lvl: str) -> str:
        if lvl == "GREEN": return "🟢"
        if lvl == "YELLOW": return "🟡"
        if lvl == "RED": return "🔴"
        return "⚪"

    # APP: scheduler + sheet + ideas freshness
    app_rows: list[tuple[str, str, str, str]] = []
    sheet_age = sheet.get("age_hours")
    app_rows.append((
        "Manual Sheet export",
        f"{'exists' if sheet.get('exists') else 'missing'} {f'({sheet_age}h ago)' if sheet_age is not None else ''}",
        "⚪",
        "informational cache; not runtime Sheet health",
    ))
    app_rows.append(("Ideas active", str(freshness.get("active_files",0)), "🟢" if freshness.get("active_files",0) > 0 else "🟡", "intelligence pipeline"))
    app_rows.append(("Events today", str(report.get("total_events",0)), "🟢", "db events"))
    app_rows.append(("Health probe", health_level, ryg_for_level(health_level), ",".join(health.get("reasons",[])[:2])))

    # LIVE: book + exits + reconciliation
    live_rows: list[tuple[str, str, str, str]] = []
    live_rows.append(("Health", health_level, ryg_for_level(health_level), ",".join(health.get("reasons",[])[:2]) or "ok"))
    live_rows.append(("Open groups", f"{summary.get('live_open_groups',0)}/{summary.get('total_groups',0)}", ryg_for_level(level), "open/total"))
    live_rows.append(("Intents today", str(summary.get("intents_today",0)), "🟢", str(summary.get("intents_by_status",{}))))
    blocked = summary.get("blocked_preflight_failed_today",0)
    live_rows.append(("Blocked preflight", str(blocked), "🟡" if blocked>0 else "🟢", "code 157 duplicates" if blocked else "none"))
    live_rows.append(("Fills today", str(summary.get("fills_today",0)), "🟢", "FILLED"))
    live_rows.append(("Recon open", str(summary.get("recon_open",0)), "🔴" if summary.get("recon_open",0)>0 else "🟢", "needs attention" if summary.get("recon_open",0)>0 else "clean"))
    live_bpr = live_book.get("bpr_used_pct")
    live_rows.append(("Live BPR", str(live_bpr) if live_bpr is not None else "missing", ryg_for_level(health_level), "live-scoped account snapshot"))

    # SHADOW: placeholder until shadow_eod wired
    shadow_rows: list[tuple[str, str, str, str]] = []
    shadow = report.get("csa_shadow") or {}
    shadow_runtime = str(shadow.get("runtime_status") or "NO_DATA")
    shadow_evidence = str(shadow.get("evidence_status") or "NO_DATA")
    recovered = int(shadow.get("recovered_run_error_count") or 0)
    shadow_rows.append(("Current runtime", shadow_runtime, ryg_for_level(shadow_runtime), f"active errors={len(shadow.get('active_run_errors') or [])}"))
    shadow_rows.append(("Day evidence", shadow_evidence, ryg_for_level(shadow_evidence), f"recovered errors={recovered}"))
    shadow_rows.append(("Shadow open", str(summary.get("shadow_open",0)), "🟢", "shadow_fills open"))
    shadow_rows.append(("Shadow closed today", str(summary.get("shadow_closed_today",0)), "🟢", "shadow_fills closed"))
    shadow_rows.append(("Advisory runs", str((report.get("advisory") or {}).get("runs",0)), "🟢", "plan_run events"))
    shadow_bpr = shadow_book.get("bpr_used_pct")
    shadow_rows.append(("Shadow BPR", str(shadow_bpr) if shadow_bpr is not None else "missing", "⚪" if shadow_bpr is None else "🟢", "shadow-scoped paper snapshot"))

    return {"app": app_rows, "live": live_rows, "shadow": shadow_rows}


def render_daily_report_ryg_markdown(report: dict[str, Any]) -> str:
    tables = _build_ryg_tables(report)
    day = report.get("trading_date") or ""
    lines = [f"# Kamandal RYG — {day}", ""]
    for title, key in [("APP", "app"), ("LIVE", "live"), ("SHADOW", "shadow")]:
        lines.extend([f"## {title}", "", "| Metric | Value | Status | Why |", "|---|---|---|---|"])
        for metric, value, status, why in tables[key]:
            lines.append(f"| {metric} | {value} | {status} | {why} |")
        lines.append("")
    return "\n".join(lines)


def render_daily_report_ryg_telegram_html(report: dict[str, Any]) -> str:
    """Telegram HTML — mobile-friendly, no <pre> tables.

    Telegram mobile wraps ~40 monospace chars (openclaw#36323); the old
    fixed-width <pre> table at 60 chars wrapped and truncated values.
    Per https://core.telegram.org/bots/api#html-style (allowed tags
    <b><i><code><pre><a> etc., no nesting), we use inline formatting:
      🟢 <b>Metric</b>: <code>value</code> — <i>why</i>
    so lines wrap naturally and parse_mode HTML (template=status) renders
    bold/code/italic correctly via lathi-bus _render_html_message.
    The outer title is supplied via send_lathi_alert(title=...), so the
    body does NOT repeat the day header.
    """

    from html import escape

    tables = _build_ryg_tables(report)
    lines: list[str] = []
    for section, key in [("APP", "app"), ("LIVE", "live"), ("SHADOW", "shadow")]:
        lines.append(f"<b>{section}</b>")
        for metric, value, status, why in tables[key]:
            # Keep full values (no truncation) — Telegram limit is 4096, not 60.
            metric_html = escape(metric)
            value_html = escape(value) if value else "—"
            why_html = escape(why) if why else ""
            # status is an emoji (🟢/🟡/🔴/⚪), not HTML; keep as-is.
            row = f"{status} <b>{metric_html}</b>: <code>{value_html}</code>"
            if why_html:
                row += f" — <i>{why_html}</i>"
            lines.append(row)
        lines.append("")  # blank line between sections
    # Footer: health reasons are already in LIVE rows; keep one-line hint only if useful.
    health = report.get("live_health") or {}
    reasons = [escape(r) for r in (health.get("reasons") or [])[:3] if r]
    if reasons:
        lines.append(f"<i>health: {','.join(reasons)}</i>")
    return "\n".join(lines).strip() + "\n"


def render_daily_report_ryg_telegram_text(report: dict[str, Any]) -> str:
    tables = _build_ryg_tables(report)
    day = report.get("trading_date") or ""
    lines = [f"Kamandal RYG — {day}", ""]
    for title, key in [("APP", "app"), ("LIVE", "live"), ("SHADOW", "shadow")]:
        lines.append(title)
        lines.append(f"{'Metric':<18} {'Value':<14} Why")
        lines.append("-" * 50)
        for metric, value, status, why in tables[key]:
            lines.append(f"{metric:<18} {value:<14} {status} {why[:30]}")
        lines.append("")
    return "\n".join(lines)
