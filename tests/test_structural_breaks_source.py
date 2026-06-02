from __future__ import annotations

import json

from kamandal_v2.config import load_control
from kamandal_v2.planner.engine import run_plan
from kamandal_v2.sources.structural_breaks import load_structural_breaks, matching_structural_break
from kamandal_v2.stores.audit import AuditWriter
from kamandal_v2.stores.sqlite import LocalStore


def test_structural_break_feed_matches_direction_and_score(tmp_path) -> None:
    path = tmp_path / "2026-05-13.json"
    path.write_text(
        json.dumps({
            "date": "2026-05-13",
            "symbols": [
                {"symbol": "NVDA", "weekly_reclaim_long": True, "confluence_score_long": 3},
            ],
        }),
        encoding="utf-8",
    )

    feed = load_structural_breaks(path)

    assert matching_structural_break(feed["NVDA"], direction="bullish", min_confluence_score=2) is True
    assert matching_structural_break(feed["NVDA"], direction="bearish", min_confluence_score=2) is False


def test_narrative_ignition_requires_structural_break_pass(tmp_path, monkeypatch) -> None:
    feed_dir = tmp_path / "structural_breaks"
    feed_dir.mkdir()
    (feed_dir / "2026-05-13.json").write_text(
        json.dumps({
            "date": "2026-05-13",
            "symbols": [
                {
                    "symbol": "NVDA",
                    "weekly_reclaim_long": True,
                    "confluence_score": 3,
                    "confluence_score_long": 3,
                    "notes": "test pass",
                },
            ],
        }),
        encoding="utf-8",
    )
    ideas = tmp_path / "ideas.yaml"
    ideas.write_text(
        """
ideas:
  - idea_id: nvda_narrative_ignition
    source: test
    underlying: NVDA
    direction: bullish
    mentioned_strategy: narrative_ignition
    thesis_tags: [breakout, momentum, catalyst]
    horizon_days: 21
    extraction_confidence: high
""",
        encoding="utf-8",
    )
    control = load_control()
    control.setdefault("runtime", {})["mode"] = "shadow"
    control.setdefault("execution", {})["approval_mode"] = "shadow_auto_top_plan"
    control.setdefault("shadow", {})["match_gate_mode"] = "permissive"
    control.setdefault("shadow", {})["candidate_filter_mode"] = "warn"
    control["structural_breaks"] = {"enabled": True, "directory": str(feed_dir), "date": "2026-05-13"}
    monkeypatch.setenv("KAMANDAL_STRUCTURAL_BREAKS_DIR", str(feed_dir))

    result = run_plan(
        control,
        idea_paths=[ideas],
        config_source="seed",
        provider="fixture",
        store=LocalStore(tmp_path / "kamandal.db"),
        audit=AuditWriter(tmp_path / "audit"),
    )

    assert any(candidate.playbook_id == "narrative_ignition_long" for candidate in result.candidates)
    assert "structural_break:pass" in result.ideas[0].notes
