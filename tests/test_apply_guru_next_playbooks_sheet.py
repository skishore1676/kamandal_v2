from __future__ import annotations

import pytest

from scripts.apply_guru_next_playbooks_sheet import CALENDARS, SHADOW_SHAPES, proposed_playbooks


def _rows() -> list[dict[str, str]]:
    return [
        {"playbook_id": playbook_id, "dte_max": "60", "notes": "existing", "mode": "live",
         "accepted_inputs": "exact_package", "live_max_bpr_per_order": "1200"}
        for playbook_id in CALENDARS
    ] + [{"playbook_id": "unrelated", "dte_max": "45", "mode": "live"}] + [
        {"playbook_id": "", "range_gate_required": "FALSE", "resting_profit_enabled": "FALSE"}
        for _ in range(4)
    ]


def test_guru_sheet_proposal_only_widens_calendars_and_adds_shadow_exact_rows() -> None:
    rows = _rows()

    proposed = proposed_playbooks(rows)

    assert rows[0]["dte_max"] == "60"
    assert [row["dte_max"] for row in proposed[:2]] == ["90", "90"]
    assert proposed[2] == rows[2]
    assert len(proposed) == len(rows)
    for row in proposed[3:]:
        assert row["mode"] == "shadow"
        assert row["csa_stage"] == "shadow"
        assert row["accepted_inputs"] == "exact_package"
        assert row["live_max_bpr_per_order"] == "1200"


def test_guru_sheet_proposal_stops_on_drift() -> None:
    rows = _rows()
    rows[0]["dte_max"] = "75"
    with pytest.raises(ValueError, match="DTE changed"):
        proposed_playbooks(rows)
