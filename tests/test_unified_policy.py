from __future__ import annotations

import json

import pytest

from kamandal_v2.strategy_engine.policy import (
    ExecutionMode,
    PolicyError,
    compile_playbook_policies,
    compile_playbook_policy,
)
from kamandal_v2.strategy_engine.registry import capability_registry


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "playbook_id": "call_spread_default",
        "enabled": "TRUE",
        "strategy_family": "call_spread",
        "structure": "call_spread",
        "csa_stage": "baseline",
        "source_mode": "idea",
        "dte_min": "30",
        "dte_max": "45",
        "max_loss_multiple": "0.5",
        "sizing_method": "fixed_contracts",
        "sizing_value": "1",
        "max_contracts": "1",
        "max_debit_to_width_ratio": "0.75",
    }
    row.update(overrides)
    return row


def _strangle_row(**overrides: object) -> dict[str, object]:
    row = _row(
        playbook_id="short_strangle_high_iv",
        strategy_family="short_strangle",
        structure="short_strangle",
        csa_stage="shadow",
        source_mode="market_scan",
        short_delta_min="0.14",
        short_delta_max="0.20",
        exit_dte_min="21",
        management_policy_json=json.dumps(
            {
                "lifecycle": {
                    "tested_side_confirmation": 2,
                    "adjustment_limit": 2,
                    "cooldown": {"minutes": 30},
                    "roll": {"duration_trigger_dte": 21, "min_credit": 0.10},
                    "inversion": {"allowed": True, "max_width": 5},
                }
            }
        ),
    )
    row.update(overrides)
    return row


def test_legacy_stage_is_confined_to_mode_adapter() -> None:
    baseline = compile_playbook_policy(_row())
    shadow = compile_playbook_policy(_strangle_row())
    pilot = compile_playbook_policy(_row(csa_stage="pilot_live"))

    assert baseline.mode is ExecutionMode.LIVE
    assert shadow.mode is ExecutionMode.SHADOW
    assert pilot.mode is ExecutionMode.LIVE
    assert baseline.compatibility["legacy_stage"] == "baseline"
    assert shadow.compatibility["legacy_stage"] == "shadow"


def test_explicit_mode_overrides_legacy_stage_and_unknown_mode_fails_closed() -> None:
    assert compile_playbook_policy(_row(mode="shadow")).mode is ExecutionMode.SHADOW
    with pytest.raises(PolicyError, match="invalid mode"):
        compile_playbook_policy(_row(mode="paper"))


def test_every_current_enabled_family_has_one_registered_capability() -> None:
    rows = [
        _row(playbook_id="put_spread_default", strategy_family="put_spread", structure="put_spread"),
        _row(playbook_id="call_spread_default"),
        _row(playbook_id="iron_condor_default", strategy_family="iron_condor", structure="iron_condor"),
        _strangle_row(),
        _row(playbook_id="jade_lizard_high_iv", strategy_family="jade_lizard", structure="jade_lizard"),
        _row(playbook_id="call_calendar_low_iv", strategy_family="call_calendar", structure="call_calendar"),
        _row(playbook_id="put_calendar_low_iv", strategy_family="put_calendar", structure="put_calendar"),
        _row(playbook_id="put_diagonal_overextended", strategy_family="put_diagonal", structure="put_diagonal"),
        _row(playbook_id="call_diagonal_oversold", strategy_family="call_diagonal", structure="call_diagonal"),
        _row(playbook_id="narrative_ignition_long", strategy_family="narrative_ignition", structure="call_diagonal"),
        _row(playbook_id="narrative_ignition_short", strategy_family="narrative_ignition", structure="put_diagonal"),
    ]

    compiled = compile_playbook_policies(rows)

    assert compiled.ok
    assert len(compiled.policies) == len(rows)
    assert {policy.capability.key for policy in compiled.policies} == {row["strategy_family"] for row in rows}
    assert all(policy.capability.key != policy.structure or policy.capability.key for policy in compiled.policies)


def test_capability_is_not_inferred_from_structure() -> None:
    generic = compile_playbook_policy(_row(playbook_id="generic", strategy_family="call_calendar", structure="call_calendar"))
    earnings = compile_playbook_policy(
        _row(
            playbook_id="event",
            strategy_family="earnings_calendar",
            structure="call_calendar",
            source_mode="idea",
            applicable_direction="bullish,bearish",
            long_dte_min="45",
            long_dte_max="60",
            dte_min="5",
            dte_max="7",
            event_timing="confirmed",
            near_expiry_after_event="TRUE",
        )
    )

    assert generic.capability.key == "call_calendar"
    assert earnings.capability.key == "earnings_calendar"
    assert generic.capability.key != earnings.capability.key


def test_short_strangle_compiles_frozen_management_policy_separate_from_entry_delta() -> None:
    policy = compile_playbook_policy(_strangle_row())
    management = policy.strangle_management

    assert management is not None
    assert management.target_delta == 0.30
    assert management.max_delta == 0.40
    assert management.dte_action == "close"
    assert management.duration_roll_limit == 0
    assert management.inversion_enabled is False
    assert management.entry_delta_range == (0.14, 0.20)
    assert management.target_delta not in management.entry_delta_range
    assert policy.compatibility["legacy_inversion_ignored"] is True


def test_strangle_sheet_controls_compile_as_one_immutable_policy() -> None:
    policy = compile_playbook_policy(
        _strangle_row(
            execution_venue="tasty_primary",
            dte_min="35",
            dte_max="50",
            target_dte="45",
            short_delta_max="0.22",
            range_gate_required="TRUE",
            range_gate_max_age_days="7",
            loss_close_multiple="3",
        )
    )

    assert policy.fields["execution_venue"] == "tasty_primary"
    assert policy.fields["target_dte"] == "45"
    assert policy.fields["range_gate_required"] == "TRUE"
    assert policy.strangle_management.loss_close_multiple == 3


def test_unknown_execution_venue_fails_closed() -> None:
    with pytest.raises(PolicyError, match="unsupported execution_venue"):
        compile_playbook_policy(_strangle_row(execution_venue="oldmac_clone"))


def test_strangle_management_delta_never_falls_back_to_entry_delta() -> None:
    with pytest.raises(PolicyError, match="management_delta_target"):
        compile_playbook_policy(
            _strangle_row(
                mode="shadow",
                management_delta_target="",
                management_delta_max="0.40",
                legacy_management_defaults="FALSE",
            )
        )


def test_live_approval_branch_and_diagonal_roll_are_rejected() -> None:
    with pytest.raises(PolicyError, match="operator approval"):
        compile_playbook_policy(_row(management_policy_json=json.dumps({"lifecycle": {"requires_approval": True}})))
    with pytest.raises(PolicyError, match="short-leg roll"):
        compile_playbook_policy(
            _row(
                strategy_family="call_diagonal",
                structure="call_diagonal",
                mode="live",
                management_policy_json=json.dumps({"lifecycle": {"short_leg": {"roll": True}}}),
            )
        )


def test_legacy_baseline_diagonal_is_converted_to_paired_management() -> None:
    policy = compile_playbook_policy(
        _row(
            strategy_family="call_diagonal",
            structure="call_diagonal",
            management_policy_json=json.dumps(
                {"lifecycle": {"short_leg": {"roll": True}, "long_only": {"requires_approval": True}}}
            ),
        )
    )

    assert policy.management["lifecycle"] == {}
    assert policy.compatibility["legacy_diagonal_management_ignored"] == ["long_only", "short_leg"]


def test_directional_diagonal_rejects_unreachable_debit_loss_and_unimplemented_sizing() -> None:
    with pytest.raises(PolicyError, match="loss fraction"):
        compile_playbook_policy(
            _row(
                strategy_family="call_diagonal",
                structure="call_diagonal",
                max_loss_multiple="1.5",
            )
        )
    with pytest.raises(PolicyError, match="fixed_contracts"):
        compile_playbook_policy(
            _row(
                strategy_family="put_diagonal",
                structure="put_diagonal",
                sizing_method="pct_account_bpr",
                sizing_value="0.04",
                max_contracts="2",
            )
        )


def test_proposed_universe_activation_and_unknown_capability_fail_closed() -> None:
    with pytest.raises(PolicyError, match="proposed"):
        compile_playbook_policy(_row(tier="proposed", enabled="TRUE"))
    with pytest.raises(PolicyError, match="Unknown strategy capability"):
        compile_playbook_policy(_row(strategy_family="not_registered"))


def test_registry_covers_supported_capabilities_without_structure_fallback() -> None:
    registry = capability_registry()

    assert registry.resolve("short_strangle").key == "short_strangle"
    assert registry.resolve("earnings_calendar").allowed_structures == frozenset({"call_calendar", "put_calendar"})
    with pytest.raises(LookupError):
        registry.resolve("call_calendar_from_structure")
