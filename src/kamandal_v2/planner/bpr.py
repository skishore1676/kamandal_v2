"""Shared buying-power-reduction (BPR) cap resolution.

Both the planner (candidate construction / vertical width search) and the
live executor (order-time cap enforcement in `kamandal_v2.live.advisory`)
need to know the per-order BPR cap for a structure. This lives in one place
so the two layers cannot drift apart.
"""

from __future__ import annotations

from typing import Any


def structure_bpr_cap(structure: str, live_cfg: dict[str, Any]) -> float:
    """Resolve the per-order BPR cap for `structure` from `live_cfg`.

    Looks up `live.max_bpr_per_order_by_structure[structure]`, falling back
    to the `default` entry in that map, and finally to `live.max_bpr_per_order`
    (or 300.0) if neither is configured.
    """
    fallback = float(live_cfg.get("max_bpr_per_order") or 300.0)
    by_structure = live_cfg.get("max_bpr_per_order_by_structure") or {}
    if not isinstance(by_structure, dict):
        return fallback
    key = str(structure or "").strip().lower()
    if key == "short_strangle" and "strangle" in by_structure and key not in by_structure:
        key = "strangle"
    raw = by_structure.get(key, by_structure.get("default", fallback))
    if raw in (None, ""):
        return fallback
    return float(raw)
