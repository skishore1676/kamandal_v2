#!/usr/bin/env python3
"""Bounded, effect-free historical source interpretation; labels never enter prompts."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
import yaml
from agent_broker import ProviderBinding
from evaluate_source_episode_models import _RecordingClient, _preserve_explicit_binding, _usage_summary
from evaluate_source_episode_vision import score_packages
from kamandal_v2.intelligence.llm_client import BrokerJsonClient
from kamandal_v2.intelligence.source_episode_compiler import compile_source_episode_packet
from kamandal_v2.paths import PROJECT_ROOT

CORPUS = PROJECT_ROOT / 'tests/fixtures/guru_history_20260905'


def load_records(root: Path = CORPUS) -> list[dict]:
    records = json.loads((root / 'records.json').read_text())
    for record in records:
        for media in record['source'].get('media', []):
            if media.get('cache_status') == 'cached':
                path = (root / media['artifact_path']).resolve()
                if hashlib.sha256(path.read_bytes()).hexdigest() != media['sha256']:
                    raise ValueError('image hash mismatch')
                media['artifact_path'] = str(path)
    return records


def score_ideas(labels: list[dict], episodes: list[dict]) -> dict:
    by_ref = {e['post_ref']: e for e in episodes}
    rows = []
    for label in labels:
        expected = Counter((e['symbol'], e['direction']) for e in label['expected_ideas'])
        emitted = Counter((e.get('symbol'), e.get('direction'))
                          for e in by_ref.get(label['post_ref'], {}).get('events', [])
                          if e.get('action') in {'open', 'scale_in'} and 'idea' in e.get('projections', [])
                          and e.get('direction') in {'bullish', 'bearish', 'neutral'}
                          and e.get('template_number') is None)
        # Watchlists may be retained as evidence, but unsupported directional
        # guesses still count as false openings. Deterministic templates excluded.
        if label['category'] == 'deterministic_template':
            emitted = Counter()
        rows.append({'post_ref': label['post_ref'], 'source_id': label['source_id'],
                     'expected': sum(expected.values()), 'matched': sum((expected & emitted).values()),
                     'missed': list((expected - emitted).elements()), 'false_openings': list((emitted - expected).elements())})
    total = sum(r['expected'] for r in rows)
    matched = sum(r['matched'] for r in rows)
    false = sum(len(r['false_openings']) for r in rows)
    return {'expected': total, 'matched': matched, 'recall': matched / total if total else None,
            'precision': matched / (matched + false) if matched + false else None,
            'false_opening_count': false, 'rows': rows}


class LimitedClient(_RecordingClient):
    def __init__(self, client, max_turns: int, token_stop: int):
        super().__init__(client)
        self.max_turns = max_turns
        self.token_stop = token_stop

    def chat_json(self, *args, **kwargs):
        if len(self.turns) >= self.max_turns or _usage_summary(self.turns)['reported_total_tokens'] >= self.token_stop:
            raise RuntimeError('Historical evaluation budget exhausted; no further model calls')
        return super().chat_json(*args, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--codex-binary', required=True)
    parser.add_argument('--max-turns', type=int, default=6)
    parser.add_argument('--token-stop', type=int, default=150000,
                        help='Stop before another call once reported usage reaches this threshold; one call may overshoot.')
    args = parser.parse_args()
    records = load_records()
    binding = ProviderBinding('codex', {'model': 'gpt-6-astra', 'reasoning_effort': 'low',
        'binary': args.codex_binary, 'sandbox': 'read-only', 'approval_policy': 'never',
        'ignore_user_config': True, 'ephemeral': True, 'verbosity': 'low'})
    client = LimitedClient(BrokerJsonClient(actor='source_episode_interpreter', lane_id='kamandal_evaluation',
        binding=binding, timeout_seconds=600), args.max_turns, args.token_stop)
    episodes, failures = [], []
    profile_hashes = {}
    with _preserve_explicit_binding():
        for source in sorted({r['profile_id'] for r in records}):
            path = PROJECT_ROOT / f'config/correspondents/{source}.yaml'
            profile_hashes[source] = hashlib.sha256(path.read_bytes()).hexdigest()
            profile = yaml.safe_load(path.read_text())
            packet = {'generated_at': '2026-09-05T00:00:00Z', 'records': [r for r in records if r['profile_id'] == source]}
            try:
                with tempfile.TemporaryDirectory(prefix='guru-history-input-only-') as cwd:
                    prior = Path.cwd()
                    try:
                        os.chdir(cwd)
                        compilation = compile_source_episode_packet(packet, profile, client)
                    finally:
                        os.chdir(prior)
                episodes.extend(compilation.episodes)
            except Exception as exc:
                failures.append({'source_id': source, 'error': str(exc)[-1500:]})
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({'episodes': episodes, 'model_turns': client.turns,
                'failures': failures, 'usage': _usage_summary(client.turns), 'partial': True}, indent=2)+'\n')
    # Labels are opened only after all model work is complete.
    labels = json.loads((CORPUS / 'labels.json').read_text())
    fixtures = json.loads((CORPUS / 'opening-packages.json').read_text())
    openings = [{**e, 'events': [v for v in e.get('events', []) if v.get('action') == 'open']} for e in episodes]
    result = {'schema': 'kamandal.guru_history_evaluation.v1', 'model': 'gpt-6-astra', 'reasoning_effort': 'low',
        'idea_score': score_ideas(labels, episodes), 'opening_package_score': score_packages(fixtures, openings),
        'episodes': episodes, 'model_turns': client.turns, 'usage': _usage_summary(client.turns), 'failures': failures,
        'profile_sha256': profile_hashes, 'corpus_manifest': json.loads((CORPUS / 'manifest.json').read_text()),
        'trading_effects': False, 'limits': 'Retained incomplete timeline; labels from source inspection, not new operator approvals. No future history supplied. Exact prices and locator consistency not included in contract score.'}
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({'ideas': {k:v for k,v in result['idea_score'].items() if k!='rows'},
                     'packages': {k:v for k,v in result['opening_package_score'].items() if k not in {'rows','limits'}},
                     'usage': result['usage'], 'failures': failures}))


if __name__ == '__main__':
    main()
