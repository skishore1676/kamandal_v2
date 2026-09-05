#!/usr/bin/env python3
"""Inspect/apply only Kamandal's source interpreter route; never the fleet profile."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from agent_broker import AgentSpec, load_policy
from agent_broker.routing import load_routing, route_key, set_override
from kamandal_v2.paths import PROJECT_ROOT

LANE = 'kamandal'
ACTOR = 'source_episode_interpreter'


def configure(*, apply: bool = False, routing_path: Path | None = None) -> dict:
    policy = load_policy(PROJECT_ROOT / '.agent-broker.yaml')
    desired = policy.resolve_agent(AgentSpec(lane_id=LANE, actor=ACTOR, role=ACTOR)).binding
    cfg = load_routing(routing_path)
    prior = cfg.route_for(LANE, ACTOR)
    result = {'lane': LANE, 'actor': ACTOR, 'active_fleet_profile': cfg.active_profile,
              'desired': desired.to_dict(), 'previous_matching_route': prior.to_dict() if prior else None,
              'applied': apply, 'trading_effects': False}
    if apply:
        set_override(LANE, ACTOR, desired, model_policy_tier='literal', note='Operator-approved guru interpretation profile; historical validation 2026-09-05', path=routing_path)
        after = load_routing(routing_path)
        assert after.active_profile == cfg.active_profile
        key = route_key(LANE, ACTOR)
        assert {k:v for k,v in after.routes.items() if k != key} == {k:v for k,v in cfg.routes.items() if k != key}
        result['readback'] = after.route_for(LANE, ACTOR).binding.to_dict()
        assert result['readback'] == result['desired']
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='Requires operator authorization; changes one model route only.')
    parser.add_argument('--routing-path', type=Path)
    args = parser.parse_args()
    print(json.dumps(configure(apply=args.apply, routing_path=args.routing_path), indent=2))


if __name__ == '__main__':
    main()
