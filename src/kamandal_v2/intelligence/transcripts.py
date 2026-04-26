"""Local transcript import and rough deterministic idea extraction."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import requests
import yaml

from kamandal_v2.paths import resolve_path


COMMON_WORDS = {
    "ABOUT",
    "CALL",
    "CAN",
    "CEO",
    "CFO",
    "DTE",
    "ETF",
    "FOR",
    "FROM",
    "IV",
    "LLC",
    "NYSE",
    "NASDAQ",
    "PUT",
    "THE",
    "THIS",
    "USD",
    "WITH",
}

STRATEGY_HINTS = [
    ("jade lizard", "jade_lizard"),
    ("put diagonal", "put_diagonal"),
    ("call calendar", "call_calendar"),
    ("calendar", "call_calendar"),
    ("iron condor", "iron_condor"),
    ("put spread", "put_spread"),
    ("call spread", "call_spread"),
    ("strangle", "strangle"),
    ("short put", "short_put"),
]

CONTROLLED_THESIS_TAGS = {
    "support_bounce",
    "mean_revert",
    "theta_harvest",
    "vol_contraction",
    "vol_expansion",
    "overextended",
    "oversold",
    "range_bound",
    "breakout",
    "breakdown",
    "capitulation",
    "blow_off_top",
    "acquire_at_discount",
    "dividend_play",
    "pre_event_anticipation",
    "post_event_consolidation",
    "distribution",
    "low_iv",
    "high_iv",
    "low_realized_vol",
    "momentum",
    "catalyst",
    "earnings",
    "resistance_rejection",
}

YOUTUBE_TRANSCRIPT_PROVIDERS = {"api", "yt_dlp"}


@dataclass(slots=True)
class TranscriptImportResult:
    transcript_count: int
    idea_count: int
    skipped_symbol_count: int
    digest_path: Path
    ideas_path: Path | None

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "transcript_count": self.transcript_count,
            "idea_count": self.idea_count,
            "skipped_symbol_count": self.skipped_symbol_count,
            "digest_path": str(self.digest_path),
            "ideas_path": str(self.ideas_path) if self.ideas_path else None,
        }


@dataclass(slots=True)
class YouTubeVideoRef:
    video_id: str
    title: str
    channel_id: str
    author: str
    published_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "channel_id": self.channel_id,
            "author": self.author,
            "published_at": self.published_at,
        }


def import_transcripts(
    source_dir: str | Path = "data/transcripts",
    *,
    digest_dir: str | Path = "data/digest",
    ideas_dir: str | Path = "data/ideas",
    run_date: date | None = None,
    allowed_symbols: set[str] | None = None,
) -> TranscriptImportResult:
    source_path = resolve_path(source_dir)
    digest_path = resolve_path(digest_dir)
    ideas_path = resolve_path(ideas_dir)
    digest_path.mkdir(parents=True, exist_ok=True)
    ideas_path.mkdir(parents=True, exist_ok=True)

    today = run_date or date.today()
    transcript_files = _transcript_files(source_path)
    idea_payloads: list[dict[str, object]] = []
    skipped_symbol_count = 0
    digest_lines = [f"# Kamandal Digest {today.isoformat()}", ""]

    for transcript_file in transcript_files:
        raw_text = transcript_file.read_text(encoding="utf-8", errors="replace")
        text = _clean_transcript_text(raw_text)
        raw_symbols = _extract_symbols(text)
        if allowed_symbols is not None:
            symbols = [symbol for symbol in raw_symbols if symbol in allowed_symbols]
            skipped_symbol_count += len(raw_symbols) - len(symbols)
        else:
            symbols = raw_symbols
        mentioned_strategy = _extract_mentioned_strategy(text)
        thesis_tags = _extract_thesis_tags(text)
        direction = _extract_direction(text)
        horizon_days = _extract_horizon_days(text)
        extraction_confidence = _extraction_confidence(text, raw_symbols, thesis_tags, mentioned_strategy)
        evidence = _evidence_snippet(text, raw_symbols, thesis_tags)
        digest_lines.extend(
            [
                f"## {transcript_file.stem}",
                f"- symbols: {', '.join(symbols) if symbols else 'none'}",
                f"- direction: {direction}",
                f"- thesis_tags: {', '.join(thesis_tags) if thesis_tags else 'none'}",
                f"- horizon_days: {horizon_days}",
                f"- extraction_confidence: {extraction_confidence}",
                f"- mentioned_strategy: {mentioned_strategy or 'none'}",
                f"- evidence: {evidence or 'none'}",
                f"- rough_summary: {_summary(text)}",
                "",
            ]
        )
        if not symbols or not thesis_tags:
            continue
        for symbol in symbols[:5]:
            idea_payloads.append(
                {
                    "idea_id": f"{today.isoformat()}_{transcript_file.stem}_{symbol}",
                    "source": f"transcript:{transcript_file.name}",
                    "underlying": symbol,
                    "direction": direction,
                    "strategy_hint": "",
                    "mentioned_strategy": mentioned_strategy,
                    "thesis_tags": list(thesis_tags),
                    "horizon_days": horizon_days,
                    "confidence": extraction_confidence,
                    "extraction_confidence": extraction_confidence,
                    "quote_evidence": evidence,
                    "extraction_notes": "Semantic transcript extraction; playbook matching is deterministic downstream.",
                    "operator_status": "pending",
                    "notes": _summary(text),
                }
            )

    digest_file = digest_path / f"{today.isoformat()}.md"
    digest_file.write_text("\n".join(digest_lines).strip() + "\n", encoding="utf-8")

    output_ideas_file: Path | None = None
    if idea_payloads:
        output_ideas_file = ideas_path / f"imported_{today.isoformat()}.yaml"
        output_ideas_file.write_text(yaml.safe_dump({"ideas": idea_payloads}, sort_keys=False, default_flow_style=False), encoding="utf-8")

    return TranscriptImportResult(
        transcript_count=len(transcript_files),
        idea_count=len(idea_payloads),
        skipped_symbol_count=skipped_symbol_count,
        digest_path=digest_file,
        ideas_path=output_ideas_file,
    )


def fetch_youtube_channel_videos(
    channel_ids: Iterable[str],
    *,
    limit: int = 1,
    include_keywords: Iterable[str] = (),
    exclude_keywords: Iterable[str] = (),
) -> list[YouTubeVideoRef]:
    videos: list[YouTubeVideoRef] = []
    seen: set[str] = set()
    for channel_id in channel_ids:
        channel_id = channel_id.strip()
        if not channel_id:
            continue
        response = requests.get(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
            timeout=20,
        )
        response.raise_for_status()
        for video in _youtube_feed_videos_from_xml(
            response.text,
            channel_id=channel_id,
            limit=limit,
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
        ):
            if video.video_id in seen:
                continue
            seen.add(video.video_id)
            videos.append(video)
    return videos


def _youtube_feed_videos_from_xml(
    xml_text: str,
    *,
    channel_id: str,
    limit: int,
    include_keywords: Iterable[str] = (),
    exclude_keywords: Iterable[str] = (),
) -> list[YouTubeVideoRef]:
    root = ET.fromstring(xml_text)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    videos: list[YouTubeVideoRef] = []
    for entry in root.findall("atom:entry", ns):
        if len(videos) >= limit:
            break
        video_id = _xml_text(entry, "videoId")
        title = _xml_text(entry, "title")
        if not video_id or not _title_passes_filter(title, include_keywords, exclude_keywords):
            continue
        videos.append(
            YouTubeVideoRef(
                video_id=video_id,
                title=title,
                channel_id=channel_id,
                author=_xml_text(entry, "name"),
                published_at=_xml_text(entry, "published"),
            )
        )
    return videos


def _xml_text(item: ET.Element, local_name: str) -> str:
    for child in item.iter():
        if child.tag.split("}")[-1] == local_name:
            return (child.text or child.get("href") or "").strip()
    return ""


def _title_passes_filter(
    title: str,
    include_keywords: Iterable[str],
    exclude_keywords: Iterable[str],
) -> bool:
    lowered = title.lower()
    includes = [keyword.lower().strip() for keyword in include_keywords if keyword.strip()]
    excludes = [keyword.lower().strip() for keyword in exclude_keywords if keyword.strip()]
    if includes and not any(re.search(pattern, lowered) for pattern in includes):
        return False
    if excludes and any(re.search(pattern, lowered) for pattern in excludes):
        return False
    return True


def fetch_youtube_transcript_api(
    video_id: str,
    *,
    transcript_dir: str | Path = "data/transcripts",
    languages: Iterable[str] = ("en",),
) -> Path:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise RuntimeError("youtube-transcript-api is not installed. Run `pip install -e '.[dev]'`.") from exc

    video_id = _youtube_video_id(video_id)
    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=list(languages))
        lines = [str(item.get("text") or "").strip() for item in transcript if str(item.get("text") or "").strip()]
    else:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=list(languages))
        lines = [str(getattr(item, "text", "") or "").strip() for item in fetched if str(getattr(item, "text", "") or "").strip()]
    target_dir = resolve_path(transcript_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"youtube_{video_id}.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def fetch_youtube_transcript_ytdlp(
    video_id: str,
    *,
    transcript_dir: str | Path = "data/transcripts",
    languages: Iterable[str] = ("en",),
    sleep_requests: float = 3.0,
    sleep_subtitles: float = 5.0,
    cookies_from_browser: str = "",
    archive_file: str | Path = "data/youtube_archive.txt",
) -> Path:
    video_id = _youtube_video_id(video_id)
    target_dir = resolve_path(transcript_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    archive_path = resolve_path(archive_file)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    output_template = str(target_dir / f"youtube_{video_id}.%(ext)s")
    executable = shutil.which("yt-dlp")
    args = [
        executable or sys.executable,
    ]
    if executable is None:
        args.extend(["-m", "yt_dlp"])
    args.extend([
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        ",".join(languages) or "en",
        "--sub-format",
        "vtt/srv3/ttml/best",
        "--sleep-requests",
        str(sleep_requests),
        "--sleep-subtitles",
        str(sleep_subtitles),
        "--download-archive",
        str(archive_path),
        "--no-progress",
        "--output",
        output_template,
        f"https://www.youtube.com/watch?v={video_id}",
    ])
    if cookies_from_browser:
        args.extend(["--cookies-from-browser", cookies_from_browser])
    result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=240)
    if result.returncode != 0:
        detail = "\n".join(part for part in (result.stderr.strip(), result.stdout.strip()) if part)
        raise RuntimeError(f"yt-dlp transcript fetch failed for {video_id}: {detail}")
    subtitle_files = sorted(target_dir.glob(f"youtube_{video_id}.*"))
    subtitle_files = [path for path in subtitle_files if path.suffix.lower() in {".vtt", ".srv3", ".ttml", ".srt"}]
    if not subtitle_files:
        raise RuntimeError(f"yt-dlp did not create a subtitle file for {video_id}")
    target = target_dir / f"youtube_{video_id}.txt"
    target.write_text(_subtitle_to_text(subtitle_files[0].read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
    return target


def fetch_youtube_transcript(
    video_id: str,
    *,
    transcript_dir: str | Path = "data/transcripts",
    languages: Iterable[str] = ("en",),
    provider: str = "yt_dlp",
    sleep_requests: float = 3.0,
    sleep_subtitles: float = 5.0,
    cookies_from_browser: str = "",
    archive_file: str | Path = "data/youtube_archive.txt",
) -> Path:
    provider = provider.strip().lower()
    if provider not in YOUTUBE_TRANSCRIPT_PROVIDERS:
        raise ValueError(f"unsupported YouTube transcript provider: {provider}")
    if provider == "api":
        return fetch_youtube_transcript_api(video_id, transcript_dir=transcript_dir, languages=languages)
    return fetch_youtube_transcript_ytdlp(
        video_id,
        transcript_dir=transcript_dir,
        languages=languages,
        sleep_requests=sleep_requests,
        sleep_subtitles=sleep_subtitles,
        cookies_from_browser=cookies_from_browser,
        archive_file=archive_file,
    )


def scrape_youtube_smoke(
    video_id: str,
    *,
    transcript_dir: str | Path = "data/transcripts",
    languages: Iterable[str] = ("en",),
) -> Path:
    return fetch_youtube_transcript_api(video_id, transcript_dir=transcript_dir, languages=languages)


def _subtitle_to_text(raw: str) -> str:
    lines: list[str] = []
    prior = ""
    for line in raw.splitlines():
        cleaned = line.strip()
        if (
            not cleaned
            or cleaned == "WEBVTT"
            or cleaned.isdigit()
            or "-->" in cleaned
            or cleaned.startswith(("Kind:", "Language:", "NOTE", "<?xml", "<tt ", "</tt"))
        ):
            continue
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        cleaned = cleaned.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned and cleaned != prior:
            lines.append(cleaned)
            prior = cleaned
    return "\n".join(lines).strip() + "\n"


def _youtube_video_id(raw: str) -> str:
    value = raw.strip()
    parsed = urlparse(value)
    if parsed.netloc.endswith("youtube.com"):
        query_id = parse_qs(parsed.query).get("v")
        if query_id:
            return query_id[0]
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/")
    return value


def _transcript_files(source_path: Path) -> list[Path]:
    if not source_path.exists():
        return []
    if source_path.is_file():
        return [source_path]
    files: list[Path] = []
    for pattern in ("*.txt", "*.md"):
        files.extend(source_path.rglob(pattern))
    return sorted(files)


def _extract_mentioned_strategy(text: str) -> str:
    lowered = text.lower()
    for phrase, hint in STRATEGY_HINTS:
        if phrase in lowered:
            return hint
    return ""


def _clean_transcript_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r"https?://\S+", "", line)
        cleaned = re.sub(r"\bFREE\b", "", cleaned)
        cleaned = re.sub(r"Helpful links:\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"Follow tastylive on X.*?(?=[A-Z][a-z])", "", cleaned)
        lowered = cleaned.strip().lower()
        if lowered.startswith(("follow ", "subscribe", "newsletter")):
            continue
        lines.append(cleaned)
    return "\n".join(lines).strip()


def _extract_symbols(text: str) -> list[str]:
    raw = re.findall(r"\b[A-Z][A-Z0-9]{1,4}\b", text)
    symbols: list[str] = []
    for symbol in raw:
        if symbol in COMMON_WORDS or symbol.isdigit():
            continue
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _extract_direction(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("overextended", "over extended", "extended", "too high", "up too much")):
        return "bearish"
    if any(token in lowered for token in ("oversold", "beaten down", "washed out", "support")):
        return "bullish"
    return "neutral"


def _extract_thesis_tags(text: str) -> list[str]:
    lowered = text.lower()
    tags: list[str] = []
    tag_rules = {
        "overextended": ("overextended", "over extended", "stretched", "too far too fast", "too high", "up too much"),
        "resistance_rejection": ("rejected at resistance", "resistance rejection", "failed at resistance", "toppy"),
        "distribution": ("distribution", "selling pressure", "institutions selling"),
        "blow_off_top": ("blow off top", "blow-off top", "parabolic"),
        "oversold": ("oversold", "beaten down", "washed out"),
        "support_bounce": ("support bounce", "bounced off support", "holding support"),
        "capitulation": ("capitulation", "panic selling"),
        "breakout": ("breakout", "breaking out", "new highs"),
        "breakdown": ("breakdown", "breaking down", "lost support"),
        "range_bound": ("range bound", "range-bound", "chop", "sideways"),
        "earnings": ("earnings", "earnings report"),
        "pre_event_anticipation": ("before earnings", "pre earnings", "pre-event", "into earnings"),
        "post_event_consolidation": ("after earnings", "post earnings", "post-event", "digesting the move"),
        "high_iv": ("high iv", "iv is high", "volatility is high"),
        "low_iv": ("low iv", "iv is low", "volatility is low"),
        "vol_contraction": ("vol crush", "volatility contraction", "vol contraction", "premium selling", "sell premium"),
        "vol_expansion": ("vol expansion", "volatility expansion", "buy volatility"),
        "theta_harvest": ("theta", "theta harvest", "premium selling", "sell premium"),
        "momentum": ("trend", "momentum", "relative strength"),
        "catalyst": ("catalyst", "upgrade", "downgrade", "announcement"),
        "mean_revert": ("mean reversion", "mean revert", "snap back", "reversion"),
    }
    for tag, phrases in tag_rules.items():
        if any(phrase in lowered for phrase in phrases):
            tags.append(tag)
    return [tag for tag in tags if tag in CONTROLLED_THESIS_TAGS]


def _extract_horizon_days(text: str) -> int:
    lowered = text.lower()
    explicit = re.search(r"\b(\d{1,3})\s*(?:day|days|dte)\b", lowered)
    if explicit:
        return max(1, min(int(explicit.group(1)), 180))
    if any(phrase in lowered for phrase in ("next week", "near term", "short term", "this week")):
        return 14
    if any(phrase in lowered for phrase in ("next month", "few weeks", "swing")):
        return 30
    if any(phrase in lowered for phrase in ("longer term", "quarter", "multi-month")):
        return 60
    return 45


def _extraction_confidence(text: str, symbols: list[str], thesis_tags: list[str], mentioned_strategy: str = "") -> str:
    lowered = text.lower()
    directional_tags = {"overextended", "oversold", "support_bounce", "resistance_rejection", "breakout", "breakdown"}
    contradictory_tags = {"breakout", "breakdown"}.issubset(set(thesis_tags))
    actionable = any(token in lowered for token in ("setup", "trade", "idea", "play", "watching", "builds", "building"))
    if symbols and thesis_tags and actionable and not contradictory_tags and (
        mentioned_strategy or set(thesis_tags).intersection(directional_tags)
    ):
        return "high"
    if symbols and thesis_tags:
        return "medium"
    return "low"


def _evidence_snippet(text: str, symbols: list[str], thesis_tags: list[str]) -> str:
    compact = " ".join(text.split())
    if not compact:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", compact)
    symbol_set = set(symbols)
    evidence_terms = {
        "breakout": ("breakout", "breaking out", "all time highs", "new highs"),
        "breakdown": ("breakdown", "breaking down", "lost support"),
        "earnings": ("earnings", "beat", "miss", "guidance"),
        "catalyst": ("catalyst", "upgrade", "downgrade", "announcement", "beat"),
        "overextended": ("overextended", "extended", "stretched", "too far too fast"),
        "oversold": ("oversold", "beaten down", "washed out"),
        "theta_harvest": ("theta", "premium", "sell premium"),
        "vol_contraction": ("vol crush", "volatility contraction", "vol contraction"),
    }
    terms = {term for tag in thesis_tags for term in evidence_terms.get(tag, (tag.replace("_", " "),))}
    for sentence in sentences:
        upper_sentence = sentence.upper()
        lower_sentence = sentence.lower()
        if symbol_set.intersection(set(re.findall(r"\b[A-Z][A-Z0-9]{1,4}\b", upper_sentence))) and any(term in lower_sentence for term in terms):
            return sentence[:240]
    for sentence in sentences:
        lower_sentence = sentence.lower()
        if any(term in lower_sentence for term in terms):
            return sentence[:240]
    return sentences[0][:240]


def _summary(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:240] + ("..." if len(compact) > 240 else "")
