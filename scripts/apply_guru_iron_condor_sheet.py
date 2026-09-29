"""Add the operator-authorized exact Guru condor route; dry-run by default.

Preserves existing playbooks, source modes, universe, and sleeve allocations.
Writes the new policy and two source allowlists in one atomic Sheets request.
"""
from __future__ import annotations

import argparse
import json

from kamandal_v2.config import load_control
from kamandal_v2.sheets import GoogleSheetClient
from kamandal_v2.strategy_engine.policy import compile_playbook_policies
from kamandal_v2.strategy_engine.sheet_policy_gate import validate_sheet_policy

PLAYBOOK_ID = 'guru_exact_iron_condor'


def proposed_tables(tables):
    result = {name: [dict(row) for row in rows] for name, rows in tables.items()}
    rows = result['playbooks']
    template = next(row for row in rows if row.get('playbook_id') == 'iron_condor_default')
    row = dict(template)
    row.update({
        'playbook_id': PLAYBOOK_ID, 'enabled': 'TRUE', 'mode': 'live', 'csa_stage': 'live',
        'source_mode': 'idea', 'accepted_inputs': 'exact_package', 'variant': 'source_exact',
        'dte_min': '0', 'exit_dte_min': '0', 'half_time_exit': 'FALSE',
        'sizing_method': 'fixed_contracts', 'sizing_value': '1', 'max_contracts': '1',
        'management_policy_json': json.dumps({'lifecycle': {
            'close_only': True, 'fill': {'max_attempts': 4, 'price_increment': 0.05},
            'expiry_day_entry_minutes_before_close': 60, 'expiry_day_exit_minutes_before_close': 30,
        }}, separators=(',', ':')),
        'rationale': 'Copy independently verified four-leg Guru iron condors; Kamandal manages full-package exits.',
        'notes': 'Exact equal-quantity opening legs only. One contract; existing condor risk, liquidity, profit and loss limits. SPX requires broker-resolved PM-settled SPXW. Expiry-day entry stops 60 minutes before close; close starts 30 minutes before close, including early sessions.',
    })
    # Inherit conservative existing limits; never silently raise them.
    if float(row['live_max_bpr_per_order']) > 500 or float(row['dte_max']) > 50:
        raise ValueError('condor template limits changed; inspect before activation')
    existing = [i for i,r in enumerate(rows) if r.get('playbook_id') == PLAYBOOK_ID]
    if existing:
        if len(existing) != 1 or rows[existing[0]] != row:
            raise ValueError('existing Guru condor policy differs; do not overwrite operator edits')
    else:
        index = max(i for i,r in enumerate(rows) if r.get('playbook_id')) + 1
        if index >= len(rows):
            raise ValueError('no prepared blank playbook row')
        populated = {k:v for k,v in rows[index].items() if str(v).strip()}
        if populated not in ({}, {'range_gate_required':'FALSE','resting_profit_enabled':'FALSE'}):
            raise ValueError('destination row contains operator data')
        rows[index] = row
    for source in ('greg_harmon', 'mike_butler'):
        matches = [r for r in result['trade_sources'] if r.get('source_id') == source and r.get('output_kind') == 'exact_package']
        if len(matches) != 1:
            raise ValueError(f'exact source row missing or ambiguous: {source}')
        source_row = matches[0]
        structures = [s.strip() for s in source_row.get('live_structures','').split(',') if s.strip()]
        if 'iron_condor' not in structures:
            source_row['live_structures'] = ','.join([*structures, 'iron_condor'])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    config = load_control()
    client = GoogleSheetClient.from_config(config)
    tables = {tab: client.read_tab(tab) for tab in ('playbooks','trade_sources','universe','portfolio_sleeves')}
    proposed = proposed_tables(tables)
    before, after = (compile_playbook_policies(t['playbooks']) for t in (tables, proposed))
    if not before.ok or not after.ok:
        raise ValueError(f'policy compilation failed: {before.errors}, {after.errors}')
    hashes = {p.playbook_id:p.policy_hash for p in after.policies}
    if any(hashes[p.playbook_id] != p.policy_hash for p in before.policies):
        raise ValueError('existing policy changed')
    gate = validate_sheet_policy(config, tables=proposed)
    if not gate.ok:
        raise ValueError('full Sheet policy gate failed: '+json.dumps(gate.to_dict()))
    requests, changes = [], []
    sheet_ids = {}
    for tab in ('playbooks','trade_sources'):
        worksheet = client._spreadsheet.worksheet(tab)
        sheet_ids[tab] = worksheet.id
        header = client.read_tab_values(tab)[0]
        for i,(old,new) in enumerate(zip(tables[tab], proposed[tab], strict=True), start=1):
            for j,column in enumerate(header):
                if str(old.get(column,'')) == str(new.get(column,'')):
                    continue
                changes.append({'tab':tab, 'row':i+1, 'column':column})
                requests.append({'updateCells': {
                    'range': {'sheetId':worksheet.id,'startRowIndex':i,'endRowIndex':i+1,'startColumnIndex':j,'endColumnIndex':j+1},
                    'rows':[{'values':[{'userEnteredValue':{'stringValue':str(new.get(column,''))}}]}],
                    'fields':'userEnteredValue',
                }})
    # Inspect the exact writable row and source-control cells, preserving native metadata.
    row_number = next(i+2 for i,r in enumerate(proposed['playbooks']) if r.get('playbook_id') == PLAYBOOK_ID)
    metadata = client._spreadsheet.fetch_sheet_metadata(params={'includeGridData':True,
        'ranges':[f"playbooks!A{row_number}:CC{row_number}", 'trade_sources!A1:F5']})
    for sheet in metadata.get('sheets',[]):
        for block in sheet.get('data',[]):
            for row in block.get('rowData',[]):
                for cell in row.get('values',[]):
                    if 'formulaValue' in cell.get('userEnteredValue',{}):
                        raise ValueError('target contains formula; inspect before write')
    summary = {'playbook_id':PLAYBOOK_ID,'row':row_number,'changed_cells':len(changes),
               'sheet_ids':sheet_ids,'gate_ok':gate.ok,'applied':False}
    if not args.apply:
        print(json.dumps(summary,sort_keys=True))
        return
    if any(client.read_tab(tab) != rows for tab,rows in tables.items()):
        raise ValueError('Sheet changed during validation; retry dry-run')
    if requests:
        client._spreadsheet.batch_update({'requests':requests})
    observed = {tab:client.read_tab(tab) for tab in tables}
    if observed != proposed or not validate_sheet_policy(config,tables=observed).ok:
        raise RuntimeError('Sheet readback mismatch; inspect operator controls')
    print(json.dumps({**summary,'applied':True,'readback_gate_ok':True},sort_keys=True))


if __name__ == '__main__':
    main()
