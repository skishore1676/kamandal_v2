# Kamandal V2

Local-first multileg options portfolio planning and management cockpit. Kamandal V2 is an automated "shadow" trading and intelligence extraction system that fuses LLM-driven idea generation with strict deterministic execution.

## Architecture & Workflow

Kamandal operates through a strictly bounded pipeline designed to keep the AI creative on idea extraction but mathematically rigorous on options execution.

1. **Intelligence Gathering**
   - Source content (YouTube video captions via `yt-dlp` and X/Twitter bookmarks/timelines) are fetched and ingested locally.
   - Raw texts are stored in `data/transcripts/` or staged in `data/digest/`.

2. **LLM Extraction**
   - The Codex LLM reads the raw text and extracts abstract trading ideas (e.g., "Bullish SPY, 7 days, mean-revert thesis"). 
   - The LLM **never** picks options legs or sees the option chain or your strategy templates. 
   - Extracted ideas are output as structured YAML files into the `data/ideas/` directory.

3. **Deterministic Planning**
   - A Python-based deterministic planner maps the LLM's generic ideas against your predefined **Playbooks** (strategy templates defined by you in your configuration).
   - It captures current Implied Volatility (IV) and live option chains to mathematically construct executable option candidates (e.g., Call Debit Spreads, Iron Condors).

4. **Portfolio Optimization**
   - Candidates are evaluated and grouped into "Plans". A beam-search portfolio optimizer selects the best combination of trades that maximize the overall score while strictly respecting your Buying Power Reduction (BPR) limits and max position caps.
   - The top plan is written out to `daily_plan` and auto-approved for shadow execution. 

5. **Reporting & Review**
   - **End-of-Day (EOD):** A deterministic script marks the shadow portfolio to market and calculates P&L.
   - **Weekly Review:** Every Friday, the LLM analyzes all *rejected* candidates from the week (e.g., rejected by the planner due to low IV or poor liquidity) and outputs suggestions to help you tune your Playbooks.

## Configuration & Control

- **Playbooks & Universe:** Strategy parameters and tracked tickers are securely managed remotely in a Google Sheet (`universe`, `playbooks`, `daily_plan`).
- **Runtime Rules:** Controlled locally via `config/control.yaml` and environment variables.
  - Live broker submission defaults to Tastytrade but is strictly gated (`trading_enabled: false`, `mode: shadow` by default).
  - Configures BPR caps, max positions, and shadow auto-approval modes.

## Local Data Architecture

The `data/` folder stores all persistent state:
- `audit/`: Local artifacts/audit logs for shadow plans.
- `ideas/`: Extracted YAML thesis objects from the LLM.
- `kamandal_v2.db`: The main active SQLite database storing positions and history.
- `logs/`: Cron execution logs for debugging.
- `reports/`: End-of-day shadow portfolio summaries.
- `reviews/`: Weekly LLM reviews of rejected candidates.
- `sheet_cache/`: Offline cache of your Google Sheet rules to prevent API rate limits.

## Scheduled Cadence (Cron)

The system is designed as a series of short scheduled jobs rather than a long-running daemon:
- **X Extraction:** Weekdays morning (8:55 AM).
- **YouTube Extraction:** Intraday sweeps (9:15, 11:45, 14:30).
- **Market Shadow Loop:** Every 15 minutes during market hours. Reloads sheets, builds candidates, optimizes plans.
- **EOD Report:** After market close.
- **Weekly Reviewer:** Fridays at 10:00 AM.

*To install the cron schedule on oldmac: run `scripts/cron_install_oldmac.sh`*

## Key CLI Commands

- `.venv/bin/kamandal run-intelligence-cycle` - Import transcripts, build plans, and optimize portfolio.
- `.venv/bin/kamandal run-shadow-cycle` - Run the deterministic shadow planner locally.
- `.venv/bin/kamandal shadow-eod-report` - Generate the daily shadow portfolio mark-to-market report.
- `.venv/bin/kamandal review-rejections` - Trigger the LLM weekly reviewer.
- `.venv/bin/kamandal public-smoke --symbol TSLA` - Dry run option chains against a provider.
