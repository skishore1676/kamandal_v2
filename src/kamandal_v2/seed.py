"""Seed rows for the initial Google Sheet cockpit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from kamandal_v2.paths import OLD_KAMANDAL_ROOT
from kamandal_v2.schemas import DAILY_PLAN_HEADER, PLAYBOOKS_HEADER, UNIVERSE_HEADER

CORE_ENABLED_PLAYBOOKS = {
    "short_put",
    "put_spread",
    "call_spread",
    "iron_condor",
    "call_calendar",
    "narrative_ignition_long",
    "narrative_ignition_short",
}

STRUCTURE_LEG_COUNT = {
    "short_put": 1,
    "short_call": 1,
    "long_call": 1,
    "long_put": 1,
    "put_spread": 2,
    "call_spread": 2,
    "short_strangle": 2,
    "call_calendar": 2,
    "calendar_spread": 2,
    "iron_condor": 4,
    "jade_lizard": 3,
    "call_diagonal": 2,
    "put_diagonal": 2,
}

STRATEGY_ID_TO_STRUCTURE = {
    "put_vertical": "put_spread",
    "call_vertical": "call_spread",
    "strangle": "short_strangle",
    "put_calendar": "call_calendar",
}

TEMPLATE_TO_PLAYBOOK = {
    "put_spread_standard": "put_spread",
    "call_spread_standard": "call_spread",
    "iron_condor_standard": "iron_condor",
    "short_put_conservative": "short_put",
    "short_call_conservative": "short_call",
    "short_strangle": "short_strangle",
    "short_strangle_high_iv": "short_strangle",
    "jade_lizard_standard": "jade_lizard",
    "calendar_spread_standard": "call_calendar",
    "long_call_exploratory": "long_call",
    "long_put_exploratory": "long_put",
}

STRUCTURE_VARIANT = {
    "call_calendar": "standard",
    "short_strangle": "high_iv",
    "jade_lizard": "standard",
}

DEFAULT_IV_RANGES = {
    "short_put": (30, 100),
    "put_spread": (30, 100),
    "call_spread": (30, 100),
    "iron_condor": (30, 100),
    "short_call": (30, 100),
    "short_strangle": (35, 100),
    "jade_lizard": (35, 100),
    "call_calendar": (0, 50),
    "long_call": (0, 60),
    "long_put": (0, 60),
}

FALLBACK_TEMPLATES = [
    {
        "id": "short_put_conservative",
        "name": "Short put",
        "structure": "short_put",
        "filters": {"dte_range": [30, 60], "delta_range": [0.15, 0.30]},
        "management": {"profit_target_pct": 50},
    },
    {
        "id": "put_spread_standard",
        "name": "Put spread",
        "structure": "put_spread",
        "filters": {"dte_range": [14, 60], "delta_range": [0.20, 0.35], "spread_width": 5},
        "management": {"profit_target_pct": 50},
    },
    {
        "id": "call_spread_standard",
        "name": "Call spread",
        "structure": "call_spread",
        "filters": {"dte_range": [14, 60], "delta_range": [0.20, 0.35], "spread_width": 5},
        "management": {"profit_target_pct": 50},
    },
    {
        "id": "iron_condor_standard",
        "name": "Iron condor",
        "structure": "iron_condor",
        "filters": {"dte_range": [30, 60], "delta_range": [0.10, 0.25], "spread_width": 5},
        "management": {"profit_target_pct": 50},
    },
    {
        "id": "calendar_spread_standard",
        "name": "Call calendar",
        "structure": "call_calendar",
        "filters": {"dte_range": [20, 60], "delta_range": [0.40, 0.60]},
        "management": {"profit_target_pct": 25},
    },
    {
        "id": "short_strangle",
        "name": "Short strangle",
        "structure": "short_strangle",
        "filters": {"dte_range": [30, 60], "delta_range": [0.10, 0.20]},
        "management": {"profit_target_pct": 50},
    },
    {
        "id": "jade_lizard_standard",
        "name": "Jade lizard",
        "structure": "jade_lizard",
        "filters": {"dte_range": [30, 60], "delta_range": [0.15, 0.30], "spread_width": 5},
        "management": {"profit_target_pct": 50},
    },
]

FALLBACK_PROFILES = [
    {
        "profile_id": "index_etf",
        "symbols": ["SPY", "QQQ", "IWM"],
        "allowed_structures": ["short_put", "put_spread", "call_spread", "iron_condor", "calendar_spread"],
        "earnings_sensitive": False,
        "max_positions": 1,
        "notes": "Built-in fallback profile used when old Kamandal seed files are unavailable.",
    },
    {
        "profile_id": "large_stocks",
        "symbols": ["TSLA", "NVDA"],
        "allowed_structures": ["short_put", "put_spread", "call_spread", "iron_condor", "calendar_spread"],
        "earnings_sensitive": True,
        "max_positions": 1,
        "notes": "Built-in fallback profile used when old Kamandal seed files are unavailable.",
    },
]


def build_seed_tables(control: dict[str, Any]) -> dict[str, list[list[Any]]]:
    playbooks = _playbook_rows()
    playbooks.extend(_narrative_ignition_rows())
    return {
        "universe": _universe_rows(control, playbooks),
        "playbooks": playbooks,
        "daily_plan": [],
    }


def seed_headers() -> dict[str, list[str]]:
    return {
        "universe": UNIVERSE_HEADER,
        "playbooks": PLAYBOOKS_HEADER,
        "daily_plan": DAILY_PLAN_HEADER,
    }


def _old_yaml(name: str) -> dict[str, Any]:
    path = OLD_KAMANDAL_ROOT / "config" / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _old_cache_rows(name: str) -> list[dict[str, Any]]:
    path = OLD_KAMANDAL_ROOT / "data" / "sheet_cache" / name
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("rows") or [])


def _template_rows() -> list[dict[str, Any]]:
    templates = _old_yaml("strategy_templates.yaml").get("templates") or []
    return list(templates) if templates else list(FALLBACK_TEMPLATES)


def _profile_rows() -> list[dict[str, Any]]:
    profiles = _old_yaml("underlying_profiles.yaml").get("profiles") or []
    return list(profiles) if profiles else list(FALLBACK_PROFILES)


def _bool_cell(value: Any) -> str:
    return "TRUE" if bool(value) else "FALSE"


def _list_cell(values: list[Any] | tuple[Any, ...] | None) -> str:
    if not values:
        return ""
    return ", ".join(str(value) for value in values if str(value).strip())


def _range_min(value: Any) -> Any:
    if isinstance(value, list | tuple) and value:
        return value[0]
    return ""


def _range_max(value: Any) -> Any:
    if isinstance(value, list | tuple) and len(value) > 1:
        return value[1]
    return ""


def _playbook_rows() -> list[list[Any]]:
    yaml_templates = {
        str(item.get("id") or ""): item
        for item in _template_rows()
        if item
    }
    cache_templates = {
        str(item.get("template_id") or ""): item
        for item in _old_cache_rows("template_library.json")
        if item
    }

    rows: list[list[Any]] = []
    seen: set[str] = set()
    for template_id, yaml_template in yaml_templates.items():
        cache = cache_templates.get(template_id, {})
        playbook_id = TEMPLATE_TO_PLAYBOOK.get(template_id, template_id)
        if playbook_id in seen:
            continue
        seen.add(playbook_id)

        structure = str(cache.get("structure") or yaml_template.get("structure") or playbook_id)
        if playbook_id == "call_calendar":
            structure = "call_calendar"
        variant = STRUCTURE_VARIANT.get(playbook_id, "standard")
        filters = yaml_template.get("filters") or {}
        management = yaml_template.get("management") or {}
        iv_min, iv_max = DEFAULT_IV_RANGES.get(playbook_id, (0, 100))
        dte_min = cache.get("dte_min") or _range_min(filters.get("dte_range"))
        dte_max = cache.get("dte_max") or _range_max(filters.get("dte_range"))
        delta_min = cache.get("delta_min") or _range_min(filters.get("delta_range"))
        delta_max = cache.get("delta_max") or _range_max(filters.get("delta_range"))
        profiles = _profiles_for_structure(structure)
        short_delta_min = delta_min if structure != "call_calendar" else ""
        short_delta_max = delta_max if structure != "call_calendar" else ""
        long_delta_min = delta_min if structure == "call_calendar" else ""
        long_delta_max = delta_max if structure == "call_calendar" else ""

        rows.append(
            [
                playbook_id,
                _bool_cell(playbook_id in CORE_ENABLED_PLAYBOOKS),
                playbook_id,
                structure,
                variant,
                STRUCTURE_LEG_COUNT.get(structure, ""),
                _list_cell(profiles),
                "",
                "",
                "",
                "",
                iv_min,
                iv_max,
                "",
                "",
                "",
                "",
                "",
                dte_min,
                dte_max,
                "",
                "",
                short_delta_min,
                short_delta_max,
                long_delta_min,
                long_delta_max,
                cache.get("spread_width") or filters.get("spread_width") or "",
                "",
                cache.get("min_credit_to_width_ratio") or filters.get("min_credit_to_width_ratio") or "",
                "",
                "",
                "",
                cache.get("profit_target_pct") or management.get("profit_target_pct") or "",
                cache.get("max_loss_multiple") or management.get("max_loss_multiple") or "",
                cache.get("roll_dte_trigger") or management.get("roll_dte_trigger") or "",
                _bool_cell(True),
                _bool_cell(cache.get("avoid_earnings", yaml_template.get("avoid_earnings", True))),
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                str(cache.get("name") or yaml_template.get("name") or playbook_id),
            ]
        )
    return rows


def _profiles_for_structure(structure: str) -> list[str]:
    matched: list[str] = []
    old_structure = "calendar_spread" if structure == "call_calendar" else structure
    for profile in _profile_rows():
        if old_structure in (profile.get("allowed_structures") or []):
            matched.append(str(profile.get("profile_id") or ""))
    if structure == "call_calendar":
        for extra in ("index_etf", "large_stocks"):
            if extra not in matched:
                matched.append(extra)
    return [value for value in matched if value]


def _narrative_ignition_rows() -> list[list[Any]]:
    base = {
        "profiles": "large_stocks, index_etf",
        "horizon_min": 5,
        "horizon_max": 60,
        "iv_min": 0,
        "iv_max": 50,
        "long_dte_min": 45,
        "long_dte_max": 60,
        "short_delta_min": 0.20,
        "short_delta_max": 0.35,
        "long_delta_min": 0.35,
        "long_delta_max": 0.60,
    }
    return [
        _narrative_row(
            playbook_id="narrative_ignition_long",
            structure="call_diagonal",
            direction="bullish",
            tags="breakout",
            rationale="Shadow-only Big Ideas playbook: Mala structural break plus bullish narrative confirmation.",
            **base,
        ),
        _narrative_row(
            playbook_id="narrative_ignition_short",
            structure="put_diagonal",
            direction="bearish",
            tags="breakdown",
            rationale="Shadow-only Big Ideas playbook: Mala structural break plus bearish narrative confirmation.",
            **base,
        ),
    ]


def _narrative_row(
    *,
    playbook_id: str,
    structure: str,
    direction: str,
    tags: str,
    profiles: str,
    horizon_min: int,
    horizon_max: int,
    iv_min: int,
    iv_max: int,
    long_dte_min: int,
    long_dte_max: int,
    short_delta_min: float,
    short_delta_max: float,
    long_delta_min: float,
    long_delta_max: float,
    rationale: str,
) -> list[Any]:
    return [
        playbook_id,
        "TRUE",
        "narrative_ignition",
        structure,
        "shadow",
        2,
        profiles,
        direction,
        tags,
        horizon_min,
        horizon_max,
        iv_min,
        iv_max,
        "",
        "",
        "",
        "",
        "",
        30,
        45,
        long_dte_min,
        long_dte_max,
        short_delta_min,
        short_delta_max,
        long_delta_min,
        long_delta_max,
        5,
        "",
        "",
        60,
        0.25,
        100,
        50,
        1.0,
        21,
        "TRUE",
        "FALSE",
        3,
        "fixed_contracts",
        1,
        1,
        "",
        "",
        "",
        "",
        50,
        rationale,
        "Requires structural_break:pass annotation from Mala feed.",
    ]


def _universe_rows(control: dict[str, Any], playbooks: list[list[Any]]) -> list[list[Any]]:
    profile_by_id = {
        str(profile.get("profile_id") or ""): profile
        for profile in _profile_rows()
        if profile
    }
    cached_universe = _old_cache_rows("universe.json")
    if cached_universe:
        symbol_profiles = [
            (str(row.get("symbol") or "").upper(), str(row.get("stock_profile") or row.get("profile") or ""))
            for row in cached_universe
            if row.get("enabled", True)
        ]
    else:
        symbol_profiles = []
        for profile_id, profile in profile_by_id.items():
            for symbol in profile.get("symbols") or []:
                symbol_profiles.append((str(symbol).upper(), profile_id))

    max_bpr = ((control.get("portfolio") or {}).get("max_bpr_per_underlying_pct") or 25)
    playbooks_by_profile = _playbooks_by_profile(playbooks)
    rows: list[list[Any]] = []
    seen: set[tuple[str, str]] = set()
    for symbol, profile_id in symbol_profiles:
        if not symbol or not profile_id or (symbol, profile_id) in seen:
            continue
        seen.add((symbol, profile_id))
        profile = profile_by_id.get(profile_id, {})
        earnings_sensitive = bool(profile.get("earnings_sensitive", True))
        rows.append(
            [
                symbol,
                "TRUE",
                profile_id,
                0,
                100,
                max_bpr,
                profile.get("max_positions") or 1,
                _bool_cell(earnings_sensitive),
                7 if earnings_sensitive else 0,
                1 if earnings_sensitive else 0,
                _list_cell(playbooks_by_profile.get(profile_id, [])),
                str(profile.get("notes") or ""),
            ]
        )
    return rows


def _playbooks_by_profile(playbooks: list[list[Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for row in playbooks:
        enabled = str(row[1]).upper() == "TRUE"
        if not enabled:
            continue
        playbook_id = str(row[0])
        profiles = [item.strip() for item in str(row[6]).split(",") if item.strip()]
        for profile in profiles:
            result.setdefault(profile, []).append(playbook_id)
    return result
