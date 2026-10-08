"""Add exact vertical capability for the authorized two-pathway release.

Dry-run by default. Preserve existing policy hashes, source switches and caps;
apply cell deltas atomically only after a fresh concurrency check.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json

from kamandal_v2.config import load_control
from kamandal_v2.sheets import GoogleSheetClient
from kamandal_v2.strategy_engine.policy import compile_playbook_policies
from kamandal_v2.strategy_engine.sheet_policy_gate import validate_sheet_policy

SHAPES = ("call_spread", "put_spread")
TABS = ("playbooks", "trade_sources", "universe", "portfolio_sleeves")
TEMPLATE = "guru_exact_call_calendar"


def proposed_tables(tables):
    result = deepcopy(tables)
    rows = result["playbooks"]
    templates = [row for row in rows if row.get("playbook_id") == TEMPLATE]
    if len(templates) != 1:
        raise ValueError("missing/duplicate exact management template")
    template = templates[0]
    if (template.get("accepted_inputs") != "exact_package"
            or float(template.get("live_max_bpr_per_order") or 0) != 1200
            or str(template.get("max_contracts")) != "1"):
        raise ValueError("approved template size/cash policy changed")
    for shape in SHAPES:
        identity = f"guru_exact_{shape}"
        matches = [row for row in rows if row.get("playbook_id") == identity]
        if matches:
            if len(matches) != 1 or matches[0].get("structure") != shape or matches[0].get("accepted_inputs") != "exact_package":
                raise ValueError("existing exact vertical identity changed")
            continue
        row = dict(template)
        row.update(playbook_id=identity, strategy_family=shape, structure=shape,
                   enabled="TRUE", mode="live", csa_stage="live", leg_count="2",
                   variant="source_exact", accepted_inputs="exact_package",
                   spread_width="5", short_delta_min="0", short_delta_max="1",
                   long_delta_min="0", long_delta_max="1", long_dte_min="", long_dte_max="",
                   resting_profit_enabled="FALSE", half_time_exit="FALSE",
                   rationale="Copy interpreted source contracts; current quotes, bounded package risk and full-package exits.",
                   notes="One exact vertical under $1200 package cap. Construction hints do not filter source strikes, DTE or delta. 40% Guru sleeve applies.")
        destination = max(i for i, existing in enumerate(rows) if existing.get("playbook_id")) + 1
        if destination >= len(rows) or any(str(value).strip() for key, value in rows[destination].items()
                if not (key in {"range_gate_required", "resting_profit_enabled"} and str(value).upper() == "FALSE")):
            raise ValueError("vertical destination contains operator data or has no reserved row")
        rows[destination] = row
    for source in ("greg_harmon", "mike_butler"):
        matches = [row for row in result["trade_sources"]
                   if row.get("source_id") == source and row.get("output_kind") == "exact_package"]
        if len(matches) != 1:
            raise ValueError("missing/duplicate source policy")
        shapes = [value.strip() for value in matches[0].get("live_structures", "").split(",") if value.strip()]
        matches[0]["live_structures"] = ",".join(dict.fromkeys([*shapes, *SHAPES]))
    for row in result["portfolio_sleeves"]:
        if row.get("lane") == "current_idea":
            row["meaning"] = "Optimized user ideas and directional source commentary"
    return result


def main(propose=proposed_tables):
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = load_control()
    client = GoogleSheetClient.from_config(config)
    tables = {tab: client.read_tab(tab) for tab in TABS}
    proposed = propose(tables)
    before, after = (compile_playbook_policies(value["playbooks"]) for value in (tables, proposed))
    if not before.ok or not after.ok:
        raise ValueError(f"compilation failed: {before.errors}, {after.errors}")
    hashes = {policy.playbook_id: policy.policy_hash for policy in after.policies}
    if any(hashes[policy.playbook_id] != policy.policy_hash for policy in before.policies):
        raise ValueError("existing policy changed")
    gate = validate_sheet_policy(config, tables=proposed)
    if not gate.ok:
        raise ValueError("full Sheet gate failed: " + json.dumps(gate.to_dict()))
    requests, changes, ranges, new_rows = [], [], [], []
    for tab in ("playbooks", "trade_sources", "portfolio_sleeves"):
        worksheet = client._spreadsheet.worksheet(tab)
        header = client.read_tab_values(tab)[0]
        for i, (old, new) in enumerate(zip(tables[tab], proposed[tab], strict=True), start=1):
            if old == new:
                continue
            ranges.append(f"'{tab}'!A{i+1}:CA{i+1}")
            if tab == "playbooks" and not old.get("playbook_id"):
                template_index = next(j for j, row in enumerate(tables[tab], start=1) if row.get("playbook_id") == TEMPLATE)
                for paste_type in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
                    requests.append({"copyPaste": {
                        "source": {"sheetId": worksheet.id, "startRowIndex": template_index, "endRowIndex": template_index+1, "startColumnIndex": 0, "endColumnIndex": len(header)},
                        "destination": {"sheetId": worksheet.id, "startRowIndex": i, "endRowIndex": i+1, "startColumnIndex": 0, "endColumnIndex": len(header)},
                        "pasteType": paste_type}})
                new_rows.append(i+1)
            for j, column in enumerate(header):
                value = str(new.get(column, ""))
                if str(old.get(column, "")) == value:
                    continue
                changes.append({"tab": tab, "row": i+1, "column": column})
                requests.append({"updateCells": {
                    "range": {"sheetId": worksheet.id, "startRowIndex": i, "endRowIndex": i+1, "startColumnIndex": j, "endColumnIndex": j+1},
                    "rows": [{"values": [{"userEnteredValue": {"boolValue": value == "TRUE"} if value in {"TRUE", "FALSE"} else {"stringValue": value}}]}],
                    "fields": "userEnteredValue"}})
    if ranges:
        metadata = client._spreadsheet.fetch_sheet_metadata(params={"includeGridData": True, "ranges": ranges})
        if any("formulaValue" in cell.get("userEnteredValue", {}) or cell.get("chipRuns")
               for sheet in metadata.get("sheets", []) for block in sheet.get("data", [])
               for row in block.get("rowData", []) for cell in row.get("values", [])):
            raise ValueError("target contains formula or rich chip")
    summary = {"gate_ok": True, "new_rows": new_rows, "changed_cells": len(changes), "changes": changes, "applied": False}
    if args.apply and requests:
        if any(client.read_tab(tab) != rows for tab, rows in tables.items()):
            raise ValueError("Sheet changed since validation")
        client._spreadsheet.batch_update({"requests": requests})
        observed = {tab: client.read_tab(tab) for tab in TABS}
        if observed != proposed or not validate_sheet_policy(config, tables=observed).ok:
            raise RuntimeError("readback mismatch")
        summary.update(applied=True, readback_gate_ok=True)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
