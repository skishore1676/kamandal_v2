from copy import deepcopy

import pytest

from kamandal_v2.intelligence.source_shorthand import resolve_declared_text_contracts
from tests.test_source_episode_compiler import (
    FakeClient, PROMPT_SCHEMA, _event, _packet, _profile, _record,
    compile_source_episode_packet, project_source_episode_compilation,
)


def fixture():
    record = _record('2105746429079646454', 'added some $MU Oct/Nov 1150 call calendars', ['MU'],
                     classification='trade_journal', published_at='2026-10-01T19:47:26.000Z')
    response = {'schema': PROMPT_SCHEMA, 'episodes': [{'signal_id': record['signal_id'], 'events': [
        _event(action='scale_in', symbol='MU', direction='bullish', structure_hint='call_calendar',
               projections=['idea'], blockers=['Exact calendar expiration dates are unresolved.'], exact_packages=[{
                   'complete': False, 'blocker': 'October and November standard-monthly expirations are identified, but exact calendar dates are unresolved. Quantities represent the normalized 1:1 calendar ratio; total size is unspecified.',
                   'displayed_price': None, 'field_provenance': ['text'], 'legs': [
                       {'quantity': 1, 'expiration': 'Oct 2026 standard monthly', 'strike': '1150', 'option_type': 'call', 'order_code': 'STO'},
                       {'quantity': 1, 'expiration': 'Nov 2026 standard monthly', 'strike': '1150', 'option_type': 'call', 'order_code': 'BTO'},
                   ],
               }])]}]}
    packet = _packet([record])
    packet['generated_at'] = '2026-10-02T17:35:00Z'
    return record, packet, response


def test_cached_monthly_calendar_resolves_dates_to_exact_text_without_paid_reinterpretation():
    record, packet, response = fixture()
    profile = _profile('greg_harmon')
    legacy = deepcopy(profile)
    legacy['episode_interpreter'].pop('text_contract_convention')
    old = compile_source_episode_packet(packet, legacy, FakeClient(response))
    before = deepcopy(old.episodes)
    client = FakeClient()
    repaired = compile_source_episode_packet(packet, profile, client, history=old.episodes)
    assert not client.calls
    assert old.episodes == before
    episode = repaired.episodes[0]
    event = episode['events'][0]
    assert episode['published_at'] == record['source']['published_at']
    assert event['event_id'] == before[0]['events'][0]['event_id']
    assert event['opportunity_group_id'] == before[0]['events'][0]['opportunity_group_id']
    assert [leg['expiration'] for leg in event['exact_packages'][0]['legs']] == ['2026-10-16', '2026-11-20']
    assert event['exact_packages'][0]['complete']
    projected = project_source_episode_compilation(repaired, packet, profile, universe_symbols=['MU'])
    assert not projected.planner_ideas
    package = projected.observed_batches[0].packages[0]
    assert package.evidence_basis == 'text'
    assert package.source_published_at == record['source']['published_at']
    assert package.source_valid_until == '2026-10-02T19:47:26+00:00'


@pytest.mark.parametrize('change', ['weekly', 'strike', 'quantity', 'side', 'extra_blocker', 'roll', 'index', 'quote', 'unknown_month', 'same_month'])
def test_calendar_resolution_preserves_conflicts_and_non_openings(change):
    record, packet, response = fixture()
    profile = _profile('greg_harmon')
    legacy = deepcopy(profile)
    legacy['episode_interpreter'].pop('text_contract_convention')
    episode = deepcopy(compile_source_episode_packet(packet, legacy, FakeClient(response)).episodes[0])
    event = episode['events'][0]
    leg = event['exact_packages'][0]['legs'][0]
    if change == 'weekly': leg['expiration'] = '2026-10-23'
    if change == 'strike': leg['strike'] = '1160'
    if change == 'quantity': leg['quantity'] = 2
    if change == 'side': leg['order_code'] = 'BTO'
    if change == 'extra_blocker': event['blockers'].append('conflicting source contracts')
    if change == 'roll': event['action'] = 'roll'
    if change == 'index':
        record['literal']['text'] = record['literal']['text'].replace('$MU', '$SPX')
        record['literal']['symbols'] = [{'symbol': 'SPX'}]
        event['symbol'] = 'SPX'
    if change == 'quote': record['literal']['text'] = 'Previously ' + record['literal']['text']
    if change == 'unknown_month': record['literal']['text'] = record['literal']['text'].replace('Oct/', 'Octoberly/')
    if change == 'same_month': record['literal']['text'] = record['literal']['text'].replace('Oct/Nov', 'Oct/Oct')
    original = deepcopy(episode)
    assert resolve_declared_text_contracts(record, episode, profile) == original
    assert episode == original
