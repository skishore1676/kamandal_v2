"""Raise Guru calendar near DTE and add bounded exact-package shadow rows.

The Sheet remains the policy owner. This migration only edits two existing
calendar DTE cells and appends four new rows after validating the full policy.
It never changes trade_sources or the live structure allowlist.
"""

from __future__ import annotations

import argparse
import json

from kamandal_v2.config import load_control
from kamandal_v2.sheets import GoogleSheetClient
from kamandal_v2.strategy_engine.policy import compile_playbook_policies
from kamandal_v2.strategy_engine.sheet_policy_gate import validate_sheet_policy


CALENDARS = ("guru_exact_call_calendar", "guru_exact_put_calendar")
SHADOW_SHAPES = (
    ("long_call", 1, 3),
    ("call_butterfly", 3, 2),
    ("put_butterfly", 3, 2),
    ("call_crab", 3, 2),
)


def proposed_playbooks(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_id = {row.get("playbook_id", ""): row for row in rows}
    if any(playbook_id not in by_id for playbook_id in CALENDARS):
        raise ValueError("both Guru exact calendar rows are required")
    if any(f"guru_exact_{shape}_shadow" in by_id for shape, _, _ in SHADOW_SHAPES):
        raise ValueError("shadow shape rows already exist; inspect them before retrying")
    proposed = [dict(row) for row in rows]
    for row in proposed:
        if row.get("playbook_id") in CALENDARS:
            if str(row.get("dte_max")) != "60":
                raise ValueError(f"{row['playbook_id']} DTE changed; inspect before retrying")
            row["dte_max"] = "90"
            row["notes"] = str(row.get("notes") or "") + " Near-leg DTE cap: 90 days."

    template = by_id["guru_exact_call_calendar"]
    for shape, leg_count, max_contracts in SHADOW_SHAPES:
        row = dict(template)
        row.update({
            "playbook_id": f"guru_exact_{shape}_shadow",
            "enabled": "TRUE",
            "csa_stage": "shadow",
            "strategy_family": shape,
            "structure": shape,
            "source_mode": "idea",
            "variant": "source_exact_shadow",
            "leg_count": str(leg_count),
            "mode": "shadow",
            "accepted_inputs": "exact_package",
            "source_profiles": "",
            "dte_min": "1",
            "dte_max": "120",
            "long_dte_min": "2" if shape == "call_crab" else "",
            "long_dte_max": "180" if shape == "call_crab" else "",
            "short_delta_min": "",
            "short_delta_max": "",
            "long_delta_min": "",
            "long_delta_max": "",
            "max_contracts": str(max_contracts),
            "live_max_bpr_per_order": "1200",
            "max_bid_ask_pct": "0.3",
            "management_policy_json": json.dumps({"lifecycle": {
                "close_only": True,
                "fill": {"max_attempts": 4, "price_increment": 0.05},
            }}, separators=(",", ":")),
            "resting_profit_enabled": "FALSE",
            "rationale": "Observe exact Guru opening and close as a bounded paper package; no live submission.",
            "notes": "Shadow only. Exact contracts and quoted package required. No broker order.",
        })
        proposed.append(row)
    return proposed


def column_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    config = load_control()
    client = GoogleSheetClient.from_config(config)
    matrix = client.read_tab_values("playbooks")
    if not matrix:
        raise ValueError("playbooks tab is empty")
    header = [str(cell).strip() for cell in matrix[0]]
    if not {"playbook_id", "dte_max", "notes", "accepted_inputs", "mode"}.issubset(header):
        raise ValueError("playbooks header missing expected controls")
    rows = client.read_tab("playbooks")
    if len(rows) != len(matrix) - 1:
        raise ValueError("playbooks has blank interior rows; inspect before append")
    proposed = proposed_playbooks(rows)
    before = compile_playbook_policies(rows)
    after = compile_playbook_policies(proposed)
    if not before.ok or not after.ok:
        raise ValueError(f"policy compilation failed: before={before.errors}, after={after.errors}")
    before_hashes = {item.playbook_id: item.policy_hash for item in before.policies}
    after_hashes = {item.playbook_id: item.policy_hash for item in after.policies}
    expected_changed = set(CALENDARS) | {f"guru_exact_{shape}_shadow" for shape, _, _ in SHADOW_SHAPES}
    changed = {key for key, digest in after_hashes.items() if before_hashes.get(key) != digest}
    if changed != expected_changed:
        raise ValueError(f"unexpected compiled policy changes: {sorted(changed ^ expected_changed)}")
    tables = {
        "universe": client.read_tab("universe"),
        "playbooks": proposed,
        "trade_sources": client.read_tab("trade_sources"),
        "portfolio_sleeves": client.read_tab("portfolio_sleeves"),
    }
    gate = validate_sheet_policy(config, tables=tables)
    if not gate.ok:
        raise ValueError("proposed Sheet policy invalid: " + json.dumps(gate.to_dict(), sort_keys=True))
    summary = {"calendar_dte_max": 90, "shadow_playbooks": sorted(expected_changed - set(CALENDARS)),
               "unrelated_policy_hashes_unchanged": len(before_hashes) - len(CALENDARS), "gate_ok": gate.ok}
    if not args.apply:
        print(json.dumps({**summary, "applied": False}, sort_keys=True))
        return

    dte_col = column_letter(header.index("dte_max") + 1)
    notes_col = column_letter(header.index("notes") + 1)
    by_id = {row.get("playbook_id"): row for row in proposed}
    updates = []
    for row_number, row in enumerate(rows, start=2):
        playbook_id = row.get("playbook_id")
        if playbook_id in CALENDARS:
            updates.extend((
                {"range": f"{dte_col}{row_number}", "values": [["90"]]},
                {"range": f"{notes_col}{row_number}", "values": [[by_id[playbook_id]["notes"]]]},
            ))
    first_new = len(matrix) + 1
    last_new = first_new + len(SHADOW_SHAPES) - 1
    last_col = column_letter(len(header))
    physical_rows, physical_cols = client.tab_dimensions("playbooks")
    if physical_rows < last_new:
        client.resize_tab("playbooks", rows=last_new, cols=physical_cols)
    updates.append({
        "range": f"A{first_new}:{last_col}{last_new}",
        "values": [[row.get(column, "") for column in header] for row in proposed[-len(SHADOW_SHAPES):]],
    })
    client.batch_update_tab("playbooks", updates)
    observed = client.read_tab("playbooks")
    if observed != proposed:
        raise RuntimeError("playbooks Sheet readback differs from proposed policy")
    readback_gate = validate_sheet_policy(config, tables={**tables, "playbooks": observed})
    if not readback_gate.ok:
        raise RuntimeError("playbooks Sheet readback failed full policy gate")
    worksheet = client._spreadsheet.worksheet("playbooks")  # noqa: SLF001 - format appended operator rows.
    worksheet.format(f"A{first_new}:{last_col}{last_new}", {
        "backgroundColor": {"red": 0.91, "green": 0.94, "blue": 1.0},
    })
    print(json.dumps({**summary, "applied": True, "first_new_row": first_new,
                      "last_new_row": last_new, "readback_gate_ok": readback_gate.ok}, sort_keys=True))


if __name__ == "__main__":
    main()
