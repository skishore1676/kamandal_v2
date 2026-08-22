"""Fail-closed, exactly-once fallback from a terminal rank-one basket."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from kamandal_v2.stores.sqlite import LocalStore


TERMINAL_UNFILLED = {"cancelled", "canceled", "rejected", "expired", "failed", "submit_failed"}
FILLED = {"filled", "filled_via_replacement", "manual_fill_recorded"}
PARTIAL_FILLED = {"partially_filled_terminal"}
UNRESOLVED = {
    "pending_approval",
    "stage_approved_pending_submit",
    "waiting_entry_window",
    "submitted",
    "repriced",
    "partially_filled",
    "replace_cancel_pending",
    "replace_waiting_cancel",
    "reprice_blocked_preflight_failed",
}
REQUIRED_VALIDATION = (
    "fresh_session",
    "fresh_quotes",
    "risk_valid",
    "bpr_valid",
    "concentration_valid",
    "overlap_valid",
    "broker_preflight_valid",
)


@dataclass(frozen=True)
class FallbackDecision:
    status: str
    campaign_id: str
    reason: str
    attempt: int = 1
    plan_id: str = ""
    ticket_hashes: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    fill_summary: dict[str, Any] | None = None
    daily_plan_rows: tuple[tuple[Any, ...], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "campaign_id": self.campaign_id,
            "reason": self.reason,
            "attempt": self.attempt,
            "plan_id": self.plan_id,
            "ticket_hashes": list(self.ticket_hashes),
            "exclusions": list(self.exclusions),
            "fill_summary": dict(self.fill_summary or {}),
            "sheet_projection_row_count": len(self.daily_plan_rows),
        }


def fallback_enabled(config: dict[str, Any]) -> bool:
    return bool(((config.get("live") or {}).get("plan_fallback") or {}).get("enabled"))


def fallback_max_attempts(config: dict[str, Any]) -> int:
    raw = ((config.get("live") or {}).get("plan_fallback") or {}).get("max_attempts")
    return max(int(raw or 2), 1)


def attempt_event_type(campaign_id: str) -> str:
    return f"live_plan_attempt:{campaign_id}"


def register_rank_one_attempt(
    store: LocalStore,
    *,
    campaign_id: str,
    plan: Any,
    tickets: list[dict[str, Any]],
    plan_run_id: str,
    idea_paths: list[str] | None = None,
    config_source: str = "sheet",
    provider: str = "public",
    daily_policy_snapshot: Any | None = None,
    lifecycle_handoffs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist one rank-one campaign once; repeated advisory ticks are harmless."""

    existing = store.latest_event(attempt_event_type(campaign_id))
    if existing:
        return existing
    state = {
        "campaign_id": campaign_id,
        "plan_run_id": plan_run_id,
        "plan_id": str(getattr(plan, "plan_id", "") or ""),
        "plan_ids": [str(getattr(plan, "plan_id", "") or "")],
        "attempted_plan_ids": [str(getattr(plan, "plan_id", "") or "")],
        "plan_rank": int(getattr(plan, "plan_rank", 1) or 1),
        "attempt": 1,
        "status": "rank_one_active",
        "plan_snapshot": plan.to_dict() if hasattr(plan, "to_dict") else {},
        "candidate_ids": sorted(str(ticket.get("candidate_id") or "") for ticket in tickets if ticket.get("candidate_id")),
        "ticket_hashes": sorted(str(ticket.get("ticket_hash") or "") for ticket in tickets if ticket.get("ticket_hash")),
        "attempted_contract_keys": sorted(_ticket_contract_keys(tickets)),
        "idea_paths": [str(path) for path in (idea_paths or [])],
        "config_source": config_source,
        "provider": provider,
        "daily_policy_snapshot": {
            "date": str(getattr(daily_policy_snapshot, "trading_date", "") or ""),
            "hash": str(getattr(daily_policy_snapshot, "snapshot_hash", "") or ""),
        },
        "lifecycle_handoffs": [dict(item) for item in (lifecycle_handoffs or [])],
        "parent_attempt_id": "",
        "fallback_receipt_emitted": False,
    }
    for ticket in tickets:
        ticket_hash = str(ticket.get("ticket_hash") or "")
        if ticket_hash:
            existing_ticket = store.live_order_intent(ticket_hash) or {}
            existing_status = str(existing_ticket.get("_ledger_status") or ticket.get("_ledger_status") or "pending_approval")
            store.update_live_order_intent_status_with_payload(ticket_hash, existing_status, {"plan_attempt_id": campaign_id})
    store.event(attempt_event_type(campaign_id), state)
    return state


def registered_campaign_ids(store: LocalStore) -> list[str]:
    ids = {
        str(ticket.get("plan_attempt_id") or "")
        for ticket in store.live_order_intents_by_type("open")
        if str(ticket.get("plan_attempt_id") or "")
    }
    return sorted(ids)


class PlanFallbackCoordinator:
    """Evaluate and advance one persisted plan attempt.

    ``replan`` is deliberately injected.  Production supplies the canonical
    advisory/planner call; tests supply a deterministic fake-broker replay.
    """

    def __init__(self, store: LocalStore, config: dict[str, Any]) -> None:
        self.store = store
        self.config = config

    def advance(
        self,
        campaign_id: str,
        *,
        replan: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> FallbackDecision:
        state = self.store.latest_event(attempt_event_type(campaign_id))
        if not state:
            return FallbackDecision("blocked", campaign_id, "campaign_not_registered")
        prior_status = str(state.get("status") or "")
        if prior_status in {"fallback_ready", "fallback_complete", "terminal_no_valid_plan", "complete"}:
            return self._decision_from_state(state, reason="idempotent_replay")

        summary = self._basket_summary(state)
        if summary["unresolved"]:
            self._record(state, {"status": "blocked_unresolved", "fill_summary": summary, "terminal_reason": "broker_or_order_state_unresolved"})
            return FallbackDecision("blocked_unresolved", campaign_id, "broker_or_order_state_unresolved", fill_summary=summary)
        if summary["complete"]:
            complete_status = "fallback_complete" if int(state.get("attempt") or 1) > 1 else "complete"
            complete_reason = "rank_two_filled" if complete_status == "fallback_complete" else "rank_one_filled"
            self._record(state, {"status": complete_status, "fill_summary": summary, "terminal_reason": complete_reason})
            return FallbackDecision(complete_status, campaign_id, complete_reason, attempt=int(state.get("attempt") or 1), fill_summary=summary)

        exclusions = tuple(sorted(set(state.get("candidate_ids") or [])))
        if int(state.get("attempt") or 1) >= fallback_max_attempts(self.config):
            self._record(state, {"status": "terminal_no_valid_plan", "fill_summary": summary, "terminal_reason": "fallback_budget_exhausted"})
            return FallbackDecision("terminal_no_valid_plan", campaign_id, "fallback_budget_exhausted", fill_summary=summary)
        if replan is None:
            self._record(state, {"status": "terminal_no_valid_plan", "fill_summary": summary, "terminal_reason": "fresh_replan_unavailable"})
            return FallbackDecision("terminal_no_valid_plan", campaign_id, "fresh_replan_unavailable", exclusions=exclusions, fill_summary=summary)

        context = {
            "campaign_id": campaign_id,
            "parent_attempt_id": campaign_id,
            "attempt": int(state.get("attempt") or 1) + 1,
            "plan_rank": int(state.get("plan_rank") or 1) + 1,
            "attempted_candidate_ids": list(exclusions),
            "attempted_contract_keys": list(state.get("attempted_contract_keys") or []),
            "filled_candidate_ids": list(summary.get("filled_candidate_ids") or []),
            "actual_portfolio_groups": self.store.open_live_position_groups(),
            "idea_paths": list(state.get("idea_paths") or []),
            "config_source": state.get("config_source") or "sheet",
            "provider": state.get("provider") or "public",
            "daily_policy_snapshot": dict(state.get("daily_policy_snapshot") or {}),
            "reason": "partial_fill_terminal" if summary.get("partial") else "zero_fill_terminal",
        }
        candidate = replan(context)
        if not candidate:
            self._record(state, {"status": "terminal_no_valid_plan", "fill_summary": summary, "terminal_reason": "no_valid_second_plan"})
            return FallbackDecision("terminal_no_valid_plan", campaign_id, "no_valid_second_plan", exclusions=exclusions, fill_summary=summary)
        validation = candidate.get("validation") or {}
        required_validation = REQUIRED_VALIDATION
        if str(state.get("config_source") or "") == "unified-plan":
            required_validation = (*REQUIRED_VALIDATION, "unified_lifecycle_handoff")
        missing = [key for key in required_validation if validation.get(key) is not True]
        if missing:
            self._record(state, {"status": "terminal_no_valid_plan", "fill_summary": summary, "terminal_reason": "fresh_validation_failed", "validation_missing": missing})
            return FallbackDecision("terminal_no_valid_plan", campaign_id, "fresh_validation_failed:" + ",".join(missing), exclusions=exclusions, fill_summary=summary)

        tickets = [ticket for ticket in candidate.get("tickets") or [] if isinstance(ticket, dict)]
        ticket_hashes = tuple(sorted(str(ticket.get("ticket_hash") or "") for ticket in tickets if ticket.get("ticket_hash")))
        if not ticket_hashes or not str(candidate.get("plan_id") or ""):
            self._record(state, {"status": "terminal_no_valid_plan", "fill_summary": summary, "terminal_reason": "fresh_replan_missing_ticket_identity"})
            return FallbackDecision("terminal_no_valid_plan", campaign_id, "fresh_replan_missing_ticket_identity", exclusions=exclusions, fill_summary=summary)
        child_state = {
            **state,
            "campaign_id": campaign_id,
            "status": "fallback_ready",
            "attempt": int(state.get("attempt") or 1) + 1,
            "plan_rank": int(state.get("plan_rank") or 1) + 1,
            "plan_id": str(candidate.get("plan_id") or ""),
            "plan_ids": [*list(state.get("plan_ids") or []), str(candidate.get("plan_id") or "")],
            "attempted_plan_ids": list(state.get("attempted_plan_ids") or []),
            "ticket_hashes": list(ticket_hashes),
            "candidate_ids": sorted(str(item) for item in candidate.get("candidate_ids") or []),
            "attempted_contract_keys": sorted(set(state.get("attempted_contract_keys") or [])),
            "parent_attempt_id": campaign_id,
            "fill_summary": summary,
            "validation": validation,
            "daily_plan_rows": [list(row) for row in candidate.get("daily_plan_rows") or []],
            "fallback_receipt_emitted": False,
        }
        for ticket in tickets:
            ticket_hash = str(ticket.get("ticket_hash") or "")
            if ticket_hash:
                self.store.update_live_order_intent_status_with_payload(ticket_hash, str(ticket.get("_ledger_status") or "pending_approval"), {"plan_attempt_id": campaign_id, "parent_attempt_id": campaign_id})
        self.store.event(attempt_event_type(campaign_id), child_state)
        return FallbackDecision(
            "fallback_ready",
            campaign_id,
            context["reason"],
            attempt=child_state["attempt"],
            plan_id=child_state["plan_id"],
            ticket_hashes=ticket_hashes,
            exclusions=exclusions,
            fill_summary=summary,
            daily_plan_rows=tuple(tuple(row) for row in child_state["daily_plan_rows"]),
        )

    def mark_submitted(self, decision: FallbackDecision, results: list[dict[str, Any]]) -> None:
        state = self.store.latest_event(attempt_event_type(decision.campaign_id))
        if not state or str(state.get("status") or "") != "fallback_ready":
            return
        self.store.event(
            attempt_event_type(decision.campaign_id),
            {
                **state,
                "status": "fallback_submitted",
                "attempted_plan_ids": sorted(set([*list(state.get("attempted_plan_ids") or []), str(state.get("plan_id") or "")])),
                "fallback_submission_results": results,
                "fallback_receipt_emitted": True,
            },
        )

    def _decision_from_state(self, state: dict[str, Any], *, reason: str) -> FallbackDecision:
        return FallbackDecision(
            str(state.get("status") or "blocked"),
            str(state.get("campaign_id") or ""),
            reason,
            attempt=int(state.get("attempt") or 1),
            plan_id=str(state.get("plan_id") or ""),
            ticket_hashes=tuple(sorted(str(item) for item in state.get("ticket_hashes") or [])),
            fill_summary=state.get("fill_summary") or {},
            daily_plan_rows=tuple(tuple(row) for row in state.get("daily_plan_rows") or []),
        )

    def _record(self, state: dict[str, Any], updates: dict[str, Any]) -> None:
        self.store.event(attempt_event_type(str(state["campaign_id"])), {**state, **updates})

    def _basket_summary(self, state: dict[str, Any]) -> dict[str, Any]:
        outcomes: list[str] = []
        filled_candidate_ids: list[str] = []
        unresolved = False
        for ticket_hash in state.get("ticket_hashes") or []:
            root = self.store.live_order_intent(str(ticket_hash))
            if not root:
                outcomes.append("unknown")
                unresolved = True
                continue
            members = [root, *self._descendants(str(ticket_hash))]
            member_outcomes = [self._classify(member) for member in self._lineage_leaves(members)]
            if "unresolved" in member_outcomes:
                unresolved = True
            if any(item in {"filled", "partial"} for item in member_outcomes):
                filled_candidate_ids.append(str(root.get("candidate_id") or ""))
            outcomes.append("partial" if "partial" in member_outcomes else "filled" if "filled" in member_outcomes else "terminal")
        partial = any(item == "partial" for item in outcomes)
        complete = bool(outcomes) and not unresolved and all(item == "filled" for item in outcomes)
        zero_fill_terminal = bool(outcomes) and not unresolved and not partial and not any(item == "filled" for item in outcomes)
        return {
            "outcomes": outcomes,
            "unresolved": unresolved,
            "complete": complete,
            "partial": partial,
            "zero_fill_terminal": zero_fill_terminal,
            "filled_candidate_ids": sorted(set(item for item in filled_candidate_ids if item)),
        }

    def _descendants(self, ticket_hash: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        queue = [ticket_hash]
        while queue:
            parent = queue.pop(0)
            children = self.store.live_order_child_intents(parent)
            found.extend(children)
            queue.extend(str(child.get("ticket_hash") or "") for child in children if child.get("ticket_hash"))
        return found

    def _lineage_leaves(self, members: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Classify authoritative descendants, not superseded repriced ancestors."""

        hashes = {str(member.get("ticket_hash") or "") for member in members}
        parents = {
            str(member.get("parent_ticket_hash") or "")
            for member in members
            if str(member.get("parent_ticket_hash") or "") in hashes
        }
        leaves = [member for member in members if str(member.get("ticket_hash") or "") not in parents]
        return leaves or members

    def _classify(self, ticket: dict[str, Any]) -> str:
        status = str(ticket.get("_ledger_status") or ticket.get("status") or "").strip().lower()
        if status in UNRESOLVED:
            return "unresolved"
        if status in PARTIAL_FILLED:
            return "partial"
        if status in FILLED:
            return "filled"
        if status == "expired":
            order_id = str(ticket.get("order_id") or "")
            history = self.store.live_order_status_history({order_id})
            last = str((history[-1] if history else {}).get("status") or "").upper()
            return "terminal" if last in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"} else "unresolved"
        if status in TERMINAL_UNFILLED or any(status.startswith(prefix) for prefix in ("blocked_", "submit_failed", "reprice_")):
            return "terminal"
        return "unresolved"


def _ticket_contract_keys(tickets: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for ticket in tickets:
        underlying = str(ticket.get("underlying") or "").upper()
        for leg in ticket.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            expiration = str(leg.get("expiration") or "")
            option_type = str(leg.get("option_type") or "").lower()
            try:
                strike = float(leg.get("strike") or 0.0)
            except (TypeError, ValueError):
                continue
            if underlying and expiration and option_type in {"call", "put"} and strike > 0:
                keys.add(f"{underlying}|{expiration}|{option_type}|{strike:g}")
    return keys
