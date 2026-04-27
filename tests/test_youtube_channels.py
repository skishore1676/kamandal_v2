from kamandal_v2.intelligence.transcripts import _subtitle_to_text, _youtube_feed_videos_from_xml, _youtube_title_score


def test_youtube_feed_parser_respects_limit_and_filters() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>AAA111</yt:videoId>
    <title>GE jade lizard trade setup</title>
    <published>2026-04-26T10:00:00+00:00</published>
    <author><name>tastylive</name></author>
  </entry>
  <entry>
    <yt:videoId>BBB222</yt:videoId>
    <title>crypto interview</title>
    <published>2026-04-26T11:00:00+00:00</published>
    <author><name>tastylive</name></author>
  </entry>
</feed>
"""

    videos = _youtube_feed_videos_from_xml(
        xml,
        channel_id="UCLJiSMXJ9K-1AOTqIqdXJgQ",
        limit=1,
        include_keywords=["trade"],
        exclude_keywords=["crypto"],
    )

    assert len(videos) == 1
    assert videos[0].video_id == "AAA111"
    assert videos[0].author == "tastylive"


def test_youtube_feed_parser_scores_same_day_idea_videos() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>EDU111</yt:videoId>
    <title>Options Pricing Concepts I Wish I Knew as a Beginner</title>
    <published>2026-04-27T14:00:00+00:00</published>
    <author><name>projectoption</name></author>
  </entry>
  <entry>
    <yt:videoId>TRADE222</yt:videoId>
    <title>April 27th LIVE Stocks, Options &amp; Futures Trading with Pros</title>
    <published>2026-04-27T15:00:00+00:00</published>
    <author><name>tastylive</name></author>
  </entry>
  <entry>
    <yt:videoId>OLD333</yt:videoId>
    <title>Last Call: Market Trades and Volatility</title>
    <published>2026-04-26T23:00:00+00:00</published>
    <author><name>tastylive</name></author>
  </entry>
</feed>
"""

    videos = _youtube_feed_videos_from_xml(
        xml,
        channel_id="UCLJiSMXJ9K-1AOTqIqdXJgQ",
        limit=1,
        scan_limit=20,
        published_date="2026-04-27",
        min_score=1,
    )

    assert [video.video_id for video in videos] == ["TRADE222"]
    assert videos[0].score > 0


def test_youtube_title_score_penalizes_educational_titles() -> None:
    idea_score = _youtube_title_score("LIVE Stocks, Options & Futures Trading with Pros")
    education_score = _youtube_title_score("Options Pricing Concepts I Wish I Knew as a Beginner")

    assert idea_score > 0
    assert education_score < 0


def test_subtitle_to_text_strips_vtt_markup_and_deduplicates() -> None:
    raw = """WEBVTT

00:00:01.000 --> 00:00:03.000
<c>TSLA is overextended &amp; could mean revert.</c>

00:00:03.000 --> 00:00:05.000
<c>TSLA is overextended &amp; could mean revert.</c>

00:00:05.000 --> 00:00:07.000
Consider a defined risk spread.
"""

    assert _subtitle_to_text(raw) == "TSLA is overextended & could mean revert.\nConsider a defined risk spread.\n"
