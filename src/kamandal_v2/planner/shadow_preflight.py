"""Shadow-only broker-account isolation for undefined-risk strangles.

Public error 159 and Tastytrade account-capacity failures describe a real
account's live entitlement or balance.  Neither makes an otherwise valid
market snapshot or strategy construction unusable as paper evidence.  This
adapter preserves those failures as live blockers while allowing the shadow
book to use exact-leg BPR evidence, then the planner's labeled local estimate.
"""

from __future__ import annotations

from typing import Any

from kamandal_v2.domain.models import Candidate, PreflightResult
from kamandal_v2.market.tastytrade import TastytradeAdapter


PUBLIC_LEVEL_FOUR_ERROR = 159


class ShadowStranglePreflight:
    """Keep broker-account capacity out of shadow strategy feasibility."""

    def __init__(self, primary: Any, *, secondary: Any | None = None) -> None:
        self.primary = primary
        self.secondary = secondary

    def preflight(self, candidate: Candidate) -> PreflightResult:
        primary = self.primary.preflight(candidate)
        if primary.ok or candidate.structure not in {"short_strangle", "strangle"}:
            return primary
        public_level_four = _public_error_code(primary) == PUBLIC_LEVEL_FOUR_ERROR
        tasty_account_blocker = _tasty_account_only_blocker(primary)
        if not public_level_four and not tasty_account_blocker:
            return primary

        secondary: PreflightResult | None = None
        secondary_failure_type = ""
        if public_level_four and self.secondary is not None:
            try:
                secondary = self.secondary.preflight(candidate)
            except Exception as exc:  # noqa: BLE001 - shadow falls back without hiding provenance.
                secondary_failure_type = type(exc).__name__

        local_bpr = abs(float(candidate.estimated_bpr or 0.0))
        evidence = secondary if secondary is not None else primary
        evidence_bpr = abs(float(evidence.bpr or 0.0))
        evidence_raw = evidence.raw if isinstance(evidence.raw, dict) else {}
        tasty_bpr_provided = evidence_bpr > 0 and (
            bool(evidence_raw.get("broker_bpr_provided"))
            or _response_contains_bpr(evidence_raw.get("response"))
        )
        bpr = evidence_bpr if tasty_bpr_provided else local_bpr
        if bpr <= 0:
            return primary

        bpr_source = "tastytrade_dry_run" if tasty_bpr_provided else "local_estimate"
        live_blocker = "public_level_4_required" if public_level_four else "tastytrade_account_capacity_or_permission"
        return PreflightResult(
            ok=True,
            bpr=round(bpr, 2),
            message="shadow feasible; active broker account state is not a shadow authorization gate",
            raw={
                "source": "shadow_strangle_feasibility",
                "quote_source": "public",
                "bpr_source": bpr_source,
                "bpr_broker": "tastytrade" if tasty_bpr_provided else "local",
                "broker_bpr_provided": tasty_bpr_provided,
                "public_error_code": PUBLIC_LEVEL_FOUR_ERROR if public_level_four else None,
                "public_live_eligibility": "level_4_required" if public_level_four else None,
                "live_eligible": False,
                "live_blocker": live_blocker,
                "shadow_eligible": True,
                "secondary_preflight_ok": secondary.ok if secondary is not None else None,
                "secondary_failure_type": secondary_failure_type,
                "broker_preflight_raw": evidence_raw,
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


def _tasty_account_only_blocker(result: PreflightResult) -> bool:
    """Recognize only account capacity/permission errors, never order defects."""

    raw = result.raw if isinstance(result.raw, dict) else {}
    venue = str(raw.get("execution_venue") or "").strip().lower()
    broker = str(raw.get("execution_broker") or "").strip().lower()
    if venue != "tasty_primary" and broker not in {"tasty", "tastytrade"}:
        return False
    response = raw.get("response")
    fragments: list[str] = []
    stack = [response]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in ("code", "message", "error", "error-code", "error_message"):
                value = item.get(key)
                if isinstance(value, str):
                    fragments.append(value.lower())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    text = " ".join(fragments)
    markers = (
        "margin_check_failed",
        "insufficient buying power",
        "insufficient_buying_power",
        "buying power is insufficient",
        "account does not have sufficient buying power",
        "option level",
        "options level",
        "trading permission",
        "not approved for",
    )
    return any(marker in text for marker in markers)
