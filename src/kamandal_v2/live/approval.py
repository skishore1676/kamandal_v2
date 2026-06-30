"""Telegram-gated live approval control plane."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

from kamandal_v2.live.orders import APPROVE_LIVE
from kamandal_v2.ops.alerts import default_lathi_bus_profile, default_lathi_invocation, optional_bool, parse_lathi_receipt, populate_secret_fallbacks, redact
from kamandal_v2.schemas import DAILY_PLAN_HEADER
from kamandal_v2.sheets import GoogleSheetClient
from kamandal_v2.stores.sqlite import LocalStore


PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"


def create_live_approval_request(
    config: dict[str, Any],
    *,
    row: dict[str, Any],
    plan: Any,
    candidate: Any,
    ticket: dict[str, Any],
    store: LocalStore,
    send: bool = True,
) -> dict[str, Any]:
    policy = telegram_approval_policy(config)
    request = {
        "request_id": _request_id(ticket, plan),
        "ticket_hash": str(ticket["ticket_hash"]),
        "order_id": str(ticket["order_id"]),
        "plan_id": str(ticket["plan_id"]),
        "plan_rank": ticket.get("plan_rank"),
        "candidate_id": str(ticket["candidate_id"]),
        "idea_id": str(ticket.get("idea_id") or ""),
        "underlying": str(ticket.get("underlying") or candidate.underlying),
        "playbook_id": str(ticket.get("playbook_id") or candidate.playbook_id),
        "structure": str(ticket.get("structure") or candidate.structure),
        "limit_price": str(ticket.get("limit_price") or ""),
        "estimated_bpr": float(getattr(candidate, "estimated_bpr", 0.0) or 0.0),
        "net_credit": float(getattr(candidate, "net_credit", 0.0) or 0.0),
        "created_at": utc_now(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=policy["expiry_minutes"])).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": PENDING,
        "source": "live_advisory",
        "ticket": ticket,
        "sheet_row": {key: row.get(key, "") for key in ("plan_date", "rank", "trade_bundle", "mode", "operator_action")},
    }
    request["message"] = render_live_approval_message(request, row=row, candidate=candidate)
    store.save_live_approval_request(request)
    store.event("live_approval_request_created", {
        "request_id": request["request_id"],
        "ticket_hash": request["ticket_hash"],
        "underlying": request["underlying"],
        "structure": request["structure"],
        "expires_at": request["expires_at"],
    })
    if send and bool(policy["enabled"]):
        try:
            send_live_approval_message(config, request["message"])
        except Exception as exc:  # noqa: BLE001
            store.update_live_approval_request_status(
                str(request["request_id"]),
                PENDING,
                {"send_error": str(exc), "delivery": {"channel": "telegram", "target": policy["target"]}},
            )
            store.event("live_approval_request_send_failed", {
                "request_id": request["request_id"],
                "ticket_hash": request["ticket_hash"],
                "error": str(exc),
            })
        else:
            store.update_live_approval_request_status(
                str(request["request_id"]),
                PENDING,
                {"sent_at": utc_now(), "delivery": {"channel": "telegram", "target": policy["target"]}},
            )
            store.event("live_approval_request_sent", {"request_id": request["request_id"], "ticket_hash": request["ticket_hash"]})
    return request


def approve_live_request(
    config: dict[str, Any],
    request_id: str,
    *,
    source: str = "manual",
    approved_by: str = "Suman",
    store: LocalStore | None = None,
) -> dict[str, Any]:
    store = store or LocalStore()
    request = _load_actionable_request(store, request_id)
    if _is_expired(request):
        store.update_live_approval_request_status(request_id, EXPIRED, {"expired_at": utc_now(), "source": source})
        store.event("live_approval_request_expired", {"request_id": request_id, "source": source})
        return {"request_id": request_id, "status": EXPIRED, "updated_sheet": False, "reason": "approval_request_expired"}
    intent = store.live_order_intent(str(request["ticket_hash"]))
    if not intent:
        raise RuntimeError(f"live order intent not found for request {request_id}")
    if str(intent.get("ticket_hash") or "") != str(request["ticket_hash"]):
        raise RuntimeError(f"ticket hash mismatch for request {request_id}")
    if str(intent.get("_ledger_status") or "") not in {"pending_approval", "dry_run"}:
        raise RuntimeError(f"ticket is not approvable: {intent.get('_ledger_status')}")
    sheet_result = update_daily_plan_operator_action(
        config,
        ticket_hash=str(request["ticket_hash"]),
        action=APPROVE_LIVE,
        note=f"approved via {source} by {approved_by}; request_id={request_id}",
        plan_status="telegram_approved",
    )
    store.update_live_approval_request_status(
        request_id,
        APPROVED,
        {"approved_at": utc_now(), "approved_by": approved_by, "source": source, "sheet_result": sheet_result},
    )
    store.event("live_approval_request_approved", {"request_id": request_id, "ticket_hash": request["ticket_hash"], "source": source})
    return {"request_id": request_id, "status": APPROVED, "updated_sheet": True, **sheet_result}


def reject_live_request(
    request_id: str,
    *,
    reason: str,
    source: str = "manual",
    rejected_by: str = "Suman",
    store: LocalStore | None = None,
) -> dict[str, Any]:
    store = store or LocalStore()
    request = _load_actionable_request(store, request_id)
    store.update_live_approval_request_status(
        request_id,
        REJECTED,
        {"rejected_at": utc_now(), "rejected_by": rejected_by, "source": source, "reason": reason},
    )
    store.event("live_approval_request_rejected", {"request_id": request_id, "ticket_hash": request["ticket_hash"], "source": source, "reason": reason})
    return {"request_id": request_id, "status": REJECTED, "reason": reason}


def expire_live_approval_requests(*, store: LocalStore | None = None) -> dict[str, Any]:
    store = store or LocalStore()
    expired = []
    for request in store.live_approval_requests_by_status({PENDING}):
        if not _is_expired(request):
            continue
        store.update_live_approval_request_status(str(request["request_id"]), EXPIRED, {"expired_at": utc_now(), "source": "expiry_sweep"})
        expired.append(str(request["request_id"]))
    if expired:
        store.event("live_approval_requests_expired", {"request_ids": expired})
    return {"expired": len(expired), "request_ids": expired}


def send_pending_live_approval_requests(config: dict[str, Any], *, store: LocalStore | None = None) -> dict[str, Any]:
    store = store or LocalStore()
    sent = []
    skipped = []
    for request in store.live_approval_requests_by_status({PENDING}):
        request_id = str(request["request_id"])
        if _is_expired(request):
            store.update_live_approval_request_status(request_id, EXPIRED, {"expired_at": utc_now(), "source": "send_pending"})
            skipped.append({"request_id": request_id, "reason": "expired"})
            continue
        if request.get("sent_at"):
            skipped.append({"request_id": request_id, "reason": "already_sent"})
            continue
        send_live_approval_message(config, str(request.get("message") or ""))
        store.update_live_approval_request_status(
            request_id,
            PENDING,
            {"sent_at": utc_now(), "delivery": {"channel": "telegram", "target": telegram_approval_policy(config)["target"]}},
        )
        store.event("live_approval_request_sent", {"request_id": request_id, "ticket_hash": request.get("ticket_hash")})
        sent.append(request_id)
    return {"sent": len(sent), "sent_request_ids": sent, "skipped": skipped}


def live_approval_status(*, store: LocalStore | None = None) -> dict[str, Any]:
    store = store or LocalStore()
    requests = store.live_approval_requests_by_status({PENDING, APPROVED, REJECTED, EXPIRED})
    counts: dict[str, int] = {}
    for request in requests:
        status = str(request.get("_ledger_status") or request.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return {"counts": counts, "requests": _status_rows(requests)}


def render_live_approval_message(request: dict[str, Any], *, row: dict[str, Any], candidate: Any) -> str:
    detail = _loads(row.get("plan_detail_json"))
    metrics = _loads(row.get("plan_metrics_json"))
    account = detail.get("real_account_json") or metrics.get("real_account_json") or {}
    action = "credit" if float(request.get("net_credit") or 0.0) > 0 else "debit"
    bpr = float(request.get("estimated_bpr") or 0.0)
    buying_power = account.get("buying_power") or account.get("buying_power_effective")
    dte = _candidate_dte(candidate)
    return "\n".join(
        [
            "Kamandal live approval",
            "",
            str(request["request_id"]),
            f"{request['underlying']} {request['playbook_id']}",
            f"Structure: {request['structure']}",
            f"Plan rank: {request.get('plan_rank') or row.get('rank') or ''}",
            f"BPR: ${bpr:,.0f}",
            f"Limit: {request['limit_price']} {action}",
            f"DTE: {dte if dte is not None else 'n/a'}",
            f"Buying power: ${float(buying_power):,.0f}" if buying_power not in (None, "") else "Buying power: n/a",
            "",
            f"Approve: approve {request['request_id']}",
            f"Reject: reject {request['request_id']} <reason>",
            f"Expires: {request['expires_at']}",
        ]
    )


def send_live_approval_message(config: dict[str, Any], message: str) -> None:
    policy = telegram_approval_policy(config)
    transport = str(policy.get("transport") or "lathi_bus").lower()
    if transport in {"lathi", "lathi_bus", "lathi-bus"}:
        _send_lathi_live_approval_message(policy, message)
        return
    target = str(policy["target"] or "").strip()
    if not target:
        raise RuntimeError("live.telegram_approval.target is not configured")
    command = ["openclaw", "message", "send", "--channel", "telegram", "--target", target, "--message", message, "--json"]
    account = str(policy.get("account") or "").strip()
    if account:
        command.extend(["--account", account])
    result = subprocess.run(command, text=True, capture_output=True, check=False, cwd=os.environ.get("OPENCLAW_ROOT") or None)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + "\n" + result.stderr).splitlines()[-20:])
        raise RuntimeError(f"Telegram approval send failed: {tail}")


def _send_lathi_live_approval_message(policy: dict[str, Any], message: str) -> None:
    mode = str(policy.get("lathi_mode") or os.getenv("KAMANDAL_LAUNCHD_ALERT_MODE") or "live").lower()
    if mode == "off":
        return
    if mode not in {"spool", "live"}:
        raise RuntimeError(f"unsupported live.telegram_approval.lathi_mode={mode!r}")
    request_id = _approval_request_id(message)
    command, cwd = default_lathi_invocation(None)
    args = [
        *command,
        "telegram-ask",
        "--profile",
        str(policy.get("lathi_profile") or default_lathi_bus_profile()),
        "--template",
        "urgent_gate",
        "--title",
        "Kamandal live entry approval",
        "--prompt",
        message,
        "--field",
        f"Request={request_id}",
        "--field",
        "Type=live_entry_approval",
        "--button-columns",
        "2",
        "--link-preview",
        "disabled",
        "--option",
        f"kamandal:approval:{request_id}:approve|Approve|primary",
        "--option",
        f"kamandal:approval:{request_id}:reject|Reject|danger",
    ]
    if mode == "live":
        args.append("--live")
    env = os.environ.copy()
    populate_secret_fallbacks(env)
    completed = subprocess.run(  # noqa: S603
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    receipt = parse_lathi_receipt(completed.stdout)
    network_call_performed = optional_bool(receipt.get("network_call_performed"))
    ok = completed.returncode == 0 and (mode != "live" or network_call_performed is True)
    if not ok:
        tail = "\n".join((redact(completed.stdout) + "\n" + redact(completed.stderr)).splitlines()[-20:])
        raise RuntimeError(f"Lathi Bus live approval send failed: {tail}")


def update_daily_plan_operator_action(
    config: dict[str, Any],
    *,
    ticket_hash: str,
    action: str,
    note: str,
    plan_status: str,
) -> dict[str, Any]:
    client = GoogleSheetClient.from_config(config)
    title = str(((config.get("google_sheets") or {}).get("tabs") or {}).get("daily_plan") or "daily_plan")
    rows = client.read_tab(title)
    matches = []
    for index, row in enumerate(rows):
        detail = _loads(row.get("plan_detail_json"))
        ticket = detail.get("order_ticket_json") or {}
        if str(ticket.get("ticket_hash") or "") == ticket_hash:
            matches.append((index, row))
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one daily_plan row for ticket_hash={ticket_hash}, found {len(matches)}")
    _index, row = matches[0]
    row["operator_action"] = action
    row["operator_notes"] = note
    row["plan_status"] = plan_status
    client.replace_tab(title, header=DAILY_PLAN_HEADER, rows=[[item.get(column, "") for column in DAILY_PLAN_HEADER] for item in rows])
    return {"sheet_tab": title, "matched_rows": 1, "ticket_hash": ticket_hash}


def telegram_approval_policy(config: dict[str, Any]) -> dict[str, Any]:
    raw = ((config.get("live") or {}).get("telegram_approval") or {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": _as_bool(raw.get("enabled"), True),
        "transport": str(raw.get("transport") or os.environ.get("KAMANDAL_TELEGRAM_APPROVAL_TRANSPORT") or "lathi_bus"),
        "target": str(raw.get("target") or "5425926875"),
        "account": str(raw.get("account") or ""),
        "lathi_profile": str(raw.get("lathi_profile") or os.environ.get("KAMANDAL_TELEGRAM_APPROVAL_LATHI_BUS_PROFILE") or default_lathi_bus_profile()),
        "lathi_mode": str(raw.get("lathi_mode") or os.environ.get("KAMANDAL_TELEGRAM_APPROVAL_LATHI_BUS_MODE") or os.environ.get("KAMANDAL_LAUNCHD_ALERT_MODE") or "live"),
        "expiry_minutes": max(int(raw.get("expiry_minutes") or 8), 1),
        "max_pending_requests": max(int(raw.get("max_pending_requests") or 3), 1),
    }


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _request_id(ticket: dict[str, Any], plan: Any) -> str:
    created = datetime.now(UTC).strftime("%m%d-%H%M")
    rank = ticket.get("plan_rank") or getattr(plan, "plan_rank", "") or "x"
    return f"KAM-{created}-{rank}-{str(ticket['ticket_hash'])[:6].upper()}"


def _load_actionable_request(store: LocalStore, request_id: str) -> dict[str, Any]:
    request = store.live_approval_request(request_id)
    if not request:
        raise RuntimeError(f"live approval request not found: {request_id}")
    status = str(request.get("_ledger_status") or request.get("status") or "")
    if status != PENDING:
        raise RuntimeError(f"live approval request is not pending: {status}")
    return request


def _is_expired(request: dict[str, Any]) -> bool:
    expires_at = str(request.get("expires_at") or "")
    if not expires_at:
        return True
    parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return datetime.now(UTC) > parsed


def _status_rows(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for request in sorted(requests, key=lambda item: str(item.get("created_at") or ""), reverse=True):
        rows.append({
            "request_id": request.get("request_id"),
            "status": request.get("_ledger_status") or request.get("status"),
            "underlying": request.get("underlying"),
            "structure": request.get("structure"),
            "ticket_hash": request.get("ticket_hash"),
            "expires_at": request.get("expires_at"),
        })
    return rows


def _candidate_dte(candidate: Any) -> int | None:
    expirations = []
    for leg in getattr(candidate, "legs", []) or []:
        raw = str(getattr(leg, "expiration", "") or "")
        if not raw:
            continue
        try:
            expirations.append(datetime.fromisoformat(raw).date())
        except ValueError:
            pass
    if not expirations:
        return None
    return min((expiration - datetime.now(UTC).date()).days for expiration in expirations)


def _approval_request_id(message: str) -> str:
    match = re.search(r"\bKAM-[A-Za-z0-9_-]+\b", message)
    return match.group(0) if match else "unknown"


def _loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _as_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
