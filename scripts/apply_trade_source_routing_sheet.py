#!/usr/bin/env python3
"""Migrate the operator Sheet from person-specific rows to source routing.

Dry-run is the default. Apply is intended only at a stopped-job session
boundary, after code deployment and immediately before a fresh daily snapshot.
The script never submits or manages an order.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kamandal_v2.config import load_control
from kamandal_v2.paths import resolve_path
from kamandal_v2.schemas import PLAYBOOKS_HEADER, TRADE_SOURCE_ACTIVITY_HEADER, TRADE_SOURCES_HEADER
from kamandal_v2.seed import build_seed_tables
from kamandal_v2.sheets import GoogleSheetClient
from kamandal_v2.strategy_engine.policy import compile_playbook_policies
from kamandal_v2.strategy_engine.sheet_policy_gate import validate_sheet_policy
from kamandal_v2.strategy_lanes.store import CsaStore


RETIRED_PLAYBOOK_IDS = {
    "mike_call_calendar_observed",
    "mike_put_calendar_observed",
    "mike_call_diagonal_observed",
    "mike_put_diagonal_observed",
}
EXACT_ACCEPTOR_BY_STRUCTURE = {
    "call_calendar": "call_calendar_low_iv",
    "put_calendar": "put_calendar_low_iv",
    "call_diagonal": "call_diagonal_oversold",
    "put_diagonal": "put_diagonal_overextended",
}


def _text_list(value: Any) -> list[str]:
    return [item.strip().lower() for item in str(value or "").split(",") if item.strip()]


def migrated_playbook_rows(existing: list[dict[str, str]]) -> list[dict[str, str]]:
    present = {str(row.get("playbook_id") or "") for row in existing}
    missing = sorted(set(EXACT_ACCEPTOR_BY_STRUCTURE.values()) - present)
    if missing:
        raise ValueError("exact-package acceptor playbooks are missing: " + ", ".join(missing))

    migrated: list[dict[str, str]] = []
    acceptors = set(EXACT_ACCEPTOR_BY_STRUCTURE.values())
    for existing_row in existing:
        playbook_id = str(existing_row.get("playbook_id") or "")
        if not playbook_id or playbook_id in RETIRED_PLAYBOOK_IDS:
            continue
        row = dict(existing_row)
        source_mode = str(row.get("source_mode") or "idea").strip().lower() or "idea"
        fallback = "exact_package" if source_mode == "observed_package" else source_mode
        accepted = _text_list(row.get("accepted_inputs")) or [fallback]
        if playbook_id in acceptors and "exact_package" not in accepted:
            accepted.append("exact_package")
        row["accepted_inputs"] = ",".join(dict.fromkeys(accepted))
        # Person routing is now owned only by trade_sources. Keep the appended
        # compatibility column physically present, but make it inert.
        row["source_profiles"] = ""
        migrated.append(row)
    return migrated


def trade_source_rows() -> list[dict[str, str]]:
    headers = TRADE_SOURCES_HEADER
    return [dict(zip(headers, row, strict=True)) for row in build_seed_tables(load_control())["trade_sources"]]


def validate_migration(
    universe_rows: list[dict[str, str]],
    existing: list[dict[str, str]],
    proposed: list[dict[str, str]],
) -> dict[str, Any]:
    before_rows = [row for row in existing if str(row.get("playbook_id") or "") not in RETIRED_PLAYBOOK_IDS]
    before = compile_playbook_policies(before_rows)
    after = compile_playbook_policies(proposed)
    if not before.ok:
        raise ValueError("current non-Mike policy does not compile: " + "; ".join(before.errors))
    if not after.ok:
        raise ValueError("proposed policy does not compile: " + "; ".join(after.errors))

    before_hashes = {policy.playbook_id: policy.policy_hash for policy in before.policies}
    after_hashes = {policy.playbook_id: policy.policy_hash for policy in after.policies}
    acceptors = set(EXACT_ACCEPTOR_BY_STRUCTURE.values())
    unexpected_changes = sorted(
        playbook_id
        for playbook_id, policy_hash in before_hashes.items()
        if playbook_id not in acceptors and after_hashes.get(playbook_id) != policy_hash
    )
    if unexpected_changes:
        raise ValueError("unrelated compiled policy hashes changed: " + ", ".join(unexpected_changes))

    sources = trade_source_rows()
    gate = validate_sheet_policy(
        load_control(),
        tables={"universe": universe_rows, "playbooks": proposed, "trade_sources": sources},
    )
    if not gate.ok:
        raise ValueError("proposed complete Sheet policy is invalid: " + json.dumps(gate.to_dict(), sort_keys=True))

    exact_owners = {
        policy.structure: policy.playbook_id
        for policy in after.policies
        if "exact_package" in policy.accepted_inputs
    }
    if exact_owners != EXACT_ACCEPTOR_BY_STRUCTURE:
        raise ValueError(f"exact-package ownership mismatch: {exact_owners}")
    return {
        "retired_playbook_ids": sorted(RETIRED_PLAYBOOK_IDS & {str(row.get('playbook_id') or '') for row in existing}),
        "exact_acceptors": exact_owners,
        "unchanged_unrelated_policy_hashes": len(before_hashes) - len(acceptors),
        "sheet_policy": gate.to_dict(),
    }


def _assert_no_working_mike_entry(database: Path) -> None:
    store = CsaStore(database, read_only=True)
    blocked: list[str] = []
    for ticket, _attempt in store.working_shadow_orders():
        lifecycle = store.lifecycle(ticket.lifecycle_id)
        identity = (lifecycle.metadata.get("source_identity") if lifecycle is not None else {}) or {}
        source = str(identity.get("source_profile") or identity.get("source_id") or "").lower()
        playbook_id = str(ticket.metadata.get("playbook_id") or "")
        if source == "mike_butler" or playbook_id in RETIRED_PLAYBOOK_IDS:
            blocked.append(ticket.ticket_id)
    if blocked:
        raise ValueError("working Mike exact-package entries must settle before migration: " + ", ".join(blocked))


def _column_letter(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db", default="data/kamandal_v2.db")
    parser.add_argument("--output-dir", default="data/deploy/trade_source_routing_sheet")
    args = parser.parse_args()

    config = load_control()
    client = GoogleSheetClient.from_config(config)
    tabs = ((config.get("google_sheets") or {}).get("tabs") or {})
    playbooks_title = str(tabs.get("playbooks") or "playbooks")
    universe_title = str(tabs.get("universe") or "universe")
    matrix = client.read_tab_values(playbooks_title)
    if not matrix:
        raise ValueError("playbooks tab is empty")
    header = [str(value).strip() for value in matrix[0]]
    prior_header = PLAYBOOKS_HEADER[:-1]
    if not (
        (len(header) == len(prior_header) and set(header) == set(prior_header))
        or (len(header) == len(PLAYBOOKS_HEADER) and set(header) == set(PLAYBOOKS_HEADER))
    ):
        raise ValueError("playbooks header differs from the approved append-only schema")
    existing = client.read_tab(playbooks_title)
    proposed = migrated_playbook_rows(existing)
    universe = client.read_tab(universe_title)
    validation = validate_migration(universe, existing, proposed)

    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    output_dir = resolve_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "kamandal.trade_source_routing_sheet_migration.v1",
        "generated_at": generated_at,
        "spreadsheet_id_sha256": hashlib.sha256(client.spreadsheet_id.encode()).hexdigest(),
        "applied": False,
        "playbooks_header_before": header,
        "playbook_count_before": sum(bool(str(row.get("playbook_id") or "")) for row in existing),
        "playbook_count_after": len(proposed),
        "trade_sources": trade_source_rows(),
        "validation": validation,
    }
    before_path = output_dir / f"before-{generated_at.replace(':', '').replace('-', '')}.json"
    before_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not args.apply:
        print(json.dumps({**manifest, "before_path": str(before_path)}, indent=2, sort_keys=True))
        return

    _assert_no_working_mike_entry(resolve_path(args.db))
    worksheet = client._spreadsheet.worksheet(playbooks_title)  # noqa: SLF001 - bounded format-preserving migration.
    target_header = header if "accepted_inputs" in header else [*header, "accepted_inputs"]
    if worksheet.col_count < len(target_header):
        worksheet.resize(cols=len(target_header))
    accepted_column = _column_letter(target_header.index("accepted_inputs") + 1)
    updates = [] if "accepted_inputs" in header else [{"range": f"{accepted_column}1", "values": [["accepted_inputs"]]}]
    by_id = {str(row.get("playbook_id") or ""): row for row in proposed}
    retire_ranges: list[str] = []
    source_profiles_column = _column_letter(target_header.index("source_profiles") + 1)
    last_column = _column_letter(len(target_header))
    for row_number, row in enumerate(existing, start=2):
        playbook_id = str(row.get("playbook_id") or "")
        if playbook_id in RETIRED_PLAYBOOK_IDS:
            retire_ranges.append(f"A{row_number}:{last_column}{row_number}")
        elif playbook_id in by_id:
            updates.extend(
                [
                    {"range": f"{accepted_column}{row_number}", "values": [[by_id[playbook_id]["accepted_inputs"]]]},
                    {"range": f"{source_profiles_column}{row_number}", "values": [[""]]},
                ]
            )
    client.batch_update_tab(playbooks_title, updates)
    client.batch_clear_tab(playbooks_title, retire_ranges)

    sources_title = str(tabs.get("trade_sources") or "trade_sources")
    source_rows = trade_source_rows()
    client.replace_tab(
        sources_title,
        header=TRADE_SOURCES_HEADER,
        rows=[[row[column] for column in TRADE_SOURCES_HEADER] for row in source_rows],
    )
    activity_title = str(tabs.get("trade_source_activity") or "trade_source_activity")
    activity_matrix = client.read_tab_values(activity_title)
    if not activity_matrix:
        client.replace_tab(activity_title, header=TRADE_SOURCE_ACTIVITY_HEADER, rows=[])
    elif [str(cell).strip() for cell in activity_matrix[0]] != TRADE_SOURCE_ACTIVITY_HEADER:
        raise RuntimeError("trade_source_activity exists with an unexpected schema")

    readback = {
        "universe": client.read_tab(universe_title),
        "playbooks": client.read_tab(playbooks_title),
        "trade_sources": client.read_tab(sources_title),
    }
    readback_gate = validate_sheet_policy(config, tables=readback)
    remaining = RETIRED_PLAYBOOK_IDS & {str(row.get("playbook_id") or "") for row in readback["playbooks"]}
    if remaining or not readback_gate.ok:
        raise RuntimeError(
            "Sheet migration readback failed: "
            + json.dumps({"remaining_retired": sorted(remaining), "gate": readback_gate.to_dict()}, sort_keys=True)
        )
    receipt = {
        **manifest,
        "applied": True,
        "before_path": str(before_path),
        "readback": readback_gate.to_dict(),
    }
    receipt_path = output_dir / "latest.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "applied", "receipt_path": str(receipt_path), "readback": readback_gate.to_dict()}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
