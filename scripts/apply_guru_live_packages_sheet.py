"""Activate explicitly authorized Guru shapes atomically; dry-run by default.

Preserves source on/off switches, allocations, ordinary playbooks and cash caps.
Existing shadow lifecycle policies remain frozen in their entry snapshots.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
from kamandal_v2.config import load_control
from kamandal_v2.sheets import GoogleSheetClient
from kamandal_v2.strategy_engine.policy import compile_playbook_policies
from kamandal_v2.strategy_engine.sheet_policy_gate import validate_sheet_policy

SHAPES = ('long_call','call_butterfly','put_butterfly','call_crab')
PROMOTED = {f'guru_exact_{shape}_shadow' for shape in SHAPES}
BUNDLE = 'guru_exact_calendar_bundle'


def proposed_tables(tables):
    result=deepcopy(tables)
    rows=result['playbooks']
    for shape in SHAPES:
        matches=[r for r in rows if r.get('playbook_id')==f'guru_exact_{shape}_shadow']
        if len(matches)!=1: raise ValueError(f'missing/duplicate {shape} row')
        row=matches[0]
        if row.get('structure')!=shape or row.get('accepted_inputs')!='exact_package':
            raise ValueError('Guru policy identity changed')
        if str(row.get('enabled')).upper()!='TRUE': raise ValueError('Guru policy disabled by operator')
        if float(row.get('live_max_bpr_per_order') or 0)!=1200: raise ValueError('Guru cash cap changed')
        if row.get('mode') not in {'shadow','live'} or row.get('csa_stage') not in {'shadow','live'}:
            raise ValueError('unexpected Guru stage')
        row.update(mode='live',csa_stage='live',variant='source_exact',half_time_exit='FALSE',resting_profit_enabled='FALSE')
        # Keep IDs stable so prior shadow evidence remains attributable.
        if shape=='long_call': row['max_contracts']='1'
        management=json.loads(row.get('management_policy_json') or '{}')
        lifecycle=management.setdefault('lifecycle',{})
        lifecycle['close_only']=True
        if shape in {'call_butterfly','put_butterfly'}:
            row.update(dte_min='0',exit_dte_min='0')
            lifecycle.update(expiry_day_entry_minutes_before_close=60,expiry_day_exit_minutes_before_close=30)
        elif shape=='call_crab':
            row.update(dte_min=str(max(2,int(row.get('dte_min') or 0))),exit_dte_min='1')
        row['management_policy_json']=json.dumps(management,separators=(',',':'))
        row['rationale']='Copy verified Guru contracts; Kamandal manages full-package exits under the existing cash cap.'
        row['notes']='Live exact package. Stable legacy ID retains shadow suffix. No historical replay. Source switch and 40% Guru sleeve apply.'
    existing=[r for r in rows if r.get('playbook_id')==BUNDLE]
    if not existing:
        template=next(r for r in rows if r.get('playbook_id')=='guru_exact_call_calendar')
        row=dict(template)
        row.update(playbook_id=BUNDLE,strategy_family='calendar_bundle',structure='calendar_bundle',leg_count='6',
            enabled='TRUE',mode='live',csa_stage='live',source_mode='idea',accepted_inputs='exact_package',variant='source_exact',
            max_contracts='1',live_max_bpr_per_order='1200',dte_min='1',exit_dte_min='0',half_time_exit='FALSE',
            resting_profit_enabled='FALSE',short_delta_min='',short_delta_max='',long_delta_min='',long_delta_max='',
            management_policy_json=json.dumps({'lifecycle':{'close_only':True,'fill':{'max_attempts':4,'price_increment':.05}}},separators=(',',':')),
            rationale='Copy two or three verified calendars in one atomic four- or six-leg order.',
            notes='Entire group shares $1200 cap. Any missing/revised member blocks the group. No partial entry or historical replay.')
        i=max(i for i,r in enumerate(rows) if r.get('playbook_id'))+1
        if i>=len(rows) or {k:v for k,v in rows[i].items() if str(v).strip()} not in ({},{'range_gate_required':'FALSE','resting_profit_enabled':'FALSE'}):
            raise ValueError('bundle destination contains operator data')
        rows[i]=row
    elif len(existing)!=1 or existing[0].get('structure')!='calendar_bundle': raise ValueError('bundle identity changed')
    for source in ('greg_harmon','mike_butler'):
        matches=[r for r in result['trade_sources'] if r.get('source_id')==source and r.get('output_kind')=='exact_package']
        if len(matches)!=1: raise ValueError('missing source policy')
        row=matches[0]
        structures=[v.strip() for v in row.get('live_structures','').split(',') if v.strip()]
        for shape in (*SHAPES,'calendar_bundle'):
            if shape not in structures: structures.append(shape)
        row['live_structures']=','.join(structures)
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    config=load_control();client=GoogleSheetClient.from_config(config)
    tabs=('playbooks','trade_sources','universe','portfolio_sleeves')
    tables={tab:client.read_tab(tab) for tab in tabs}
    proposed=proposed_tables(tables)
    before,after=(compile_playbook_policies(t['playbooks']) for t in (tables,proposed))
    if not before.ok or not after.ok: raise ValueError(f'compilation failed: {before.errors}, {after.errors}')
    hashes={p.playbook_id:p.policy_hash for p in after.policies}
    if any(hashes[p.playbook_id]!=p.policy_hash for p in before.policies if p.playbook_id not in PROMOTED):
        raise ValueError('unrelated policy changed')
    gate=validate_sheet_policy(config,tables=proposed)
    if not gate.ok: raise ValueError('full Sheet gate failed: '+json.dumps(gate.to_dict()))
    requests=[];changes=[];target_rows=[]
    for tab in ('playbooks','trade_sources'):
        worksheet=client._spreadsheet.worksheet(tab);header=client.read_tab_values(tab)[0]
        for i,(old,new) in enumerate(zip(tables[tab],proposed[tab],strict=True),start=1):
            if tab=='playbooks' and new.get('playbook_id') in PROMOTED|{BUNDLE}: target_rows.append(i+1)
            for j,column in enumerate(header):
                value=str(new.get(column,''))
                if str(old.get(column,''))==value: continue
                changes.append({'tab':tab,'row':i+1,'column':column})
                requests.append({'updateCells':{'range':{'sheetId':worksheet.id,'startRowIndex':i,'endRowIndex':i+1,'startColumnIndex':j,'endColumnIndex':j+1},
                    'rows':[{'values':[{'userEnteredValue':{'boolValue':value=='TRUE'} if value in {'TRUE','FALSE'} else {'stringValue':value}}]}], 'fields':'userEnteredValue'}})
        if tab=='playbooks':
            j=header.index('accepted_inputs')
            for rownum in target_rows:
                requests.append({'setDataValidation':{'range':{'sheetId':worksheet.id,'startRowIndex':rownum-1,'endRowIndex':rownum,'startColumnIndex':j,'endColumnIndex':j+1},
                    'rule':{'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':'exact_package'}]},'strict':True,'showCustomUi':True}}})
    metadata=client._spreadsheet.fetch_sheet_metadata(params={'includeGridData':True,'ranges':[*(f'playbooks!A{r}:CA{r}' for r in target_rows),'trade_sources!A1:E5']})
    if any('formulaValue' in cell.get('userEnteredValue',{}) for sh in metadata.get('sheets',[]) for block in sh.get('data',[]) for row in block.get('rowData',[]) for cell in row.get('values',[])):
        raise ValueError('target contains formula')
    summary={'gate_ok':True,'target_rows':target_rows,'changed_cells':len(changes),'applied':False}
    if not args.apply: print(json.dumps(summary));return
    if any(client.read_tab(tab)!=rows for tab,rows in tables.items()): raise ValueError('Sheet changed since validation')
    client._spreadsheet.batch_update({'requests':requests})
    observed={tab:client.read_tab(tab) for tab in tabs}
    if observed!=proposed or not validate_sheet_policy(config,tables=observed).ok: raise RuntimeError('readback mismatch')
    print(json.dumps({**summary,'applied':True,'readback_gate_ok':True}))

if __name__=='__main__': main()
