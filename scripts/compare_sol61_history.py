"""Effect-free replay of frozen natural Astra compilations; never activates sources."""
from __future__ import annotations
import argparse,hashlib,json,os,re,tempfile
from datetime import datetime
from decimal import Decimal
from kamandal_v2.intelligence.source_episode_projection import _observed_leg
from pathlib import Path
import yaml
from agent_broker import ProviderBinding
from kamandal_v2.intelligence import source_episode_compiler as compiler
from kamandal_v2.intelligence.llm_client import BrokerJsonClient
from evaluate_source_episode_models import _RecordingClient,_preserve_explicit_binding,_usage_summary,EFFECT_KEYS

from kamandal_v2.paths import PROJECT_ROOT
ROOT=PROJECT_ROOT
STORE=ROOT/'data/research/correspondent_signals'
def digest(x):return hashlib.sha256(compiler._stable_json(x).encode()).hexdigest()
def write(p,x):
 p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix('.tmp');t.write_text(json.dumps(x,indent=2,sort_keys=True)+'\n');t.replace(p)

def freeze(out):
 if (out/'manifest.json').exists():return json.loads((out/'manifest.json').read_text())
 cases=[]
 for source in ['greg_harmon','mike_butler']:
  runs=sorted([(p,json.loads(p.read_text())) for p in (STORE/'source_episodes/runs'/source).glob('*.json')],key=lambda v:(v[1]['compiled_at'],str(v[0])))
  packets={digest(json.loads(p.read_text())):p for p in (STORE/'packets'/source).glob('*.json')}
  chosen=[]
  for day in ['2026-09-21','2026-09-22','2026-09-23','2026-09-24','2026-09-25']:
   items=[r for r in runs if r[1]['compiled_at'].startswith(day) and r[1].get('model_receipts')]
   if items:chosen.append(items[0])
  chosen += [r for r in runs if r[1]['compiled_at'].startswith('2026-09-29') and r[1].get('model_receipts')]
  for path,baseline in chosen:
   receipts=baseline['model_receipts']
   assert all(r.get('usage',{}).get('model')=='gpt-6-astra' for r in receipts),path
   packetpath=packets[baseline['source_packet_sha256']];packet=json.loads(packetpath.read_text())
   prior={}
   for _,run in runs:
    if run['compiled_at']>=baseline['compiled_at']:break
    for e in run['episodes']:prior[e['post_ref']]=e
   history=list(prior.values())
   history.sort(key=lambda e:(e.get('published_at',''),e.get('post_ref','')),reverse=True);history=history[:500]
   # No future-published source events enter the reconstructed history.
   assert all(e.get('published_at','')<=baseline['compiled_at'] for e in history)
   profilepath=ROOT/f'config/correspondents/{source}.yaml';profile=yaml.safe_load(profilepath.read_text())
   images=[]
   for record in packet['records']:
    for media in record.get('source',{}).get('media',[]):
     if media.get('cache_status')=='cached' and media.get('type')=='photo':
      p=Path(media['artifact_path']);assert p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()==media['sha256'],p
      images.append({'path':str(p),'sha256':media['sha256']})
   key=source+'-'+path.stem
   frozen={'case_id':key,'source_id':source,'compiled_at':baseline['compiled_at'],'baseline_path':str(path),'packet_path':str(packetpath),'packet_sha256':digest(packet),'profile_sha256':hashlib.sha256(profilepath.read_bytes()).hexdigest(),'profile':profile,'packet':packet,'history':history,'history_sha256':digest(history),'baseline':baseline,'images':images}
   write(out/'inputs'/f'{key}.json',frozen);cases.append({k:frozen[k] for k in ['case_id','source_id','compiled_at','baseline_path','packet_sha256','profile_sha256','history_sha256']})
 manifest={'schema':'kamandal.sol61_natural_replay.v1','model':'gpt-6.1-sol','reasoning_effort':'low','selection':'First inference compilation per source per weekday September 21-25; all four inference compilations September 29. Chosen before challenger outputs. Cache-only runs excluded.','source_head':os.popen('git -C '+str(ROOT)+' rev-parse HEAD').read().strip(),'cases':cases,'effects':{k:False for k in EFFECT_KEYS}}
 write(out/'manifest.json',manifest);return manifest

class Recorder(_RecordingClient):
 def __init__(self,client,requestpath,budget):super().__init__(client);self.requestpath=requestpath;self.budget=budget;self.ids=set()
 def chat_json(self,system_prompt,user_prompt,*,images=()):
  if self.budget['calls']>=32 or self.budget['tokens']>=750000:raise RuntimeError('Frozen replay budget exhausted')
  self.budget['calls']+=1
  try:posts=json.loads(user_prompt)['posts'];self.ids.update(p['signal_id'] for p in posts)
  except (ValueError,KeyError):pass
  with self.requestpath.open('a') as f:f.write(json.dumps({'system':system_prompt,'user':user_prompt,'images':list(images),'prompt_sha256':hashlib.sha256((system_prompt+'\n'+user_prompt).encode()).hexdigest()})+'\n')
  try:return super().chat_json(system_prompt,user_prompt,images=images)
  finally:
   if self.turns:self.budget['tokens']+=int((self.turns[-1].get('receipt') or {}).get('usage',{}).get('total_tokens') or 0)
   write(self.requestpath.with_suffix('.turns.json'),{'model_turns':self.turns,'usage':_usage_summary(self.turns)})

SEMANTIC=['action','symbol','direction','structure_hint','projections','planner_new_entry','evidence_status','exact_packages','blockers','projection_dispositions','template_number']
def semantics(e):
 events=[]
 for v in e.get('events',[]):
  d={k:v.get(k) for k in SEMANTIC}
  # Compare contract meaning rather than display spellings or leg order.
  d['exact_packages']=[]
  day=datetime.fromisoformat(e['published_at'].replace('Z','+00:00')).date()
  for package in v.get('exact_packages',[]):
   package=dict(package)
   blocker=package.get('blocker')
   if blocker and not re.fullmatch(r'[a-z][a-z0-9_]*',str(blocker)):package['blocker']='source_evidence_incomplete'
   legs=[]
   for leg in package.get('legs',[]):
    try:
     x=_observed_leg(leg,day);legs.append({'expiration':str(x.expiration),'strike':str(Decimal(str(x.strike)).normalize()),'option_type':x.option_type,'order_code':x.order_code,'quantity':x.quantity})
    except (KeyError,ValueError,TypeError):legs.append(leg)
   package['legs']=sorted(legs,key=lambda x:json.dumps(x,sort_keys=True))
   package['field_provenance']=sorted(package.get('field_provenance',[]))
   price=package.get('displayed_price')
   if isinstance(price,dict) and price.get('amount') is not None:
    price=dict(price)
    try:price['amount']=str(Decimal(str(price['amount'])).normalize())
    except Exception:pass
    package['displayed_price']=price
   d['exact_packages'].append(package)
  d['exact_packages']=sorted(d['exact_packages'],key=lambda x:json.dumps(x,sort_keys=True))
  # Exclude identifiers, prose and confidence.
  d['projections']=sorted(d['projections'] or [])
  d['projection_dispositions']=sorted(d['projection_dispositions'] or [],key=lambda x:json.dumps(x,sort_keys=True))
  d['blockers']=sorted(b for b in d['blockers'] or [] if re.fullmatch(r'[a-z][a-z0-9_]*',b))
  events.append(d)
 return sorted(events,key=lambda x:json.dumps(x,sort_keys=True))

def entry_decisions(e):
 return [v for v in semantics(e) if
         (v['action'] in ('open','scale_in') and v['symbol'])
         or v['planner_new_entry']
         or (v['action'] in ('open','scale_in') and any(p.get('complete') for p in v['exact_packages']))]

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--prepare-only',action='store_true');ap.add_argument('--analyze-only',action='store_true');a=ap.parse_args();out=a.output.resolve();manifest=freeze(out)
 if a.analyze_only:
  analysis=[]
  for case in manifest['cases']:
   resultpath=out/'results'/f"{case['case_id']}.json"
   if not resultpath.exists():continue
   result=json.loads(resultpath.read_text());f=json.loads((out/'inputs'/f"{case['case_id']}.json").read_text())
   old={e['post_ref']:e for e in f['baseline']['episodes']};new={e['post_ref']:e for e in result.get('compilation',{}).get('episodes',[])}
   records={e['signal_id']:e for e in f['packet']['records']};diffs=[]
   for ref in result.get('challenged_post_refs',[]):
    if semantics(old[ref])!=semantics(new[ref]):
     diffs.append({'post_ref':ref,'published_at':records[ref]['source'].get('published_at'),'text':records[ref]['literal']['text'],'images':records[ref]['source'].get('media',[]),'astra':semantics(old[ref]),'sol61':semantics(new[ref])})
   decision_changes=[d for d in diffs if entry_decisions(old[d['post_ref']])!=entry_decisions(new[d['post_ref']])]
   analysis.append({**{k:result.get(k) for k in ['case_id','source_id','compiled_at','challenged_posts','compiler_prompt_hash_matches_baseline','usage','error']},'changed_posts':len(diffs),'decision_changes':decision_changes,'materially_changed_posts':len(decision_changes),'differences':diffs})
  write(out/'analysis.json',{'cases':analysis,'completed':len(analysis),'selected':len(manifest['cases']),'effects':{k:False for k in EFFECT_KEYS}});print(json.dumps({'completed':len(analysis),'changed':sum(c['changed_posts'] for c in analysis)}));return
 if a.prepare_only:print(json.dumps({'cases':len(manifest['cases']),'manifest':str(out/'manifest.json')}));return
 budget={'calls':0,'tokens':0};results=[]
 for case in manifest['cases']:
  key=case['case_id'];target=out/'results'/f'{key}.json'
  if target.exists():
   previous=json.loads(target.read_text());results.append(previous);u=previous['usage'];budget['calls']+=u['attempts'];budget['tokens']+=u['reported_total_tokens'];continue
  f=json.loads((out/'inputs'/f'{key}.json').read_text());binding=ProviderBinding('codex',{'model':'gpt-6.1-sol','reasoning_effort':'low','binary':'/Users/sunny/.local/bin/codex','sandbox':'read-only','approval_policy':'never','ignore_user_config':True,'ephemeral':True,'verbosity':'low','tools':[]})
  client=Recorder(BrokerJsonClient(actor='source_episode_interpreter',lane_id='kamandal_evaluation',binding=binding,timeout_seconds=600),out/f'{key}-requests.jsonl',budget)
  r={**case,'model':'gpt-6.1-sol','reasoning_effort':'low','effects':{k:False for k in EFFECT_KEYS}}
  try:
   with _preserve_explicit_binding(),tempfile.TemporaryDirectory(prefix='sol61-source-only-') as cwd:
    prior=os.getcwd()
    try:os.chdir(cwd);comp=compiler.compile_source_episode_packet(f['packet'],f['profile'],client,history=f['history'])
    finally:os.chdir(prior)
   r['compilation']=comp.to_dict();r['compiler_prompt_hash_matches_baseline']=comp.prompt_sha256==f['baseline']['prompt_sha256'];r['challenged_post_refs']=sorted(client.ids)
   old={e['post_ref']:e for e in f['baseline']['episodes']};new={e['post_ref']:e for e in comp.episodes};diffs=[]
   records={e['signal_id']:e for e in f['packet']['records']}
   for ref in sorted(client.ids):
    if semantics(old.get(ref,{}))!=semantics(new.get(ref,{})):
     diffs.append({'post_ref':ref,'published_at':records[ref]['source'].get('published_at'),'text':records[ref]['literal']['text'],'images':records[ref]['source'].get('media',[]),'astra':semantics(old.get(ref,{})),'sol61':semantics(new.get(ref,{}))})
   r['differences']=diffs;r['changed_posts']=len(diffs);r['challenged_posts']=len(client.ids)
  except Exception as e:r['error']=str(e)[-2000:]
  r['model_turns']=client.turns;r['usage']=_usage_summary(client.turns);write(target,r);results.append(r)
  write(out/'summary.json',{'manifest':manifest,'results':results,'budget':budget});print(json.dumps({'case':key,'posts':r.get('challenged_posts'),'changed':r.get('changed_posts'),'prompt_match':r.get('compiler_prompt_hash_matches_baseline'),'error':r.get('error'),'usage':r['usage']}),flush=True)
 write(out/'summary.json',{'manifest':manifest,'results':results,'budget':budget})
if __name__=='__main__':main()
