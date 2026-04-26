import json

import yaml

from kamandal_v2.intelligence.llm_extractor import extract_ideas_llm
from kamandal_v2.intelligence.reviewer import review_rejections


class _FakeExtractorClient:
    def chat_json(self, system_prompt: str, user_prompt: str):
        assert "Allowed thesis_tags" in system_prompt
        assert "put_spread_default" not in system_prompt
        assert "jade_lizard_high_iv" not in system_prompt
        return {
            "digest": {
                "headline": "TSLA stretched",
                "summary": "Speaker described TSLA as overextended.",
                "actionable_ideas_present": True,
            },
            "ideas": [
                {
                    "underlying": "TSLA",
                    "direction": "bearish",
                    "thesis_tags": ["overextended", "resistance_rejection"],
                    "horizon_days": 14,
                    "mentioned_strategy": "put_diagonal",
                    "extraction_confidence": "high",
                    "quote_evidence": "TSLA looks stretched here.",
                    "extraction_notes": "Clear bearish thesis.",
                },
                {
                    "underlying": "ZZZZ",
                    "direction": "bullish",
                    "thesis_tags": ["breakout"],
                    "horizon_days": 30,
                    "extraction_confidence": "medium",
                },
            ],
        }


class _FakeReviewClient:
    def chat_json(self, system_prompt: str, user_prompt: str):
        assert "must not approve trades" in system_prompt.lower()
        assert "Controlled thesis tags" in user_prompt
        assert "Enabled playbooks" in user_prompt
        return {
            "summary": "Rejected on liquidity.",
            "engine_improvements": [
                {
                    "area": "filters",
                    "observation": "OI filter blocked the candidate.",
                    "suggestion": "Consider chain-level liquidity precheck.",
                    "reason": "Every candidate failed OI.",
                }
            ],
            "intelligence_improvements": [
                {
                    "area": "confidence",
                    "observation": "No need for new thesis tags.",
                    "suggestion": "Represent ambiguity as extraction confidence or notes.",
                    "reason": "Meta-flags should not pollute thesis tags.",
                }
            ],
            "strategy_within_bounds": [
                {"playbook_id": "jade_lizard_high_iv", "suggestion": "Keep strict OI gates.", "reason": "Illiquid legs."},
                {"playbook_id": "unknown_new_playbook", "suggestion": "Add something new.", "reason": "Out of bounds."},
            ],
            "human_review_queue": [],
            "operator_questions": ["Should GE remain in the jade lizard universe?"],
        }


def test_llm_extractor_writes_thesis_ideas_and_filters_universe(tmp_path) -> None:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "sample.txt").write_text("TSLA looks stretched here.", encoding="utf-8")

    result = extract_ideas_llm(
        {},
        transcripts,
        digest_dir=tmp_path / "digest",
        ideas_dir=tmp_path / "ideas",
        allowed_symbols={"TSLA"},
        client=_FakeExtractorClient(),
    )

    assert result.transcript_count == 1
    assert result.idea_count == 1
    assert result.skipped_symbol_count == 1
    payload = yaml.safe_load(result.ideas_path.read_text(encoding="utf-8"))
    idea = payload["ideas"][0]
    assert idea["underlying"] == "TSLA"
    assert idea["strategy_hint"] == ""
    assert idea["mentioned_strategy"] == "put_diagonal"
    assert idea["extraction_confidence"] == "high"
    assert "&id" not in result.ideas_path.read_text(encoding="utf-8")


def test_reviewer_writes_local_json_and_markdown(tmp_path) -> None:
    latest_run = tmp_path / "latest_plan_run.json"
    latest_run.write_text(
        json.dumps({
            "metrics": {"candidates_rejected": 1},
            "candidates": [{"candidate_id": "c1", "rejection_reason": "open_interest_below_min"}],
            "plans": [],
        }),
        encoding="utf-8",
    )

    result = review_rejections(
        {"reviewer": {"config_source": "seed"}},
        latest_run=latest_run,
        output_dir=tmp_path / "reviews",
        client=_FakeReviewClient(),
    )

    assert result.json_path.exists()
    assert result.markdown_path.exists()
    markdown = result.markdown_path.read_text(encoding="utf-8")
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert "Rejected on liquidity" in markdown
    assert "tag_suggestions" not in payload
    assert payload["human_review_queue"]
