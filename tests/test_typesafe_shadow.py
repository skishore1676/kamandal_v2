import importlib.util
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import run_typesafe_shadow as m


def response(confidence=.86):
    return {'answers': {'needs_more': {'noul': .05},'new_entry': {'noul': .99},'symbol_SPY': {'confidence':confidence,'choice':'bullish'}}}


def test_thresholds_and_media():
    record={'source':{}}
    assert m.route(record,response(),.85)=='typesafe_decisions_only'
    assert m.route(record,response(),.88)=='astra_uncertain'
    assert m.route({'source':{'media':[{}]}},response(1),.85)=='astra_media'
    assert m.route({'source':{'expanded_urls':['https://x.com/a/photo/1']}},response(1),.85)=='astra_media'


def test_incomplete_unknown_and_errors_fallback():
    assert m.route({'source':{}},None,.85)=='astra_unavailable'
    r=response();r['answers']['needs_more']['noul']=.2
    assert m.route({'source':{}},r,.85)=='astra_incomplete'
    r=response();r['answers']['symbol_SPY']['choice']='unknown'
    assert m.route({'source':{}},r,.85)=='astra_uncertain'


def test_disabled_makes_no_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(m,'ROOT',tmp_path)
    monkeypatch.setenv('TYPE_SAFE_SHADOW_ENABLED','0')
    monkeypatch.setattr(sys,'argv',['shadow'])
    m.main()
    assert not (tmp_path/'data').exists()


def test_requires_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(m,'ROOT',tmp_path)
    monkeypatch.setenv('TYPE_SAFE_SHADOW_ENABLED','1')
    monkeypatch.setenv('TYPE_SAFE_SHADOW_UNTIL','2026-09-29T00:00:00Z')
    monkeypatch.setattr(sys,'argv',['shadow'])
    with pytest.raises(RuntimeError,match='Seed required'): m.main()


def test_seed_new_record_exact_baseline_and_dedup(tmp_path, monkeypatch):
    import json, hashlib
    monkeypatch.setattr(m,'ROOT',tmp_path)
    monkeypatch.setenv('TYPE_SAFE_SHADOW_ENABLED','1')
    monkeypatch.setenv('TYPE_SAFE_SHADOW_UNTIL','2099-01-01T00:00:00Z')
    monkeypatch.setenv('TYPE_SAFE_KEY','fake-test-key')
    monkeypatch.setattr(sys,'argv',['shadow','--seed'])
    m.main()
    root=tmp_path/'data/research/correspondent_signals'
    packetdir=root/'packets/greg_harmon';packetdir.mkdir(parents=True)
    record={'signal_id':'x-post:1','profile_id':'greg_harmon','source':{'published_at':'2026-09-23T00:00:00Z'},'literal':{'text':'added $SPY calls'}}
    (packetdir/'packet.json').write_text(json.dumps({'records':[record]}))
    profile=tmp_path/'config/correspondents/greg_harmon.yaml';profile.parent.mkdir(parents=True);profile.write_text('{}')
    compilation=root/'compiled.json'
    sha=hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
    compilation.write_text(json.dumps({'episodes':[{'post_ref':'x-post:1','source_record_sha256':sha,'events':[]}]}))
    (root/'activation').mkdir()
    (root/'activation/latest.json').write_text(json.dumps({'profiles':[{'translation_path':str(compilation)}]}))
    calls=[]
    class Reply:
        status_code=200
        def json(self): return {'answers':{'needs_more':{'type':'noul','noul':.01},'new_entry':{'type':'noul','noul':.99},'symbol_SPY':{'type':'choice','choice':'bullish','confidence':.99}},'usage':{'input_tokens':100}}
    monkeypatch.setattr(m.requests,'post',lambda *a,**kw: calls.append(kw) or Reply())
    monkeypatch.setattr(sys,'argv',['shadow'])
    m.main();m.main()
    assert len(calls)==1
    summary=json.loads((tmp_path/'data/research/typesafe_shadow/summary.json').read_text())
    assert summary['retained_receipts']==1
    assert summary['effects']['astra_calls']==0
    assert summary['routes']['0.85']['typesafe_decisions_only']==1
    assert 'fake-test-key' not in (tmp_path/'data/research/typesafe_shadow/summary.json').read_text()
    # Changed observation metadata must not trigger another paid interpretation.
    record['source']['seen_at']='later'
    (packetdir/'packet.json').write_text(json.dumps({'records':[record]}))
    m.main()
    assert len(calls)==1
