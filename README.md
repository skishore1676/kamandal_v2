# Kamandal V2

Local-first multileg options portfolio planning and management cockpit.

The current scaffold implements the first execution-grade planning loop:

- env/local runtime control
- Google Sheet configuration cockpit for `universe`, `playbooks`, and `daily_plan`
- seed generation from old `kamandal`
- local idea ingestion from YAML/JSON
- deterministic fixture market data and preflight
- multileg candidate builders and shape validators
- beam-search portfolio plan generation
- local SQLite/audit artifacts
- shadow auto-approval of the top eligible plan
- local transcript import into digest and rough idea YAML
- local IV snapshot history and IV percentile overlay for Public planning

## Sheet

Control sheet:

https://docs.google.com/spreadsheets/d/16Vjgrj80VDeTIGg0y60w4LHenZg7R-tGGvOyLNFdFsE/edit

Tabs:

- `universe`
- `playbooks`
- `daily_plan`

## Runtime

Create and use the project-local venv:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'

.venv/bin/kamandal seed-preview
.venv/bin/kamandal bootstrap-sheet
.venv/bin/kamandal plan --ideas data/ideas/sample.yaml --config-source seed --provider fixture
.venv/bin/kamandal run-shadow-cycle --ideas data/ideas/sample.yaml --config-source seed --provider fixture
.venv/bin/kamandal import-transcripts --source-dir data/transcripts
.venv/bin/python -m pytest tests -q
```

## Control

Runtime control lives in `config/control.yaml` and env overrides, not the sheet.

Current defaults:

- `mode: shadow`
- `trading_enabled: false`
- `halt: false`
- Public runs use broker-reported account size and buying power
- portfolio BPR cap: `90%`
- per-underlying BPR cap: `25%`
- max positions: `5`
- approval mode: `shadow_auto_top_plan`
- missing IV policy: neutral provisional `50` for bootstrap/shadow testing
- playbooks can optionally gate on `iv_percentile`, `iv_rank`, and absolute `iv_abs`

## CLI Surface

- `kamandal pull-sheet`
- `kamandal validate-config`
- `kamandal plan --ideas data/ideas/*.yaml`
- `kamandal write-daily-plan`
- `kamandal run-shadow-cycle`
- `kamandal run-intelligence-cycle`
- `kamandal extract-ideas-llm`
- `kamandal run-llm-cycle`
- `kamandal review-rejections`
- `kamandal public-smoke --symbol TSLA`
- `kamandal capture-iv --config-source sheet --provider public`
- `kamandal iv-status --symbols TSLA NVDA`
- `kamandal import-transcripts`
- `kamandal scrape-youtube-smoke --video-id VIDEO_ID`
- `kamandal fetch-youtube-transcript --video-id VIDEO_ID`
- `kamandal list-youtube-channel-videos --channel-id CHANNEL_ID --limit 1`

Public integration is intentionally conservative at this stage: the fixture adapter is the deterministic test path, and live order submission remains gated off.

## Monday Shadow Runbook

Dry smoke, no sheet write:

```bash
.venv/bin/kamandal public-smoke --symbol TSLA
.venv/bin/kamandal capture-iv --config-source sheet --provider public
.venv/bin/kamandal iv-status --symbols TSLA NVDA SPY
.venv/bin/kamandal run-intelligence-cycle \
  --source-dir data/transcripts/archive/youtube/2026-04-25 \
  --config-source sheet \
  --provider public \
  --no-write-sheet
```

Operator shadow cycle, writes `daily_plan` and local shadow artifacts only:

```bash
.venv/bin/kamandal run-intelligence-cycle \
  --source-dir data/transcripts/archive/youtube/2026-04-25 \
  --config-source sheet \
  --provider public
```

The command imports transcripts as thesis objects, filters extracted symbols to the configured universe, builds Public-preflighted candidates, ranks plan-level bundles, writes `daily_plan`, and records the auto-approved top shadow plan locally. It does not submit live orders.

Transcript extraction emits semantic idea fields such as `direction`, controlled `thesis_tags`, `horizon_days`, `mentioned_strategy`, `extraction_confidence`, and `quote_evidence`. It does not choose executable legs; playbook matching remains deterministic.

The LLM loop keeps the same boundary: Codex CLI extracts thesis objects, optional IV capture updates local volatility history, the deterministic planner builds candidates/plans, and `review-rejections` writes local suggestions only. It never mutates playbooks or submits orders.

## Scheduled Shadow Cadence

The oldmac server uses cron, matching Bhiksha's scheduling style while keeping
Kamandal V2 as short scheduled jobs rather than a long-running daemon:

- `scripts/run_youtube_extraction.sh`: trading days at 9:15, 11:45, and 14:30 Central. Fetches configured YouTube captions and runs Codex LLM extraction into `data/ideas/active`.
- `scripts/run_market_shadow.sh`: every 15 minutes, guarded to trading days and market hours. It validates and reloads `universe`/`playbooks` from Google Sheets on every run, then writes plan rows to `daily_plan`.
- `scripts/run_iv_capture.sh`: trading days at 15:30 Central. Captures one local IV observation per enabled universe symbol from Public option chains.
- `scripts/run_weekly_reviewer.sh`: Fridays at 10:00 Central, reviewing the latest local plan audit only.

Install or refresh the cron schedule on oldmac:

```bash
scripts/cron_install_oldmac.sh
```

The installer writes a marked `KAMANDAL_V2` block in the user's crontab and
removes the older Kamandal V2 LaunchAgents so macOS does not show them as Login
Items. Existing non-Kamandal cron entries are preserved.

Approval behavior is controlled by `execution.approval_mode` in `config/control.yaml`, or by the env override `KAMANDAL_APPROVAL_MODE`. Current shadow automation uses `shadow_auto_top_plan`; live trading still requires `KAMANDAL_MODE=live`, `KAMANDAL_TRADING_ENABLED=true`, no halt, and valid Public preflight.

YouTube can be configured either with explicit video IDs (`KAMANDAL_YOUTUBE_VIDEO_IDS` or `data/youtube_queue.txt`) or with channel IDs (`KAMANDAL_YOUTUBE_CHANNEL_IDS` or `config/youtube_channels.txt`). Channel discovery scans recent same-day videos, scores titles toward market/trade idea content, penalizes educational/tutorial titles, and selects the best `KAMANDAL_YOUTUBE_CHANNEL_LIMIT` videos per channel from `KAMANDAL_YOUTUBE_CHANNEL_SCAN_LIMIT` feed entries. For the current shadow experiment, oldmac is focused on the tastylive/tastytrade channel only so we can learn which show titles produce useful daily ideas.

Transcript fetching defaults to `yt-dlp` in subtitle-only mode: no audio/video download and no `ffmpeg` required. The old `youtube-transcript-api` path remains available with `--provider api`. Slow-fetch controls live in env as `KAMANDAL_YTDLP_SLEEP_REQUESTS`, `KAMANDAL_YTDLP_SLEEP_SUBTITLES`, and `KAMANDAL_YTDLP_ARCHIVE_FILE`. If needed, oldmac can use browser cookies with `KAMANDAL_YTDLP_COOKIES_FROM_BROWSER=safari` or another supported browser.
