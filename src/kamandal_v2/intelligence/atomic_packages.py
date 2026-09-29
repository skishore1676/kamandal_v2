"""Compose verified calendar openings into one indivisible broker package.

Original evidence stays immutable. The derived identity includes every member;
reconstructing it from the latest feed makes edits/removals revoke old tickets.
"""
from collections import defaultdict
from dataclasses import replace
import hashlib
import json

from kamandal_v2.intelligence.observed_packages import ObservedPackageEvidence, _package_signature


def combine_calendar_openings(packages):
    packages = tuple(packages)
    groups = defaultdict(list)
    for package in packages:
        if package.action == 'open':
            groups[(package.source_profile, package.canonical_post_id, package.symbol)].append(package)
    replacements = {}
    consumed = set()
    for members in groups.values():
        if not 2 <= len(members) <= 3:
            continue
        if any(not p.complete or not p.source_verified or p.source_verification_reason
               or not p.source_verification_ref or p.structure not in {'call_calendar','put_calendar'}
               or p.source_opening_package_count != len(members) or len(p.legs) != 2
               for p in members):
            continue
        if any(not p.opportunity_group_id for p in members):
            continue
        if len({p.product_type for p in members}) != 1:
            continue
        if len({p.package_signature for p in members}) != len(members):
            continue
        legs = tuple(leg for p in sorted(members,key=lambda p:p.package_signature) for leg in p.legs)
        if any(l.quantity != 1 or l.effect != 'open' for l in legs):
            continue
        # No netting, no mixed roots, no ambiguous overlapping ownership.
        if len({(l.expiration,l.option_type,l.strike) for l in legs}) != len(legs):
            continue
        if len({l.expiration for l in legs}) != 2:
            continue
        if any(not p.source_valid_until or not p.source_published_at for p in members):
            continue
        if len({p.source_published_at for p in members}) != 1:
            continue
        refs = sorted((p.package_signature,p.evidence_revision_id,p.source_verification_ref,p.opportunity_group_id) for p in members)
        digest = hashlib.sha256(json.dumps(refs,separators=(',',':')).encode()).hexdigest()[:24]
        first = members[0]
        group_id = hashlib.sha256(f'{first.source_profile}:{first.canonical_post_id}:{first.symbol}'.encode()).hexdigest()[:24]
        combined = replace(first, structure='calendar_bundle', legs=legs,
            opportunity_group_id='corr_group_'+group_id,
            source_event_id='sevt_group_'+hashlib.sha256(f'{first.source_profile}:{first.canonical_post_id}:{first.symbol}'.encode()).hexdigest()[:24],
            package_signature=_package_signature(legs), evidence_revision_id='orev_group_'+digest,
            source_verification_ref='sv_group_'+digest, source_opening_package_count=1,
            source_valid_until=min(p.source_valid_until for p in members), displayed_price=None)
        replacements[id(first)] = combined
        consumed.update(id(p) for p in members[1:])
    return tuple(replacements.get(id(p),p) for p in packages if id(p) not in consumed)


def calendar_group_members(package, originals):
    if package.structure != "calendar_bundle":
        return ()
    return tuple(p for p in originals if p.action == "open" and
                 (p.source_profile, p.canonical_post_id, p.symbol) ==
                 (package.source_profile, package.canonical_post_id, package.symbol))
