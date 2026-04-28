"""Build concrete option candidates from ideas and playbooks."""

from __future__ import annotations

import hashlib
from itertools import product

from kamandal_v2.domain.models import Candidate, Greeks, Idea, OptionLeg, OptionQuote, Playbook, PreflightResult, UniverseEntry
from kamandal_v2.market.interfaces import MarketDataProvider, PreflightClient
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


def build_candidates(
    ideas: list[Idea],
    universe: list[UniverseEntry],
    playbooks: list[Playbook],
    market: MarketDataProvider,
    preflight: PreflightClient,
    *,
    per_idea_cap: int = 5,
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
            if not _matches(idea, entry, playbook, iv_pct, iv_rank, iv_abs, event_status):
                continue
            raw_candidates = _build_for_playbook(idea, playbook, chain.underlying_price, chain.quotes)
            for candidate in raw_candidates:
                result = validate_structure(candidate.structure, candidate.legs, chain.underlying_price)
                if not result.valid:
                    candidate.rejection_reason = result.reason
                    rejected_for_idea.append(candidate)
                    continue
                filter_rejection = _filter_rejection(candidate, playbook)
                if filter_rejection:
                    candidate.rejection_reason = filter_rejection
                    rejected_for_idea.append(candidate)
                    continue
                pf = preflight.preflight(candidate)
                candidate.preflight = pf
                if not pf.ok:
                    candidate.rejection_reason = pf.message or "preflight_failed"
                    rejected_for_idea.append(candidate)
                    continue
                candidate.estimated_bpr = pf.bpr
                thesis_fit = _thesis_fit_score(idea, candidate)
                candidate.score = _candidate_score(candidate, thesis_fit=thesis_fit)
                candidate.reasons.append(f"thesis_fit={thesis_fit}")
                candidate.reasons.append(f"iv_pct={iv_pct}")
                candidate.reasons.append(f"iv_rank={iv_rank}")
                candidate.reasons.append(f"iv_abs={iv_abs}")
                candidate.reasons.append(f"event_status={event_status}")
                built_for_idea.append(candidate)
        all_candidates.extend(sorted(built_for_idea, key=lambda item: item.score, reverse=True)[:per_idea_cap])
        all_candidates.extend(rejected_for_idea)
    return all_candidates


def diagnose_idea_matches(
    ideas: list[Idea],
    universe: list[UniverseEntry],
    playbooks: list[Playbook],
    market: MarketDataProvider,
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
        for playbook in playbooks:
            reasons = _match_rejections(idea, entry, playbook, iv_pct, iv_rank, iv_abs, event_status)
            if reasons:
                for reason in reasons:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
            else:
                matched.append(playbook.playbook_id)
            playbook_rows.append({
                "playbook_id": playbook.playbook_id,
                "structure": playbook.structure,
                "enabled": playbook.enabled,
                "matched": not reasons,
                "reasons": reasons,
            })
        status = "matched_playbooks" if matched else "no_playbook_match"
        diagnostics.append({
            "idea_id": idea.idea_id,
            "underlying": idea.underlying,
            "direction": idea.direction,
            "thesis_tags": list(idea.thesis_tags),
            "horizon_days": idea.horizon_days,
            "mentioned_strategy": idea.mentioned_strategy,
            "status": status,
            "matched_playbooks": matched,
            "summary": _diagnostic_summary(matched, reason_counts),
            "reason_counts": dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))),
            "market_context": {
                "iv_percentile": iv_pct,
                "iv_rank": iv_rank,
                "iv_abs": iv_abs,
                "event_status": event_status,
            },
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
) -> bool:
    return not _match_rejections(idea, entry, playbook, iv_pct, iv_rank, iv_abs, event_status)


def _match_rejections(
    idea: Idea,
    entry: UniverseEntry,
    playbook: Playbook,
    iv_pct: float | None,
    iv_rank: float | None,
    iv_abs: float | None,
    event_status: str,
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
    if idea.strategy_hint and not _strategy_aliases(idea.strategy_hint).intersection({playbook.playbook_id, playbook.structure, playbook.strategy_family}):
        reasons.append(f"strategy_hint_mismatch:{idea.strategy_hint}")
    if playbook.applicable_direction and idea.direction not in playbook.applicable_direction:
        reasons.append(f"direction_mismatch:{idea.direction}")
    idea_tags = {tag.lower() for tag in idea.thesis_tags}
    mentioned_match = _mentioned_strategy_matches(idea, playbook)
    if playbook.applicable_thesis_tags and not idea_tags.intersection(playbook.applicable_thesis_tags) and not mentioned_match:
        reasons.append("thesis_tags_mismatch")
    if playbook.applicable_horizon_min is not None and idea.horizon_days < playbook.applicable_horizon_min:
        reasons.append(f"horizon_below_min:{idea.horizon_days}<{playbook.applicable_horizon_min}")
    if playbook.applicable_horizon_max is not None and idea.horizon_days > playbook.applicable_horizon_max:
        reasons.append(f"horizon_above_max:{idea.horizon_days}>{playbook.applicable_horizon_max}")
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


def _diagnostic_summary(matched: list[str], reason_counts: dict[str, int]) -> str:
    if matched:
        return "Matched playbooks: " + ", ".join(matched[:5])
    if not reason_counts:
        return "No playbooks evaluated."
    top_reasons = sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    return "No playbook matched; top gates: " + ", ".join(f"{reason} ({count})" for reason, count in top_reasons)


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
) -> list[Candidate]:
    if playbook.structure == "short_put":
        return _short_put_candidates(idea, playbook, quotes)
    if playbook.structure == "long_call":
        return _long_option_candidates(idea, playbook, quotes, option_type="call")
    if playbook.structure == "long_put":
        return _long_option_candidates(idea, playbook, quotes, option_type="put")
    if playbook.structure == "put_spread":
        return _put_spread_candidates(idea, playbook, quotes)
    if playbook.structure == "call_spread":
        return _call_spread_candidates(idea, playbook, quotes)
    if playbook.structure == "iron_condor":
        return _iron_condor_candidates(idea, playbook, quotes)
    if playbook.structure == "call_calendar":
        return _call_calendar_candidates(idea, playbook, quotes)
    if playbook.structure == "put_calendar":
        return _put_calendar_candidates(idea, playbook, quotes)
    if playbook.structure == "put_diagonal":
        return _put_diagonal_candidates(idea, playbook, quotes)
    if playbook.structure == "call_diagonal":
        return _call_diagonal_candidates(idea, playbook, quotes)
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


def _put_spread_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote]) -> list[Candidate]:
    candidates = []
    shorts = _near_delta(quotes, "put", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
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


def _call_spread_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote]) -> list[Candidate]:
    candidates = []
    shorts = _near_delta(quotes, "call", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
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
    candidates = []
    short_puts = _near_delta(quotes, "put", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    short_calls = _near_delta(quotes, "call", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    for short_put, short_call in product(short_puts[:4], short_calls[:4]):
        if short_put.expiration != short_call.expiration:
            continue
        candidates.append(_candidate(idea, playbook, [
            OptionLeg.from_quote(short_put, role="short_put", side="sell"),
            OptionLeg.from_quote(short_call, role="short_call", side="sell"),
        ]))
    return candidates[:6]


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


def _call_calendar_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote]) -> list[Candidate]:
    candidates = []
    near_calls = _near_delta(quotes, "call", playbook.long_delta_min, playbook.long_delta_max, playbook.dte_min, playbook.dte_max)
    far_calls = _near_delta(quotes, "call", playbook.long_delta_min, playbook.long_delta_max, playbook.long_dte_min or playbook.dte_min + 20, playbook.long_dte_max or playbook.dte_max + 40)
    for near in near_calls:
        far_choices = [
            quote for quote in far_calls
            if quote.strike == near.strike and quote.expiration > near.expiration
        ]
        if not far_choices:
            continue
        far = sorted(far_choices, key=lambda quote: quote.dte)[0]
        candidates.append(_candidate(idea, playbook, [
            OptionLeg.from_quote(near, role="short_near", side="sell"),
            OptionLeg.from_quote(far, role="long_far", side="buy"),
        ]))
    return candidates[:4]


def _put_calendar_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote]) -> list[Candidate]:
    candidates = []
    near_puts = _near_delta(quotes, "put", playbook.long_delta_min, playbook.long_delta_max, playbook.dte_min, playbook.dte_max)
    far_puts = _near_delta(quotes, "put", playbook.long_delta_min, playbook.long_delta_max, playbook.long_dte_min or playbook.dte_min + 20, playbook.long_dte_max or playbook.dte_max + 40)
    for near in near_puts:
        far_choices = [
            quote for quote in far_puts
            if quote.strike == near.strike and quote.expiration > near.expiration
        ]
        if not far_choices:
            continue
        far = sorted(far_choices, key=lambda quote: quote.dte)[0]
        candidates.append(_candidate(idea, playbook, [
            OptionLeg.from_quote(near, role="short_near", side="sell"),
            OptionLeg.from_quote(far, role="long_far", side="buy"),
        ]))
    return candidates[:4]


def _put_diagonal_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote]) -> list[Candidate]:
    candidates = []
    shorts = _near_delta(quotes, "put", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    longs = _near_delta(quotes, "put", playbook.long_delta_min, playbook.long_delta_max, playbook.long_dte_min or playbook.dte_min + 20, playbook.long_dte_max or playbook.dte_max + 45)
    width = playbook.spread_width or 5.0
    for short in shorts[:4]:
        long_choices = [
            quote for quote in longs
            if quote.expiration > short.expiration and quote.strike >= short.strike
        ]
        if not long_choices:
            continue
        long = sorted(long_choices, key=lambda quote: (abs((quote.strike - short.strike) - width), quote.dte))[0]
        candidates.append(_candidate(idea, playbook, [
            OptionLeg.from_quote(short, role="short_near", side="sell"),
            OptionLeg.from_quote(long, role="long_far", side="buy"),
        ]))
    return candidates[:4]


def _call_diagonal_candidates(idea: Idea, playbook: Playbook, quotes: list[OptionQuote]) -> list[Candidate]:
    candidates = []
    shorts = _near_delta(quotes, "call", playbook.short_delta_min, playbook.short_delta_max, playbook.dte_min, playbook.dte_max)
    longs = _near_delta(quotes, "call", playbook.long_delta_min, playbook.long_delta_max, playbook.long_dte_min or playbook.dte_min + 20, playbook.long_dte_max or playbook.dte_max + 45)
    width = playbook.spread_width or 5.0
    for short in shorts[:4]:
        long_choices = [
            quote for quote in longs
            if quote.expiration > short.expiration and quote.strike <= short.strike
        ]
        if not long_choices:
            continue
        long = sorted(long_choices, key=lambda quote: (abs((short.strike - quote.strike) - width), quote.dte))[0]
        candidates.append(_candidate(idea, playbook, [
            OptionLeg.from_quote(short, role="short_near", side="sell"),
            OptionLeg.from_quote(long, role="long_far", side="buy"),
        ]))
    return candidates[:4]


def _near_delta(
    quotes: list[OptionQuote],
    option_type: str,
    delta_min: float | None,
    delta_max: float | None,
    dte_min: int,
    dte_max: int,
) -> list[OptionQuote]:
    low = delta_min if delta_min is not None else 0.10
    high = delta_max if delta_max is not None else 0.60
    target = (low + high) / 2.0
    filtered = [
        quote for quote in quotes
        if quote.option_type == option_type
        and dte_min <= quote.dte <= dte_max
        and low <= abs(quote.delta) <= high
    ]
    return sorted(filtered, key=lambda quote: (abs(abs(quote.delta) - target), abs(quote.dte - ((dte_min + dte_max) / 2.0)), quote.spread_pct))


def _candidate(idea: Idea, playbook: Playbook, legs: list[OptionLeg]) -> Candidate:
    net_credit = round(sum(leg.signed_mid for leg in legs), 4)
    greeks = Greeks()
    for leg in legs:
        greeks = greeks + leg.signed_greeks
    liquidity_score = max(0.0, min(1.0, 1.0 - (sum(((leg.ask - leg.bid) / max(leg.mid, 0.01)) for leg in legs) / len(legs))))
    bpr = _estimate_bpr(playbook.structure, legs, net_credit)
    candidate_id = _stable_id(idea.idea_id, playbook.playbook_id, [(leg.role, leg.expiration, leg.strike, leg.side) for leg in legs])
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
        reasons=[f"built_from={idea.idea_id}", f"playbook={playbook.playbook_id}"],
    )


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
    theta = candidate.greeks.theta
    return round(credit_yield * 35 + candidate.liquidity_score * 25 + theta * 3 + thesis_fit - abs(candidate.greeks.gamma) * 5, 4)


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


def _filter_rejection(candidate: Candidate, playbook: Playbook) -> str:
    if playbook.min_option_oi is not None:
        for leg in candidate.legs:
            if leg.open_interest < playbook.min_option_oi:
                return f"open_interest_below_min:{leg.open_interest}<{playbook.min_option_oi}"
    if playbook.max_bid_ask_pct is not None:
        for leg in candidate.legs:
            spread_pct = (leg.ask - leg.bid) / max(leg.mid, 0.01)
            if spread_pct > playbook.max_bid_ask_pct:
                return f"bid_ask_pct_above_max:{spread_pct:.4f}>{playbook.max_bid_ask_pct}"
    if playbook.min_credit_to_width_ratio is not None and candidate.net_credit > 0:
        width = _risk_width(candidate)
        if width > 0:
            ratio = candidate.net_credit / width
            if ratio < playbook.min_credit_to_width_ratio:
                return f"credit_width_ratio_below_min:{ratio:.4f}<{playbook.min_credit_to_width_ratio}"
    if candidate.structure == "jade_lizard":
        width = _risk_width(candidate)
        if width > 0 and candidate.net_credit < width:
            return f"jade_lizard_credit_below_call_width:{candidate.net_credit:.4f}<{width:.4f}"
    return ""


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
