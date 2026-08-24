"""Sheet-authorized Cartographer range gate for neutral market scans."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kamandal_v2.domain.models import Candidate
from kamandal_v2.intelligence.market_questions import run_range_regime_exchange
from kamandal_v2.strategy_engine.policy import PlaybookPolicy


def apply_range_regime_gate(
    candidates: list[Candidate],
    policies: tuple[PlaybookPolicy, ...],
    settings: dict[str, Any],
    *,
    observed_at: str,
    command_runner: Callable[[list[str], Any], str] | None = None,
) -> dict[str, Any]:
    """Reject only Sheet-gated candidates that lack fresh confirmed-range evidence."""

    policy_by_id = {policy.playbook_id: policy for policy in policies}
    gated = [
        candidate
        for candidate in candidates
        if not candidate.rejection_reason
        and _required(policy_by_id.get(candidate.playbook_id))
    ]
    if not gated:
        return {"status": "not_needed", "candidate_count": 0, "answers": {}}

    maximum = max(1, int(settings.get("max_symbols_per_request") or 8))
    answers: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[str] = []
    by_playbook: dict[str, set[str]] = {}
    for candidate in gated:
        by_playbook.setdefault(candidate.playbook_id, set()).add(candidate.underlying)

    for playbook_id, symbols in sorted(by_playbook.items()):
        ordered = sorted(symbols)
        for offset in range(0, len(ordered), maximum):
            chunk = ordered[offset : offset + maximum]
            result = run_range_regime_exchange(
                chunk,
                settings,
                as_of=observed_at,
                playbook_id=playbook_id,
                command_runner=command_runner,
            )
            if result.response_path is None:
                errors.append(f"{playbook_id}:{result.status}:{result.error or 'response unavailable'}")
                continue
            payload = json.loads(result.response_path.read_text(encoding="utf-8"))
            for answer in payload.get("answers") or []:
                answers[(playbook_id, str(answer.get("symbol") or "").upper())] = dict(answer)

    admitted = 0
    for candidate in gated:
        policy = policy_by_id[candidate.playbook_id]
        answer = answers.get((candidate.playbook_id, candidate.underlying.upper()))
        blocker = _blocker(answer, policy, observed_at=observed_at)
        if blocker:
            candidate.rejection_reason = blocker
            continue
        admitted += 1
        candidate.reasons.extend(
            [
                "cartographer_range_state=confirmed_range",
                f"cartographer_observed_at={answer['observed_at']}",
                f"cartographer_latest_session={answer.get('latest_complete_session') or ''}",
                f"cartographer_lower_boundary={answer['lower_boundary']}",
                f"cartographer_upper_boundary={answer['upper_boundary']}",
                f"cartographer_range_width_atr={answer.get('range_width_atr')}",
            ]
        )
    return {
        "status": "succeeded" if not errors else "partial",
        "candidate_count": len(gated),
        "admitted_count": admitted,
        "answer_count": len(answers),
        "errors": errors,
    }


def _required(policy: PlaybookPolicy | None) -> bool:
    if policy is None:
        return False
    return _as_bool(policy.fields.get("range_gate_required"))


def _blocker(answer: dict[str, Any] | None, policy: PlaybookPolicy, *, observed_at: str) -> str:
    if answer is None:
        return "cartographer_range_evidence_unavailable"
    if answer.get("answer_status") != "evaluated":
        return "cartographer_range_evidence_insufficient"
    if answer.get("range_state") != "confirmed_range" or answer.get("current_within_range") is not True:
        return f"cartographer_range_state:{answer.get('range_state') or 'unknown'}"
    maximum_age = int(float(policy.fields.get("range_gate_max_age_days") or 7))
    observed = _timestamp(observed_at)
    latest = _timestamp(str(answer.get("latest_complete_session") or answer.get("observed_at") or ""))
    if (observed.date() - latest.date()).days > maximum_age:
        return "cartographer_range_evidence_stale"
    return ""


def _timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    if not normalized:
        raise ValueError("range evidence timestamp is missing")
    return datetime.fromisoformat(normalized)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}
