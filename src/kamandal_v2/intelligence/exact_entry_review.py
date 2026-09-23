"""Evidence-only exact-entry review. Never authorizes execution or allocation."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from kamandal_v2.intelligence.trade_sources import LIVE_EXACT_STRUCTURES

REVIEW_VERSION = 'exact-entry-review-v1'
LEG_COUNTS = {
    'long_call': 1, 'short_put': 1, 'covered_call': 1,
    'call_spread': 2, 'put_spread': 2, 'call_calendar': 2,
    'put_calendar': 2, 'call_diagonal': 2, 'put_diagonal': 2,
    'short_strangle': 2, 'short_straddle': 2,
    'short_put_financed_long_calls': 2, 'butterfly': 3,
    'call_crab': 3, 'bull_call_spread_with_short_put': 3,
    'call_calendar_with_short_put': 3, 'put_ratio_1x2': 3,
    'iron_condor': 4, 'split_call_fly': 4, 'super_bull': 4,
}


def possible_edit_duplicates(records):
    """Flag matching nearby source evidence, without claiming unknown edit lineage."""
    groups = defaultdict(list)
    for record in records:
        media = tuple(sorted(str(m.get('sha256')) for m in record.get('source', {}).get('media', []) if m.get('sha256')))
        if not media:
            continue
        text = re.sub(r'https?://\S+', '', record.get('literal', {}).get('text', '')).strip()
        groups[(record.get('profile_id'), text, media)].append(record)
    result = {}
    for group in groups.values():
        for record in group:
            stamp = datetime.fromisoformat(record['source']['published_at'].replace('Z', '+00:00'))
            peers = [r['signal_id'] for r in group if r['signal_id'] != record['signal_id'] and abs((datetime.fromisoformat(r['source']['published_at'].replace('Z','+00:00'))-stamp).total_seconds()) <= 120]
            if peers:
                result[record['signal_id']] = sorted(set(peers))
    return result


def build_review(packet, compilation):
    records = {r['signal_id']: r for r in packet['records']}
    duplicates = possible_edit_duplicates(packet['records'])
    rows = []
    episodes = compilation.to_dict() if hasattr(compilation, 'to_dict') else compilation
    for episode in episodes['episodes']:
        record = records[episode['post_ref']]
        for event in episode['events']:
            blockers = []
            if event['action'] not in {'open', 'scale_in'}:
                blockers.append('follow_up_not_entry')
            if event.get('template_number') is not None:
                blockers.append('template_not_confirmed_contract_entry')
            if episode['post_ref'] in duplicates:
                blockers.append('possible_edit_duplicate_requires_resolution')
            if event.get('evidence_status') != 'complete':
                blockers.append('source_evidence_incomplete')
            packages = event.get('exact_packages', [])
            if not packages or any(not p.get('complete') or not p.get('legs') for p in packages):
                blockers.append('exact_contracts_incomplete')
            if event.get('structure_hint') == 'covered_call':
                blockers.append('existing_share_coverage_requires_verification')
            if event.get('link_state') in {'needs_history', 'ambiguous'}:
                blockers.append('lifecycle_reference_unresolved')
            if not event.get('symbol'):
                blockers.append('symbol_unresolved')
            for package in packages:
                expected_legs = LEG_COUNTS.get(event.get('structure_hint'))
                if expected_legs is None or len(package.get('legs', [])) != expected_legs:
                    blockers.append('structure_leg_count_requires_review')
                for leg in package.get('legs', []):
                    try:
                        strike = Decimal(str(leg.get('strike')))
                        quantity = Decimal(str(leg.get('quantity')))
                        if not strike.is_finite() or strike <= 0 or not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value():
                            raise ValueError('invalid contract quantity or strike')
                        if leg.get('order_code') not in {'BTO', 'STO'} or leg.get('option_type') not in {'call', 'put'}:
                            raise ValueError('invalid opening contract')
                    except (InvalidOperation, ValueError):
                        blockers.append('invalid_or_nonopening_contract_leg')
                    try:
                        datetime.strptime(leg['expiration'], '%b %d %Y')
                    except (KeyError, ValueError):
                        try:
                            datetime.strptime(leg.get('expiration',''), '%Y-%m-%d')
                        except ValueError:
                            blockers.append('expiration_date_unresolved')
            # Execution capability is separate from transcription completeness.
            capability = 'supported_structure_requires_live_gates' if event.get('structure_hint') in LIVE_EXACT_STRUCTURES else 'unsupported_live_exact_structure'
            rows.append({'post_ref':episode['post_ref'], 'source_url':record['source'].get('source_url'),
                         'event_id':event['event_id'], 'symbol':event.get('symbol'),
                         'action':event['action'], 'structure':event.get('structure_hint'),
                         'contracts':packages, 'review_blockers':sorted(set(blockers)),
                         'translation_status':'reviewable_contracts' if not blockers else 'blocked',
                         'live_capability':capability, 'execution_authorized':False})
    return {'schema':'kamandal.exact_entry_review.v1','review_version':REVIEW_VERSION,
            'source_sha256':hashlib.sha256(json.dumps(packet,sort_keys=True,default=str).encode()).hexdigest(),
            'acquisition_status':packet.get('acquisition',{}).get('status','unknown'),
            'post_count':len(records),'empty_episode_posts':[e['post_ref'] for e in episodes['episodes'] if not e['events']],
            'possible_edit_duplicates':duplicates, 'rows':rows,
            'management_contract':'guru_entry_with_kamandal_management',
            'effects':{'sheet_write':False,'idea_publication':False,'broker':False,'allocation_change':False}}
