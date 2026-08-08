"""Transparent Sheet-weighted CSA scoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from kamandal_v2.strategy_lanes.policy import CsaPolicy


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: float
    components: dict[str, float]
    weights: dict[str, float]
    penalties: dict[str, float]
    penalty_weights: dict[str, float]
    formula: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "components": self.components,
            "weights": self.weights,
            "penalties": self.penalties,
            "penalty_weights": self.penalty_weights,
            "formula": self.formula,
        }


def score_opportunity(
    policy: CsaPolicy,
    components: Mapping[str, Any],
    *,
    penalties: Mapping[str, Any] | None = None,
) -> ScoreResult:
    weights = _numeric_map(policy.management.get("score_weights"), label="score_weights")
    normalized_components = _numeric_map(components, label="components", bounded=True)
    missing = sorted(set(weights) - set(normalized_components))
    unexpected = sorted(set(normalized_components) - set(weights))
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if unexpected:
            details.append(f"unexpected={','.join(unexpected)}")
        raise ValueError("score components do not match Sheet weights: " + " ".join(details))
    weight_total = sum(weights.values())
    if weight_total <= 0:
        raise ValueError("Sheet score_weights must have positive total weight")
    base = sum(normalized_components[name] * weights[name] for name in sorted(weights)) / weight_total

    raw_penalty_weights = policy.management.get("penalty_weights") or {}
    penalty_weights = _numeric_map(raw_penalty_weights, label="penalty_weights")
    normalized_penalties = _numeric_map(penalties or {}, label="penalties", bounded=True)
    missing_penalties = sorted(set(penalty_weights) - set(normalized_penalties))
    unexpected_penalties = sorted(set(normalized_penalties) - set(penalty_weights))
    if missing_penalties or unexpected_penalties:
        raise ValueError("penalties do not match Sheet penalty_weights")
    penalty = sum(normalized_penalties[name] * penalty_weights[name] for name in sorted(penalty_weights))
    score = round(max(0.0, min(100.0, base - penalty)), 6)
    return ScoreResult(
        score=score,
        components=normalized_components,
        weights=weights,
        penalties=normalized_penalties,
        penalty_weights=penalty_weights,
        formula="weighted_component_average_minus_sheet_weighted_penalties",
    )


def _numeric_map(value: Any, *, label: str, bounded: bool = False) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    result: dict[str, float] = {}
    for raw_name, raw_value in sorted(value.items(), key=lambda item: str(item[0])):
        name = str(raw_name).strip()
        if not name or isinstance(raw_value, bool):
            raise ValueError(f"{label} contains invalid value for {raw_name!r}")
        try:
            number = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}.{name} must be numeric") from exc
        if number < 0 or (bounded and number > 100):
            raise ValueError(f"{label}.{name} is outside its domain")
        result[name] = number
    return result
