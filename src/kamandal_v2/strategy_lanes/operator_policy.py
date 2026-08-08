"""Read-only compilation of CSA policy from the canonical Google Sheet."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kamandal_v2.domain.models import UniverseEntry, utc_now
from kamandal_v2.sheets import pull_sheet_tables
from kamandal_v2.strategy_lanes.policy import CsaPolicy, compile_csa_policies


@dataclass(frozen=True, slots=True)
class OperatorPolicyBundle:
    universe: tuple[UniverseEntry, ...]
    policies: tuple[CsaPolicy, ...]
    errors: tuple[str, ...]
    read_at: str
    source: str = "google_sheet"

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "universe_count": len(self.universe),
            "policies": [policy.to_dict() for policy in self.policies],
            "errors": list(self.errors),
            "read_at": self.read_at,
            "source": self.source,
            "ok": self.ok,
        }


def load_csa_operator_policy(
    config: dict[str, Any],
    *,
    tables: dict[str, list[dict[str, Any]]] | None = None,
    read_at: str | None = None,
    source_fresh: bool = True,
) -> OperatorPolicyBundle:
    resolved_tables = tables if tables is not None else pull_sheet_tables(config)
    observed_at = read_at or utc_now()
    compilation = compile_csa_policies(
        resolved_tables.get("playbooks") or [],
        source="google_sheet",
        read_at=observed_at,
        source_fresh=source_fresh,
    )
    universe = tuple(
        UniverseEntry.from_row(row)
        for row in (resolved_tables.get("universe") or [])
        if row.get("symbol")
    )
    return OperatorPolicyBundle(
        universe=universe,
        policies=compilation.policies,
        errors=compilation.errors,
        read_at=observed_at,
    )
