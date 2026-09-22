#!/usr/bin/env python3
"""Read retained packets/episodes and write only an isolated TypeSafe experiment directory."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import requests
import yaml
from evaluate_typesafe_gurus import build_request, digest, validate_response
from kamandal_v2.intelligence.source_episode_compiler import _deterministic_episode

ROOT = Path(__file__).resolve().parents[1]

def atomic(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2)+'\n')
    tmp.replace(path)

def route(record, response, threshold):
    # This accepts decision labels only, never complete trade packages.
    if record['source'].get('media') or any('/photo/' in u for u in record['source'].get('expanded_urls', [])):
        return 'astra_media'
    if not response:
        return 'astra_unavailable'
    answers = response['answers']
    if answers['needs_more']['noul'] > 1-threshold:
        return 'astra_incomplete'
    choices = [v for k,v in answers.items() if k.startswith('symbol_')]
    if not choices or any(v['confidence'] < threshold or v['choice']=='unknown' for v in choices):
        return 'astra_uncertain'
    has_entry = any(v['choice'] in {'bullish','bearish','neutral'} for v in choices)
    entry_probability = answers['new_entry']['noul']
    if (has_entry and entry_probability < threshold) or (not has_entry and entry_probability > 1-threshold):
        return 'astra_uncertain'
    return 'typesafe_decisions_only'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', action='store_true', help='Mark existing records as pretrial; make no API calls')
    parser.add_argument('--report-only', action='store_true')
    args = parser.parse_args()
    if os.getenv('TYPE_SAFE_SHADOW_ENABLED','0') != '1':
        print('TypeSafe shadow disabled'); return
    threshold = float(os.getenv('TYPE_SAFE_CONFIDENCE_THRESHOLD','0.85'))
    if not 0.5 <= threshold <= 1: raise ValueError('Threshold must be between 0.5 and 1')
    end = datetime.fromisoformat(os.environ['TYPE_SAFE_SHADOW_UNTIL'].replace('Z','+00:00'))
    now = datetime.now(timezone.utc)
    output = ROOT/'data/research/typesafe_shadow'
    output.mkdir(parents=True, exist_ok=True)
    with (output/'lock').open('w') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
        state_path = output/'state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {'seen':{},'started_at':now.isoformat()}
        if not state_path.exists() and not args.seed:
            raise RuntimeError('Seed required before enabling new-post evaluation')
        root = ROOT/'data/research/correspondent_signals'
        # Latest retained packet per guru; no acquisition or production job invocation.
        records = []
        for profile in ('greg_harmon','mike_butler'):
            paths = list((root/'packets'/profile).glob('*.json'))
            if paths:
                packet = json.loads(max(paths,key=lambda p:p.stat().st_mtime_ns).read_text())
                records.extend(packet['records'])
        records.sort(key=lambda r:(r['source']['published_at'],r['signal_id']))
        baseline = {}
        latest = root/'activation/latest.json'
        if latest.exists():
            for p in json.loads(latest.read_text()).get('profiles',[]):
                path = Path(p.get('translation_path') or '/nonexistent')
                if path.is_file():
                    for e in json.loads(path.read_text()).get('episodes',[]):
                        baseline[(e['post_ref'],e.get('source_record_sha256'))] = e
        calls = 0
        max_calls = min(50,max(0,int(os.getenv('TYPE_SAFE_SHADOW_MAX_CALLS','20'))))
        deadline = time.monotonic()+120
        for index, record in enumerate(records):
            # Full exact source revision matches Astra compiler identity.
            source_sha = hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
            identity = record['signal_id']+':'+digest({'profile':record['profile_id'], 'text':record['literal']['text'], 'published_at':record['source']['published_at'], 'media':[(m.get('sha256'),m.get('source_url') or m.get('url')) for m in record['source'].get('media',[])], 'urls': sorted(record['source'].get('expanded_urls',[]))})
            if identity in state['seen']: continue
            if args.seed:
                state['seen'][identity] = 'pretrial'; continue
            if args.report_only or now >= end: continue
            profile = yaml.safe_load((ROOT/'config/correspondents'/f"{record['profile_id']}.yaml").read_text())
            deterministic = _deterministic_episode(record,profile)
            if deterministic is not None:
                state['seen'][identity]='deterministic'; continue
            # Require matching primary evidence before spending on comparison.
            primary = baseline.get((record['signal_id'],source_sha))
            if primary is None: continue
            if calls >= max_calls or time.monotonic() >= deadline: break
            receipt = {'identity':identity,'post_ref':record['signal_id'],'record':record,
                       'observed_at':now.isoformat(),'primary_episode':primary,'threshold':threshold}
            request = build_request(record,records[:index],os.getenv('TYPE_SAFE_MODEL','jev-1.13.0'))
            receipt['request']=request
            receipt['request_sha256']=digest(request)
            # Persist reservation first: no automatic rebilling after uncertain failure/crash.
            state['seen'][identity]='reserved'
            atomic(state_path,state)
            if record['source'].get('media') or any('/photo/' in u for u in record['source'].get('expanded_urls',[])):
                receipt['status']='media_fallback'; response=None
            else:
                calls += 1
                started=time.monotonic()
                try:
                    key=os.environ.get('TYPE_SAFE_KEY')
                    if not key: raise RuntimeError('missing_key')
                    http=requests.post('https://api.typesafe.ai/v1/systemone',json=request,
                        headers={'Authorization':'Bearer '+key},timeout=(5,15),allow_redirects=False)
                    if http.status_code != 200: raise RuntimeError('http_'+str(http.status_code))
                    response=http.json(); validate_response(request,response)
                    receipt['response']=response; receipt['status']='evaluated'
                except Exception as exc:
                    response=None; receipt['status']='error'; receipt['error_type']=type(exc).__name__
                receipt['elapsed_seconds']=time.monotonic()-started
            receipt['routes']={str(t):route(record,response,t) for t in sorted({threshold,.85,.88})}
            primary_ideas = {(e.get('symbol'), e.get('direction')) for e in primary.get('events',[])
                if e.get('action') in {'open','scale_in'} and e.get('template_number') is None}
            candidate_ideas = {(k[7:],v['choice']) for k,v in (response or {}).get('answers',{}).items()
                if k.startswith('symbol_') and v.get('choice') in {'bullish','bearish','neutral'}}
            receipt['direction_comparison']={'astra':sorted(primary_ideas), 'typesafe':sorted(candidate_ideas),
                'agreement':candidate_ideas==primary_ideas if response else None}
            receipt['comparison_limits']='Direction comparison only; Astra is not human gold; follow-up events and exact legs not replaced.'
            receipt['effects']={'idea_publication':False,'broker':False,'sheet_write':False,'astra_calls':0}
            path=output/(hashlib.sha256(identity.encode()).hexdigest()+'.json')
            atomic(path,receipt)
            state['seen'][identity]=receipt['status']
        atomic(state_path,state)
        receipts=[json.loads(p.read_text()) for p in output.glob('*.json') if p.name not in {'state.json','summary.json'}]
        summary={'updated_at':now.isoformat(),'until':end.isoformat(),'expired':now>=end,
                 'threshold':threshold,'calls_this_run':calls,'retained_receipts':len(receipts),
                 'statuses':{s:sum(v==s for v in state['seen'].values()) for s in set(state['seen'].values())},
                 'routes':{str(t):{r:sum(x.get('routes',{}).get(str(t))==r for x in receipts)
                      for r in ('astra_media','astra_incomplete','astra_uncertain','astra_unavailable','typesafe_decisions_only')}
                      for t in sorted({threshold,.85,.88})},
                 'input_tokens':sum(x.get('response',{}).get('usage',{}).get('input_tokens',0) for x in receipts),
                 'limits':'Agreement with Astra is not human-verified accuracy. Accepted outputs are decision labels, not full packages.',
                 'effects':{'idea_publication':False,'broker':False,'sheet_write':False,'astra_calls':0}}
        atomic(output/'summary.json',summary)
        print(json.dumps(summary))

if __name__=='__main__': main()
