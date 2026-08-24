"""Stable client and broker order identity helpers.

Kamandal's ``order_id`` is a deterministic client id used for idempotency and
lineage. Brokers may return a different, broker-assigned id. Never replace the
client id with the broker id; persist both and use the broker id only for
broker lifecycle calls.
"""

from __future__ import annotations

from typing import Any


def client_order_id(ticket: dict[str, Any]) -> str:
    return str(ticket.get("client_order_id") or ticket.get("order_id") or "")


def broker_order_id(ticket: dict[str, Any]) -> str:
    return str(ticket.get("broker_order_id") or ticket.get("order_id") or "")


def persist_broker_identity(ticket: dict[str, Any], response: dict[str, Any]) -> str:
    assigned = str(response.get("orderId") or response.get("brokerOrderId") or "")
    if not assigned:
        raise RuntimeError("broker response missing orderId")
    ticket.setdefault("client_order_id", client_order_id(ticket))
    ticket["broker_order_id"] = assigned
    return assigned
