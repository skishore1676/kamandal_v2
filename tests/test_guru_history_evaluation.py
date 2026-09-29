from __future__ import annotations
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path('scripts').resolve()))
from evaluate_guru_history import load_records, score_ideas, LimitedClient
from configure_source_interpreter import configure


def test_history_inputs_exclude_labels_and_every_previously_evaluated_post():
    records = load_records()
    previous = {json.loads(line)['post_ref'] for line in Path('tests/fixtures/trade_source_interpretation/gold-v0.jsonl').read_text().splitlines() if line}
    previous |= {'x-post:'+r['post_id'] for r in json.loads(Path('tests/fixtures/mike_observed_packages/ground-truth.json').read_text())['fixtures']}
    assert len(records) == 38
    assert not previous & {r['signal_id'] for r in records}
    assert all('expected' not in json.dumps(r) for r in records)
    assert sum(bool(r['source'].get('media')) for r in records) >= 5


def test_wrong_direction_and_duplicate_openings_are_not_credited():
    label = [{'post_ref':'p', 'source_id':'greg', 'category':'directional_entry', 'expected_ideas':[{'symbol':'MOS','direction':'bullish'}]}]
    episode = [{'post_ref':'p','events':[{'action':'open','symbol':'MOS','direction':'bearish','projections':['idea']}]}]
    result=score_ideas(label, episode)
    assert result['matched'] == 0 and result['false_opening_count'] == 1


def test_budget_exhaustion_makes_no_model_call():
    class NeverCalled:
        def chat_json(self, *args, **kwargs): raise AssertionError('model called')
    client=LimitedClient(NeverCalled(), 0, 150000)
    with pytest.raises(RuntimeError, match='budget exhausted'):
        client.chat_json('system','user')


def test_interpreter_route_preserves_fleet_and_other_actor(tmp_path):
    import yaml
    path=tmp_path/'routing.yaml'
    payload={'schema':'agent_broker.routing.v1','active_profile':'terra',
      'profiles':{'terra':{'description':'test','binding':{'provider':'codex','options':{'model':'gpt-5.6-terra'}},'health_providers':['codex']}},
      'routes':{'other::actor':{'binding':{'provider':'codex','options':{'model':'gpt-5.5'}},'model_policy_tier':'literal'}}}
    path.write_text(yaml.safe_dump(payload))
    before=path.read_text()
    assert configure(routing_path=path)['applied'] is False
    assert path.read_text()==before
    result=configure(apply=True,routing_path=path)
    after=yaml.safe_load(path.read_text())
    assert after['active_profile']=='terra'
    assert after['routes']['other::actor']==payload['routes']['other::actor'] or after['routes']['other::actor']['binding']==payload['routes']['other::actor']['binding']
    assert result['readback']['options']['model']=='gpt-6-astra'
    assert result['readback']['options']['reasoning_effort']=='low'
