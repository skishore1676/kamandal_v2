from __future__ import annotations

from scripts.apply_trade_source_routing_sheet import (
    EXACT_ACCEPTOR_BY_STRUCTURE,
    RETIRED_PLAYBOOK_IDS,
    migrated_playbook_rows,
)


def test_sheet_migration_reuses_existing_playbooks_and_removes_mike_rows() -> None:
    existing = [
        {
            "playbook_id": playbook_id,
            "source_mode": "idea",
            "accepted_inputs": "",
            "source_profiles": "",
        }
        for playbook_id in EXACT_ACCEPTOR_BY_STRUCTURE.values()
    ]
    existing.extend(
        {
            "playbook_id": playbook_id,
            "source_mode": "observed_package",
            "accepted_inputs": "exact_package",
            "source_profiles": "mike_butler",
        }
        for playbook_id in RETIRED_PLAYBOOK_IDS
    )
    existing.append(
        {
            "playbook_id": "ordinary_iron_condor",
            "source_mode": "market_scan",
            "accepted_inputs": "",
            "source_profiles": "",
        }
    )

    migrated = migrated_playbook_rows(existing)
    by_id = {row["playbook_id"]: row for row in migrated}
    assert not (RETIRED_PLAYBOOK_IDS & set(by_id))
    assert all(by_id[playbook_id]["accepted_inputs"] == "idea,exact_package" for playbook_id in EXACT_ACCEPTOR_BY_STRUCTURE.values())
    assert by_id["ordinary_iron_condor"]["accepted_inputs"] == "market_scan"
    assert all(row["source_profiles"] == "" for row in migrated)
