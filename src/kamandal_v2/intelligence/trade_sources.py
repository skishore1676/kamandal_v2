"""Compile source-neutral operator routing for translated trade sources."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable


class TradeSourcePolicyError(ValueError):
    """The Sheet cannot safely authorize a trade-source output."""


class TradeSourceOutputKind(StrEnum):
    IDEA = "idea"
    EXACT_PACKAGE = "exact_package"


class TradeSourceMode(StrEnum):
    OFF = "off"
    OBSERVE = "observe"
    SHADOW = "shadow"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class TradeSourcePolicy:
    source_id: str
    output_kind: TradeSourceOutputKind
    mode: TradeSourceMode
    notes: str = ""

    @property
    def inference_enabled(self) -> bool:
        return self.mode is not TradeSourceMode.OFF

    @property
    def planner_enabled(self) -> bool:
        return self.mode in {TradeSourceMode.SHADOW, TradeSourceMode.LIVE}


@dataclass(frozen=True, slots=True)
class TradeSourcePolicyCompilation:
    policies: tuple[TradeSourcePolicy, ...]
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def by_key(self) -> dict[tuple[str, TradeSourceOutputKind], TradeSourcePolicy]:
        return {(policy.source_id, policy.output_kind): policy for policy in self.policies}


def compile_trade_source_policies(
    rows: Iterable[dict[str, Any]],
    *,
    required_source_ids: Iterable[str] = (),
) -> TradeSourcePolicyCompilation:
    policies: list[TradeSourcePolicy] = []
    errors: list[str] = []
    seen: set[tuple[str, TradeSourceOutputKind]] = set()
    for row_number, row in enumerate(rows, start=2):
        if not any(str(value or "").strip() for value in row.values()):
            continue
        source_id = str(row.get("source_id") or "").strip().lower()
        raw_kind = str(row.get("output_kind") or "").strip().lower()
        raw_mode = str(row.get("mode") or "").strip().lower()
        if not source_id or not source_id.replace("_", "").isalnum():
            errors.append(f"trade_sources row {row_number}: invalid source_id={source_id!r}")
            continue
        try:
            output_kind = TradeSourceOutputKind(raw_kind)
        except ValueError:
            errors.append(f"trade_sources row {row_number}: invalid output_kind={raw_kind!r}")
            continue
        try:
            mode = TradeSourceMode(raw_mode)
        except ValueError:
            errors.append(f"trade_sources row {row_number}: invalid mode={raw_mode!r}")
            continue
        key = (source_id, output_kind)
        if key in seen:
            errors.append(f"trade_sources: duplicate source/output row {source_id}/{output_kind.value}")
            continue
        seen.add(key)
        if output_kind is TradeSourceOutputKind.EXACT_PACKAGE and mode is TradeSourceMode.LIVE:
            errors.append(f"trade_sources: {source_id}/exact_package cannot be live in the first release")
            continue
        policies.append(
            TradeSourcePolicy(
                source_id=source_id,
                output_kind=output_kind,
                mode=mode,
                notes=str(row.get("notes") or "").strip(),
            )
        )

    required = {str(source_id).strip().lower() for source_id in required_source_ids if str(source_id).strip()}
    for source_id in sorted(required):
        for output_kind in TradeSourceOutputKind:
            if (source_id, output_kind) not in seen:
                errors.append(f"trade_sources: missing required row {source_id}/{output_kind.value}")
    return TradeSourcePolicyCompilation(tuple(policies), tuple(errors))


def source_id_from_idea_source(value: str) -> str | None:
    """Return the governed source id embedded in a correspondent Idea source."""

    parts = str(value or "").split(":", 2)
    if len(parts) >= 2 and parts[0].strip().lower() == "correspondent":
        source_id = parts[1].strip().lower()
        return source_id or None
    return None
