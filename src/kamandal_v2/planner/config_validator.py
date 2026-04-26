"""Validation for loaded universe and playbook configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kamandal_v2.domain.models import Playbook, UniverseEntry
from kamandal_v2.intelligence.transcripts import CONTROLLED_THESIS_TAGS
from kamandal_v2.planner.candidate_builder import SUPPORTED_STRUCTURES
from kamandal_v2.planner.shape_validators import SUPPORTED_VALIDATOR_STRUCTURES


@dataclass(slots=True)
class ConfigValidationResult:
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_config(universe: list[UniverseEntry], playbooks: list[Playbook]) -> ConfigValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    enabled_playbooks = [playbook for playbook in playbooks if playbook.enabled]

    _validate_support(enabled_playbooks, errors)
    _validate_thesis_tags(enabled_playbooks, errors)
    _validate_universe_allowlists(universe, playbooks, warnings)
    _validate_variant_overlap(enabled_playbooks, warnings)

    return ConfigValidationResult(errors=errors, warnings=warnings)


def _validate_support(playbooks: list[Playbook], errors: list[str]) -> None:
    for playbook in playbooks:
        if playbook.structure not in SUPPORTED_STRUCTURES:
            errors.append(f"enabled_playbook_missing_builder:{playbook.playbook_id}:{playbook.structure}")
        if playbook.structure not in SUPPORTED_VALIDATOR_STRUCTURES:
            errors.append(f"enabled_playbook_missing_validator:{playbook.playbook_id}:{playbook.structure}")


def _validate_thesis_tags(playbooks: list[Playbook], errors: list[str]) -> None:
    for playbook in playbooks:
        unknown_tags = sorted({tag for tag in playbook.applicable_thesis_tags if tag not in CONTROLLED_THESIS_TAGS})
        for tag in unknown_tags:
            errors.append(f"enabled_playbook_unknown_thesis_tag:{playbook.playbook_id}:{tag}")


def _validate_universe_allowlists(
    universe: list[UniverseEntry],
    playbooks: list[Playbook],
    warnings: list[str],
) -> None:
    known = set()
    for playbook in playbooks:
        known.update({playbook.playbook_id, playbook.structure, playbook.strategy_family})
    for entry in universe:
        for allowed in entry.allowed_playbooks:
            if allowed and allowed not in known:
                warnings.append(f"universe_allowed_playbook_unknown:{entry.symbol}:{allowed}")


def _validate_variant_overlap(playbooks: list[Playbook], warnings: list[str]) -> None:
    for left_index, left in enumerate(playbooks):
        for right in playbooks[left_index + 1:]:
            if left.structure != right.structure:
                continue
            if left.playbook_id == right.playbook_id:
                warnings.append(f"duplicate_playbook_id:{left.playbook_id}")
                continue
            if not _lists_overlap(left.profiles, right.profiles):
                continue
            if not _lists_overlap(left.applicable_direction, right.applicable_direction):
                continue
            if not _tags_overlap(left.applicable_thesis_tags, right.applicable_thesis_tags):
                continue
            if not _range_overlap(left.applicable_horizon_min, left.applicable_horizon_max, right.applicable_horizon_min, right.applicable_horizon_max, 0, 9999):
                continue
            if not _range_overlap(left.iv_percentile_min, left.iv_percentile_max, right.iv_percentile_min, right.iv_percentile_max, 0.0, 100.0):
                continue
            if not _range_overlap(left.iv_rank_min, left.iv_rank_max, right.iv_rank_min, right.iv_rank_max, 0.0, 100.0):
                continue
            if not _range_overlap(left.iv_abs_min, left.iv_abs_max, right.iv_abs_min, right.iv_abs_max, 0.0, 999.0):
                continue
            warnings.append(
                "overlapping_enabled_variants:"
                f"{left.structure}:{left.playbook_id}<->{right.playbook_id}"
            )


def _lists_overlap(left: list[str], right: list[str]) -> bool:
    if not left or not right:
        return True
    return bool(set(left).intersection(right))


def _tags_overlap(left: list[str], right: list[str]) -> bool:
    if not left or not right:
        return True
    return bool(set(left).intersection(right))


def _range_overlap(
    left_min: float | int | None,
    left_max: float | int | None,
    right_min: float | int | None,
    right_max: float | int | None,
    default_min: float,
    default_max: float,
) -> bool:
    a_min = default_min if left_min is None else float(left_min)
    a_max = default_max if left_max is None else float(left_max)
    b_min = default_min if right_min is None else float(right_min)
    b_max = default_max if right_max is None else float(right_max)
    return max(a_min, b_min) <= min(a_max, b_max)
