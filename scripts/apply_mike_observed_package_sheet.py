#!/usr/bin/env python3
"""Apply the bounded Mike observed-package rows to the existing playbooks tab.

This migration never clears or replaces the tab.  It appends one schema column,
fills four existing blank template rows, preserves neighboring formatting and
validation, and writes a before-image plus an exact readback receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kamandal_v2.config import load_control
from kamandal_v2.domain.models import Playbook
from kamandal_v2.paths import resolve_path
from kamandal_v2.schemas import PLAYBOOKS_HEADER
from kamandal_v2.sheets import GoogleSheetClient
from kamandal_v2.strategy_engine.policy import compile_playbook_policies
from kamandal_v2.strategy_lanes.policy import compile_csa_policy


SOURCE_ROWS = {
    "call_calendar": "call_calendar_low_iv",
    "put_calendar": "put_calendar_low_iv",
    "call_diagonal": "call_diagonal_oversold",
    "put_diagonal": "put_diagonal_overextended",
}
TARGET_IDS = {
    "call_calendar": "mike_call_calendar_observed",
    "put_calendar": "mike_put_calendar_observed",
    "call_diagonal": "mike_call_diagonal_observed",
    "put_diagonal": "mike_put_diagonal_observed",
}


def build_rows(existing: list[dict[str, str]]) -> list[dict[str, str]]:
    by_id = {str(row.get("playbook_id") or ""): row for row in existing}
    rows: list[dict[str, str]] = []
    for structure in ("call_calendar", "put_calendar", "call_diagonal", "put_diagonal"):
        source = dict(by_id[SOURCE_ROWS[structure]])
        source.update(
            {
                "playbook_id": TARGET_IDS[structure],
                "enabled": "TRUE",
                "mode": "shadow",
                "csa_stage": "shadow",
                "source_mode": "observed_package",
                "source_profiles": "mike_butler",
                "variant": "source_observed",
                "sizing_method": "fixed_contracts",
                "sizing_value": "1",
                "max_contracts": "1",
                "live_max_bpr_per_order": "1500",
                "max_bid_ask_pct": "0.20",
                "min_option_oi": "25",
                "paired_order_required": "TRUE",
                "profit_target_pct": "40",
                "range_gate_required": "FALSE",
                "notes": (
                    "Mike-only shadow evidence. construction=source_exact_legs; "
                    "entry mark=first actionable package midpoint; management=Kamandal."
                ),
                "rationale": (
                    "Measure source-exact Mike package entries inside the one Kamandal planner and manager; "
                    "Mike follow-up posts remain benchmark evidence only."
                ),
            }
        )
        if structure in {"call_calendar", "put_calendar"}:
            source.update(
                {
                    "strategy_family": structure,
                    "structure": structure,
                    "avoid_earnings": "FALSE",
                    "earnings_blackout_days": "0",
                    "exit_pre_event_days": "",
                    "exit_dte_min": "0",
                    "half_time_exit": "TRUE",
                    "max_loss_multiple": "1",
                    "management_policy_json": json.dumps(
                        {
                            "lifecycle": {
                                "close_only": True,
                                "fill": {"max_attempts": 4, "price_increment": 0.05},
                            }
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                }
            )
        else:
            source.update(
                {
                    "strategy_family": structure,
                    "structure": structure,
                    "exit_dte_min": "14",
                    "half_time_exit": "FALSE",
                    "max_loss_multiple": "0.5",
                    "max_debit_to_width_ratio": "0.75",
                    "management_policy_json": json.dumps(
                        {
                            "lifecycle": {
                                "fill": {"max_attempts": 4, "price_increment": 0.05},
                                "long_only": {"requires_approval": False},
                                "short_leg": {"roll": False, "roll_dte": 0},
                            }
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                }
            )
        rows.append(source)
    return rows


def validate_proposal(existing: list[dict[str, str]], proposed: list[dict[str, str]]) -> dict[str, Any]:
    before = compile_playbook_policies(existing)
    after = compile_playbook_policies([*existing, *proposed])
    if not before.ok:
        raise ValueError(f"current Sheet policy does not compile: {before.errors}")
    if not after.ok:
        raise ValueError(f"proposed Sheet policy does not compile: {after.errors}")
    before_hashes = {item.playbook_id: item.policy_hash for item in before.policies}
    after_hashes = {item.playbook_id: item.policy_hash for item in after.policies}
    changed = sorted(key for key, value in before_hashes.items() if after_hashes.get(key) != value)
    if changed:
        raise ValueError(f"existing compiled policy hashes changed: {changed}")
    read_at = datetime.now(UTC).isoformat()
    for row in proposed:
        Playbook.from_row(row)
        compiled = compile_csa_policy(row, source="google_sheet", read_at=read_at)
        if compiled is None or compiled.stage != "shadow" or compiled.source_mode != "observed_package":
            raise ValueError(f"{row['playbook_id']} did not compile as observed-package shadow")
    return {
        "existing_policy_count": len(before.policies),
        "proposed_policy_count": len(proposed),
        "unchanged_existing_policy_hashes": len(before_hashes),
        "changed_existing_policy_ids": changed,
        "new_policy_hashes": {key: after_hashes[key] for key in TARGET_IDS.values()},
    }


def _column_letter(index: int) -> str:
    text = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        text = chr(65 + remainder) + text
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output-dir", default="data/deploy/mike_observed_package_sheet")
    args = parser.parse_args()

    config = load_control()
    client = GoogleSheetClient.from_config(config)
    title = str((((config.get("google_sheets") or {}).get("tabs") or {}).get("playbooks") or "playbooks"))
    matrix = client.read_tab_values(title)
    if not matrix:
        raise ValueError("playbooks tab is empty")
    header = [str(value).strip() for value in matrix[0]]
    schema_without_source = set(PLAYBOOKS_HEADER[:-1])
    schema_with_source = set(PLAYBOOKS_HEADER)
    if not (
        (len(header) == len(PLAYBOOKS_HEADER) - 1 and set(header) == schema_without_source)
        or (len(header) == len(PLAYBOOKS_HEADER) and set(header) == schema_with_source)
    ):
        raise ValueError("playbooks header differs from the approved append-only schema")
    existing = client.read_tab(title)
    proposed = build_rows(existing)
    validation = validate_proposal(existing, proposed)
    existing_by_id = {str(row.get("playbook_id") or ""): index for index, row in enumerate(existing, start=2)}
    target_row_numbers: list[int] = []
    for row in proposed:
        existing_row = existing_by_id.get(row["playbook_id"])
        if existing_row:
            target_row_numbers.append(existing_row)
            continue
        for row_number in range(2, max(len(matrix) + 1, 6)):
            cell = matrix[row_number - 1][0] if row_number - 1 < len(matrix) and matrix[row_number - 1] else ""
            if not str(cell).strip() and row_number not in target_row_numbers:
                target_row_numbers.append(row_number)
                break
        else:
            raise ValueError("no blank template row is available")

    output_dir = resolve_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest = {
        "schema": "kamandal.mike_observed_package_sheet_migration.v1",
        "generated_at": generated_at,
        "spreadsheet_id_sha256": hashlib.sha256(client.spreadsheet_id.encode()).hexdigest(),
        "sheet": title,
        "header_before": header,
        "target_rows": target_row_numbers,
        "source_rows": SOURCE_ROWS,
        "proposed_rows": proposed,
        "validation": validation,
        "applied": False,
    }
    before_path = output_dir / f"before-{generated_at.replace(':', '').replace('-', '')}.json"
    before_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not args.apply:
        print(json.dumps({**manifest, "before_path": str(before_path)}, indent=2, sort_keys=True))
        return

    worksheet = client._spreadsheet.worksheet(title)  # noqa: SLF001 - bounded migration needs native copyPaste.
    target_header = list(header) if "source_profiles" in header else [*header, "source_profiles"]
    if worksheet.col_count < len(target_header):
        worksheet.resize(cols=len(target_header))
    sheet_id = int(worksheet.id)
    requests: list[dict[str, Any]] = []
    if "source_profiles" not in header:
        requests.append(
            {
                "copyPaste": {
                    "source": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": len(header) - 1, "endColumnIndex": len(header)},
                    "destination": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": len(header), "endColumnIndex": len(header) + 1},
                    "pasteType": "PASTE_FORMAT",
                    "pasteOrientation": "NORMAL",
                }
            }
        )
    source_row_numbers = [next(index for index, row in enumerate(existing, start=2) if row.get("playbook_id") == SOURCE_ROWS[structure]) for structure in ("call_calendar", "put_calendar", "call_diagonal", "put_diagonal")]
    for source_row, target_row in zip(source_row_numbers, target_row_numbers, strict=True):
        for paste_type in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
            requests.append(
                {
                    "copyPaste": {
                        "source": {"sheetId": sheet_id, "startRowIndex": source_row - 1, "endRowIndex": source_row, "startColumnIndex": 0, "endColumnIndex": len(header)},
                        "destination": {"sheetId": sheet_id, "startRowIndex": target_row - 1, "endRowIndex": target_row, "startColumnIndex": 0, "endColumnIndex": len(header)},
                        "pasteType": paste_type,
                        "pasteOrientation": "NORMAL",
                    }
                }
            )
    if requests:
        client._spreadsheet.batch_update({"requests": requests})  # noqa: SLF001
    last_col = _column_letter(len(target_header))
    updates = [{"range": f"{last_col}1", "values": [["source_profiles"]]}]
    for row_number, row in zip(target_row_numbers, proposed, strict=True):
        updates.append(
            {
                "range": f"A{row_number}:{last_col}{row_number}",
                "values": [[row.get(column, "") for column in target_header]],
            }
        )
    client.batch_update_tab(title, updates)

    readback_matrix = client.read_tab_values(title)
    readback_header = [str(value).strip() for value in readback_matrix[0]]
    readback_rows = client.read_tab(title)
    readback_by_id = {str(row.get("playbook_id") or ""): row for row in readback_rows}
    if readback_header != target_header:
        raise RuntimeError("Sheet header readback mismatch")
    for expected in proposed:
        actual = readback_by_id.get(expected["playbook_id"])
        if actual is None or any(str(actual.get(key) or "") != str(value or "") for key, value in expected.items()):
            raise RuntimeError(f"Sheet row readback mismatch: {expected['playbook_id']}")
    readback_validation = validate_proposal(
        [row for row in readback_rows if row.get("playbook_id") not in TARGET_IDS.values()],
        [readback_by_id[key] for key in TARGET_IDS.values()],
    )
    receipt = {
        **manifest,
        "applied": True,
        "header_after": readback_header,
        "readback_policy_ids": list(TARGET_IDS.values()),
        "readback_validation": readback_validation,
        "before_path": str(before_path),
    }
    receipt_path = output_dir / "latest.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "applied", "receipt_path": str(receipt_path), "target_rows": target_row_numbers, "policy_ids": list(TARGET_IDS.values())}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
