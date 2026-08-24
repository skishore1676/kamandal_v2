# Kamandal V2

Local-first live multileg options portfolio planning, execution, and management cockpit. Kamandal V2 fuses LLM-assisted idea extraction with deterministic construction, Public-broker preflight, portfolio/risk gates, live execution, reconciliation, and exit management. Historical shadow evidence remains available but the active operating lane is live.

## Architecture & Workflow

Kamandal operates through a strictly bounded pipeline designed to keep the AI creative on idea extraction but mathematically rigorous on options execution.

The canonical north-star and the bounded single-engine cutover are documented in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). CSA is temporary implementation
scaffolding, not a permanent product lane: the target is one portfolio planner,
one strategy lifecycle engine, and shadow/live execution adapters.

1. **Intelligence Gathering**
   - Source content (YouTube video captions via `yt-dlp` and X/Twitter bookmarks/timelines) are fetched and ingested locally.
   - Raw texts are stored in `data/transcripts/` or staged in `data/digest/`.

2. **LLM Extraction**
   - Agent Broker routes the configured LLM to extract abstract trading ideas (e.g., "Bullish SPY, 7 days, mean-revert thesis").
   - The LLM **never** picks options legs or sees the option chain or your strategy templates. 
   - Extracted ideas are output as structured YAML files into the `data/ideas/` directory.

3. **Deterministic Planning**
   - A Python-based deterministic planner maps the LLM's generic ideas against your predefined **Playbooks** (strategy templates defined by you in your configuration).
   - It captures current Implied Volatility (IV) and live option chains to mathematically construct executable option candidates (e.g., Call Debit Spreads, Iron Condors).
   - Put/call spread construction can optionally search a set of widths (`planner.vertical_width_search` in `control.yaml`, off by default) instead of a single fixed width, keeping the narrowest construction that clears both the playbook's credit-to-width gate and the structure's per-order BPR cap — see `docs/CANDIDATE_GATE_SEARCH.md`.

4. **Portfolio Optimization**
   - Candidates are evaluated and grouped into "Plans". A beam-search portfolio optimizer selects the best combination of trades that maximize the overall score while strictly respecting your Buying Power Reduction (BPR) limits and max position caps.
   - In the live lane, `auto_top_plan` makes only the rank-1 eligible plan available to the guarded submission path.

5. **Reporting & Review**
   - **Intraday:** Three RYG reports summarize app, live-book, and retained shadow evidence through Lathi Bus.
   - **Historical shadow:** Legacy EOD artifacts remain evidence; the active shadow collector is retired.
   - **Weekly Review:** Every Friday, the LLM analyzes all *rejected* candidates from the week (e.g., rejected by the planner due to low IV or poor liquidity) and outputs suggestions to help you tune your Playbooks.

## Configuration & Control

- **Playbooks & Universe:** Strategy parameters, composition, and deployment stage are managed remotely in a Google Sheet (`universe`, `playbooks`, `daily_plan`). Kamandal owns the reusable capability; a playbook row owns whether it is baseline, shadow, pilot-live, or live. See [Strategy Promotion Loop](docs/STRATEGY_PROMOTION_LOOP.md).
- **Runtime Rules:** Controlled locally via `config/control.yaml` and environment variables.
  - The default and all current live strategy venues are Public. The shadow
    short-strangle row is frozen to `tasty_primary` for future-entry testing;
    changing that row does not reroute existing positions.
  - Tastytrade also supplies selected market metrics. Its order/account path is
    fail-closed until an explicit account is configured and the separate live
    promotion gate is approved.
  - The checked-in posture is live and trading-enabled. Oldmac environment overrides remain part of runtime truth.
  - Configures BPR caps, concentration limits, automated live selection/submission, optional alternate approval modes, and retained shadow behavior.

For undefined-risk short strangles, broker preflight BPR is authoritative. The
local formula is retained only as a labeled fallback when the broker omits BPR.
Additional symbols already enabled in the operator universe may reach the
short-strangle playbook only when that playbook's Sheet-owned expansion switch,
underlying-price bounds, and IV-rank bounds allow it. Existing explicit permissions
remain valid outside that overlay. The repository supplies no fallback policy values. See
[docs/STRANGLE_BPR_AND_ELIGIBILITY.md](docs/STRANGLE_BPR_AND_ELIGIBILITY.md).

## Local Data Architecture

The `data/` folder stores all persistent state:
- `audit/`: Local artifacts/audit logs for shadow plans.
- `ideas/`: Extracted YAML thesis objects from the LLM.
- `kamandal_v2.db`: The main active SQLite database storing positions and history.
- `logs/`: Scheduled job logs for debugging, including launchd logs under `data/logs/launchd/`.
- `reports/`: End-of-day shadow portfolio summaries.
- `reviews/`: Weekly LLM reviews of rejected candidates.
- `sheet_cache/`: Offline cache of your Google Sheet rules to prevent API rate limits.

## Scheduled Cadence (Launchd)

The system is designed as a series of short scheduled jobs rather than a long-running daemon:
- **X Extraction:** Weekdays morning (8:55 AM).
- **Universe Proposer:** Weekdays 8:50 AM; appends at most five disabled, evidence-backed proposal rows.
- **YouTube Extraction:** Intraday sweeps (9:15, 11:45, 14:00) so the final
  intelligence batch is available to the 14:30 live advisory.
- **Live Reconciliation:** Intraday broker/local ledger checks before advisory and management cycles.
- **Live Advisory:** Three intraday planning passes.
- **Live Approved Orders:** Every 5 minutes during the live market window.
- **Live Management:** Every 15 minutes during the live market window.
- **Live Health Report:** Morning, midday, afternoon, and close readbacks through Lathi Bus when operator attention is needed.
- **Daily Report:** 9:10, 11:45, and 14:45 CT RYG operational readbacks through Lathi Bus.
- **Scheduled Job Health:** Every 15 minutes during the live market window; watches launchd logs for stale, missing, or failed Kamandal jobs.
- **Weekly Reviewer:** Fridays at 10:00 AM.

To install or refresh the oldmac schedule:

```bash
scripts/launchd/install_kamandal_launchd.sh install
```

`scripts/cron_install_oldmac.sh` is now a compatibility wrapper that installs launchd labels and removes the old marked Kamandal cron block. See [docs/KAMANDAL_LAUNCHD_AND_ALERTS.md](docs/KAMANDAL_LAUNCHD_AND_ALERTS.md).

For the broader Control Tower boundary across Kamandal, Lathi, and Lathi Bus,
see [docs/lathi_control_tower_kamandal_jobs.md](docs/lathi_control_tower_kamandal_jobs.md).

## Key CLI Commands

- `.venv/bin/kamandal run-intelligence-cycle` - Import transcripts, build plans, and optimize portfolio.
- `.venv/bin/kamandal run-shadow-cycle` - Run the deterministic shadow planner locally.
- `.venv/bin/kamandal shadow-eod-report` - Generate the daily shadow portfolio mark-to-market report.
- `.venv/bin/kamandal review-rejections` - Trigger the LLM weekly reviewer.
- `.venv/bin/kamandal public-smoke --symbol TSLA` - Dry run option chains against a provider.
- `.venv/bin/kamandal live-health` - Print the live book health status (GREEN/YELLOW/RED) with reasons.
- `.venv/bin/kamandal live-book --write-sheet` - Refresh the per-position cockpit rows in the `live_book` sheet tab.
- `PYTHONPATH=src python3 -m kamandal_v2.tools.launchd_job live-health-report --force --alert-mode spool` - Safe launchd/Lathi smoke test without broker submission.
- [docs/RISK_MANAGER.md](docs/RISK_MANAGER.md) - Current and future design notes for the disabled-by-default live entry risk manager.

## Live Health: Operator Playbook

The `_HEALTH_` row at the top of the `live_book` sheet tab (and `kamandal live-health`) summarizes
whether the system can safely manage the book. New live entries are blocked while RED.
The system self-heals everything it can; a status only persists when it genuinely needs you.

**GREEN** — no action. Entries flow.

**YELLOW** — informational; check in once a day:
- `working_close_order` — a close is in flight at the broker. No action.
- `exit_pipeline_pending` — the ledger has an approved close waiting for the
  submitter. No action unless it becomes RED.
- `position_target_reached` — profit target hit; a close should appear within a cycle. If it
  persists more than ~2 cycles, see the position row for what blocked it.
- `close_order_stale` — a close has been working longer than `stale_close_order_minutes`.
  Check the order in the Public app; consider cancelling/repricing it manually.
- `stale_failed_close_order` — a close failed but the exit condition has since lapsed. The next
  order-reconciliation cycle retires it automatically; if it persists, reconciliation isn't running.

**RED** — act today; entries stay blocked until resolved:
- `failed_preflight_close` / `failed_close_order` — the system wants out of a position and the
  broker keeps refusing (see `broker_error_code` on the position row, e.g. 157 = quantity
  mismatch / pending order conflict). Action: close the position manually in the Public app,
  or cancel the conflicting working order so the next cycle's close can pass preflight.
- `exit_pipeline_stalled` — management approved an auto close locally, but
  `live-approved-orders` did not drain it within
  `KAMANDAL_LIVE_HEALTH_EXIT_PIPELINE_STALLED_MINUTES`. Action: check the
  launchd log for `live_approved_orders`; the broker may not have received any
  order yet.
- `reconciliation_blocker` — local book and broker disagree about what you own. Known order
  replacement lineages, transient post-fill lag, and confirmed ghosts are repaired automatically.
  Action is required only if the bounded review says ownership or structure is genuinely ambiguous;
  see [the reconciliation contract](docs/LIVE_RECONCILIATION.md).
- `loss_watch` — a position crossed its max-loss multiple; an auto-close fires after the
  confirmation mark. No action needed unless its close then fails (which turns into the case above).

## Lathi Notifications

Attention alerts and launchd failures go through Lathi Bus by default:

- `KAMANDAL_LAUNCHD_ALERT_MODE=live` sends Telegram notifications and requires Lathi Bus to confirm a network send.
- `KAMANDAL_LAUNCHD_ALERT_MODE=spool` is for dry-run/smoke checks.
- `KAMANDAL_LATHI_BUS_PROFILE=kamandal-northstar` is the default Lathi Bus profile; `KAMANDAL_LATHI_PROFILE` remains a legacy alias.

Healthy, progressing, and self-handled work stays in SQLite, launchd logs, CLI
JSON, and Control Tower instead of Telegram. Order submission, fill, reprice,
cancel, and successful auto-repair are operational facts, not notifications.
Kamandal waits through the configured broker-flat confirmation window before a
ghost position can become a review request. Unresolved ambiguous issues are
persisted once; Lathi owns their single Telegram projection and callback. The
fallback command remains `kamandal review <request_id> <action> [note]`.
