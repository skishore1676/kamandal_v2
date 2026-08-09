"""Compile sheet rows into domain objects."""

from __future__ import annotations

from typing import Any

from kamandal_v2.domain.models import Playbook, UniverseEntry
from kamandal_v2.seed import build_seed_tables, seed_headers
from kamandal_v2.sheets import pull_sheet_tables


def load_planner_config(config: dict[str, Any], *, source: str = "sheet") -> tuple[list[UniverseEntry], list[Playbook]]:
    if source == "seed":
        tables = build_seed_tables(config)
        headers = seed_headers()
        universe_rows = [_row_dict(headers["universe"], row) for row in tables["universe"]]
        playbook_rows = [_row_dict(headers["playbooks"], row) for row in tables["playbooks"]]
    else:
        tables = pull_sheet_tables(config)
        universe_rows = tables["universe"]
        playbook_rows = tables["playbooks"]
    universe = [UniverseEntry.from_row(row) for row in universe_rows if row.get("symbol")]
    # A playbook has exactly one runtime owner. Blank/baseline rows belong to the
    # established planner; staged experiment rows belong to strategy_lanes. This
    # prevents the same enabled Sheet row from being planned twice.
    playbooks = [
        Playbook.from_row(row)
        for row in playbook_rows
        if row.get("playbook_id")
        and str(row.get("csa_stage") or "baseline").strip().lower() in {"", "baseline"}
    ]
    return universe, playbooks


def _row_dict(header: list[str], row: list[Any]) -> dict[str, Any]:
    padded = list(row) + [""] * (len(header) - len(row))
    return {header[index]: padded[index] for index in range(len(header))}
