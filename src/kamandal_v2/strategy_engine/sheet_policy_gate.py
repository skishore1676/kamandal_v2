"""Read-only deployment gate for the canonical Google Sheet policy.

The gate deliberately validates every policy consumer against one Sheet read.
It does not capture a daily snapshot or touch a database, report, or broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kamandal_v2.domain.models import Playbook, UniverseEntry, utc_now
from kamandal_v2.intelligence.trade_sources import compile_trade_source_policies
from kamandal_v2.planner.config_validator import validate_config
from kamandal_v2.schemas import TRADE_SOURCES_HEADER, UNIVERSE_HEADER
from kamandal_v2.sheets import pull_sheet_tables
from kamandal_v2.strategy_engine.policy import compile_playbook_policies
from kamandal_v2.strategy_lanes.daily_policy import policy_tables_hash
from kamandal_v2.strategy_lanes.policy import compile_csa_policies


@dataclass(frozen=True, slots=True)
class SheetPolicyGateResult:
    read_at: str
    snapshot_hash: str
    universe_count: int
    playbook_count: int
    enabled_playbook_count: int
    planner_warnings: tuple[str, ...]
    planner_errors: tuple[str, ...]
    unified_policy_count: int
    unified_errors: tuple[str, ...]
    csa_policy_count: int
    csa_errors: tuple[str, ...]
    trade_source_count: int = 0
    trade_source_errors: tuple[str, ...] = ()
    model_errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (
            self.model_errors
            or self.planner_errors
            or self.unified_errors
            or self.csa_errors
            or self.trade_source_errors
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "kamandal.sheet-policy-deployment-gate.v1",
            "ok": self.ok,
            "source": "google_sheet",
            "read_at": self.read_at,
            "snapshot_hash": self.snapshot_hash,
            "rows": {
                "universe": self.universe_count,
                "playbooks": self.playbook_count,
                "enabled_playbooks": self.enabled_playbook_count,
            },
            "planner": {
                "ok": not (self.model_errors or self.planner_errors),
                "warnings": list(self.planner_warnings),
                "errors": list(self.model_errors + self.planner_errors),
            },
            "unified": {
                "ok": not self.unified_errors,
                "policy_count": self.unified_policy_count,
                "errors": list(self.unified_errors),
            },
            "csa_compatibility": {
                "ok": not self.csa_errors,
                "policy_count": self.csa_policy_count,
                "errors": list(self.csa_errors),
            },
            "trade_sources": {
                "ok": not self.trade_source_errors,
                "policy_count": self.trade_source_count,
                "errors": list(self.trade_source_errors),
            },
        }


def validate_sheet_policy(
    config: dict[str, Any],
    *,
    tables: dict[str, list[dict[str, Any]]] | None = None,
    read_at: str | None = None,
) -> SheetPolicyGateResult:
    """Compile the actual operator policy through every active contract."""

    resolved = tables if tables is not None else pull_sheet_tables(config)
    policy_tables = {
        "universe": [dict(row) for row in (resolved.get("universe") or [])],
        "playbooks": [dict(row) for row in (resolved.get("playbooks") or [])],
        "trade_sources": [dict(row) for row in (resolved.get("trade_sources") or [])],
    }
    observed_at = read_at or utc_now()
    model_errors: list[str] = []
    universe: list[UniverseEntry] = []
    playbooks: list[Playbook] = []

    if policy_tables["universe"]:
        observed_headers = set(policy_tables["universe"][0])
        missing_universe_headers = sorted(set(UNIVERSE_HEADER) - observed_headers)
        if missing_universe_headers:
            model_errors.append(
                "universe_header_missing:" + ",".join(missing_universe_headers)
            )

    if policy_tables["playbooks"]:
        observed_headers = set(policy_tables["playbooks"][0])
        if "accepted_inputs" not in observed_headers:
            model_errors.append("playbooks_header_missing:accepted_inputs")
    else:
        model_errors.append("playbooks_header_missing:accepted_inputs")

    if policy_tables["trade_sources"]:
        observed_headers = set(policy_tables["trade_sources"][0])
        missing_source_headers = sorted(set(TRADE_SOURCES_HEADER) - observed_headers)
        if missing_source_headers:
            model_errors.append(
                "trade_sources_header_missing:" + ",".join(missing_source_headers)
            )
    else:
        model_errors.append("trade_sources_header_missing:" + ",".join(TRADE_SOURCES_HEADER))

    for index, row in enumerate(policy_tables["universe"], start=2):
        if not row.get("symbol"):
            continue
        try:
            universe.append(UniverseEntry.from_row(row))
        except (TypeError, ValueError) as exc:
            model_errors.append(f"universe_row_{index}:{exc}")
    for index, row in enumerate(policy_tables["playbooks"], start=2):
        if not row.get("playbook_id"):
            continue
        try:
            playbooks.append(Playbook.from_row(row))
        except (TypeError, ValueError) as exc:
            model_errors.append(f"playbook_row_{index}:{exc}")

    planner = validate_config(universe, playbooks)
    unified = compile_playbook_policies(policy_tables["playbooks"])
    csa = compile_csa_policies(
        policy_tables["playbooks"],
        source="google_sheet",
        read_at=observed_at,
    )
    required_source_ids = [
        str(profile.get("profile_id") or "")
        for profile in (((config.get("source_intelligence") or {}).get("correspondents") or {}).get("profiles") or [])
        if isinstance(profile, dict) and profile.get("enabled") is True
    ]
    trade_sources = compile_trade_source_policies(
        policy_tables["trade_sources"],
        required_source_ids=required_source_ids,
    )
    enabled_count = sum(
        str(row.get("enabled") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
        for row in policy_tables["playbooks"]
        if row.get("playbook_id")
    )
    return SheetPolicyGateResult(
        read_at=observed_at,
        snapshot_hash=policy_tables_hash(policy_tables),
        universe_count=len(universe),
        playbook_count=len(playbooks),
        enabled_playbook_count=enabled_count,
        model_errors=tuple(model_errors),
        planner_warnings=tuple(planner.warnings),
        planner_errors=tuple(planner.errors),
        unified_policy_count=len(unified.policies),
        unified_errors=unified.errors,
        csa_policy_count=len(csa.policies),
        csa_errors=csa.errors,
        trade_source_count=len(trade_sources.policies),
        trade_source_errors=trade_sources.errors,
    )
