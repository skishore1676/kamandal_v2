"""Normalize provider IV metrics into planner-facing percent points."""

from __future__ import annotations

from typing import Any


def normalize_iv_percentile(value: Any) -> float | None:
    return _normalize_bounded_percent(value)


def normalize_iv_rank(value: Any) -> float | None:
    return _normalize_bounded_percent(value)


def normalize_iv_abs(value: Any) -> float | None:
    parsed = _optional_float(value)
    if parsed is None:
        return None
    if 0.0 < abs(parsed) <= 5.0:
        return round(parsed * 100.0, 4)
    return parsed


def _normalize_bounded_percent(value: Any) -> float | None:
    parsed = _optional_float(value)
    if parsed is None:
        return None
    if 0.0 < abs(parsed) <= 1.0:
        return round(parsed * 100.0, 4)
    return parsed


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
