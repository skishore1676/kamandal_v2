"""Reusable operator-review requests for Telegram/Jarvis control flows."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from kamandal_v2.stores.sqlite import LocalStore


PENDING = "pending"
SENT = "sent"
APPLIED = "applied"
HELD = "held"
DISMISSED = "dismissed"
EXPIRED = "expired"
FAILED = "failed"

BUTTON_RE = re.compile(r"\bkamandal:review:([A-Za-z0-9_.:-]+):([A-Za-z0-9_-]+)\b")
TEXT_RE = re.compile(r"\bkamandal\s+review\s+([A-Za-z0-9_.:-]+)\s+([A-Za-z0-9_-]+)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)


class OperatorReviewError(RuntimeError):
    pass


def operator_review_policy(config: dict[str, Any]) -> dict[str, Any]:
    live_cfg = config.get("live") or {}
    review = live_cfg.get("operator_review") or {}
    telegram = live_cfg.get("telegram_approval") or {}
    return {
        "enabled": _as_bool(review.get("enabled"), True),
        "channel": str(review.get("channel") or "telegram"),
        "target": str(review.get("target") or telegram.get("target") or os.environ.get("KAMANDAL_TELEGRAM_APPROVAL_TARGET") or ""),
        "account": str(review.get("account") or telegram.get("account") or os.environ.get("KAMANDAL_TELEGRAM_APPROVAL_ACCOUNT") or ""),
        "expiry_minutes": int(review.get("expiry_minutes") or telegram.get("expiry_minutes") or 30),
        "max_pending_requests": int(review.get("max_pending_requests") or telegram.get("max_pending_requests") or 10),
        "use_inline_buttons": _as_bool(review.get("use_inline_buttons"), True),
        "text_fallback": _as_bool(review.get("text_fallback"), True),
        "openclaw_binary": str(review.get("openclaw_binary") or os.environ.get("OPENCLAW_BINARY") or "openclaw"),
    }


def create_operator_review_request(
    config: dict[str, Any],
    *,
    request_type: str,
    subject_id: str,
    title: str,
    summary: str,
    allowed_actions: list[str],
    payload: dict[str, Any],
    store: LocalStore | None = None,
    send: bool = False,
    request_id: str = "",
) -> dict[str, Any]:
    store = store or LocalStore()
    policy = operator_review_policy(config)
    if not policy["enabled"]:
        return {"status": "disabled", "request_id": request_id or ""}
    pending = store.operator_review_requests_by_status({PENDING, SENT})
    if len(pending) >= int(policy["max_pending_requests"]):
        raise OperatorReviewError("operator review max_pending_requests reached")
    now = datetime.now(UTC)
    request = {
        "request_id": request_id or f"or_{uuid4().hex[:16]}",
        "request_type": request_type,
        "subject_id": subject_id,
        "title": title,
        "summary": summary,
        "allowed_actions": sorted({str(action).strip() for action in allowed_actions if str(action).strip()}),
        "payload": payload,
        "status": PENDING,
        "created_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=int(policy["expiry_minutes"]))).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    store.save_operator_review_request(request)
    if send:
        result = send_operator_review_message(config, request, store=store)
        request["send_result"] = result
    return request


def send_pending_operator_review_requests(config: dict[str, Any], *, store: LocalStore | None = None) -> dict[str, Any]:
    store = store or LocalStore()
    sent = []
    skipped = []
    for request in store.operator_review_requests_by_status({PENDING}):
        if _is_expired(request):
            store.update_operator_review_request_status(str(request["request_id"]), EXPIRED, {"expired_at": _now()})
            skipped.append({"request_id": request["request_id"], "reason": "expired"})
            continue
        sent.append(send_operator_review_message(config, request, store=store))
    return {"sent": sent, "skipped": skipped}


def send_operator_review_message(config: dict[str, Any], request: dict[str, Any], *, store: LocalStore | None = None) -> dict[str, Any]:
    store = store or LocalStore()
    policy = operator_review_policy(config)
    if not policy["enabled"]:
        return {"request_id": request.get("request_id"), "status": "disabled"}
    if not policy["target"]:
        raise OperatorReviewError("operator review target is not configured")
    message = render_operator_review_message(request, text_fallback=bool(policy["text_fallback"]))
    command = [
        str(policy["openclaw_binary"]),
        "message",
        "send",
        "--channel",
        str(policy["channel"]),
        "--target",
        str(policy["target"]),
        "--message",
        message,
        "--json",
    ]
    presentation = _presentation_payload(request) if policy["use_inline_buttons"] else {}
    if presentation:
        command.extend(["--presentation", json.dumps(presentation, sort_keys=True)])
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    payload = {
        "sent_at": _now(),
        "send_returncode": completed.returncode,
        "send_stdout": completed.stdout[-4000:],
        "send_stderr": completed.stderr[-4000:],
        "presentation": presentation,
    }
    status = SENT if completed.returncode == 0 else FAILED
    store.update_operator_review_request_status(str(request["request_id"]), status, payload)
    store.event("operator_review_message_sent", {"request_id": request.get("request_id"), "status": status, "returncode": completed.returncode})
    return {"request_id": request.get("request_id"), "status": status, "returncode": completed.returncode}


def render_operator_review_message(request: dict[str, Any], *, text_fallback: bool = True) -> str:
    request_id = str(request.get("request_id") or "")
    lines = [
        f"Kamandal review: {request.get('title') or request.get('request_type')}",
        f"Request: {request_id}",
        f"Type: {request.get('request_type')}",
        "",
        str(request.get("summary") or "").strip(),
        "",
        "Allowed actions: " + ", ".join(request.get("allowed_actions") or []),
    ]
    if text_fallback:
        lines.append("")
        lines.append(f"Fallback: kamandal review {request_id} <action> [note]")
    return "\n".join(line for line in lines if line is not None)


def operator_review_decision_from_message(
    config: dict[str, Any],
    message: str,
    *,
    source: str = "telegram",
    decided_by: str = "Suman",
    store: LocalStore | None = None,
) -> dict[str, Any]:
    parsed = parse_operator_review_decision(message)
    return apply_operator_review_decision(
        config,
        str(parsed["request_id"]),
        str(parsed["action"]),
        note=str(parsed.get("note") or ""),
        source=source,
        decided_by=decided_by,
        store=store,
    )


def parse_operator_review_decision(message: str) -> dict[str, str]:
    button = BUTTON_RE.search(message or "")
    if button:
        return {"request_id": button.group(1), "action": button.group(2), "note": ""}
    text = TEXT_RE.search((message or "").strip())
    if text:
        return {"request_id": text.group(1), "action": text.group(2), "note": (text.group(3) or "").strip()}
    raise OperatorReviewError("message did not contain a deterministic Kamandal review decision")


def apply_operator_review_decision(
    config: dict[str, Any],
    request_id: str,
    action: str,
    *,
    note: str = "",
    source: str = "manual",
    decided_by: str = "Suman",
    store: LocalStore | None = None,
) -> dict[str, Any]:
    store = store or LocalStore()
    request = store.operator_review_request(request_id)
    if not request:
        raise OperatorReviewError(f"operator review request not found: {request_id}")
    status = str(request.get("_ledger_status") or request.get("status") or "")
    if status not in {PENDING, SENT}:
        raise OperatorReviewError(f"operator review request {request_id} is not pending; status={status}")
    if _is_expired(request):
        store.update_operator_review_request_status(request_id, EXPIRED, {"expired_at": _now()})
        raise OperatorReviewError(f"operator review request {request_id} is expired")
    action = action.strip().lower()
    allowed = {str(item).strip().lower() for item in request.get("allowed_actions") or []}
    if action not in allowed:
        raise OperatorReviewError(f"action {action!r} is not allowed for request {request_id}")

    audit = {"decided_at": _now(), "decided_by": decided_by, "source": source, "action": action, "note": note}
    if str(request.get("request_type")) == "live_reconciliation":
        from kamandal_v2.live.reconciliation import apply_reconciliation_review_action

        result = apply_reconciliation_review_action(config, request, action, note=note, source=source, decided_by=decided_by, store=store)
        request_status = result.get("request_status") or APPLIED
    else:
        result = {"request_id": request_id, "action": action, "status": "recorded"}
        request_status = APPLIED
    store.update_operator_review_request_status(request_id, str(request_status), {**audit, "apply_result": result})
    store.event("operator_review_decision_applied", {"request_id": request_id, **audit, "result": result})
    return {"request_id": request_id, "request_status": request_status, "result": result}


def _presentation_payload(request: dict[str, Any]) -> dict[str, Any]:
    buttons = [
        {
            "label": _action_label(action),
            "value": f"kamandal:review:{request.get('request_id')}:{action}",
        }
        for action in request.get("allowed_actions") or []
    ]
    return {"blocks": [{"type": "buttons", "buttons": buttons}]}


def _action_label(action: str) -> str:
    return str(action).replace("_", " ").title()


def _is_expired(request: dict[str, Any]) -> bool:
    try:
        expires_at = datetime.fromisoformat(str(request.get("expires_at")).replace("Z", "+00:00"))
    except Exception:
        return False
    return datetime.now(UTC) > expires_at


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
