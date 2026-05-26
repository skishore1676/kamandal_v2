"""Shadow portfolio marking, management, and reporting."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import Playbook, utc_now
from kamandal_v2.events.earnings import EarningsStore
from kamandal_v2.live.position_management import live_exit_decision, mark_live_group
from kamandal_v2.paths import resolve_path
from kamandal_v2.planner.config_loader import load_planner_config
from kamandal_v2.stores.sqlite import LocalStore


@dataclass(slots=True)
class ShadowManagementResult:
    mark: dict[str, Any]
    decisions: list[dict[str, Any]]
    closed_count: int
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "mark": self.mark,
            "decisions": list(self.decisions),
            "closed_count": self.closed_count,
            "dry_run": self.dry_run,
        }


def mark_shadow_portfolio(
    store: LocalStore | None = None,
    *,
    playbook_by_id: dict[str, Playbook] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = store or LocalStore()
    sqlite_path = store.sqlite_path
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        fills = conn.execute(
            """
            SELECT id, opened_at, plan_run_id, plan_id, candidate_id, idea_id, underlying, playbook_id,
                   structure, net_credit, estimated_bpr, delta, gamma, theta, vega, payload
            FROM shadow_fills
            WHERE status = 'open'
            ORDER BY opened_at
            """
        ).fetchall()
        chain_by_underlying = _latest_chain_quote_maps(conn, {str(fill["underlying"]) for fill in fills})
    finally:
        conn.close()

    rows = []
    total_entry = 0.0
    total_mid_pnl = 0.0
    total_natural_pnl = 0.0
    shadow_config = _shadow_exit_config(config or {})
    for fill in fills:
        payload = json.loads(fill["payload"] or "{}")
        legs = payload.get("legs") or []
        chain = chain_by_underlying.get(str(fill["underlying"]))
        captured_at, underlying_price = (chain[0], chain[1]) if chain is not None else ("", None)
        quote_map = chain[2] if chain is not None else {}
        group = _shadow_fill_group(fill, payload, legs)
        playbook = (playbook_by_id or {}).get(str(group.get("playbook_id") or ""))
        live_mark = mark_live_group(group, quote_map, playbook, quote_fresh=chain is not None, config=shadow_config)
        entry_net = float(live_mark.get("entry_net_cashflow") or 0.0)
        mid_pnl = float(live_mark.get("pnl_mid") or 0.0)
        natural_pnl = float(live_mark.get("pnl_natural") or 0.0)
        total_entry += entry_net
        total_mid_pnl += mid_pnl
        total_natural_pnl += natural_pnl
        rows.append({
            "fill_id": fill["id"],
            "group_id": live_mark.get("group_id"),
            "opened_at": fill["opened_at"],
            "underlying": fill["underlying"],
            "structure": fill["structure"],
            "idea_id": fill["idea_id"] or payload.get("idea_id"),
            "playbook_id": fill["playbook_id"] or payload.get("playbook_id"),
            "estimated_bpr": fill["estimated_bpr"] if fill["estimated_bpr"] is not None else payload.get("estimated_bpr"),
            "entry_credit": round(entry_net, 2),
            "entry_net_cashflow": live_mark.get("entry_net_cashflow"),
            "entry_value": live_mark.get("entry_value"),
            "entry_kind": live_mark.get("entry_kind"),
            "close_mid_net": live_mark.get("close_mid_net"),
            "close_natural_net": live_mark.get("close_natural_net"),
            "mid_pnl": round(mid_pnl, 2),
            "natural_pnl": round(natural_pnl, 2),
            "pnl_pct_of_credit": live_mark.get("pnl_pct_of_entry_value"),
            "pnl_pct_of_entry_value": live_mark.get("pnl_pct_of_entry_value"),
            "target_profit": live_mark.get("target_profit"),
            "target_close_net": live_mark.get("target_close_net"),
            "target_close_value": live_mark.get("target_close_value"),
            "target_progress_pct": live_mark.get("target_progress_pct"),
            "profit_target_pct": live_mark.get("profit_target_pct"),
            "trigger_progress_pct": live_mark.get("trigger_progress_pct"),
            "quote_fresh": live_mark.get("quote_fresh"),
            "mark_time": captured_at,
            "underlying_price": underlying_price,
            "missing_quotes": live_mark.get("missing_quotes") or [],
            "legs": legs,
            "marked_legs": live_mark.get("marked_legs") or [],
            "dte": live_mark.get("dte") or {},
            "mark": live_mark,
        })
    mark = {
        "mark_id": "shadow_mark_" + utc_now().replace(":", "").replace("-", ""),
        "marked_at": utc_now(),
        "position_count": len(rows),
        "total_entry_credit": round(total_entry, 2),
        "total_entry_net_cashflow": round(total_entry, 2),
        "total_mid_pnl": round(total_mid_pnl, 2),
        "total_natural_pnl": round(total_natural_pnl, 2),
        "rows": rows,
    }
    store.save_shadow_mark(str(mark["mark_id"]), mark)
    store.event("shadow_portfolio_marked", {
        "mark_id": mark["mark_id"],
        "position_count": mark["position_count"],
        "total_mid_pnl": mark["total_mid_pnl"],
        "total_natural_pnl": mark["total_natural_pnl"],
    })
    return mark


def manage_shadow_positions(
    config: dict[str, Any],
    *,
    config_source: str = "sheet",
    dry_run: bool = False,
    store: LocalStore | None = None,
) -> ShadowManagementResult:
    store = store or LocalStore()
    _universe, playbooks = load_planner_config(config, source=config_source)
    playbook_by_id = {playbook.playbook_id: playbook for playbook in playbooks}
    earnings = EarningsStore()
    mark = mark_shadow_portfolio(store, playbook_by_id=playbook_by_id, config=config)
    decisions = [_shadow_decision_for_mark_row(row, playbook_by_id, earnings, config) for row in mark.get("rows") or []]
    closed = [decision for decision in decisions if decision["action"] == "close"]
    if not dry_run:
        for decision in closed:
            store.close_shadow_fill(
                str(decision["fill_id"]),
                reason=str(decision["reason"]),
                pnl=float(decision["mid_pnl"] or 0.0),
                payload=decision,
            )
            store.event("shadow_position_closed", decision)
    store.event("shadow_management_evaluated", {
        "mark_id": mark["mark_id"],
        "decisions": len(decisions),
        "closed_count": len(closed) if not dry_run else 0,
        "close_recommendations": len(closed),
        "dry_run": dry_run,
    })
    return ShadowManagementResult(mark=mark, decisions=decisions, closed_count=0 if dry_run else len(closed), dry_run=dry_run)


def write_shadow_eod_report(
    config: dict[str, Any],
    *,
    config_source: str = "sheet",
    output_dir: str | Path = "data/reports/eod",
    store: LocalStore | None = None,
) -> dict[str, Any]:
    result = manage_shadow_positions(config, config_source=config_source, dry_run=True, store=store)
    output_root = resolve_path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    report_date = date.today().isoformat()
    json_path = output_root / f"{report_date}_shadow_eod.json"
    md_path = output_root / f"{report_date}_shadow_eod.md"
    payload = result.to_dict()
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_report_markdown(payload), encoding="utf-8")
    (store or LocalStore()).event("shadow_eod_report_written", {"json_path": str(json_path), "markdown_path": str(md_path)})
    return {"json_path": str(json_path), "markdown_path": str(md_path), **payload}


def _shadow_decision_for_mark_row(
    row: dict[str, Any],
    playbook_by_id: dict[str, Playbook],
    earnings: EarningsStore,
    config: dict[str, Any],
) -> dict[str, Any]:
    playbook = playbook_by_id.get(str(row.get("playbook_id") or ""))
    decision = live_exit_decision(row.get("mark") or row, playbook, earnings, _shadow_exit_config(config))
    return {
        **decision,
        "fill_id": row.get("fill_id"),
        "entry_credit": row.get("entry_credit"),
        "pnl_pct_of_credit": row.get("pnl_pct_of_credit"),
        "mark_time": row.get("mark_time"),
    }


def _shadow_fill_group(fill: sqlite3.Row, payload: dict[str, Any], legs: list[dict[str, Any]]) -> dict[str, Any]:
    net_credit = fill["net_credit"] if fill["net_credit"] is not None else payload.get("net_credit")
    candidate = {
        **payload,
        "candidate_id": fill["candidate_id"] or payload.get("candidate_id"),
        "idea_id": fill["idea_id"] or payload.get("idea_id"),
        "underlying": fill["underlying"] or payload.get("underlying"),
        "playbook_id": fill["playbook_id"] or payload.get("playbook_id"),
        "structure": fill["structure"] or payload.get("structure"),
        "net_credit": float(net_credit or 0.0),
        "legs": legs,
    }
    return {
        "group_id": fill["id"],
        "fill_id": fill["id"],
        "underlying": fill["underlying"] or payload.get("underlying"),
        "playbook_id": fill["playbook_id"] or payload.get("playbook_id"),
        "structure": fill["structure"] or payload.get("structure"),
        "opened_at": fill["opened_at"],
        "candidate": candidate,
    }


def _shadow_exit_config(config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config or {})
    live_cfg = dict(result.get("live") or {})
    exit_pricing = dict(live_cfg.get("exit_pricing") or {})
    exit_pricing["require_fresh_quotes"] = False
    live_cfg["exit_pricing"] = exit_pricing
    result["live"] = live_cfg
    return result


def _latest_chain_quote_maps(conn: sqlite3.Connection, underlyings: set[str]) -> dict[str, tuple[str, float | None, dict[tuple[str, str, float], dict]]]:
    result = {}
    for underlying in underlyings:
        rows = conn.execute("SELECT payload FROM chain_snapshots WHERE underlying = ?", (underlying,)).fetchall()
        if not rows:
            continue
        payloads = [json.loads(row["payload"]) for row in rows]
        payloads.sort(key=lambda item: str(item.get("captured_at") or ""))
        latest = payloads[-1]
        quote_map = {}
        for quote in latest.get("quotes") or []:
            key = (str(quote.get("expiration")), str(quote.get("option_type")), float(quote.get("strike") or 0.0))
            quote_map[key] = quote
        result[underlying] = (str(latest.get("captured_at") or ""), latest.get("underlying_price"), quote_map)
    return result


def _report_markdown(payload: dict[str, Any]) -> str:
    mark = payload.get("mark") or {}
    decisions = payload.get("decisions") or []
    closes = [item for item in decisions if item.get("action") == "close"]
    lines = [
        f"# Shadow EOD Report {date.today().isoformat()}",
        "",
        f"- positions: {mark.get('position_count', 0)}",
        f"- entry_credit: {mark.get('total_entry_credit', 0)}",
        f"- mid_pnl: {mark.get('total_mid_pnl', 0)}",
        f"- natural_pnl: {mark.get('total_natural_pnl', 0)}",
        f"- close_recommendations: {len(closes)}",
        "",
        "## Close Candidates",
    ]
    if not closes:
        lines.append("- none")
    for item in closes:
        lines.append(
            f"- {item.get('underlying')} {item.get('structure')} {item.get('playbook_id')} "
            f"reason={item.get('reason')} mid_pnl={item.get('mid_pnl')} pnl_pct={item.get('pnl_pct_of_credit')}"
        )
    lines.extend(["", "## Holds"])
    for item in decisions:
        if item.get("action") == "hold":
            lines.append(
                f"- {item.get('underlying')} {item.get('structure')} reason={item.get('reason')} "
                f"mid_pnl={item.get('mid_pnl')} dte={item.get('dte_remaining')}"
            )
    return "\n".join(lines).strip() + "\n"
