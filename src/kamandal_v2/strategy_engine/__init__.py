"""Generic strategy policy contracts for the unified Kamandal engine.

The old ``strategy_lanes`` package remains the deployed implementation until
the cutover phases adopt these contracts.  New code uses this package so CSA is
not a permanent public ownership boundary.
"""

from kamandal_v2.strategy_engine.policy import (
    ExecutionMode,
    PlaybookPolicy,
    PolicyCompilation,
    PolicyError,
    StrangleManagementPolicy,
    compile_playbook_policies,
    compile_playbook_policy,
)
from kamandal_v2.strategy_engine.registry import Capability, CapabilityRegistry, capability_registry

__all__ = [
    "Capability",
    "CapabilityRegistry",
    "ExecutionMode",
    "PlaybookPolicy",
    "PolicyCompilation",
    "PolicyError",
    "StrangleManagementPolicy",
    "capability_registry",
    "compile_playbook_policies",
    "compile_playbook_policy",
]
