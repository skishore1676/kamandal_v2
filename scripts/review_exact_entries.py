#!/usr/bin/env python3
"""Render an offline exact-entry review from a saved audit snapshot; no API calls."""
import argparse
import csv
import json
from pathlib import Path
from kamandal_v2.intelligence.exact_entry_review import build_review


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    snapshot=json.loads(args.snapshot.read_text())
    args.output.mkdir(parents=True,exist_ok=True)
    rows=[]
    for source in snapshot['sources']:
        review=build_review(source['packet'],source['compilation'])
        (args.output/(source['profile']+'.json')).write_text(json.dumps(review,indent=2)+'\n')
        for row in review['rows']:
            rows.append({'guru':source['profile'],**{k:json.dumps(v) if isinstance(v,(dict,list)) else v for k,v in row.items()}})
        print(source['profile'], 'posts',review['post_count'],'reviewable contracts',sum(r['translation_status']=='reviewable_contracts' for r in review['rows']),'duplicate posts',len(review['possible_edit_duplicates']))
    if rows:
        with (args.output/'review.csv').open('w') as handle:
            writer=csv.DictWriter(handle,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)

if __name__=='__main__': main()
