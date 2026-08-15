"""LLM transcript-to-thesis extraction."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from kamandal_v2.intelligence.llm_client import JsonLlmClient, build_llm_client
from kamandal_v2.intelligence.transcripts import CONTROLLED_THESIS_TAGS, STRATEGY_HINTS, _clean_transcript_text, _summary, _transcript_files
from kamandal_v2.paths import resolve_path
from kamandal_v2.stores.sqlite import LocalStore


DIRECTIONS = {"bullish", "bearish", "neutral", "vol_up", "vol_down"}
MENTIONED_STRATEGIES = {hint for _phrase, hint in STRATEGY_HINTS} | {"", "none"}


@dataclass(slots=True)
class LlmExtractionResult:
    transcript_count: int
    idea_count: int
    skipped_symbol_count: int
    digest_path: Path
    ideas_path: Path | None
    raw_path: Path

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "transcript_count": self.transcript_count,
            "idea_count": self.idea_count,
            "skipped_symbol_count": self.skipped_symbol_count,
            "digest_path": str(self.digest_path),
            "ideas_path": str(self.ideas_path) if self.ideas_path else None,
            "raw_path": str(self.raw_path),
        }


def extract_ideas_llm(
    config: dict[str, Any],
    source_dir: str | Path,
    *,
    digest_dir: str | Path = "data/digest",
    ideas_dir: str | Path = "data/ideas",
    output_prefix: str = "llm_imported",
    run_date: date | None = None,
    allowed_symbols: set[str] | None = None,
    client: JsonLlmClient | None = None,
    store: LocalStore | None = None,
) -> LlmExtractionResult:
    client = client or build_llm_client(config, actor="thesis_extractor")
    source_path = resolve_path(source_dir)
    digest_path = resolve_path(digest_dir)
    ideas_path = resolve_path(ideas_dir)
    digest_path.mkdir(parents=True, exist_ok=True)
    ideas_path.mkdir(parents=True, exist_ok=True)
    today = run_date or date.today()

    transcript_files = _transcript_files(source_path)
    all_ideas: list[dict[str, Any]] = []
    raw_payloads: list[dict[str, Any]] = []
    skipped_symbol_count = 0
    digest_lines = [f"# Kamandal LLM Digest {today.isoformat()}", ""]

    for transcript_file in transcript_files:
        text = _clean_transcript_text(transcript_file.read_text(encoding="utf-8", errors="replace"))
        payload = client.chat_json(
            _system_prompt(allowed_symbols),
            _user_prompt(transcript_file.name, text),
        )
        raw_payloads.append({"source": str(transcript_file), "payload": payload})
        source_ideas = payload.get("ideas") or []
        if not isinstance(source_ideas, list):
            source_ideas = []
        normalized = []
        for idea_index, raw_idea in enumerate(source_ideas, start=1):
            idea = _normalize_idea(raw_idea, transcript_file=transcript_file, today=today, idea_index=idea_index)
            if idea is None:
                continue
            if allowed_symbols is not None and idea["underlying"] not in allowed_symbols:
                skipped_symbol_count += 1
                if store is not None:
                    record_id = hashlib.sha256(f"{transcript_file}:{idea_index}".encode("utf-8")).hexdigest()
                    store.record_discovery_evidence(
                        symbol=str(idea["underlying"]),
                        source_profile=_discovery_profile(transcript_file),
                        source_record_id=record_id,
                        exclusion_reason="outside_enabled_universe",
                        evidence_ref=f"llm_extract:{transcript_file.name}:{idea_index}",
                    )
                continue
            normalized.append(idea)
        all_ideas.extend(normalized)
        digest_lines.extend(_digest_lines(transcript_file.stem, text, payload, normalized))

    digest_file = digest_path / f"{today.isoformat()}_llm.md"
    digest_file.write_text("\n".join(digest_lines).strip() + "\n", encoding="utf-8")
    raw_file = digest_path / f"{today.isoformat()}_llm_raw.json"
    raw_file.write_text(json.dumps(raw_payloads, indent=2, sort_keys=True), encoding="utf-8")

    output_ideas_file: Path | None = None
    if all_ideas:
        output_ideas_file = ideas_path / f"{_safe_output_prefix(output_prefix)}_{today.isoformat()}.yaml"
        output_ideas_file.write_text(
            yaml.safe_dump({"ideas": all_ideas}, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )

    return LlmExtractionResult(
        transcript_count=len(transcript_files),
        idea_count=len(all_ideas),
        skipped_symbol_count=skipped_symbol_count,
        digest_path=digest_file,
        ideas_path=output_ideas_file,
        raw_path=raw_file,
    )


def _discovery_profile(path: Path) -> str:
    """Classify the shared extractor's source without creating a planner lane."""
    parts = {part.lower() for part in path.parts}
    if "x_bookmarks" in parts or "x_digest" in parts:
        return "x"
    if "transcripts" in parts or "youtube" in parts:
        return "youtube"
    return "llm_source"


def _system_prompt(allowed_symbols: set[str] | None) -> str:
    universe_text = ", ".join(sorted(allowed_symbols)) if allowed_symbols else "No universe filter was provided."
    return f"""\
You extract thesis objects from trading transcripts for Kamandal.

You must not construct broker orders, choose option legs, or decide whether a trade is good.
You do not see playbooks. Your job is semantic extraction only.

Return JSON only:
{{
  "digest": {{
    "headline": "short human digest",
    "summary": "2-4 sentence summary",
    "actionable_ideas_present": true
  }},
  "ideas": [
    {{
      "underlying": "TSLA",
      "direction": "bullish|bearish|neutral|vol_up|vol_down",
      "thesis_tags": ["controlled_tag"],
      "catalyst_horizon_days": 14,
      "trade_horizon_days": null,
      "mentioned_strategy": "put_spread|call_spread|short_strangle|jade_lizard|short_put|call_calendar|put_calendar|put_diagonal|call_diagonal|long_call|long_put|none",
      "extraction_confidence": "low|medium|high",
      "quote_evidence": "short direct transcript evidence under 25 words",
      "extraction_notes": "why you mapped this thesis"
    }}
  ]
}}

Allowed thesis_tags:
{", ".join(sorted(CONTROLLED_THESIS_TAGS))}

Allowed universe symbols:
{universe_text}

Rules:
- Extract a thesis even when no exact trade was recommended, but mark confidence honestly.
- extraction_confidence means confidence that you understood the transcript, not trade conviction.
- Use low confidence for offhand mentions, sarcasm, noisy captions, or ambiguous bias.
- Use medium confidence for clear ticker bias without a concrete setup.
- Use high confidence only for explicit current setup, trade example, or strong ticker thesis.
- Keep mentioned_strategy as a mention only. Do not force tags to fit the strategy.
- catalyst_horizon_days is when the source expects the thesis/catalyst to matter; it is not option DTE.
- Leave trade_horizon_days null unless the source explicitly discusses a trade holding period. Do not infer option DTE.
- If no useful thesis exists, return "ideas": [].
"""


def _user_prompt(source_name: str, text: str) -> str:
    return f"""\
Source: {source_name}

--- TRANSCRIPT ---
{text[:24000]}
--- END TRANSCRIPT ---
"""


def _normalize_idea(raw: Any, *, transcript_file: Path, today: date, idea_index: int = 1) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    underlying = str(raw.get("underlying") or raw.get("ticker") or "").upper().strip()
    if not underlying:
        return None
    direction = str(raw.get("direction") or "neutral").lower().strip()
    if direction not in DIRECTIONS:
        direction = "neutral"
    tags_raw = raw.get("thesis_tags") or []
    if isinstance(tags_raw, str):
        tags_raw = [item.strip() for item in tags_raw.split(",")]
    tags = [str(tag).lower().strip() for tag in tags_raw if str(tag).lower().strip() in CONTROLLED_THESIS_TAGS]
    if not tags:
        return None
    mentioned_strategy = str(raw.get("mentioned_strategy") or "").lower().strip()
    if mentioned_strategy == "none":
        mentioned_strategy = ""
    if mentioned_strategy not in MENTIONED_STRATEGIES:
        mentioned_strategy = ""
    confidence = str(raw.get("extraction_confidence") or raw.get("confidence") or "low").lower().strip()
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    catalyst_horizon_days = _coerce_horizon(raw.get("catalyst_horizon_days", raw.get("horizon_days")))
    trade_horizon_days = _coerce_optional_horizon(raw.get("trade_horizon_days"))
    idea_id = f"{today.isoformat()}_{transcript_file.stem}_{underlying}_{idea_index:02d}"
    return {
        "idea_id": idea_id,
        "source": f"llm_transcript:{transcript_file.name}",
        "underlying": underlying,
        "direction": direction,
        "strategy_hint": "",
        "mentioned_strategy": mentioned_strategy,
        "thesis_tags": list(tags),
        "horizon_days": catalyst_horizon_days,
        "catalyst_horizon_days": catalyst_horizon_days,
        "trade_horizon_days": trade_horizon_days,
        "confidence": confidence,
        "extraction_confidence": confidence,
        "quote_evidence": str(raw.get("quote_evidence") or "")[:300],
        "extraction_notes": str(raw.get("extraction_notes") or "")[:500],
        "operator_status": "pending",
        "notes": str(raw.get("extraction_notes") or raw.get("quote_evidence") or "")[:500],
    }


def _coerce_horizon(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 45
    return max(1, min(value, 180))


def _coerce_optional_horizon(raw: Any) -> int | None:
    if raw in (None, "", "none", "null"):
        return None
    return _coerce_horizon(raw)


def _safe_output_prefix(raw: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in raw.strip())
    return safe or "llm_imported"


def _digest_lines(source: str, text: str, payload: dict[str, Any], ideas: list[dict[str, Any]]) -> list[str]:
    digest = payload.get("digest") if isinstance(payload.get("digest"), dict) else {}
    lines = [
        f"## {source}",
        f"- headline: {digest.get('headline') or 'none'}",
        f"- actionable_ideas_present: {digest.get('actionable_ideas_present')}",
        f"- ideas: {len(ideas)}",
        f"- summary: {digest.get('summary') or _summary(text)}",
    ]
    for idea in ideas:
        lines.append(
            f"  - {idea['underlying']} {idea['direction']} tags={','.join(idea['thesis_tags'])} "
            f"mentioned={idea['mentioned_strategy'] or 'none'} confidence={idea['extraction_confidence']}"
        )
    lines.append("")
    return lines
