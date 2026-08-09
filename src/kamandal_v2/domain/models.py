"""Core domain models for deterministic planning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(slots=True)
class Greeks:
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0

    def __add__(self, other: "Greeks") -> "Greeks":
        return Greeks(
            delta=self.delta + other.delta,
            gamma=self.gamma + other.gamma,
            theta=self.theta + other.theta,
            vega=self.vega + other.vega,
        )

    def scale(self, factor: float) -> "Greeks":
        return Greeks(
            delta=self.delta * factor,
            gamma=self.gamma * factor,
            theta=self.theta * factor,
            vega=self.vega * factor,
        )

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(slots=True)
class IdeaAnnotation:
    trade_horizon_suggestion: dict[str, Any] = field(default_factory=dict)
    profile_suggestions: list[str] = field(default_factory=list)
    direction_sanity: str = ""
    liquidity_preference: str = ""
    review_flags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "IdeaAnnotation":
        payload = payload or {}
        return cls(
            trade_horizon_suggestion=dict(payload.get("trade_horizon_suggestion") or {}),
            profile_suggestions=_as_list(payload.get("profile_suggestions")),
            direction_sanity=str(payload.get("direction_sanity") or ""),
            liquidity_preference=str(payload.get("liquidity_preference") or ""),
            review_flags=_as_list(payload.get("review_flags")),
        )

    def suggested_trade_horizon_days(self) -> int | None:
        for key in ("days", "target_days", "trade_horizon_days"):
            parsed = _optional_int(self.trade_horizon_suggestion.get(key))
            if parsed is not None:
                return parsed
        low = _optional_int(self.trade_horizon_suggestion.get("min_days"))
        high = _optional_int(self.trade_horizon_suggestion.get("max_days"))
        if low is not None and high is not None:
            return int(round((low + high) / 2.0))
        return low if low is not None else high

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Idea:
    idea_id: str
    source: str
    underlying: str
    direction: str
    strategy_hint: str = ""
    mentioned_strategy: str = ""
    allowed_structures: list[str] = field(default_factory=list)
    thesis_tags: list[str] = field(default_factory=list)
    horizon_days: int = 45
    catalyst_horizon_days: int | None = None
    trade_horizon_days: int | None = None
    confidence: str = ""
    extraction_confidence: str = ""
    quote_evidence: str = ""
    extraction_notes: str = ""
    operator_status: str = "approved"
    notes: str = ""
    annotation: IdeaAnnotation = field(default_factory=IdeaAnnotation)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Idea":
        tags = payload.get("thesis_tags") or []
        if isinstance(tags, str):
            tags = [item.strip() for item in tags.split(",") if item.strip()]
        stable_payload = json.dumps(payload, sort_keys=True, default=str)
        generated_id = "idea_" + hashlib.sha256(stable_payload.encode("utf-8")).hexdigest()[:12]
        annotation = IdeaAnnotation.from_dict(payload.get("annotation") or payload.get("idea_annotation"))
        trade_horizon_days = _optional_int(payload.get("trade_horizon_days"))
        if trade_horizon_days is None:
            trade_horizon_days = annotation.suggested_trade_horizon_days()
        return cls(
            idea_id=str(payload.get("idea_id") or generated_id),
            source=str(payload.get("source") or "manual"),
            underlying=str(payload.get("underlying") or payload.get("ticker") or "").upper(),
            direction=str(payload.get("direction") or "neutral").lower(),
            strategy_hint=str(payload.get("strategy_hint") or "").strip(),
            mentioned_strategy=str(payload.get("mentioned_strategy") or "").strip(),
            allowed_structures=_as_list(payload.get("allowed_structures")),
            thesis_tags=list(tags),
            horizon_days=int(payload.get("horizon_days") or 45),
            catalyst_horizon_days=_optional_int(payload.get("catalyst_horizon_days")),
            trade_horizon_days=trade_horizon_days,
            confidence=str(payload.get("confidence") or ""),
            extraction_confidence=str(payload.get("extraction_confidence") or payload.get("confidence") or ""),
            quote_evidence=str(payload.get("quote_evidence") or ""),
            extraction_notes=str(payload.get("extraction_notes") or ""),
            operator_status=str(payload.get("operator_status") or payload.get("status") or "approved").lower(),
            notes=str(payload.get("notes") or ""),
            annotation=annotation,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class UniverseEntry:
    symbol: str
    enabled: bool
    profile: str
    tradable_iv_percentile_min: float = 0.0
    tradable_iv_percentile_max: float = 100.0
    max_bpr_pct: float = 25.0
    max_positions: int = 1
    earnings_sensitive: bool = True
    event_avoid_days_before: int = 7
    event_avoid_days_after: int = 1
    allowed_playbooks: list[str] = field(default_factory=list)
    tier: str = "primary"
    proposal_source: str = ""
    proposal_reason: str = ""
    proposal_date: str = ""
    notes: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "UniverseEntry":
        return cls(
            symbol=str(row.get("symbol") or "").upper(),
            enabled=_as_bool(row.get("enabled"), default=True),
            profile=str(row.get("profile") or row.get("stock_profile") or ""),
            tradable_iv_percentile_min=_as_float(row.get("tradable_iv_percentile_min"), 0.0),
            tradable_iv_percentile_max=_as_float(row.get("tradable_iv_percentile_max"), 100.0),
            max_bpr_pct=_as_float(row.get("max_bpr_pct"), 25.0),
            max_positions=int(_as_float(row.get("max_positions"), 1.0)),
            earnings_sensitive=_as_bool(row.get("earnings_sensitive"), default=True),
            event_avoid_days_before=int(_as_float(row.get("event_avoid_days_before"), 7.0)),
            event_avoid_days_after=int(_as_float(row.get("event_avoid_days_after"), 1.0)),
            allowed_playbooks=_as_list(row.get("allowed_playbooks")),
            tier=str(row.get("tier") or "primary").strip().lower() or "primary",
            proposal_source=str(row.get("proposal_source") or "").strip(),
            proposal_reason=str(row.get("proposal_reason") or "").strip(),
            proposal_date=str(row.get("proposal_date") or "").strip(),
            notes=str(row.get("notes") or ""),
        )


@dataclass(slots=True)
class Playbook:
    playbook_id: str
    enabled: bool
    strategy_family: str
    structure: str
    variant: str
    leg_count: int
    profiles: list[str]
    applicable_direction: list[str] = field(default_factory=list)
    applicable_thesis_tags: list[str] = field(default_factory=list)
    applicable_horizon_min: int | None = None
    applicable_horizon_max: int | None = None
    iv_percentile_min: float = 0.0
    iv_percentile_max: float = 100.0
    iv_rank_min: float | None = None
    iv_rank_max: float | None = None
    iv_abs_min: float | None = None
    iv_abs_max: float | None = None
    requires_iv_percentile: bool = False
    earnings_blackout_days: int | None = None
    dte_min: int = 30
    dte_max: int = 45
    long_dte_min: int | None = None
    long_dte_max: int | None = None
    short_delta_min: float | None = None
    short_delta_max: float | None = None
    long_delta_min: float | None = None
    long_delta_max: float | None = None
    spread_width: float | None = None
    legs_extra: str = ""
    min_credit_to_width_ratio: float | None = None
    max_debit_pct_bpr: float | None = None
    max_bid_ask_pct: float | None = None
    min_option_oi: int | None = None
    profit_target_pct: float = 50.0
    max_loss_multiple: float | None = 2.0
    exit_dte_min: int = 21
    half_time_exit: bool = True
    avoid_earnings: bool = True
    exit_pre_event_days: int | None = None
    sizing_method: str = ""
    sizing_value: float | None = None
    max_contracts: int | None = None
    live_max_bpr_per_order: float | None = None
    score_weight_credit: float | None = None
    score_weight_pop: float | None = None
    score_weight_liquidity: float | None = None
    score_weight_spread: float | None = None
    priority: float = 0.0
    rationale: str = ""
    notes: str = ""
    universe_expansion_enabled: bool = False
    underlying_price_min: float | None = None
    underlying_price_max: float | None = None
    deployment_stage: str = "baseline"

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Playbook":
        return cls(
            playbook_id=str(row.get("playbook_id") or ""),
            enabled=_as_bool(row.get("enabled"), default=False),
            strategy_family=str(row.get("strategy_family") or row.get("playbook_id") or ""),
            structure=str(row.get("structure") or ""),
            variant=str(row.get("variant") or "standard"),
            leg_count=int(_as_float(row.get("leg_count"), 0.0)),
            profiles=_as_list(row.get("profiles")),
            applicable_direction=[item.lower() for item in _as_list(row.get("applicable_direction"))],
            applicable_thesis_tags=[item.lower() for item in _as_list(row.get("applicable_thesis_tags"))],
            applicable_horizon_min=_optional_int(row.get("applicable_horizon_min")),
            applicable_horizon_max=_optional_int(row.get("applicable_horizon_max")),
            iv_percentile_min=_as_float(row.get("iv_percentile_min"), 0.0),
            iv_percentile_max=_as_float(row.get("iv_percentile_max"), 100.0),
            iv_rank_min=_optional_float(row.get("iv_rank_min")),
            iv_rank_max=_optional_float(row.get("iv_rank_max")),
            iv_abs_min=_optional_float(row.get("iv_abs_min")),
            iv_abs_max=_optional_float(row.get("iv_abs_max")),
            requires_iv_percentile=row.get("iv_percentile_min") not in (None, "") or row.get("iv_percentile_max") not in (None, ""),
            earnings_blackout_days=_optional_int(row.get("earnings_blackout_days")),
            dte_min=int(_as_float(row.get("dte_min"), 30.0)),
            dte_max=int(_as_float(row.get("dte_max"), 45.0)),
            long_dte_min=_optional_int(row.get("long_dte_min")),
            long_dte_max=_optional_int(row.get("long_dte_max")),
            short_delta_min=_optional_float(row.get("short_delta_min")),
            short_delta_max=_optional_float(row.get("short_delta_max")),
            long_delta_min=_optional_float(row.get("long_delta_min")),
            long_delta_max=_optional_float(row.get("long_delta_max")),
            spread_width=_optional_float(row.get("spread_width")),
            legs_extra=str(row.get("legs_extra") or ""),
            min_credit_to_width_ratio=_optional_float(row.get("min_credit_to_width_ratio")),
            max_debit_pct_bpr=_optional_float(row.get("max_debit_pct_bpr")),
            max_bid_ask_pct=_optional_float(row.get("max_bid_ask_pct")),
            min_option_oi=_optional_int(row.get("min_option_oi")),
            profit_target_pct=_as_float(row.get("profit_target_pct"), 50.0),
            max_loss_multiple=_optional_float(row.get("max_loss_multiple")),
            exit_dte_min=int(_as_float(row.get("exit_dte_min"), 21.0)),
            half_time_exit=_as_bool(row.get("half_time_exit"), default=True),
            avoid_earnings=_as_bool(row.get("avoid_earnings"), default=True),
            exit_pre_event_days=_optional_int(row.get("exit_pre_event_days")),
            sizing_method=str(row.get("sizing_method") or ""),
            sizing_value=_optional_float(row.get("sizing_value")),
            max_contracts=_optional_int(row.get("max_contracts")),
            live_max_bpr_per_order=_optional_float(row.get("live_max_bpr_per_order")),
            score_weight_credit=_optional_float(row.get("score_weight_credit")),
            score_weight_pop=_optional_float(row.get("score_weight_pop")),
            score_weight_liquidity=_optional_float(row.get("score_weight_liquidity")),
            score_weight_spread=_optional_float(row.get("score_weight_spread")),
            priority=_as_float(row.get("priority"), 0.0),
            rationale=str(row.get("rationale") or ""),
            notes=str(row.get("notes") or ""),
            universe_expansion_enabled=_as_bool(row.get("universe_expansion_enabled"), default=False),
            underlying_price_min=_optional_float(row.get("underlying_price_min")),
            underlying_price_max=_optional_float(row.get("underlying_price_max")),
            deployment_stage=str(row.get("csa_stage") or "baseline").strip().lower() or "baseline",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OptionQuote:
    underlying: str
    expiration: str
    option_type: str
    strike: float
    bid: float
    ask: float
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float
    open_interest: int = 0
    volume: int = 0

    @property
    def mid(self) -> float:
        return round((self.bid + self.ask) / 2.0, 4)

    @property
    def dte(self) -> int:
        try:
            expiry = date.fromisoformat(self.expiration)
        except ValueError:
            return 0
        return max((expiry - date.today()).days, 0)

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        if mid <= 0:
            return 1.0
        return (self.ask - self.bid) / mid

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OptionLeg:
    role: str
    side: str
    option_type: str
    strike: float
    expiration: str
    quantity: int
    mid: float
    bid: float
    ask: float
    delta: float
    gamma: float
    theta: float
    vega: float
    open_interest: int

    @classmethod
    def from_quote(cls, quote: OptionQuote, *, role: str, side: str, quantity: int = 1) -> "OptionLeg":
        return cls(
            role=role,
            side=side,
            option_type=quote.option_type,
            strike=quote.strike,
            expiration=quote.expiration,
            quantity=quantity,
            mid=quote.mid,
            bid=quote.bid,
            ask=quote.ask,
            delta=quote.delta,
            gamma=quote.gamma,
            theta=quote.theta,
            vega=quote.vega,
            open_interest=quote.open_interest,
        )

    @property
    def signed_mid(self) -> float:
        return self.mid if self.side == "sell" else -self.mid

    @property
    def signed_greeks(self) -> Greeks:
        sign = -1.0 if self.side == "sell" else 1.0
        return Greeks(
            delta=self.delta * sign * self.quantity,
            gamma=self.gamma * sign * self.quantity,
            theta=self.theta * sign * self.quantity,
            vega=self.vega * sign * self.quantity,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ChainSnapshot:
    chain_snapshot_id: str
    underlying: str
    captured_at: str
    underlying_price: float
    quotes: list[OptionQuote]
    source: str = "fixture"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["quotes"] = [quote.to_dict() for quote in self.quotes]
        return data


@dataclass(slots=True)
class PreflightResult:
    ok: bool
    bpr: float
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PortfolioState:
    account_size: float
    buying_power: float
    bpr_used: float
    positions_count: int
    greeks: Greeks = field(default_factory=Greeks)
    per_underlying_bpr: dict[str, float] = field(default_factory=dict)

    @property
    def bpr_used_pct(self) -> float:
        if self.account_size <= 0:
            return 0.0
        return (self.bpr_used / self.account_size) * 100.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["greeks"] = self.greeks.to_dict()
        data["bpr_used_pct"] = self.bpr_used_pct
        return data


@dataclass(slots=True)
class Candidate:
    candidate_id: str
    idea_id: str
    underlying: str
    playbook_id: str
    structure: str
    legs: list[OptionLeg]
    net_credit: float
    estimated_bpr: float
    greeks: Greeks
    liquidity_score: float
    score: float
    reasons: list[str] = field(default_factory=list)
    rejection_reason: str = ""
    preflight: PreflightResult | None = None

    @property
    def eligible(self) -> bool:
        return not self.rejection_reason and self.estimated_bpr > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "idea_id": self.idea_id,
            "underlying": self.underlying,
            "playbook_id": self.playbook_id,
            "structure": self.structure,
            "legs": [leg.to_dict() for leg in self.legs],
            "net_credit": self.net_credit,
            "estimated_bpr": self.estimated_bpr,
            "greeks": self.greeks.to_dict(),
            "liquidity_score": self.liquidity_score,
            "score": self.score,
            "reasons": list(self.reasons),
            "rejection_reason": self.rejection_reason,
            "preflight": self.preflight.to_dict() if self.preflight else None,
        }


@dataclass(slots=True)
class Plan:
    plan_id: str
    plan_rank: int
    status: str
    candidates: list[Candidate]
    score: float
    total_bpr: float
    bpr_utilization_pct: float
    buying_power_after: float
    portfolio_before: PortfolioState
    portfolio_after: PortfolioState
    reasons: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    operator_action: str = ""

    @property
    def trade_bundle(self) -> str:
        return "; ".join(
            f"{candidate.underlying} {candidate.playbook_id}"
            for candidate in self.candidates
        )

    @property
    def greeks_change(self) -> Greeks:
        return Greeks(
            delta=self.portfolio_after.greeks.delta - self.portfolio_before.greeks.delta,
            gamma=self.portfolio_after.greeks.gamma - self.portfolio_before.greeks.gamma,
            theta=self.portfolio_after.greeks.theta - self.portfolio_before.greeks.theta,
            vega=self.portfolio_after.greeks.vega - self.portfolio_before.greeks.vega,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_rank": self.plan_rank,
            "status": self.status,
            "score": self.score,
            "total_bpr": self.total_bpr,
            "bpr_utilization_pct": self.bpr_utilization_pct,
            "buying_power_after": self.buying_power_after,
            "trade_bundle": self.trade_bundle,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "portfolio_before": self.portfolio_before.to_dict(),
            "portfolio_after": self.portfolio_after.to_dict(),
            "greeks_change": self.greeks_change.to_dict(),
            "reasons": list(self.reasons),
            "blocked_by": list(self.blocked_by),
            "operator_action": self.operator_action,
        }


def _as_bool(value: Any, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_float(value: Any, default: float) -> float:
    parsed = _optional_float(value)
    return default if parsed is None else parsed


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    parsed = _optional_float(value)
    return None if parsed is None else int(parsed)


def _as_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]
