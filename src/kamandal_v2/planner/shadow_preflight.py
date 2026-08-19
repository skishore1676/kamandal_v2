"""Shadow-only broker-feasibility fallback for undefined-risk strangles.

Public error 159 describes the account's live entitlement.  It does not make
an otherwise valid market snapshot or strategy construction unusable as paper
evidence.  This adapter preserves Public as the live authority while allowing
the shadow book to obtain an exact-leg BPR estimate from Tastytrade, then the
planner's labeled local estimate when Tastytrade is unavailable.
"""

from __future__ import annotations

from typing import Any

from kamandal_v2.domain.models import Candidate, PreflightResult
from kamandal_v2.market.tastytrade import TastytradeAdapter


PUBLIC_LEVEL_FOUR_ERROR = 159


class ShadowStranglePreflight:
    """Turn only Public's Level-4 rejection into shadow feasibility."""

    def __init__(self, primary: Any, *, secondary: Any | None = None) -> None:
        self.primary = primary
        self.secondary = secondary

    def preflight(self, candidate: Candidate) -> PreflightResult:
        primary = self.primary.preflight(candidate)
        if primary.ok or candidate.structure not in {"short_strangle", "strangle"}:
            return primary
        if _public_error_code(primary) != PUBLIC_LEVEL_FOUR_ERROR:
            return primary

        secondary: PreflightResult | None = None
        secondary_failure_type = ""
        if self.secondary is not None:
            try:
                secondary = self.secondary.preflight(candidate)
            except Exception as exc:  # noqa: BLE001 - shadow falls back without hiding provenance.
                secondary_failure_type = type(exc).__name__

        local_bpr = abs(float(candidate.estimated_bpr or 0.0))
        secondary_bpr = abs(float(secondary.bpr or 0.0)) if secondary is not None else 0.0
        secondary_raw = secondary.raw if secondary is not None and isinstance(secondary.raw, dict) else {}
        tasty_bpr_provided = secondary_bpr > 0 and _response_contains_bpr(secondary_raw.get("response"))
        bpr = secondary_bpr if tasty_bpr_provided else local_bpr
        if bpr <= 0:
            return primary

        bpr_source = "tastytrade_dry_run" if tasty_bpr_provided else "local_estimate"
        return PreflightResult(
            ok=True,
            bpr=round(bpr, 2),
            message="shadow feasible; Public Level 4 is still required for live entry",
            raw={
                "source": "shadow_strangle_feasibility",
                "quote_source": "public",
                "bpr_source": bpr_source,
                "bpr_broker": "tastytrade" if tasty_bpr_provided else "local",
                "broker_bpr_provided": tasty_bpr_provided,
                "public_error_code": PUBLIC_LEVEL_FOUR_ERROR,
                "public_live_eligibility": "level_4_required",
                "live_eligible": False,
                "live_blocker": "public_level_4_required",
                "shadow_eligible": True,
                "secondary_preflight_ok": secondary.ok if secondary is not None else None,
                "secondary_failure_type": secondary_failure_type,
            },
        )


def shadow_preflight_client(config: dict[str, Any], primary: Any, *, provider: str, mode: str) -> Any:
    """Wrap Public preflight only for the shadow book."""

    if provider != "public" or mode != "shadow":
        return primary
    tasty = TastytradeAdapter(config)
    secondary = tasty if tasty.available() else None
    return ShadowStranglePreflight(primary, secondary=secondary)


def _public_error_code(result: PreflightResult) -> int:
    raw = result.raw if isinstance(result.raw, dict) else {}
    error = raw.get("public_api_error") if isinstance(raw.get("public_api_error"), dict) else {}
    try:
        return int(error.get("code") or 0)
    except (TypeError, ValueError):
        return 0


def _response_contains_bpr(value: object) -> bool:
    keys = {
        "impact",
        "change-in-buying-power",
        "isolated-order-margin-requirement",
        "buying-power-requirement",
    }
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if any(key in item and item[key] not in (None, "") for key in keys):
                return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False
