"""Typed CSA opportunity, lifecycle, action, and ticket contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CsaStage(StrEnum):
    BASELINE = "baseline"
    SHADOW = "shadow"
    PILOT_LIVE = "pilot_live"
    LIVE = "live"


class SourceMode(StrEnum):
    IDEA = "idea"
    MARKET_SCAN = "market_scan"
    PORTFOLIO_HEDGE = "portfolio_hedge"


class LaneId(StrEnum):
    SHORT_STRANGLE = "short_strangle"
    CALL_VERTICAL = "call_vertical"
    DIRECTIONAL_DIAGONAL = "directional_diagonal"
    GENERIC_CLOSE_ONLY = "generic_close_only"
    EARNINGS_CALENDAR = "earnings_calendar"


class ActionType(StrEnum):
    HOLD = "hold"
    OPEN = "open"
    CLOSE = "close"
    ADJUST = "adjust"
    DURATION_ROLL = "duration_roll"
    BLOCK = "block"


class ActionDisposition(StrEnum):
    PROPOSED = "proposed"
    SELECTED = "selected"
    SUPERSEDED = "superseded"
    BLOCKED = "blocked"


class LegSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class LegEffect(StrEnum):
    OPEN = "open"
    CLOSE = "close"


def stable_csa_id(namespace: str, payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:24]
    return f"csa:{namespace}:{digest}"


@dataclass(frozen=True, slots=True)
class StrategyOpportunity:
    opportunity_id: str
    lane: LaneId
    source_mode: SourceMode
    playbook_id: str
    underlying: str
    observed_at: str
    source_id: str
    policy_hash: str
    evidence: dict[str, Any]
    market_context: dict[str, Any] = field(default_factory=dict)
    event_context: dict[str, Any] = field(default_factory=dict)
    portfolio_context: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AdmissionStageResult:
    stage: str
    passed: bool
    reasons: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    decision_id: str
    opportunity_id: str
    admitted: bool
    primary_blocker: str
    stages: tuple[AdmissionStageResult, ...]
    policy_hash: str
    decided_at: str
    score: float | None = None
    score_components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LifecycleState:
    lifecycle_id: str
    opportunity_id: str
    lane: LaneId
    version: int
    status: str
    active_legs: tuple[dict[str, Any], ...]
    cashflow_ledger: tuple[dict[str, Any], ...]
    opened_at: str
    updated_at: str
    policy_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def position_projection_id(self) -> str:
        """Return the live-book projection owned by this lifecycle."""

        return str(
            self.metadata.get("position_projection_id")
            or self.metadata.get("legacy_source_id")
            or ""
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CsaAction:
    action_id: str
    lifecycle_id: str
    lifecycle_version: int
    action_type: ActionType
    disposition: ActionDisposition
    reason_codes: tuple[str, ...]
    proposed_at: str
    priority: int
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        return stable_csa_id(
            "action",
            [self.lifecycle_id, self.lifecycle_version, self.action_type.value, self.payload],
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["idempotency_key"] = self.idempotency_key
        return data


@dataclass(frozen=True, slots=True)
class TicketLeg:
    instrument_id: str
    side: LegSide
    effect: LegEffect
    quantity: int
    option_type: str
    expiration: str
    strike: float
    role: str

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("ticket leg quantity must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StrategyTicket:
    ticket_id: str
    action_id: str
    lifecycle_id: str
    lifecycle_version: int
    lane: LaneId
    underlying: str
    order_kind: str
    limit_price: float
    legs: tuple[TicketLeg, ...]
    policy_hash: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.legs:
            raise ValueError("strategy ticket requires at least one leg")

    @property
    def idempotency_key(self) -> str:
        return stable_csa_id(
            "ticket",
            [
                self.action_id,
                self.lifecycle_version,
                str(self.metadata.get("execution_venue") or "public_primary"),
                [leg.to_dict() for leg in self.legs],
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["idempotency_key"] = self.idempotency_key
        return data


@dataclass(frozen=True, slots=True)
class ShadowFill:
    fill_id: str
    ticket_id: str
    lifecycle_id: str
    status: str
    attempt: int
    natural_price: float
    working_price: float
    filled_price: float | None
    filled_at: str
    quote_evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
