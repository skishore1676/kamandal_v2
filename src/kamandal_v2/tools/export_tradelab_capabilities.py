"""Export code-declared Kamandal capabilities without Sheets, DBs, or brokers."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Sequence

from kamandal_v2.planner.candidate_builder import SUPPORTED_STRUCTURES
from kamandal_v2.planner.shape_validators import SUPPORTED_VALIDATOR_STRUCTURES


REPO = Path(__file__).resolve().parents[3]


def _head() -> str | None:
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return process.stdout.strip() if process.returncode == 0 else None


def _effects() -> dict[str, bool]:
    return {
        "broker_accessed": False,
        "account_accessed": False,
        "auth_accessed": False,
        "sheet_read": False,
        "database_read": False,
        "database_mutated": False,
        "shadow_started": False,
        "live_started": False,
        "order_api_accessed": False,
    }


def build_manifest(*, runtime_readback_commit: str | None = None) -> dict:
    source_commit = _head()
    deployed = bool(
        runtime_readback_commit
        and source_commit
        and runtime_readback_commit == source_commit
    )
    structures = sorted(SUPPORTED_STRUCTURES.intersection(SUPPORTED_VALIDATOR_STRUCTURES))
    payload = {
        "schema": "kamandal.planner_capabilities.v1",
        "manifest_id": "kamandal.planner-and-shadow.capabilities",
        "version": "1",
        "source_commit": source_commit,
        "producer_receipt": None,
        "capabilities": [
            {
                "capability_id": "kamandal.plan_and_observe",
                "operation_class": "paper_observation",
                "supported_instruments": ["listed equity options"],
                "supported_structures": structures,
                "parameter_constraints": {
                    "planner_fields": [
                        "DTE",
                        "spread width",
                        "IV percentile/rank",
                        "direction",
                        "thesis tags",
                    ],
                    "automatic_shadow_allowed": False,
                },
                "required_inputs": [
                    "validated playbook",
                    "market snapshot",
                    "operator-owned configuration",
                ],
                "available_outputs": [
                    "candidate rejection reasons",
                    "plan receipts",
                    "shadow evidence",
                    "management marks",
                ],
                "declared_in_code": True,
                "verified": True,
                "deployed": deployed,
                "operationally_available": False,
                "limitations": [
                    "Exporter does not read Sheets, configuration values, databases, accounts, or market sessions.",
                    "TradeLab cannot start planning, shadow, live, or management jobs.",
                ],
            }
        ],
        "limitations": [
            "Code-declared support is not evidence that an operator playbook is configured or active."
        ],
        "source_references": [
            "src/kamandal_v2/planner/candidate_builder.py",
            "src/kamandal_v2/planner/shape_validators.py",
            "src/kamandal_v2/management/shadow.py",
        ],
        "protected_effects_performed": _effects(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["manifest_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-readback-commit")
    args = parser.parse_args(argv)
    print(
        json.dumps(
            build_manifest(runtime_readback_commit=args.runtime_readback_commit),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
