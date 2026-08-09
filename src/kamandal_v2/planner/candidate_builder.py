"""Build concrete option candidates from ideas and playbooks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from itertools import product

from kamandal_v2.domain.models import Candidate, Greeks, Idea, OptionLeg, OptionQuote, Playbook, PreflightResult, UniverseEntry
from kamandal_v2.liquidity import bad_quote_reason, candidate_liquidity_metrics
from kamandal_v2.market.interfaces import MarketDataProvider, PreflightClient
from kamandal_v2.planner.bpr import structure_bpr_cap
from kamandal_v2.planner.shape_validators import validate_structure


SUPPORTED_STRUCTURES = {
    "short_put",
    "long_call",
    "long_put",
    "put_spread",
    "call_spread",
    "iron_condor",
    "call_calendar",
    "put_calendar",
    "put_diagonal",
    "call_diagonal",
    "short_strangle",
    "jade_lizard",
}

SHORT_CATALYST_MAX_DAYS = 14

CALENDAR_DIAGONAL_STRUCTURES = {
    "call_calendar",
    "put_calendar",
    "put_diagonal",
    "call_diagonal",
}

PERMISSIVE_MATCH_GATE_PREFIXES = (
    "horizon_above_max:",
    "horizon_below_min:",
    "iv_percentile_missing",
    "iv_abs_above_max",
    "iv_abs_below_min",
    "iv_abs_missing",
    "iv_rank_above_max",
    "iv_rank_below_min",
    "iv_rank_missing",
    "playbook_iv_percentile_out_of_range:",
    "universe_iv_percentile_out_of_range:",
)


@dataclass(slots=True)
class DteFallbackSettings:
    enabled: bool = True
    max_near_gap_days: int = 7
    max_far_gap_days: int = 14

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "max_near_gap_days": self.max_near_gap_days,
            "max_far_gap_days": self.max_far_gap_days,
        }


@dataclass(slots=True)
class DeltaSelection:
    quotes: list[OptionQuote]
    dte_min: int
    dte_max: int
    fallback_role: str = ""
    fallback_dtes: list[int] | None = None


@dataclass(slots=True)
class VerticalWidthSearchSettings:
    enabled: bool = False
    widths: tuple[float, ...] = (5.0, 7.5, 10.0)
    respect_bpr_cap: bool = True


def build_candidates(
    ideas: list[Idea],
    universe: list[UniverseEntry],
    playbooks: list[Playbook],
    market: MarketDataProvider,
    preflight: PreflightClient,
    *,
    per_idea_cap: int = 5,
    match_gate_mode: str = "strict",
    candidate_filter_mode: str = "strict",
    config: dict | None = None,
) -> list[Candidate]:
    universe_by_symbol = {entry.symbol: entry for entry in universe if entry.enabled}
    all_candidates: list[Candidate] = []
    for idea in ideas:
        entry = universe_by_symbol.get(idea.underlying)
        if entry is None:
            continue
        chain = market.chain_snapshot(idea.underlying)
        iv_pct = market.iv_percentile(idea.underlying)
        iv_rank = market.iv_rank(idea.underlying)
        iv_abs = market.iv_abs(idea.underlying)
        event_status = market.event_status(idea.underlying)
        built_for_idea: list[Candidate] = []
        rejected_for_idea: list[Candidate] = []
        for playbook in playbooks:
            if not _matches(
                idea,
                entry,
                playbook,
                iv_pct,
                iv_rank,
                iv_abs,
                event_status,
                underlying_price=chain.underlying_price,
                match_gate_mode=match_gate_mode,
            ):
                continue
            raw_candidates = _build_for_playbook(idea, playbook, chain.underlying_price, chain.quotes, config=config)
            for candidate in raw_candidates:
                match_horizon, match_horizon_source = _match_horizon(idea, playbook)
                candidate.reasons.append(f"match_horizon_days={match_horizon}")
                candidate.reasons.append(f"match_horizon_source={match_horizon_source}")
                result = validate_structure(candidate.structure, candidate.legs, chain.underlying_price)
                if not result.valid:
                    candidate.rejection_reason = result.reason
                    rejected_for_idea.append(candidate)
                    continue
                if candidate.rejection_reason:
                    rejected_for_idea.append(candidate)
                    continue
                filter_rejections = _filter_rejections(candidate, playbook, config)
                hard_filter_rejections = []
                for filter_rejection in filter_rejections:
                    if _low_oi_price_through_enabled(filter_rejection, config):
                        candidate.reasons.append(f"filter_warning={filter_rejection}")
                        candidate.reasons.append("low_oi_price_through=true")
                        continue
                    if _wide_bid_ask_price_through_enabled(filter_rejection, config):
                        candidate.reasons.append(f"filter_warning={filter_rejection}")
                        candidate.reasons.append("wide_bid_ask_price_through=true")
                        continue
                    if candidate_filter_mode == "warn":
                        candidate.reasons.append(f"filter_warning={filter_rejection}")
                    else:
                        hard_filter_rejections.append(filter_rejection)
                if hard_filter_rejections:
                    candidate.rejection_reason = hard_filter_rejections[0]
                    rejected_for_idea.append(candidate)
                    continue
                pf = preflight.preflight(candidate)
                candidate.preflight = pf
                if not pf.ok:
                    candidate.rejection_reason = pf.message or "preflight_failed"
                    rejected_for_idea.append(candidate)
                    continue
                _apply_preflight_bpr(candidate, pf)
                if _strangle_expansion_allows(entry, playbook, iv_rank, chain.underlying_price):
                    candidate.reasons.append("strangle_eligibility=sheet_playbook_overlay")
                    candidate.reasons.append(f"strangle_underlying_price={chain.underlying_price}")
                thesis_fit = _thesis_fit_score(idea, candidate)
                candidate.score = _candidate_score(candidate, thesis_fit=thesis_fit)
                candidate.reasons.append(f"thesis_fit={thesis_fit}")
                candidate.reasons.append(f"iv_pct={iv_pct}")
                candidate.reasons.append(f"iv_rank={iv_rank}")
                candidate.reasons.append(f"iv_abs={iv_abs}")
                candidate.reasons.append(f"event_status={event_status}")
                if match_gate_mode == "permissive":
                    ignored = _ignored_match_rejections(
                        idea,
                        entry,
                        playbook,
                        iv_pct,
                        iv_rank,
                        iv_abs,
                        event_status,
                        underlying_price=chain.underlying_price,
                    )
                    if ignored:
                        candidate.reasons.append("match_gate_warning=" + ",".join(ignored))
                built_for_idea.append(candidate)
        all_candidates.extend(_select_diverse_candidates(built_for_idea, per_idea_cap))
        all_candidates.extend(rejected_for_idea)
    return all_candidates


def diagnose_idea_matches(
    ideas: list[Idea],
    universe: list[UniverseEntry],
    playbooks: list[Playbook],
    market: MarketDataProvider,
    *,
    match_gate_mode: str = "strict",
    config: dict | None = None,
) -> list[dict[str, object]]:
    universe_by_symbol = {entry.symbol: entry for entry in universe if entry.enabled}
    diagnostics: list[dict[str, object]] = []
    for idea in ideas:
        entry = universe_by_symbol.get(idea.underlying)
        if entry is None:
            diagnostics.append({
                "idea_id": idea.idea_id,
                "underlying": idea.underlying,
                "status": "out_of_universe",
                "matched_playbooks": [],
                "summary": "No enabled universe entry for underlying.",
                "reason_counts": {"underlying_not_enabled_or_missing": 1},
                "playbooks": [],
            })
            continue
        iv_pct = market.iv_percentile(idea.underlying)
        iv_rank = market.iv_rank(idea.underlying)
        iv_abs = market.iv_abs(idea.underlying)
        event_status = market.event_status(idea.underlying)
        playbook_rows: list[dict[str, object]] = []
        reason_counts: dict[str, int] = {}
        matched: list[str] = []
        matched_playbooks: list[Playbook] = []
        chain = None
        try:
            chain = market.chain_snapshot(idea.underlying)
        except Exception:
            pass
        for playbook in playbooks:
            raw_reasons = _match_rejections(
                idea,
                entry,
                playbook,
                iv_pct,
                iv_rank,
                iv_abs,
                event_status,
                underlying_price=chain.underlying_price if chain is not None else None,
            )
            reasons = _apply_match_gate_mode(raw_reasons, match_gate_mode)
            if reasons:
                for reason in reasons:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
            else:
                matched.append(playbook.playbook_id)
                matched_playbooks.append(playbook)
            playbook_rows.append({
                "playbook_id": playbook.playbook_id,
                "structure": playbook.structure,
                "enabled": playbook.enabled,
                "matched": not reasons,
                "reasons": reasons,
                "ignored_reasons": [reason for reason in raw_reasons if reason not in reasons],
            })
        zero_candidate_diagnostics: list[dict[str, object]] = []
        if matched_playbooks:
            try:
                chain = chain or market.chain_snapshot(idea.underlying)
                for playbook in matched_playbooks:
                    raw_candidates = _build_for_playbook(idea, playbook, chain.underlying_price, chain.quotes, config=config)
                    raw_count = len(raw_candidates)
                    build_diagnostic = _raw_candidate_build_diagnostic(playbook, chain.quotes, raw_count=raw_count, config=config)
                    for row in playbook_rows:
                        if row["playbook_id"] == playbook.playbook_id:
                            row["raw_candidate_count"] = raw_count
                            row["build_diagnostic"] = build_diagnostic
                            break
                    if raw_count == 0:
                        zero_candidate_diagnostics.append({
                            "playbook_id": playbook.playbook_id,
                            "structure": playbook.structure,
                            **build_diagnostic,
                        })
            except Exception as exc:  # noqa: BLE001
                zero_candidate_diagnostics.append({
                    "playbook_id": ",".join(matched),
                    "structure": "",
                    "reason": "chain_snapshot_failed",
                    "message": str(exc),
                })
        status = "matched_playbooks" if matched else "no_playbook_match"
        diagnostics.append({
            "idea_id": idea.idea_id,
            "underlying": idea.underlying,
            "direction": idea.direction,
            "thesis_tags": list(idea.thesis_tags),
            "horizon_days": idea.horizon_days,
            "catalyst_horizon_days": idea.catalyst_horizon_days,
            "trade_horizon_days": idea.trade_horizon_days,
            "mentioned_strategy": idea.mentioned_strategy,
            "status": status,
            "matched_playbooks": matched,
            "summary": _diagnostic_summary(matched, reason_counts, zero_candidate_diagnostics),
            "reason_counts": dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))),
            "zero_candidate_diagnostics": zero_candidate_diagnostics,
            "market_context": {
                "iv_percentile": iv_pct,
                "iv_rank": iv_rank,
                "iv_abs": iv_abs,
                "event_status": event_status,
            },
            "match_context": _match_context(idea),
            "playbooks": playbook_rows,
        })
    return diagnostics


def _matches(
    idea: Idea,
    entry: UniverseEntry,
    playbook: Playbook,
    iv_pct: float | None,
    iv_rank: float | None,
    iv_abs: float | None,
    event_status: str,
    *,
    underlying_price: float | None = None,
    match_gate_mode: str = "strict",
) -> bool:
    return not _apply_match_gate_mode(
        _match_rejections(
            idea,
            entry,
            playbook,
            iv_pct,
            iv_rank,
            iv_abs,
            event_status,
            underlying_price=underlying_price,
        ),
        match_gate_mode,
    )


def _match_rejections(
    idea: Idea,
    entry: UniverseEntry,
    playbook: Playbook,
    iv_pct: float | None,
    iv_rank: float | None,
    iv_abs: float | None,
    event_status: str,
    *,
    underlying_price: float | None = None,
) -> list[str]:
    reasons: list[str] = []
    if not playbook.enabled or playbook.structure not in SUPPORTED_STRUCTURES:
        if not playbook.enabled:
            reasons.append("playbook_disabled")
        if playbook.structure not in SUPPORTED_STRUCTURES:
            reasons.append(f"unsupported_structure:{playbook.structure}")
        return reasons
    if playbook.profiles and entry.profile not in playbook.profiles:
        reasons.append(f"profile_mismatch:{entry.profile}")
    if entry.allowed_playbooks and not {playbook.playbook_id, playbook.structure, playbook.strategy_family}.intersection(entry.allowed_playbooks):
        reasons.append("universe_playbook_not_allowed")
    if _strangle_expansion_allows(entry, playbook, iv_rank, underlying_price):
        reasons = [
            reason
            for reason in reasons
            if not reason.startswith("profile_mismatch:") and reason != "universe_playbook_not_allowed"
        ]
    if idea.allowed_structures:
        allowed = set().union(*(_strategy_aliases(value) for value in idea.allowed_structures))
        if not allowed.intersection({playbook.playbook_id, playbook.structure, playbook.strategy_family}):
            reasons.append("idea_structure_not_allowed")
    if idea.strategy_hint and not _strategy_aliases(idea.strategy_hint).intersection({playbook.playbook_id, playbook.structure, playbook.strategy_family}):
        reasons.append(f"strategy_hint_mismatch:{idea.strategy_hint}")
    if playbook.applicable_direction and idea.direction not in playbook.applicable_direction:
        reasons.append(f"direction_mismatch:{idea.direction}")
    idea_tags = {tag.lower() for tag in idea.thesis_tags}
    mentioned_match = _mentioned_strategy_matches(idea, playbook)
    if playbook.applicable_thesis_tags and not idea_tags.intersection(playbook.applicable_thesis_tags) and not mentioned_match:
        reasons.append("thesis_tags_mismatch")
    if playbook.strategy_family == "narrative_ignition" or playbook.playbook_id.startswith("narrative_ignition"):
        if "structural_break:pass" not in idea.notes:
            reasons.append("structural_break_gate_blocked")
    match_horizon, _source = _match_horizon(idea, playbook)
    if playbook.applicable_horizon_min is not None and match_horizon < playbook.applicable_horizon_min:
        reasons.append(f"horizon_below_min:{match_horizon}<{playbook.applicable_horizon_min}")
    if playbook.applicable_horizon_max is not None and match_horizon > playbook.applicable_horizon_max:
        reasons.append(f"horizon_above_max:{match_horizon}>{playbook.applicable_horizon_max}")
    if playbook.requires_iv_percentile and iv_pct is None:
        reasons.append("iv_percentile_missing")
    if iv_pct is not None:
        if iv_pct < entry.tradable_iv_percentile_min or iv_pct > entry.tradable_iv_percentile_max:
            reasons.append(f"universe_iv_percentile_out_of_range:{iv_pct}")
        if iv_pct < playbook.iv_percentile_min or iv_pct > playbook.iv_percentile_max:
            reasons.append(f"playbook_iv_percentile_out_of_range:{iv_pct}")
    if playbook.iv_rank_min is not None and (iv_rank is None or iv_rank < playbook.iv_rank_min):
        reasons.append("iv_rank_below_min" if iv_rank is not None else "iv_rank_missing")
    if playbook.iv_rank_max is not None and (iv_rank is None or iv_rank > playbook.iv_rank_max):
        reasons.append("iv_rank_above_max" if iv_rank is not None else "iv_rank_missing")
    if playbook.iv_abs_min is not None and (iv_abs is None or iv_abs < playbook.iv_abs_min):
        reasons.append("iv_abs_below_min" if iv_abs is not None else "iv_abs_missing")
    if playbook.iv_abs_max is not None and (iv_abs is None or iv_abs > playbook.iv_abs_max):
        reasons.append("iv_abs_above_max" if iv_abs is not None else "iv_abs_missing")
    if playbook.avoid_earnings and event_status not in {"clear", "unknown"}:
        reasons.append(f"event_status_blocked:{event_status}")
    return reasons


def _ignored_match_rejections(
    idea: Idea,
    entry: UniverseEntry,
    playbook: Playbook,
    iv_pct: float | None,
    iv_rank: float | None,
    iv_abs: float | None,
    event_status: str,
    *,
    underlying_price: float | None = None,
) -> list[str]:
    raw_reasons = _match_rejections(
        idea,
        entry,
        playbook,
        iv_pct,
        iv_rank,
        iv_abs,
        event_status,
        underlying_price=underlying_price,
    )
    strict_reasons = _apply_match_gate_mode(raw_reasons, "permissive")
    return [reason for reason in raw_reasons if reason not in strict_reasons]


def _apply_match_gate_mode(reasons: list[str], match_gate_mode: str) -> list[str]:
    if match_gate_mode != "permissive":
        return reasons
    return [
        reason for reason in reasons
        if not any(reason.startswith(prefix) for prefix in PERMISSIVE_MATCH_GATE_PREFIXES)
    ]


def _strangle_expansion_allows(
    entry: UniverseEntry,
    playbook: Playbook,
    iv_rank: float | None,
    underlying_price: float | None,
) -> bool:
    if not playbook.universe_expansion_enabled or not entry.enabled or playbook.structure != "short_strangle":
        return False
    if any(
        value is None
        for value in (
            iv_rank,
            underlying_price,
            playbook.iv_rank_min,
            playbook.iv_rank_max,
            playbook.underlying_price_min,
            playbook.underlying_price_max,
        )
    ):
        return False
    return (
        float(playbook.underlying_price_min) <= float(underlying_price) <= float(playbook.underlying_price_max)
        and float(playbook.iv_rank_min) <= float(iv_rank) <= float(playbook.iv_rank_max)
    )


def _apply_preflight_bpr(candidate: Candidate, preflight: PreflightResult) -> None:
    broker_bpr = abs(float(preflight.bpr or 0.0))
    fallback_bpr = abs(float(candidate.estimated_bpr or 0.0))
    raw = preflight.raw if isinstance(preflight.raw, dict) else {}
    broker_provided = bool(raw.get("broker_bpr_provided")) or _raw_contains_broker_bpr(raw.get("response"))
    if candidate.structure in {"short_strangle", "strangle"}:
        if broker_provided and broker_bpr > 0:
            candidate.estimated_bpr = round(broker_bpr, 2)
            candidate.reasons.append("bpr_source=broker_preflight")
            candidate.reasons.append(f"local_bpr_fallback={round(fallback_bpr, 2)}")
            return
        candidate.estimated_bpr = round(max(fallback_bpr, broker_bpr), 2)
        candidate.reasons.append("bpr_source=local_fallback")
        candidate.reasons.append("broker_bpr_missing=true")
        return
    candidate.estimated_bpr = round(max(fallback_bpr, broker_bpr), 2)
    if candidate.estimated_bpr > broker_bpr:
        candidate.reasons.append(f"preflight_bpr_floored:{broker_bpr}->{candidate.estimated_bpr}")


def _raw_contains_broker_bpr(value: object) -> bool:
    keys = {"buyingPowerRequirement", "buyingPowerEffect", "estimatedBuyingPower"}
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in keys:
                if key in item:
                    try:
                        return abs(float(item[key])) > 0
                    except (TypeError, ValueError):
                        pass
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _diagnostic_summary(
    matched: list[str],
    reason_counts: dict[str, int],
    zero_candidate_diagnostics: list[dict[str, object]] | None = None,
) -> str:
    if matched:
        zero_candidate_diagnostics = zero_candidate_diagnostics or []
        if zero_candidate_diagnostics:
            details = [
                f"{item.get('playbook_id')} ({item.get('reason')})"
                for item in zero_candidate_diagnostics[:3]
            ]
            return "Matched playbooks but built no raw candidates: " + ", ".join(details)
        return "Matched playbooks: " + ", ".join(matched[:5])
    if not reason_counts:
        return "No playbooks evaluated."
    top_reasons = sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    return "No playbook matched; top gates: " + ", ".join(f"{reason} ({count})" for reason, count in top_reasons)


def _match_context(idea: Idea) -> dict[str, object]:
    catalyst = _catalyst_horizon(idea)
    return {
        "catalyst_horizon_days": catalyst,
        "trade_horizon_days": idea.trade_horizon_days,
        "uses_playbook_horizon_for_short_catalyst": _uses_playbook_horizon(idea, catalyst),
    }


def _match_horizon(idea: Idea, playbook: Playbook) -> tuple[int, str]:
    if idea.trade_horizon_days is not None:
        return idea.trade_horizon_days, "trade_horizon_days"
    catalyst = _catalyst_horizon(idea)
    if _uses_playbook_horizon(idea, catalyst):
        low = playbook.applicable_horizon_min or playbook.dte_min
        high = playbook.applicable_horizon_max or playbook.dte_max
        return int(round((low + high) / 2.0)), "playbook_horizon_for_short_catalyst"
    return idea.horizon_days, "horizon_days"


def _catalyst_horizon(idea: Idea) -> int | None:
    if idea.catalyst_horizon_days is not None:
        return idea.catalyst_horizon_days
    if str(idea.source).startswith("llm_transcript:"):
        return idea.horizon_days
    return None


def _uses_playbook_horizon(idea: Idea, catalyst_horizon: int | None) -> bool:
    if catalyst_horizon is None:
        return False
    if catalyst_horizon > SHORT_CATALYST_MAX_DAYS:
        return False
    source = str(idea.source)
    if source.startswith("llm_transcript:"):
        return True
    return idea.catalyst_horizon_days is not None


def _strategy_aliases(value: str) -> set[str]:
    normalized = value.strip().lower()
    aliases = {normalized}
    if normalized == "strangle":
        aliases.add("short_strangle")
    if normalized == "calendar":
        aliases.update({"call_calendar", "put_calendar"})
    return aliases


def _mentioned_strategy_matches(idea: Idea, playbook: Playbook) -> bool:
    if not idea.mentioned_strategy:
        return False
    return bool(
        _strategy_aliases(idea.mentioned_strategy).intersection(
            {playbook.playbook_id, playbook.structure, playbook.strategy_family}
        )
    )


def _build_for_playbook(
    idea: Idea,
    playbook: Playbook,
    underlying_price: float,
    quotes: list[OptionQuote],
    *,
    config: dict | None = None,
) -> list[Candidate]:
    if playbook.structure == "short_put":
        return _short_put_candidates(idea, playbook, quotes)
    if playbook.structure == "long_call":
        return _long_option_candidates(idea, playbook, quotes, option_type="call")
    if playbook.structure == "long_put":
        return _long_option_candidates(idea, playbook, quotes, option_type="put")
    if playbook.structure == "put_spread":
        return _put_spread_candidates(idea, playbook, quotes, config=config)
    if playbook.structure == "call_spread":
        return _call_spread_candidates(idea, playbook, quotes, config=config)
    if playbook.structure == "iron_condor":
        return _iron_condor_candidates(idea, playbook, quotes)
    if playbook.structure == "call_calendar":
        return _call_calendar_candidates(idea, playbook, quotes, config=config)
    if playbook.structure == "put_calendar":
        return _put_calendar_candidates(idea, playbook, quotes, config=config)
    if playbook.structure == "put_diagonal":
        return _put_diagonal_candidates(idea, playbook, quotes, config=config)
    if playbook.structure == "call_diagonal":
        return _call_diagonal_candidates(idea, playbook, quotes, config=config)
    if playbook.structure == "short_strangle":
        return _short_strangle_candidates(idea, playbook, quotes)
    if playbook.structure == "jade_lizard":
        return _jade_lizard_candidates(idea, playbook, quotes)
    return []


def _short_put_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote]) -> list[Candidate]:
    candidates = []
    for quote in _near_delta(quotes, "put", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)[:4]:
        legs = [OptionLeg.from_quote(quote, role="short_put", side="sell")]
        candidates.append(_candidate(idea, playbook, legs))
    return candidates


def _long_option_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote], *, option_type: str) -> list[Candidate]:
    candidates = []
    delta_min = playbook.long_delta_min if playbook.long_delta_min is not None else playbook.short_delta_min
    delta_max = playbook.long_delta_max if playbook.long_delta_max is not None else playbook.short_delta_max
    role = f"long_{option_type}"
    for quote in _near_delta(quotes, option_type, delta_min, delta_max, playbook.dte_min, playbook.dte_max)[:4]:
        candidates.append(_candidate(idea, playbook, [OptionLeg.from_quote(quote, role=role, side="buy")]))
    return candidates


def _put_spread_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote], *, config: dict | None = None) -> list[Candidate]:
    settings = _vertical_width_search_settings(config)
    shorts = _near_delta(quotes, "put", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    if not settings.enabled:
        candidates = []
        for short in shorts[:4]:
            long_choices = [
                quote for quote in quotes
                if quote.option_type == "put"
                and quote.expiration == short.expiration
                and quote.strike < short.strike
            ]
            if not long_choices:
                continue
            long = sorted(long_choices, key=lambda quote: abs((short.strike - quote.strike) - (playbook.spread_width or 5.0)))[0]
            candidates.append(_candidate(idea, playbook, [
                OptionLeg.from_quote(long, role="long_put", side="buy"),
                OptionLeg.from_quote(short, role="short_put", side="sell"),
            ]))
        return candidates
    candidates = []
    for short in shorts[:4]:
        candidate = _vertical_spread_for_short(idea, playbook, quotes, short, option_type="put", settings=settings, config=config)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _call_spread_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote], *, config: dict | None = None) -> list[Candidate]:
    settings = _vertical_width_search_settings(config)
    shorts = _near_delta(quotes, "call", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    if not settings.enabled:
        candidates = []
        for short in shorts[:4]:
            long_choices = [
                quote for quote in quotes
                if quote.option_type == "call"
                and quote.expiration == short.expiration
                and quote.strike > short.strike
            ]
            if not long_choices:
                continue
            long = sorted(long_choices, key=lambda quote: abs((quote.strike - short.strike) - (playbook.spread_width or 5.0)))[0]
            candidates.append(_candidate(idea, playbook, [
                OptionLeg.from_quote(short, role="short_call", side="sell"),
                OptionLeg.from_quote(long, role="long_call", side="buy"),
            ]))
        return candidates
    candidates = []
    for short in shorts[:4]:
        candidate = _vertical_spread_for_short(idea, playbook, quotes, short, option_type="call", settings=settings, config=config)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _vertical_long_for_width(
    quotes: list[OptionQuote],
    short: OptionQuote,
    width_target: float,
    option_type: str,
) -> OptionQuote | None:
    """Pick the long strike nearest `width_target` points from `short`."""
    if option_type == "put":
        long_choices = [
            quote for quote in quotes
            if quote.option_type == "put"
            and quote.expiration == short.expiration
            and quote.strike < short.strike
        ]
        if not long_choices:
            return None
        return sorted(long_choices, key=lambda quote: abs((short.strike - quote.strike) - width_target))[0]
    long_choices = [
        quote for quote in quotes
        if quote.option_type == "call"
        and quote.expiration == short.expiration
        and quote.strike > short.strike
    ]
    if not long_choices:
        return None
    return sorted(long_choices, key=lambda quote: abs((quote.strike - short.strike) - width_target))[0]


def _vertical_legs(short: OptionQuote, long: OptionQuote, option_type: str) -> list[OptionLeg]:
    if option_type == "put":
        return [
            OptionLeg.from_quote(long, role="long_put", side="buy"),
            OptionLeg.from_quote(short, role="short_put", side="sell"),
        ]
    return [
        OptionLeg.from_quote(short, role="short_call", side="sell"),
        OptionLeg.from_quote(long, role="long_call", side="buy"),
    ]


def _width_targets(playbook: Playbook, structure: str, config: dict | None) -> list[float]:
    settings = _vertical_width_search_settings(config)
    widths = {playbook.spread_width or 5.0}
    widths.update(settings.widths)
    return sorted(widths)


def _vertical_gate_passes(candidate: Candidate, playbook: Playbook) -> bool:
    """Mirror the credit-width-ratio branch of `_filter_rejections` exactly."""
    if playbook.min_credit_to_width_ratio is None or candidate.net_credit <= 0:
        return True
    width = _risk_width(candidate)
    if width <= 0:
        return True
    return (candidate.net_credit / width) >= playbook.min_credit_to_width_ratio


def _vertical_spread_for_short(
    idea: Idea,
    playbook: Playbook,
    quotes: list[OptionQuote],
    short: OptionQuote,
    *,
    option_type: str,
    settings: VerticalWidthSearchSettings,
    config: dict | None,
) -> Candidate | None:
    cap = structure_bpr_cap(playbook.structure, (config or {}).get("live") or {}) if settings.respect_bpr_cap else None
    constructions: list[tuple[float, Candidate, float]] = []
    for width_target in _width_targets(playbook, playbook.structure, config):
        long = _vertical_long_for_width(quotes, short, width_target, option_type)
        if long is None:
            continue
        candidate = _candidate(idea, playbook, _vertical_legs(short, long, option_type))
        width = _risk_width(candidate)
        ratio = (candidate.net_credit / width) if width > 0 else 0.0
        constructions.append((width_target, candidate, ratio))
    if not constructions:
        return None
    widths_tried = sorted({width_target for width_target, _candidate_ref, _ratio in constructions})
    bpr_ok = [
        item for item in constructions
        if cap is None or item[1].estimated_bpr <= cap
    ]
    pool = bpr_ok
    if not pool:
        chosen = min(constructions, key=lambda item: item[1].estimated_bpr)
        candidate = chosen[1]
        candidate.reasons.append("widths_tried=[" + ",".join(str(width) for width in widths_tried) + "]")
        candidate.rejection_reason = (
            f"vertical_bpr_above_cap:{candidate.estimated_bpr}>{cap}"
        )
        return candidate
    gate_passing = [item for item in pool if _vertical_gate_passes(item[1], playbook)]
    if gate_passing:
        chosen = min(gate_passing, key=lambda item: (_risk_width(item[1]), -item[2]))
    else:
        chosen = max(pool, key=lambda item: item[2])
    candidate = chosen[1]
    candidate.reasons.append("widths_tried=[" + ",".join(str(width) for width in widths_tried) + "]")
    return candidate


def _vertical_width_search_settings(config: dict | None) -> VerticalWidthSearchSettings:
    section = ((((config or {}).get("planner") or {}).get("vertical_width_search")) or {})
    if not isinstance(section, dict):
        section = {}
    enabled = _config_bool(section.get("enabled"), default=False)
    raw_widths = section.get("widths")
    widths: list[float] = []
    if isinstance(raw_widths, (list, tuple)):
        for value in raw_widths:
            try:
                widths.append(float(value))
            except (TypeError, ValueError):
                continue
    if not widths:
        widths = [5.0, 7.5, 10.0]
    respect_bpr_cap = _config_bool(section.get("respect_bpr_cap"), default=True)
    return VerticalWidthSearchSettings(enabled=enabled, widths=tuple(sorted(set(widths))), respect_bpr_cap=respect_bpr_cap)


def _iron_condor_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote]) -> list[Candidate]:
    candidates = []
    short_puts = _near_delta(quotes, "put", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    short_calls = _near_delta(quotes, "call", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    for short_put, short_call in product(short_puts[:3], short_calls[:3]):
        if short_put.expiration != short_call.expiration:
            continue
        long_puts = [q for q in quotes if q.option_type == "put" and q.expiration == short_put.expiration and q.strike < short_put.strike]
        long_calls = [q for q in quotes if q.option_type == "call" and q.expiration == short_call.expiration and q.strike > short_call.strike]
        if not long_puts or not long_calls:
            continue
        long_put = sorted(long_puts, key=lambda q: abs((short_put.strike - q.strike) - (playbook.spread_width or 5.0)))[0]
        long_call = sorted(long_calls, key=lambda q: abs((q.strike - short_call.strike) - (playbook.spread_width or 5.0)))[0]
        candidates.append(_candidate(idea, playbook, [
            OptionLeg.from_quote(long_put, role="long_put", side="buy"),
            OptionLeg.from_quote(short_put, role="short_put", side="sell"),
            OptionLeg.from_quote(short_call, role="short_call", side="sell"),
            OptionLeg.from_quote(long_call, role="long_call", side="buy"),
        ]))
    return candidates


def _short_strangle_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote]) -> list[Candidate]:
    short_puts = _near_delta(quotes, "put", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    short_calls = _near_delta(quotes, "call", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    target_delta = ((playbook.short_delta_min or 0.10) + (playbook.short_delta_max or 0.60)) / 2.0
    target_dte = (playbook.dte_min + playbook.dte_max) / 2.0
    pairs = [
        (short_put, short_call)
        for short_put, short_call in product(short_puts[:4], short_calls[:4])
        if short_put.expiration == short_call.expiration and short_put.strike < short_call.strike
    ]
    pairs.sort(
        key=lambda pair: (
            abs(abs(pair[0].delta) - abs(pair[1].delta)),
            abs(((abs(pair[0].delta) + abs(pair[1].delta)) / 2.0) - target_delta),
            abs(((pair[0].dte + pair[1].dte) / 2.0) - target_dte),
            pair[0].spread_pct + pair[1].spread_pct,
            pair[0].strike,
            pair[1].strike,
        )
    )
    return [
        _candidate(idea, playbook, [
            OptionLeg.from_quote(short_put, role="short_put", side="sell"),
            OptionLeg.from_quote(short_call, role="short_call", side="sell"),
        ])
        for short_put, short_call in pairs[:6]
    ]


def _jade_lizard_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote]) -> list[Candidate]:
    candidates = []
    short_puts = _near_delta(quotes, "put", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    short_calls = _near_delta(quotes, "call", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    width = playbook.spread_width or 5.0
    for short_put, short_call in product(short_puts[:3], short_calls[:3]):
        if short_put.expiration != short_call.expiration:
            continue
        long_calls = [
            quote for quote in quotes
            if quote.option_type == "call"
            and quote.expiration == short_call.expiration
            and quote.strike > short_call.strike
        ]
        if not long_calls:
            continue
        long_call = sorted(long_calls, key=lambda quote: abs((quote.strike - short_call.strike) - width))[0]
        candidates.append(_candidate(idea, playbook, [
            OptionLeg.from_quote(short_put, role="short_put", side="sell"),
            OptionLeg.from_quote(short_call, role="short_call", side="sell"),
            OptionLeg.from_quote(long_call, role="long_call", side="buy"),
        ]))
    return candidates[:6]


def _call_calendar_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote], *, config: dict | None = None) -> list[Candidate]:
    candidates = []
    fallback = _dte_fallback_settings(config)
    far_min, far_max = _far_dte_window(playbook, default_min_offset=20, default_max_offset=40)
    near_selection = _near_delta_selection(
        quotes, "call", playbook.long_delta_min, playbook.long_delta_max, playbook.dte_min, playbook.dte_max,
        fallback_settings=fallback, fallback_role="near",
    )
    far_selection = _near_delta_selection(
        quotes, "call", playbook.long_delta_min, playbook.long_delta_max, far_min, far_max,
        fallback_settings=fallback, fallback_role="far",
    )
    for near in near_selection.quotes:
        far_choices = [
            quote for quote in far_selection.quotes
            if quote.strike == near.strike and quote.expiration > near.expiration
        ]
        if not far_choices:
            continue
        far = sorted(far_choices, key=lambda quote: quote.dte)[0]
        candidate = _candidate(idea, playbook, [
            OptionLeg.from_quote(near, role="short_near", side="sell"),
            OptionLeg.from_quote(far, role="long_far", side="buy"),
        ])
        _append_dte_fallback_warnings(candidate, [("near", near, near_selection), ("far", far, far_selection)])
        candidates.append(candidate)
    return candidates[:4]


def _put_calendar_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote], *, config: dict | None = None) -> list[Candidate]:
    candidates = []
    fallback = _dte_fallback_settings(config)
    far_min, far_max = _far_dte_window(playbook, default_min_offset=20, default_max_offset=40)
    near_selection = _near_delta_selection(
        quotes, "put", playbook.long_delta_min, playbook.long_delta_max, playbook.dte_min, playbook.dte_max,
        fallback_settings=fallback, fallback_role="near",
    )
    far_selection = _near_delta_selection(
        quotes, "put", playbook.long_delta_min, playbook.long_delta_max, far_min, far_max,
        fallback_settings=fallback, fallback_role="far",
    )
    for near in near_selection.quotes:
        far_choices = [
            quote for quote in far_selection.quotes
            if quote.strike == near.strike and quote.expiration > near.expiration
        ]
        if not far_choices:
            continue
        far = sorted(far_choices, key=lambda quote: quote.dte)[0]
        candidate = _candidate(idea, playbook, [
            OptionLeg.from_quote(near, role="short_near", side="sell"),
            OptionLeg.from_quote(far, role="long_far", side="buy"),
        ])
        _append_dte_fallback_warnings(candidate, [("near", near, near_selection), ("far", far, far_selection)])
        candidates.append(candidate)
    return candidates[:4]


def _put_diagonal_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote], *, config: dict | None = None) -> list[Candidate]:
    candidates = []
    fallback = _dte_fallback_settings(config)
    far_min, far_max = _far_dte_window(playbook, default_min_offset=20, default_max_offset=45)
    short_selection = _near_delta_selection(
        quotes, "put", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max,
        fallback_settings=fallback, fallback_role="near",
    )
    long_selection = _near_delta_selection(
        quotes, "put", playbook.long_delta_min, playbook.long_delta_max, far_min, far_max,
        fallback_settings=fallback, fallback_role="far",
    )
    width = playbook.spread_width or 5.0
    for short in short_selection.quotes[:4]:
        long_choices = [
            quote for quote in long_selection.quotes
            if quote.expiration > short.expiration and quote.strike >= short.strike
        ]
        if not long_choices:
            continue
        long = sorted(long_choices, key=lambda quote: (abs((quote.strike - short.strike) - width), quote.dte))[0]
        candidate = _candidate(idea, playbook, [
            OptionLeg.from_quote(short, role="short_near", side="sell"),
            OptionLeg.from_quote(long, role="long_far", side="buy"),
        ])
        _append_dte_fallback_warnings(candidate, [("near", short, short_selection), ("far", long, long_selection)])
        candidates.append(candidate)
    return candidates[:4]


def _call_diagonal_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote], *, config: dict | None = None) -> list[Candidate]:
    candidates = []
    fallback = _dte_fallback_settings(config)
    far_min, far_max = _far_dte_window(playbook, default_min_offset=20, default_max_offset=45)
    short_selection = _near_delta_selection(
        quotes, "call", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max,
        fallback_settings=fallback, fallback_role="near",
    )
    long_selection = _near_delta_selection(
        quotes, "call", playbook.long_delta_min, playbook.long_delta_max, far_min, far_max,
        fallback_settings=fallback, fallback_role="far",
    )
    width = playbook.spread_width or 5.0
    for short in short_selection.quotes[:4]:
        long_choices = [
            quote for quote in long_selection.quotes
            if quote.expiration > short.expiration and quote.strike <= short.strike
        ]
        if not long_choices:
            continue
        long = sorted(long_choices, key=lambda quote: (abs((short.strike - quote.strike) - width), quote.dte))[0]
        candidate = _candidate(idea, playbook, [
            OptionLeg.from_quote(short, role="short_near", side="sell"),
            OptionLeg.from_quote(long, role="long_far", side="buy"),
        ])
        _append_dte_fallback_warnings(candidate, [("near", short, short_selection), ("far", long, long_selection)])
        candidates.append(candidate)
    return candidates[:4]


def _near_delta(
    quotes: list[OptionQuote],
    option_type: str,
    delta_min: float | None,
    delta_max: float | None,
    dte_min: int,
    dte_max: int,
) -> list[OptionQuote]:
    return _near_delta_selection(quotes, option_type, delta_min, delta_max, dte_min, dte_max).quotes


def _near_delta_selection(
    quotes: list[OptionQuote],
    option_type: str,
    delta_min: float | None,
    delta_max: float | None,
    dte_min: int,
    dte_max: int,
    *,
    fallback_settings: DteFallbackSettings | None = None,
    fallback_role: str = "",
) -> DeltaSelection:
    low = delta_min if delta_min is not None else 0.10
    high = delta_max if delta_max is not None else 0.60
    target = (low + high) / 2.0
    filtered = [
        quote for quote in quotes
        if quote.option_type == option_type
        and dte_min <= quote.dte <= dte_max
        and low <= abs(quote.delta) <= high
    ]
    if filtered:
        return DeltaSelection(
            quotes=_rank_delta_quotes(filtered, target_delta=target, target_dte=(dte_min + dte_max) / 2.0),
            dte_min=dte_min,
            dte_max=dte_max,
        )
    fallback_settings = fallback_settings or DteFallbackSettings(enabled=False)
    if not fallback_settings.enabled:
        return DeltaSelection(quotes=[], dte_min=dte_min, dte_max=dte_max)
    if _has_expiration_in_window(quotes, option_type, dte_min, dte_max):
        return DeltaSelection(quotes=[], dte_min=dte_min, dte_max=dte_max)

    max_gap = fallback_settings.max_far_gap_days if fallback_role == "far" else fallback_settings.max_near_gap_days
    fallback_pool = [
        quote for quote in quotes
        if quote.option_type == option_type
        and low <= abs(quote.delta) <= high
        and 0 < _dte_window_gap(quote.dte, dte_min, dte_max) <= max_gap
    ]
    if not fallback_pool:
        return DeltaSelection(quotes=[], dte_min=dte_min, dte_max=dte_max)
    nearest_gap = min(_dte_window_gap(quote.dte, dte_min, dte_max) for quote in fallback_pool)
    selected_dtes = sorted({quote.dte for quote in fallback_pool if _dte_window_gap(quote.dte, dte_min, dte_max) == nearest_gap})
    selected = [quote for quote in fallback_pool if quote.dte in selected_dtes]
    target_dte = min(selected_dtes, key=lambda dte: abs(dte - ((dte_min + dte_max) / 2.0)))
    return DeltaSelection(
        quotes=_rank_delta_quotes(selected, target_delta=target, target_dte=target_dte),
        dte_min=dte_min,
        dte_max=dte_max,
        fallback_role=fallback_role,
        fallback_dtes=selected_dtes,
    )


def _rank_delta_quotes(quotes: list[OptionQuote], *, target_delta: float, target_dte: float) -> list[OptionQuote]:
    return sorted(
        quotes,
        key=lambda quote: -_leg_rank_score(
            quote,
            target_delta=target_delta,
            target_dte=target_dte,
        ),
    )


def _has_expiration_in_window(quotes: list[OptionQuote], option_type: str, dte_min: int, dte_max: int) -> bool:
    return any(quote.option_type == option_type and dte_min <= quote.dte <= dte_max for quote in quotes)


def _dte_window_gap(dte: int, dte_min: int, dte_max: int) -> int:
    if dte < dte_min:
        return dte_min - dte
    if dte > dte_max:
        return dte - dte_max
    return 0


def _append_dte_fallback_warnings(candidate: Candidate, selections: list[tuple[str, OptionQuote, DeltaSelection]]) -> None:
    seen: set[str] = set()
    for role, quote, selection in selections:
        if not selection.fallback_dtes or quote.dte not in selection.fallback_dtes:
            continue
        reason = f"dte_fallback_warning={role}:{quote.expiration}:dte={quote.dte}:window={selection.dte_min}-{selection.dte_max}"
        if reason not in seen:
            candidate.reasons.append(reason)
            seen.add(reason)


def _far_dte_window(playbook: Playbook, *, default_min_offset: int, default_max_offset: int) -> tuple[int, int]:
    return (
        playbook.long_dte_min or playbook.dte_min + default_min_offset,
        playbook.long_dte_max or playbook.dte_max + default_max_offset,
    )


def _dte_fallback_settings(config: dict | None) -> DteFallbackSettings:
    expiry_cfg = ((((config or {}).get("planner") or {}).get("expiry") or {}))
    fallback_cfg = (
        expiry_cfg.get("diagonal_calendar_dte_fallback")
        or expiry_cfg.get("dte_fallback")
        or ((config or {}).get("planner") or {}).get("dte_fallback")
        or {}
    )
    if not isinstance(fallback_cfg, dict):
        fallback_cfg = {}
    enabled = _config_bool(fallback_cfg.get("enabled"), default=True)
    return DteFallbackSettings(
        enabled=enabled,
        max_near_gap_days=_config_int(fallback_cfg.get("max_near_gap_days"), default=7),
        max_far_gap_days=_config_int(fallback_cfg.get("max_far_gap_days"), default=14),
    )


def _raw_candidate_build_diagnostic(
    playbook: Playbook,
    quotes: list[OptionQuote],
    *,
    raw_count: int,
    config: dict | None = None,
) -> dict[str, object]:
    near_delta = _near_delta_window(playbook)
    far_delta = _far_delta_window(playbook)
    near_window = {"min": playbook.dte_min, "max": playbook.dte_max}
    far_min, far_max = _diagnostic_far_window(playbook)
    far_window = {"min": far_min, "max": far_max} if far_min is not None and far_max is not None else None
    reason = "" if raw_count else _zero_raw_candidate_reason(playbook, quotes, config=config)
    return {
        "raw_candidate_count": raw_count,
        "reason": reason,
        "available_expirations": _available_expiration_dtes(quotes),
        "near_dte_window": near_window,
        "far_dte_window": far_window,
        "delta_windows": {
            "near": near_delta,
            "far": far_delta,
        },
        "dte_fallback": _dte_fallback_settings(config).to_dict(),
    }


def _zero_raw_candidate_reason(playbook: Playbook, quotes: list[OptionQuote], *, config: dict | None = None) -> str:
    if not quotes:
        return "no_option_quotes"
    if playbook.structure == "call_calendar":
        return _calendar_zero_reason(playbook, quotes, option_type="call", config=config)
    if playbook.structure == "put_calendar":
        return _calendar_zero_reason(playbook, quotes, option_type="put", config=config)
    if playbook.structure == "put_diagonal":
        return _diagonal_zero_reason(playbook, quotes, option_type="put", config=config)
    if playbook.structure == "call_diagonal":
        return _diagonal_zero_reason(playbook, quotes, option_type="call", config=config)
    if playbook.structure == "short_strangle":
        put_reason = _leg_zero_reason(
            quotes, "put", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max,
            label="near",
        )
        call_reason = _leg_zero_reason(
            quotes, "call", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max,
            label="near",
        )
        if put_reason:
            return "no_put_" + put_reason.removeprefix("no_near_")
        if call_reason:
            return "no_call_" + call_reason.removeprefix("no_near_")
        return "no_shared_expiration_match"
    if playbook.structure in {"short_put", "put_spread", "iron_condor", "jade_lizard"}:
        return _leg_zero_reason(quotes, "put", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max, label="near") or "no_put_leg_match"
    if playbook.structure in {"long_put"}:
        return _leg_zero_reason(quotes, "put", playbook.long_delta_min, playbook.long_delta_max, playbook.dte_min, playbook.dte_max, label="near") or "no_put_leg_match"
    if playbook.structure in {"long_call"}:
        return _leg_zero_reason(quotes, "call", playbook.long_delta_min, playbook.long_delta_max, playbook.dte_min, playbook.dte_max, label="near") or "no_call_leg_match"
    if playbook.structure in {"call_spread"}:
        return _leg_zero_reason(quotes, "call", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max, label="near") or "no_call_leg_match"
    return "no_leg_combination_match"


def _calendar_zero_reason(playbook: Playbook, quotes: list[OptionQuote], *, option_type: str, config: dict | None) -> str:
    near_reason = _leg_zero_reason(
        quotes, option_type, playbook.long_delta_min, playbook.long_delta_max, playbook.dte_min, playbook.dte_max,
        label="near", config=config,
    )
    if near_reason:
        return near_reason
    far_min, far_max = _far_dte_window(playbook, default_min_offset=20, default_max_offset=40)
    far_reason = _leg_zero_reason(
        quotes, option_type, playbook.long_delta_min, playbook.long_delta_max, far_min, far_max,
        label="far", config=config,
    )
    if far_reason:
        return far_reason
    return "no_same_strike_calendar_pair"


def _diagonal_zero_reason(playbook: Playbook, quotes: list[OptionQuote], *, option_type: str, config: dict | None) -> str:
    near_reason = _leg_zero_reason(
        quotes, option_type, playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max,
        label="near", config=config,
    )
    if near_reason:
        return near_reason
    far_min, far_max = _far_dte_window(playbook, default_min_offset=20, default_max_offset=45)
    far_reason = _leg_zero_reason(
        quotes, option_type, playbook.long_delta_min, playbook.long_delta_max, far_min, far_max,
        label="far", config=config,
    )
    if far_reason:
        return far_reason
    return "no_diagonal_strike_pair"


def _leg_zero_reason(
    quotes: list[OptionQuote],
    option_type: str,
    delta_min: float | None,
    delta_max: float | None,
    dte_min: int,
    dte_max: int,
    *,
    label: str,
    config: dict | None = None,
) -> str:
    if not any(quote.option_type == option_type for quote in quotes):
        return f"no_{label}_{option_type}_quotes"
    if not _has_expiration_in_window(quotes, option_type, dte_min, dte_max):
        selection = _near_delta_selection(
            quotes, option_type, delta_min, delta_max, dte_min, dte_max,
            fallback_settings=_dte_fallback_settings(config),
            fallback_role=label,
        )
        if selection.quotes:
            return ""
        return f"no_{label}_expiration_in_window"
    low = delta_min if delta_min is not None else 0.10
    high = delta_max if delta_max is not None else 0.60
    if not any(
        quote.option_type == option_type
        and dte_min <= quote.dte <= dte_max
        and low <= abs(quote.delta) <= high
        for quote in quotes
    ):
        return f"no_{label}_delta_match"
    return ""


def _available_expiration_dtes(quotes: list[OptionQuote]) -> list[dict[str, object]]:
    seen: dict[str, int] = {}
    for quote in quotes:
        seen[quote.expiration] = quote.dte
    return [
        {"expiration": expiration, "dte": dte}
        for expiration, dte in sorted(seen.items(), key=lambda item: (item[1], item[0]))
    ]


def _near_delta_window(playbook: Playbook) -> dict[str, float | None]:
    if playbook.structure in {"call_calendar", "put_calendar", "long_call", "long_put"}:
        return {"min": playbook.long_delta_min, "max": playbook.long_delta_max}
    return {"min": playbook.short_delta_min, "max": playbook.short_delta_max}


def _far_delta_window(playbook: Playbook) -> dict[str, float | None] | None:
    if playbook.structure in CALENDAR_DIAGONAL_STRUCTURES:
        if playbook.structure in {"call_calendar", "put_calendar"}:
            return {"min": playbook.long_delta_min, "max": playbook.long_delta_max}
        return {"min": playbook.long_delta_min, "max": playbook.long_delta_max}
    return None


def _diagnostic_far_window(playbook: Playbook) -> tuple[int | None, int | None]:
    if playbook.structure in {"call_calendar", "put_calendar"}:
        return _far_dte_window(playbook, default_min_offset=20, default_max_offset=40)
    if playbook.structure in {"put_diagonal", "call_diagonal"}:
        return _far_dte_window(playbook, default_min_offset=20, default_max_offset=45)
    return None, None


def _config_bool(value: object, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _config_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _candidate(idea: Idea, playbook: Playbook, legs: list[OptionLeg]) -> Candidate:
    net_credit = round(sum(leg.signed_mid for leg in legs), 4)
    greeks = Greeks()
    for leg in legs:
        greeks = greeks + leg.signed_greeks
    liquidity_score = max(0.0, min(1.0, 1.0 - (sum(((leg.ask - leg.bid) / max(leg.mid, 0.01)) for leg in legs) / len(legs))))
    bpr = _estimate_bpr(playbook.structure, legs, net_credit)
    candidate_id = _stable_id(idea.idea_id, playbook.playbook_id, [(leg.role, leg.expiration, leg.strike, leg.side) for leg in legs])
    min_oi = min((int(leg.open_interest or 0) for leg in legs), default=0)
    avg_spread_pct = sum(((leg.ask - leg.bid) / max(leg.mid, 0.01)) for leg in legs) / max(len(legs), 1)
    expiry_quality = ",".join(sorted({_expiry_quality(leg.expiration) for leg in legs}))
    liquidity_metrics = candidate_liquidity_metrics({"legs": legs, "net_credit": net_credit})
    return Candidate(
        candidate_id=candidate_id,
        idea_id=idea.idea_id,
        underlying=idea.underlying,
        playbook_id=playbook.playbook_id,
        structure=playbook.structure,
        legs=legs,
        net_credit=net_credit,
        estimated_bpr=bpr,
        greeks=greeks,
        liquidity_score=round(liquidity_score, 4),
        score=0.0,
        reasons=[
            f"built_from={idea.idea_id}",
            f"playbook={playbook.playbook_id}",
            f"expiry_quality={expiry_quality}",
            f"min_open_interest={min_oi}",
            f"avg_bid_ask_pct={avg_spread_pct:.4f}",
            f"max_bid_ask_pct={liquidity_metrics['max_bid_ask_pct']:.4f}",
            f"aggregate_spread_to_mid_pct={liquidity_metrics['aggregate_spread_to_mid_pct']:.4f}",
            f"execution_liquidity_tier={liquidity_metrics['execution_liquidity_tier']}",
            *([f"live_max_bpr_per_order={playbook.live_max_bpr_per_order}"] if playbook.live_max_bpr_per_order is not None else []),
            *([f"mentioned_strategy={idea.mentioned_strategy}"] if idea.mentioned_strategy else []),
        ],
    )


def _leg_rank_score(quote: OptionQuote, *, target_delta: float, target_dte: float) -> float:
    delta_fit = max(0.0, 25.0 - abs(abs(quote.delta) - target_delta) * 100.0)
    dte_fit = max(0.0, 20.0 - abs(quote.dte - target_dte) * 0.5)
    oi = max(int(quote.open_interest or 0), 0)
    if oi >= 1000:
        oi_score = 20.0
    elif oi >= 500:
        oi_score = 16.0
    elif oi >= 100:
        oi_score = 10.0
    elif oi > 0:
        oi_score = 4.0
    else:
        oi_score = 0.0
    spread_score = max(0.0, 20.0 - min(quote.spread_pct, 2.0) * 10.0)
    expiry_score = 4.0 if _expiry_quality(quote.expiration) == "monthly" else 0.0
    return delta_fit + dte_fit + oi_score + spread_score + expiry_score


def _expiry_quality(expiration: str) -> str:
    try:
        import datetime as _dt

        expiry = _dt.date.fromisoformat(expiration)
    except ValueError:
        return "unknown"
    return "monthly" if expiry.weekday() == 4 and 15 <= expiry.day <= 21 else "weekly"


def _estimate_bpr(structure: str, legs: list[OptionLeg], net_credit: float) -> float:
    if structure in {"put_spread", "call_spread"} and len(legs) == 2:
        width = abs(legs[0].strike - legs[1].strike)
        return round(max(width * 100 - max(net_credit, 0) * 100, 1.0), 2)
    if structure == "iron_condor" and len(legs) == 4:
        puts = [leg for leg in legs if leg.option_type == "put"]
        calls = [leg for leg in legs if leg.option_type == "call"]
        put_width = abs(puts[0].strike - puts[1].strike)
        call_width = abs(calls[0].strike - calls[1].strike)
        return round(max(max(put_width, call_width) * 100 - max(net_credit, 0) * 100, 1.0), 2)
    if structure == "short_put":
        return round(max(legs[0].strike * 100 * 0.2, 1.0), 2)
    if structure == "short_strangle" and len(legs) == 2:
        short_strikes = [leg.strike for leg in legs if leg.side == "sell"]
        return round(max(max(short_strikes) * 100 * 0.2, 1.0), 2)
    if structure == "jade_lizard" and len(legs) == 3:
        short_puts = [leg for leg in legs if leg.option_type == "put" and leg.side == "sell"]
        calls = [leg for leg in legs if leg.option_type == "call"]
        put_risk = short_puts[0].strike * 100 * 0.2 if short_puts else 0.0
        call_width = abs(calls[0].strike - calls[1].strike) if len(calls) == 2 else 0.0
        call_risk = max(call_width * 100 - max(net_credit, 0) * 100, 0.0)
        return round(max(put_risk + call_risk, 1.0), 2)
    if structure in {"call_calendar", "put_calendar", "put_diagonal", "call_diagonal", "long_call", "long_put"}:
        return round(max(abs(net_credit) * 100, 1.0), 2)
    return round(max(abs(net_credit) * 100, 1.0), 2)


def _candidate_score(candidate: Candidate, *, thesis_fit: float = 0.0) -> float:
    credit_yield = max(candidate.net_credit, 0.0) * 100 / max(candidate.estimated_bpr, 1.0)
    theta_dollars_per_day = candidate.greeks.theta * 100.0
    theta_per_1k_bpr = theta_dollars_per_day / max(candidate.estimated_bpr / 1000.0, 0.001)
    if theta_per_1k_bpr >= 0:
        theta_score = min(theta_per_1k_bpr * 3.0, 25.0)
    else:
        theta_score = max(theta_per_1k_bpr * 6.0, -35.0)
    gamma_penalty = min(abs(candidate.greeks.gamma) * 250.0, 20.0)
    return round(
        thesis_fit
        + credit_yield * 20.0
        + candidate.liquidity_score * 15.0
        + theta_score
        - gamma_penalty,
        4,
    )


def _select_diverse_candidates(candidates: list[Candidate], limit: int) -> list[Candidate]:
    if limit <= 0:
        return []
    ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
    selected: list[Candidate] = []
    selected_ids: set[str] = set()
    seen_structures: set[str] = set()
    for candidate in ranked:
        if candidate.structure in seen_structures:
            continue
        selected.append(candidate)
        selected_ids.add(candidate.candidate_id)
        seen_structures.add(candidate.structure)
        if len(selected) >= limit:
            return sorted(selected, key=lambda item: item.score, reverse=True)
    for candidate in ranked:
        if candidate.candidate_id in selected_ids:
            continue
        selected.append(candidate)
        selected_ids.add(candidate.candidate_id)
        if len(selected) >= limit:
            break
    return sorted(selected, key=lambda item: item.score, reverse=True)


def _thesis_fit_score(idea: Idea, candidate: Candidate) -> float:
    direction = idea.direction.lower()
    tags = set(idea.thesis_tags)
    structure = candidate.structure
    strategy_bonus = 15.0 if _strategy_aliases(idea.mentioned_strategy).intersection({candidate.playbook_id, candidate.structure}) else 0.0
    if direction == "bearish" or "overextended" in tags:
        return strategy_bonus + {
            "put_diagonal": 40.0,
            "call_spread": 30.0,
            "long_put": 22.0,
            "put_calendar": 12.0,
            "short_strangle": 8.0,
            "iron_condor": 5.0,
            "jade_lizard": 3.0,
            "call_calendar": 0.0,
            "put_spread": -15.0,
            "short_put": -25.0,
        }.get(structure, 0.0)
    if direction == "bullish" or "oversold" in tags:
        return strategy_bonus + {
            "call_diagonal": 35.0,
            "put_spread": 25.0,
            "long_call": 22.0,
            "short_put": 20.0,
            "jade_lizard": 12.0,
            "call_calendar": 0.0,
            "iron_condor": 5.0,
            "call_spread": -25.0,
        }.get(structure, 0.0)
    if direction == "neutral":
        return strategy_bonus + {
            "short_strangle": 18.0,
            "iron_condor": 15.0,
            "call_calendar": 12.0,
            "put_calendar": 12.0,
            "jade_lizard": 10.0,
            "put_spread": 0.0,
            "call_spread": 0.0,
            "short_put": 0.0,
        }.get(structure, 0.0)
    return strategy_bonus


def _filter_rejections(candidate: Candidate, playbook: Playbook, config: dict | None = None) -> list[str]:
    rejections: list[str] = []
    absurd_bid_ask_pct = _absurd_bid_ask_pct(config)
    for leg in candidate.legs:
        bad_quote = bad_quote_reason(leg, absurd_bid_ask_pct=absurd_bid_ask_pct)
        if bad_quote:
            rejections.append(bad_quote)
            break
    if playbook.min_option_oi is not None:
        for leg in candidate.legs:
            if leg.open_interest < playbook.min_option_oi:
                rejections.append(f"open_interest_below_min:{leg.open_interest}<{playbook.min_option_oi}")
                break
    if playbook.max_bid_ask_pct is not None:
        for leg in candidate.legs:
            spread_pct = (leg.ask - leg.bid) / max(leg.mid, 0.01)
            if spread_pct > playbook.max_bid_ask_pct:
                rejections.append(f"bid_ask_pct_above_max:{spread_pct:.4f}>{playbook.max_bid_ask_pct}")
                break
    if playbook.min_credit_to_width_ratio is not None and candidate.net_credit > 0:
        width = _risk_width(candidate)
        if width > 0:
            ratio = candidate.net_credit / width
            if ratio < playbook.min_credit_to_width_ratio:
                rejections.append(f"credit_width_ratio_below_min:{ratio:.4f}<{playbook.min_credit_to_width_ratio}")
    if candidate.structure == "jade_lizard":
        width = _risk_width(candidate)
        if width > 0 and candidate.net_credit < width:
            rejections.append(f"jade_lizard_credit_below_call_width:{candidate.net_credit:.4f}<{width:.4f}")
    return rejections


def _filter_rejection(candidate: Candidate, playbook: Playbook) -> str:
    rejections = _filter_rejections(candidate, playbook)
    return rejections[0] if rejections else ""


def _low_oi_price_through_enabled(rejection: str, config: dict | None) -> bool:
    if not rejection.startswith("open_interest_below_min:"):
        return False
    if str((((config or {}).get("runtime") or {}).get("mode") or "")).strip().lower() != "live":
        return False
    mode = str((((config or {}).get("live") or {}).get("liquidity_policy") or {}).get("low_oi_mode") or "").strip().lower()
    return mode == "price_through"


def _wide_bid_ask_price_through_enabled(rejection: str, config: dict | None) -> bool:
    if not rejection.startswith("bid_ask_pct_above_max:"):
        return False
    if str((((config or {}).get("runtime") or {}).get("mode") or "")).strip().lower() != "live":
        return False
    policy = (((config or {}).get("live") or {}).get("liquidity_policy") or {})
    mode = str(policy.get("wide_bid_ask_mode") or "").strip().lower()
    return mode == "price_through"


def _absurd_bid_ask_pct(config: dict | None) -> float:
    policy = (((config or {}).get("live") or {}).get("liquidity_policy") or {})
    try:
        return float(policy.get("absurd_bid_ask_pct") or 3.0)
    except (TypeError, ValueError):
        return 3.0


def _risk_width(candidate: Candidate) -> float:
    if candidate.structure in {"put_spread", "call_spread"} and len(candidate.legs) == 2:
        return abs(candidate.legs[0].strike - candidate.legs[1].strike)
    if candidate.structure == "iron_condor" and len(candidate.legs) == 4:
        puts = [leg for leg in candidate.legs if leg.option_type == "put"]
        calls = [leg for leg in candidate.legs if leg.option_type == "call"]
        if len(puts) == 2 and len(calls) == 2:
            return max(abs(puts[0].strike - puts[1].strike), abs(calls[0].strike - calls[1].strike))
    if candidate.structure == "jade_lizard" and len(candidate.legs) == 3:
        calls = [leg for leg in candidate.legs if leg.option_type == "call"]
        if len(calls) == 2:
            return abs(calls[0].strike - calls[1].strike)
    return 0.0


def _stable_id(*parts: object) -> str:
    raw = repr(parts).encode("utf-8")
    return "cand_" + hashlib.sha256(raw).hexdigest()[:12]
