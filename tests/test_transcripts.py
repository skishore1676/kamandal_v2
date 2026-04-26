import yaml

from kamandal_v2.intelligence.transcripts import import_transcripts


def test_local_transcript_import_writes_digest_and_ideas(tmp_path) -> None:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "sample.txt").write_text(
        "Today the setup was a TSLA call calendar and an NVDA strangle. IV is high and this is a theta harvest idea.",
        encoding="utf-8",
    )

    result = import_transcripts(
        transcripts,
        digest_dir=tmp_path / "digest",
        ideas_dir=tmp_path / "ideas",
    )

    assert result.transcript_count == 1
    assert result.idea_count >= 1
    assert result.digest_path.exists()
    assert result.ideas_path is not None
    payload = yaml.safe_load(result.ideas_path.read_text(encoding="utf-8"))
    assert payload["ideas"][0]["strategy_hint"] == ""
    assert payload["ideas"][0]["mentioned_strategy"] == "call_calendar"
    assert payload["ideas"][0]["extraction_confidence"] == "high"
    assert payload["ideas"][0]["quote_evidence"]
    assert payload["ideas"][0]["operator_status"] == "pending"


def test_put_diagonal_transcript_stays_thesis_not_trade_instruction(tmp_path) -> None:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "overextended.txt").write_text(
        "TSLA is overextended and someone mentioned a put diagonal idea.",
        encoding="utf-8",
    )

    result = import_transcripts(
        transcripts,
        digest_dir=tmp_path / "digest",
        ideas_dir=tmp_path / "ideas",
    )

    payload = yaml.safe_load(result.ideas_path.read_text(encoding="utf-8"))
    idea = payload["ideas"][0]
    assert idea["underlying"] == "TSLA"
    assert idea["direction"] == "bearish"
    assert idea["strategy_hint"] == ""
    assert idea["mentioned_strategy"] == "put_diagonal"
    assert "overextended" in idea["thesis_tags"]
    assert "mentioned:put_diagonal" not in idea["thesis_tags"]


def test_multiple_symbols_do_not_share_yaml_anchors(tmp_path) -> None:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "multi.txt").write_text(
        "AMD and IWM are breaking out after earnings.",
        encoding="utf-8",
    )

    result = import_transcripts(
        transcripts,
        digest_dir=tmp_path / "digest",
        ideas_dir=tmp_path / "ideas",
    )

    text = result.ideas_path.read_text(encoding="utf-8")
    assert "&id" not in text
    assert "*id" not in text


def test_noisy_transcript_creates_digest_without_executable_ideas(tmp_path) -> None:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "noise.txt").write_text("Market chatter without tickers or a strategy.", encoding="utf-8")

    result = import_transcripts(
        transcripts,
        digest_dir=tmp_path / "digest",
        ideas_dir=tmp_path / "ideas",
    )

    assert result.transcript_count == 1
    assert result.idea_count == 0
    assert result.digest_path.exists()
    assert result.ideas_path is None


def test_transcript_import_recurses_and_filters_symbols(tmp_path) -> None:
    nested = tmp_path / "transcripts" / "archive" / "youtube" / "2026-04-25"
    nested.mkdir(parents=True)
    (nested / "sample.txt").write_text(
        "TSLA is overextended. NVDA is outside this test universe.",
        encoding="utf-8",
    )

    result = import_transcripts(
        tmp_path / "transcripts",
        digest_dir=tmp_path / "digest",
        ideas_dir=tmp_path / "ideas",
        allowed_symbols={"TSLA"},
    )

    payload = yaml.safe_load(result.ideas_path.read_text(encoding="utf-8"))
    assert result.transcript_count == 1
    assert result.idea_count == 1
    assert result.skipped_symbol_count == 1
    assert payload["ideas"][0]["underlying"] == "TSLA"
