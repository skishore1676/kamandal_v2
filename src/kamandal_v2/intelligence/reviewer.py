"""LLM reviewer for rejected ideas and candidate failures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from kamandal_v2.intelligence.llm_client import JsonLlmClient, build_llm_client
from kamandal_v2.intelligence.transcripts import CONTROLLED_THESIS_TAGS
from kamandal_v2.planner.config_loader import load_planner_config
from kamandal_v2.paths import resolve_path


@dataclass(slots=True)
class ReviewResult:
    json_path: Path
    markdown_path: Path

    def to_dict(self) -> dict[str, str]:
        return {"json_path": str(self.json_path), "markdown_path": str(self.markdown_path)}


def review_rejections(
    config: dict[str, Any],
    *,
    latest_run: str | Path = "data/audit/latest_plan_run.json",
    ideas_path: str | Path | None = None,
    output_dir: str | Path = "data/reviews",
    client: JsonLlmClient | None = None,
) -> ReviewResult:
    client = client or build_llm_client(config, actor="rejection_reviewer")
    latest_file = resolve_path(latest_run)
    payload = json.loads(latest_file.read_text(encoding="utf-8"))
    ideas_text = ""
    if ideas_path is not None:
        path = resolve_path(ideas_path)
        if path.exists():
            ideas_text = path.read_text(encoding="utf-8")[:12000]
    rejected = [
        candidate for candidate in payload.get("candidates", [])
        if candidate.get("rejection_reason")
    ]
    source = str((config.get("reviewer") or {}).get("config_source") or "sheet")
    universe, playbooks = load_planner_config(config, source=source)
    enabled_playbooks = [playbook for playbook in playbooks if playbook.enabled]
    review = client.chat_json(
        _review_system_prompt(),
        _review_user_prompt(payload, rejected, ideas_text, universe, enabled_playbooks),
    )
    review = _sanitize_review(review, {playbook.playbook_id for playbook in enabled_playbooks})
    output_path = resolve_path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    json_path = output_path / f"{stamp}_rejection_review.json"
    markdown_path = output_path / f"{stamp}_rejection_review.md"
    json_path.write_text(json.dumps(review, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_review_markdown(review), encoding="utf-8")
    return ReviewResult(json_path=json_path, markdown_path=markdown_path)


def _sanitize_review(review: dict[str, Any], enabled_playbook_ids: set[str]) -> dict[str, Any]:
    sanitized = dict(review)
    strategy_items = []
    human_queue = list(sanitized.get("human_review_queue") or [])
    for item in sanitized.get("strategy_within_bounds") or []:
        if not isinstance(item, dict):
            continue
        playbook_id = str(item.get("playbook_id") or "")
        if playbook_id in enabled_playbook_ids:
            strategy_items.append(item)
        else:
            human_queue.append({
                "type": "new_playbook",
                "proposal": item.get("suggestion") or item,
                "reason": item.get("reason") or f"Unknown or disabled playbook_id: {playbook_id}",
            })
    sanitized["strategy_within_bounds"] = strategy_items
    sanitized["human_review_queue"] = human_queue
    sanitized.pop("tag_suggestions", None)
    sanitized.pop("playbook_suggestions", None)
    sanitized.pop("risk_filter_observations", None)
    return sanitized


def _review_system_prompt() -> str:
    return """\
You review Kamandal rejected ideas and candidate failures.

You review within existing system boundaries. You may recommend human review, but you may not invent
new thesis tags, symbol-specific playbooks, or execution rules as if they already exist.

Separate observations into:
- engine_improvements: deterministic planner, filtering, preflight, audit, or scoring changes.
- intelligence_improvements: extraction quality, confidence, evidence, ambiguity, transcript cleaning.
- strategy_within_bounds: suggestions using existing enabled playbooks/tags/universe controls only.
- human_review_queue: proposals outside current bounds. These are research items, not changes to apply.

Do not put meta-flags like liquidity_constrained, needs_confirmation, direction_ambiguous, short_horizon,
or poor_evidence into thesis tag suggestions. Those belong in intelligence_improvements or human_review_queue.
Only use controlled thesis tags provided in the prompt.
Do not suggest symbol-specific strategy variants or universe strategy restrictions.
The universe controls symbol enablement only; prefer evidence about strategy criteria and liquidity.
You must not approve trades, submit orders, or silently mutate execution rules.

Return JSON only:
{
  "summary": "short review",
  "engine_improvements": [{"area":"planner|filters|preflight|audit|scoring","observation":"...","suggestion":"...","reason":"..."}],
  "intelligence_improvements": [{"area":"extraction|confidence|evidence|transcript_cleaning","observation":"...","suggestion":"...","reason":"..."}],
  "strategy_within_bounds": [{"playbook_id":"existing_enabled_playbook_id","suggestion":"...","reason":"..."}],
  "human_review_queue": [{"type":"new_tag|new_playbook|universe_change|operator_decision","proposal":"...","reason":"..."}],
  "operator_questions": ["..."]
}
"""


def _review_user_prompt(
    plan_payload: dict[str, Any],
    rejected: list[dict[str, Any]],
    ideas_text: str,
    universe: list[Any],
    enabled_playbooks: list[Any],
) -> str:
    compact = {
        "metrics": plan_payload.get("metrics"),
        "rejected_candidates": rejected[:30],
        "plans": plan_payload.get("plans", [])[:5],
    }
    universe_summary = [
        {
            "symbol": entry.symbol,
            "notes": entry.notes,
        }
        for entry in universe
        if entry.enabled
    ]
    playbook_summary = [
        {
            "playbook_id": playbook.playbook_id,
            "structure": playbook.structure,
            "variant": playbook.variant,
            "directions": playbook.applicable_direction,
            "tags": playbook.applicable_thesis_tags,
            "horizon_min": playbook.applicable_horizon_min,
            "horizon_max": playbook.applicable_horizon_max,
            "iv_percentile_min": playbook.iv_percentile_min,
            "iv_percentile_max": playbook.iv_percentile_max,
            "min_option_oi": playbook.min_option_oi,
            "max_bid_ask_pct": playbook.max_bid_ask_pct,
        }
        for playbook in enabled_playbooks
    ]
    return f"""\
Controlled thesis tags:
{", ".join(sorted(CONTROLLED_THESIS_TAGS))}

Enabled playbooks:
{json.dumps(playbook_summary, indent=2, sort_keys=True)[:12000]}

Enabled universe:
{json.dumps(universe_summary, indent=2, sort_keys=True)[:12000]}

Latest plan run:
{json.dumps(compact, indent=2, sort_keys=True)[:24000]}

Idea YAML, if available:
{ideas_text}
"""


def _review_markdown(review: dict[str, Any]) -> str:
    lines = ["# Kamandal Rejection Review", "", str(review.get("summary") or ""), ""]
    for section in (
        "engine_improvements",
        "intelligence_improvements",
        "strategy_within_bounds",
        "human_review_queue",
        "operator_questions",
    ):
        values = review.get(section) or []
        lines.extend([f"## {section}", ""])
        if not values:
            lines.append("- none")
        elif isinstance(values, list):
            for value in values:
                lines.append(f"- {json.dumps(value, sort_keys=True)}")
        else:
            lines.append(f"- {values}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
