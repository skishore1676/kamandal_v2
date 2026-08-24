from __future__ import annotations

from kamandal_v2.domain.models import Candidate, Greeks, PreflightResult
from kamandal_v2.market.venue_router import VenueAwareMarket
from kamandal_v2.planner.shadow_preflight import ShadowStranglePreflight, shadow_preflight_client


def _candidate(
    *,
    structure: str = "short_strangle",
    bpr: float = 3100.0,
    execution_venue: str = "public_primary",
) -> Candidate:
    return Candidate(
        candidate_id="candidate",
        idea_id="idea",
        underlying="XYZ",
        playbook_id="short_strangle_high_iv",
        structure=structure,
        legs=[],
        net_credit=2.0,
        estimated_bpr=bpr,
        greeks=Greeks(),
        liquidity_score=1.0,
        score=0.0,
        execution_venue=execution_venue,
    )


class _Primary:
    def __init__(self, result: PreflightResult) -> None:
        self.result = result

    def preflight(self, _candidate: Candidate) -> PreflightResult:
        return self.result


def _public_failure(code: int) -> PreflightResult:
    return PreflightResult(
        ok=False,
        bpr=3100.0,
        message=f"Public error {code}",
        raw={"public_api_error": {"http_status": 400, "code": code}},
    )


def test_level_four_failure_uses_tastytrade_bpr_for_shadow() -> None:
    class TastytradeDryRun:
        def preflight(self, _candidate: Candidate) -> PreflightResult:
            return PreflightResult(
                ok=False,
                bpr=3922.61658,
                message="margin check failed",
                raw={"response": {"data": {"buying-power-effect": {"change-in-buying-power": "3922.61658"}}}},
            )

    result = ShadowStranglePreflight(
        _Primary(_public_failure(159)),
        secondary=TastytradeDryRun(),
    ).preflight(_candidate())

    assert result.ok is True
    assert result.bpr == 3922.62
    assert result.raw["quote_source"] == "public"
    assert result.raw["bpr_source"] == "tastytrade_dry_run"
    assert result.raw["shadow_eligible"] is True
    assert result.raw["live_eligible"] is False
    assert result.raw["live_blocker"] == "public_level_4_required"


def test_level_four_failure_uses_labeled_local_estimate_when_tastytrade_is_unavailable() -> None:
    result = ShadowStranglePreflight(_Primary(_public_failure(159))).preflight(_candidate(bpr=4100.0))

    assert result.ok is True
    assert result.bpr == 4100.0
    assert result.raw["bpr_source"] == "local_estimate"
    assert result.raw["broker_bpr_provided"] is False


def test_tastytrade_response_without_bpr_does_not_masquerade_as_broker_bpr() -> None:
    class TastytradeWithoutBpr:
        def preflight(self, _candidate: Candidate) -> PreflightResult:
            return PreflightResult(
                ok=False,
                bpr=3100.0,
                message="dry run returned no buying-power field",
                raw={"response": {"error": {"message": "unavailable"}}},
            )

    result = ShadowStranglePreflight(
        _Primary(_public_failure(159)),
        secondary=TastytradeWithoutBpr(),
    ).preflight(_candidate(bpr=4100.0))

    assert result.bpr == 4100.0
    assert result.raw["bpr_source"] == "local_estimate"


def test_non_entitlement_failure_and_non_strangle_remain_blocked() -> None:
    invalid_contract = _public_failure(157)
    assert ShadowStranglePreflight(_Primary(invalid_contract)).preflight(_candidate()) is invalid_contract
    entitlement = _public_failure(159)
    assert ShadowStranglePreflight(_Primary(entitlement)).preflight(_candidate(structure="call_spread")) is entitlement


def test_live_mode_never_receives_shadow_fallback() -> None:
    primary = _Primary(_public_failure(159))
    assert shadow_preflight_client({}, primary, provider="public", mode="live") is primary


def test_direct_tasty_account_capacity_failure_is_shadow_evidence_not_authority(monkeypatch) -> None:
    tasty_failure = PreflightResult(
        ok=False,
        bpr=1339.48,
        message="tastytrade preflight returned errors",
        raw={
            "response": {
                "data": {"buying-power-effect": {"change-in-buying-power": "1339.48"}},
                "error": {
                    "errors": [{
                        "code": "margin_check_failed_with_flags",
                        "message": "Account does not have sufficient buying power.",
                    }]
                },
            },
            "broker_bpr_provided": True,
            "bpr_source": "tastytrade_dry_run",
        },
    )

    class Tasty:
        def available(self) -> bool:
            return True

        def preflight(self, _candidate: Candidate) -> PreflightResult:
            return tasty_failure

    monkeypatch.setattr("kamandal_v2.market.venue_router.broker_adapter", lambda *_args, **_kwargs: Tasty())
    routed = VenueAwareMarket(
        inner=object(),
        primary_preflight=_Primary(_public_failure(159)),
        config={},
        mode="shadow",
        venues={"tasty_primary"},
        provider="public",
    )
    shadow = shadow_preflight_client({}, routed, provider="public", mode="shadow")

    result = shadow.preflight(_candidate(execution_venue="tasty_primary", bpr=1710.0))

    assert result.ok is True
    assert result.bpr == 1339.48
    assert result.raw["bpr_source"] == "tastytrade_dry_run"
    assert result.raw["live_eligible"] is False
    assert result.raw["live_blocker"] == "tastytrade_account_capacity_or_permission"


def test_direct_tasty_structural_failure_remains_blocked_in_shadow() -> None:
    failure = PreflightResult(
        ok=False,
        bpr=0.0,
        message="invalid order",
        raw={
            "execution_venue": "tasty_primary",
            "execution_broker": "tastytrade",
            "response": {"error": {"errors": [{"code": "invalid_symbol", "message": "Invalid option symbol"}]}},
        },
    )

    assert ShadowStranglePreflight(_Primary(failure)).preflight(
        _candidate(execution_venue="tasty_primary")
    ) is failure


def test_direct_tasty_account_capacity_failure_remains_blocked_live() -> None:
    failure = PreflightResult(
        ok=False,
        bpr=1339.48,
        message="insufficient buying power",
        raw={"execution_venue": "tasty_primary", "execution_broker": "tastytrade"},
    )
    primary = _Primary(failure)

    assert shadow_preflight_client({}, primary, provider="public", mode="live").preflight(
        _candidate(execution_venue="tasty_primary")
    ) is failure
