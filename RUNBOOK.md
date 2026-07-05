# Kamandal V2 — Runbook

## Purpose

Local-first options planning/execution cockpit: LLM idea extraction (bounded,
never sees option chains) -> deterministic Playbook planner -> Tastytrade
execution. Runs unattended via launchd on oldmac (uid 501).

## launchd jobs + restart commands

Restart: `launchctl kickstart -k gui/501/<label>` (`-k` kills+re-runs; omit
`-k` to trigger without killing an in-flight run). Non-live jobs are safe to
kickstart; **live_* jobs are trading actions**.

12 jobs, label prefix `com.kamandal.v2.`: earnings, iv, iv_afternoon,
live_advisory, **live_approved_orders** (LIVE), live_health_report,
**live_management** (LIVE), **live_reconciliation** (LIVE), my_ideas,
scheduled_job_health (Mon/Tue 09:15 CT — also runs **log rotation**, see
below; Tower shows effective_enabled=false even though launchd runs it fine),
weekly_reviewer, x_bookmarks, youtube.

Bridge (Lathi Control Tower):
- Status: `.venv/bin/python -m kamandal_v2.tools.launchd_status --json`
- Action: `.venv/bin/python -m kamandal_v2.tools.launchd_control <action> --json ...`
- `retry-job` only supports `--job x-bookmarks`/`--job youtube`; kickstarts
  the launchd label. Lock is scoped per `--job` (fixed 2026-07-05) —
  previously a bare `retry-job` lock let concurrent retries for *different*
  jobs collide (one got lock_busy, the other hung past the caller's timeout).

## Tests

```
PYTHONPATH=src .venv/bin/python -m pytest -q
```

## Log paths

- Live: `data/logs/launchd/<label>.{out,err}.log`.
- Legacy/retired: top-level `data/logs/*.log` (`cron_*.log`, old
  `com.kamandal.v2.*.log`) — no active job writes here; historical debris.
- Archive: `data/logs/archive/*.gz` (rotation output, never auto-deleted).
- Rotation (`kamandal_v2.ops.log_rotation`) runs every `scheduled-job-health`
  invocation (any day, before the trading-calendar gate): top-level `*.log`
  older than 14 days -> gzip-archived + truncated in place;
  `data/logs/launchd/*.log` archived once it exceeds 10MB regardless of age.
  Nothing is ever deleted.

## Health verification

- Tower: kamandal source + KAM-01 external_health should read `green`.
- Per-unit: `launchd_status --json` -> `units[].last_run_status`.
- Manual: `PYTHONPATH=src .venv/bin/python -m kamandal_v2.tools.launchd_job scheduled-job-health --force`
  (also triggers rotation; `--force` bypasses the weekend/holiday skip and
  WILL send a live Telegram alert if issues exist — don't run casually).
- `du -sh data/logs/` should stay well under 100MB once rotation is live.

## DANGER ZONES (verbatim from audit)

- **Live trading**: Tastytrade session/account (`config/tastytrade_session.json`,
  `config/tastytrade_account.json`); `live_approved_orders`/`live_management`/
  `live_reconciliation` touch real orders. Never kickstart live_* jobs or
  touch config/.
- **KAM-07 disabled**: `scheduled_job_health` is `declared_enabled=true` but
  `effective_enabled=false` in the Tower's own view — a watch-the-watchman
  gap. Don't assume Tower green means the health job ran; don't guess-fix the
  effective_enabled flag.
- **Never touch**: `src/kamandal_v2/live/{orders,execution,approval}.py`,
  the `live_approved_orders` job, or any `config/*session*.json`.
