"""Add one-contract exact short puts; dry-run unless explicitly applied."""
from copy import deepcopy
import json
from scripts.apply_two_entry_pathways_sheet import main


def proposed_tables(tables):
    result = deepcopy(tables)
    rows = result['playbooks']
    templates = [r for r in rows if r.get('playbook_id') == 'short_put_premium_harvest']
    if len(templates) != 1:
        raise ValueError('missing/duplicate short-put management template')
    template = templates[0]
    if (str(template.get('enabled')).upper() != 'FALSE'
            or template.get('accepted_inputs') != 'idea'
            or float(template.get('live_max_bpr_per_order') or 0) != 2500
            or template.get('execution_venue') != 'public_primary'):
        raise ValueError('approved short-put template changed')
    row = dict(template)
    row.update(playbook_id='guru_exact_short_put', enabled='TRUE', csa_stage='live',
               variant='source_exact', accepted_inputs='exact_package', max_contracts='1',
               management_policy_json=json.dumps({'lifecycle': {'close_only': True, 'fill': {'max_attempts': 4, 'price_increment': 0.05}}}, separators=(',', ':')),
               rationale='Copy one source-observed short put with current quotes and native broker buying power.',
               notes='Exact-only, one contract, $2500 order BPR cap, existing Guru sleeve. Generic close-only management; no automatic assignment or rolling. Ideas short puts remain disabled.')
    matches = [r for r in rows if r.get('playbook_id') == row['playbook_id']]
    if matches:
        if len(matches) != 1 or matches[0] != row:
            raise ValueError('existing exact short-put policy differs')
    else:
        destination = max(i for i, existing in enumerate(rows) if existing.get('playbook_id')) + 1
        if destination >= len(rows) or any(str(v).strip() for k, v in rows[destination].items()
                if not (k in {'range_gate_required', 'resting_profit_enabled'} and str(v).upper() == 'FALSE')):
            raise ValueError('destination contains operator data or has no reserved row')
        rows[destination] = row
    for source in ('greg_harmon', 'mike_butler'):
        matches = [r for r in result['trade_sources'] if r.get('source_id') == source and r.get('output_kind') == 'exact_package']
        if len(matches) != 1:
            raise ValueError('missing/duplicate source policy')
        shapes = [s.strip() for s in matches[0].get('live_structures', '').split(',') if s.strip()]
        matches[0]['live_structures'] = ','.join(dict.fromkeys([*shapes, 'short_put']))
    return result


if __name__ == '__main__':
    main(proposed_tables)
