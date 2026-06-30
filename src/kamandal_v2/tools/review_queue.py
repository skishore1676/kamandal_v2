"""Read-only Kamandal operator review queue for Lathi."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from typing import Any

from kamandal_v2.live.operator_review import PENDING, SENT, expire_stale_operator_review_requests
from kamandal_v2.stores.sqlite import LocalStore


SCHEMA = "kamandal.review_queue.v1"
ACTIVE_REVIEW_STATUSES = {PENDING, SENT}


def build_review_queue(*, store: LocalStore | None = None, expire_stale: bool = True) -> dict[str, Any]:
    store = store or LocalStore()
    expired: dict[str, Any] = {"expired": []}
    if expire_stale:
        expired = expire_stale_operator_review_requests(store=store, statuses=set(ACTIVE_REVIEW_STATUSES))
    requests = [
        _review_request_unit(request)
        for request in store.operator_review_requests_by_status(set(ACTIVE_REVIEW_STATUSES))
    ]
    return {
        "schema": SCHEMA,
        "generated_at": _now(),
        "review_requests": requests,
        "counts": {
            "active": len(requests),
            "expired_swept": len(expired.get("expired") or []),
        },
    }


def subject_fingerprint(request: dict[str, Any]) -> str:
    """Return a stable subject fingerprint for app-side decision validation."""

    payload = {
        "request_id": request.get("request_id"),
        "request_type": request.get("request_type"),
        "subject_id": request.get("subject_id"),
        "allowed_actions": sorted(str(item) for item in request.get("allowed_actions") or []),
        "payload": request.get("payload") or {},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _review_request_unit(request: dict[str, Any]) -> dict[str, Any]:
    status = str(request.get("_ledger_status") or request.get("status") or PENDING)
    actions = sorted({str(action).strip() for action in request.get("allowed_actions") or [] if str(action).strip()})
    action_requirements = {
        action: {
            "requires_confirmation": True,
            "reason": _confirmation_reason(action, request),
        }
        for action in actions
    }
    return {
        "request_id": str(request.get("request_id") or ""),
        "unit_id": str(request.get("request_id") or ""),
        "kind": "external_review_request",
        "serves_job": "C",
        "status": status,
        "lifecycle": "waiting_you",
        "summary": str(request.get("summary") or request.get("title") or request.get("request_type") or ""),
        "request_type": str(request.get("request_type") or ""),
        "subject_id": str(request.get("subject_id") or ""),
        "subject_fingerprint": subject_fingerprint(request),
        "allowed_actions": actions,
        "available_actions": actions,
        "expires_at": str(request.get("expires_at") or ""),
        "risk_class": _risk_class(request),
        "requires_confirmation": True,
        "action_requirements": action_requirements,
        "source_id": "kamandal",
        "findings": _findings(request),
    }


def _risk_class(request: dict[str, Any]) -> str:
    request_type = str(request.get("request_type") or "")
    if request_type.startswith("live_"):
        return "trading_review"
    return "operator_review"


def _confirmation_reason(action: str, request: dict[str, Any]) -> str:
    request_type = str(request.get("request_type") or "operator review").replace("_", " ")
    if action == "retire_local":
        return "This can retire a Kamandal local live group."
    if action == "adopt_broker_position":
        return "This can create Kamandal local state from broker state."
    if action == "dismiss":
        return "This can dismiss an active Kamandal review issue."
    if action == "hold":
        return "This keeps the review decision in Kamandal's live workflow state."
    return f"This applies a {request_type} decision inside Kamandal."


def _findings(request: dict[str, Any]) -> list[str]:
    findings = []
    if request.get("expires_at"):
        findings.append("expires " + str(request["expires_at"]))
    if request.get("subject_id"):
        findings.append("subject " + str(request["subject_id"]))
    if request.get("request_type"):
        findings.append("type " + str(request["request_type"]))
    return findings


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--db", default="", help="Optional SQLite path; defaults to Kamandal data DB")
    parser.add_argument("--no-expire-stale", action="store_true", help="Do not sweep stale active review requests before output")
    args = parser.parse_args(argv)

    store = LocalStore(args.db) if args.db else LocalStore()
    payload = build_review_queue(store=store, expire_stale=not args.no_expire_stale)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
