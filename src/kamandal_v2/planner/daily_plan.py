"""Render plan rows for the daily_plan sheet."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from kamandal_v2.domain.models import Plan
from kamandal_v2.schemas import DAILY_PLAN_HEADER


def render_daily_plan_rows(plans: list[Plan], *, mode: str) -> list[list[Any]]:
    return [_render_plan(plan, mode=mode) for plan in plans]


def _render_plan(plan: Plan, *, mode: str) -> list[Any]:
    before = plan.portfolio_before.greeks
    after = plan.portfolio_after.greeks
    change = plan.greeks_change
    metrics = {
        "before": before.to_dict(),
        "after": after.to_dict(),
        "change": change.to_dict(),
        "total_bpr": plan.total_bpr,
        "bpr_utilization_pct": plan.bpr_utilization_pct,
        "buying_power_after": plan.buying_power_after,
    }
    bundle = [
        {
            "candidate_id": candidate.candidate_id,
            "idea_id": candidate.idea_id,
            "underlying": candidate.underlying,
            "playbook_id": candidate.playbook_id,
            "structure": candidate.structure,
            "legs": [leg.to_dict() for leg in candidate.legs],
            "net_credit": candidate.net_credit,
            "estimated_bpr": candidate.estimated_bpr,
            "greeks": candidate.greeks.to_dict(),
            "reasons": candidate.reasons,
        }
        for candidate in plan.candidates
    ]
    row_by_name = {
        "plan_date": date.today().isoformat(),
        "plan_rank": plan.plan_rank,
        "plan_id": plan.plan_id,
        "plan_status": plan.status,
        "plan_trade_count": len(plan.candidates),
        "plan_score": plan.score,
        "plan_summary": f"{len(plan.candidates)} trades, BPR {plan.total_bpr:.0f}, theta {change.theta:.2f}, delta {change.delta:.2f}",
        "trade_bundle": plan.trade_bundle,
        "trade_bundle_json": _json(bundle),
        "plan_total_bpr": plan.total_bpr,
        "plan_bpr_utilization_pct": plan.bpr_utilization_pct,
        "buying_power_after": plan.buying_power_after,
        "portfolio_delta_before": before.delta,
        "portfolio_delta_after": after.delta,
        "portfolio_delta_change": change.delta,
        "portfolio_gamma_before": before.gamma,
        "portfolio_gamma_after": after.gamma,
        "portfolio_gamma_change": change.gamma,
        "portfolio_theta_before": before.theta,
        "portfolio_theta_after": after.theta,
        "portfolio_theta_change": change.theta,
        "portfolio_vega_before": before.vega,
        "portfolio_vega_after": after.vega,
        "portfolio_vega_change": change.vega,
        "mode": mode,
        "plan_reasons": "; ".join(plan.reasons),
        "blocked_by": "; ".join(plan.blocked_by),
        "plan_metrics_json": _json(metrics),
        "plan_detail_json": _json(plan.to_dict()),
        "operator_action": plan.operator_action,
        "operator_notes": "",
    }
    return [row_by_name.get(name, "") for name in DAILY_PLAN_HEADER]


def _json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))

