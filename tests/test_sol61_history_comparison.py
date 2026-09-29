from __future__ import annotations
import importlib.util,io,json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path('scripts').resolve()))
import compare_sol61_history as comparison

def event(expiration='Sep 18 2026',quantity=1,price='1.50'):
 return {'published_at':'2026-09-01T12:00:00Z','events':[{'action':'open','symbol':'AAPL','direction':'bullish','structure_hint':'long_call','projections':['exact_package'],'planner_new_entry':True,'evidence_status':'complete','blockers':[],'exact_packages':[{'complete':True,'legs':[{'expiration':expiration,'strike':'200.0','option_type':'call','order_code':'BTO','quantity':quantity}],'displayed_price':{'amount':price,'effect':'debit'},'field_provenance':['image:1','text']}]}]}

def test_contract_comparison_normalizes_display_but_retains_quantity_and_price():
 assert comparison.semantics(event())==comparison.semantics(event('2026-09-18',price='1.5'))
 assert comparison.semantics(event())!=comparison.semantics(event(quantity=2))
 assert comparison.semantics(event())!=comparison.semantics(event(price='2.5'))

def test_budget_stop_precedes_provider_call(tmp_path):
 class NeverCalled:
  def chat_json(self,*args,**kwargs):raise AssertionError('provider called')
 client=comparison.Recorder(NeverCalled(),tmp_path/'requests.jsonl',{'calls':32,'tokens':0})
 with pytest.raises(RuntimeError,match='budget exhausted'):client.chat_json('system','{}')
 assert not (tmp_path/'requests.jsonl').exists()

def test_selection_and_history_are_frozen_before_challenger_results(tmp_path,monkeypatch):
 root=tmp_path/'runtime';store=root/'data/research/correspondent_signals'
 monkeypatch.setattr(comparison,'ROOT',root);monkeypatch.setattr(comparison,'STORE',store)
 monkeypatch.setattr(comparison.os,'popen',lambda command:io.StringIO('test-head\n'))
 for source in ['greg_harmon','mike_butler']:
  (root/'config/correspondents').mkdir(parents=True,exist_ok=True)
  (root/f'config/correspondents/{source}.yaml').write_text('profile_id: '+source+'\n')
  for i,stamp in enumerate(['2026-09-21T12:00:00Z','2026-09-21T13:00:00Z','2026-09-29T12:00:00Z','2026-09-29T13:00:00Z']):
   packet={'generated_at':stamp,'records':[]};h=comparison.digest(packet)
   comparison.write(store/f'packets/{source}/{h}.json',packet)
   baseline={'compiled_at':stamp,'source_packet_sha256':h,'model_receipts':[{'usage':{'model':'gpt-6-astra'}}],'episodes':[{'post_ref':str(i),'published_at':stamp}]}
   comparison.write(store/f'source_episodes/runs/{source}/{i}.json',baseline)
 out=tmp_path/'out';manifest=comparison.freeze(out)
 assert len(manifest['cases'])==6
 assert not any(c['case_id'].endswith('-1') for c in manifest['cases'])
 for c in manifest['cases']:
  frozen=json.loads((out/'inputs'/f"{c['case_id']}.json").read_text())
  assert all(e['published_at']<frozen['compiled_at'] for e in frozen['history'])
 assert comparison.freeze(out)==manifest
