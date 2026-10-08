"""Add the bounded Greg split-call-fly capability; dry-run unless --apply."""
from copy import deepcopy
from scripts.apply_two_entry_pathways_sheet import main


def proposed_tables(tables):
    result = deepcopy(tables)
    rows = result['playbooks']
    templates = [r for r in rows if r.get('playbook_id') == 'guru_exact_call_calendar']
    if len(templates) != 1:
        raise ValueError('missing/duplicate exact management template')
    template = templates[0]
    if (template.get('accepted_inputs') != 'exact_package'
            or float(template.get('live_max_bpr_per_order') or 0) != 1200
            or str(template.get('max_contracts')) != '1'):
        raise ValueError('approved template size/cash policy changed')
    identity = 'guru_exact_split_call_fly'
    matches = [r for r in rows if r.get('playbook_id') == identity]
    if matches:
        if len(matches) != 1 or matches[0].get('structure') != 'split_call_fly' or matches[0].get('accepted_inputs') != 'exact_package':
            raise ValueError('existing split-call-fly identity changed')
    else:
        row = dict(template)
        row.update(playbook_id=identity, strategy_family='split_call_fly', structure='split_call_fly',
                   leg_count='4', variant='source_exact', accepted_inputs='exact_package',
                   dte_min='1', long_dte_min='', long_dte_max='',
                   resting_profit_enabled='FALSE', half_time_exit='FALSE',
                   rationale='Preserve all four source call contracts as one managed package.',
                   notes='One 1:1:1:1 package under $1200 risk cap and 40% Guru sleeve. Full-package exits; no source-leg substitution.')
        destination = max(i for i, existing in enumerate(rows) if existing.get('playbook_id')) + 1
        if destination >= len(rows) or any(str(value).strip() for key, value in rows[destination].items()
                if not (key in {'range_gate_required', 'resting_profit_enabled'} and str(value).upper() == 'FALSE')):
            raise ValueError('destination contains operator data or has no reserved row')
        rows[destination] = row
    sources = [r for r in result['trade_sources'] if r.get('source_id') == 'greg_harmon' and r.get('output_kind') == 'exact_package']
    if len(sources) != 1:
        raise ValueError('missing/duplicate Greg source policy')
    shapes = [s.strip() for s in sources[0].get('live_structures', '').split(',') if s.strip()]
    sources[0]['live_structures'] = ','.join(dict.fromkeys([*shapes, 'split_call_fly']))
    return result


if __name__ == '__main__':
    main(proposed_tables)
