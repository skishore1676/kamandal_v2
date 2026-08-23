# Kamandal Launchd and Lathi Bus Alerts

Kamandal scheduled work is owned by Kamandal launchd labels on oldmac. Lathi Bus
is the notification and bounded-decision transport. Trading logic stays inside
the existing Kamandal CLIs and live modules.

## Install and Uninstall

Install or refresh the oldmac schedule:

```bash
cd /Users/sunny/Documents/kamandal_v2
scripts/launchd/install_kamandal_launchd.sh install
```

Compatibility wrappers call the same installer:

```bash
scripts/launchd_install_oldmac.sh
scripts/cron_install_oldmac.sh
```

Uninstall Kamandal launchd labels:

```bash
scripts/launchd/install_kamandal_launchd.sh uninstall
```

Remove only the legacy Kamandal cron block:

```bash
scripts/launchd/install_kamandal_launchd.sh uninstall-cron
```

The installer writes plists to `~/Library/LaunchAgents`, logs to
`data/logs/launchd/`, loads with `launchctl bootstrap`, and removes only the
marked `BEGIN KAMANDAL_V2` cron block after labels are installed.

## Launchd Jobs

All times are oldmac local time, expected to be America/Chicago.

| Label suffix | Job | Schedule |
| --- | --- | --- |
| `x_bookmarks` | `x-bookmarks` | Weekdays 08:15 |
| `youtube` | `youtube` | Weekdays 09:15, 11:45, 14:00 |
| `my_ideas` | `my-ideas` | Weekdays 08:05, 09:20 |
| `live_reconciliation` | `live-reconciliation` | Weekdays 08:35, 10:30, 12:30, 14:10 |
| `unified_planning` | `unified-planning` | Weekdays 08:50, 09:25, 11:55, 14:15 |
| `live_approved_orders` | `live-approved-orders` | Weekdays every 5 minutes, 08:30-15:15 |
| `unified_lifecycle_management` | `unified-lifecycle-management` | Weekdays every 5 minutes, 08:30-15:15 |
| `live_health_report` | `live-health-report` | Weekdays 09:10, 11:45, 14:45, 15:20 |
| `scheduled_job_health` | `scheduled-job-health` | Weekdays every 15 minutes, 09:15-15:45 |
| `daily_report` | `daily-report` | Weekdays 09:10, 11:45, 14:45, 15:25 |
| `earnings` | `earnings` | Weekdays 08:40 |
| `iv` | `iv` | Weekdays 08:45 |
| `iv_afternoon` | `iv-afternoon` | Weekdays 13:45 |
| `weekly_reviewer` | `weekly-reviewer` | Fridays 10:00 |

There is no `RunAtLoad` for live trading jobs. Manual smoke tests should use
`--force` and `--alert-mode spool`.

CSA shadow plists are rendered with `Disabled=true` and the installer preserves
that disabled state. Enabling the three sidecars is a separate protected
deployment action after Sheet policy and database migration readback.

The late-day dependency chain is deliberate: afternoon IV at 13:45, final
YouTube intelligence at 14:00, reconciliation at 14:10, unified planning at
14:15, and the first post-plan executor pass at 14:20. Regular-session opening
orders stop at 14:40, preserving a 20-minute buffer before the 15:00 close.
Lifecycle management remains entry-independent and continues every five minutes
through the final pre-close exit evaluation.

### Shadow evidence status

The legacy `market-shadow` and `shadow-eod-report` labels are intentionally not
part of the active launchd registry. Their existing database rows and EOD files
are historical evidence, not proof that a shadow collector is currently
running.

`python -m kamandal_v2.tools.launchd_status --json` therefore exposes
`shadow_evidence` with:

- a current collector state derived from the app-owned launchd registry
  (`staged_disabled` for the new CSA jobs until the protected enable step);
- aggregate historical fill counts and last fill, mark, and EOD timestamps;
- stable semantic hashes that exclude the observation timestamp;
- `alpha_eligible=false`; and
- an all-false protected-effects receipt.

This lets TradeLab distinguish “retired collector with historical data” from a
broken or stale active feed. Re-enabling shadow collection is a separate
operator decision and must add an app-owned job back to the registry.

## Runner Contract

The launchd runner is:

```bash
PYTHONPATH=src python3 -m kamandal_v2.tools.launchd_job <job>
```

It prints exactly one machine-readable result line:

```text
KAMANDAL_LAUNCHD_JOB={...}
```

The runner skips non-trading days unless `--force` is passed, captures stdout and
stderr tails, sends a Lathi failure alert when a scheduled job fails, and returns
nonzero on failure. Scheduled-job health fingerprints an open failure, sends it
once while unchanged, and clears that incident after recovery.

The YouTube job treats "no usable transcripts available" as a clean no-op by
default. It still discovers videos and attempts transcript fetches, but a
zero-transcript day should not leave Control Tower stuck red after all safe
intake attempts are exhausted. Set
`KAMANDAL_YOUTUBE_EMPTY_TRANSCRIPTS_STATUS=failed` to make zero-transcript days
fail closed during debugging.

Safe local smoke:

```bash
PYTHONPATH=src python3 -m kamandal_v2.tools.launchd_job live-health-report --force --alert-mode spool
```

## Control Actions

Lathi Control Tower should call Kamandal through the app-owned control contract:

```bash
PYTHONPATH=src python3 -m kamandal_v2.tools.launchd_control <action> --json
```

Kamandal currently supports these non-broker operational actions:

| Action | Purpose |
| --- | --- |
| `live-status` | Read current live-health state. |
| `scheduled-job-health-now` | Run scheduled-job health immediately. |
| `live-health-report-now` | Run the live-health report immediately. |
| `daily-report-now` | Build and deliver the current RYG daily report. |
| `send-pending-review-requests` | Re-send pending bounded review cards. |
| `apply-review-decision` | Apply a bounded reconciliation decision after Kamandal revalidates it. |
| `retry-job` | Re-run a safe intelligence job. |

`retry-job` is intentionally restricted to intelligence ingestion jobs that do
not submit, cancel, replace, or close broker orders:

```bash
PYTHONPATH=src python3 -m kamandal_v2.tools.launchd_control retry-job --job x-bookmarks --json
PYTHONPATH=src python3 -m kamandal_v2.tools.launchd_control retry-job --job youtube --json
```

The retry is a fast trigger, not a completion wait. Kamandal validates the job,
kicks the matching launchd label, and returns `status=triggered` for Lathi's
action journal. On dev machines without a loaded launchd label, the command can
fall back to a detached local runner and returns the spawned pid/log paths.
Later success or failure is reported by the normal scheduled-job status rows and
launchd logs.

## Lathi Bus Alert Modes

Operator-attention and launchd failure alerts go through
`kamandal_v2.ops.alerts.send_lathi_alert`.

| Mode | Behavior |
| --- | --- |
| `off` | Do not attempt an alert. |
| `spool` | Write/send through Lathi without `--live`; useful for smoke tests. |
| `live` | Pass `--live` and require Lathi receipt `network_call_performed=true`. |

Defaults:

```bash
KAMANDAL_LAUNCHD_ALERT_MODE=live
KAMANDAL_LATHI_BUS_PROFILE=kamandal-northstar
```

Optional overrides:

```bash
KAMANDAL_LATHI_BUS_CMD='python3 -m lathi_bus.cli'
KAMANDAL_LATHI_BUS_CWD=/Users/sunny/code/lathi-bus
KAMANDAL_ALERT_TIMEOUT_SECONDS=30
KAMANDAL_ALERT_BODY_MAX_CHARS=3200
```

`KAMANDAL_LATHI_PROFILE` remains a legacy alias for older wrappers. New
configuration should say `LATHI_BUS` so it is not confused with the separate
Lathi job-runner application.

The alert layer redacts token fields, bearer strings, and URL auth parameters
before storing command output. Kamandal also compacts long alert bodies before
they reach Lathi Bus. The alert should carry the job name, root error, and log
path, not a full traceback. Full stdout/stderr remains in
`data/logs/launchd/`.

Ownership split:

- Kamandal owns semantic alert shaping: what deserves attention, what summary is
  useful, and where the complete app-owned logs live.
- Lathi Bus should still keep a transport-level hard cap so one verbose app can
  never make Telegram delivery fail. That shared cap belongs in Lathi Bus, not
  in every app, but Kamandal should not wait for it before sending compact
  domain alerts.

## Attention Policy

Routine proof belongs in the ledger, logs, and Control Tower. Telegram is an
attention surface, not an execution feed.

The paging predicate is:

```text
unresolved
AND human_action_required
AND (recovery_exhausted OR no_safe_auto_action)
AND not_duplicate
```

Google Sheets reads and writes use bounded retry before a launchd cycle is
declared failed. The defaults are three attempts with 1s/2s exponential delay,
capped at 4s. Only rate limits, server failures (`429/500/502/503/504`), and
transient transport errors retry; permanent request/configuration errors still
fail closed immediately. Configure with `google_sheets.retry` or:

```bash
KAMANDAL_SHEETS_RETRY_ATTEMPTS=3
KAMANDAL_SHEETS_RETRY_BASE_DELAY_SECONDS=1
KAMANDAL_SHEETS_RETRY_MAX_DELAY_SECONDS=4
```

- Healthy `live-health-report` runs print `KAMANDAL_LAUNCHD_JOB={...}` and do
  not send a message.
- Successful order submission, fill, intermediate reprice, cancellation, and
  auto-repair do not send messages. Their normal command output and SQLite
  records remain the audit trail. One exception is an entry workflow that ends
  without a position: after the broker confirms the terminal unfilled status,
  Kamandal sends one informational summary containing the symbol, structure,
  attempt count, limit path, and explicit `no live position was opened` result.
  This terminal summary is claimed by an atomic ledger transition, so competing
  sync cycles cannot send duplicates.
  `KAMANDAL_ENTRY_TERMINAL_RECEIPT_ENABLED=false` disables it, and
  `KAMANDAL_ENTRY_TERMINAL_RECEIPT_MODE=spool` exercises the projection without
  a network send.
- A selected opening ticket that has aged past its preflight window gets one
  bounded recovery attempt. Kamandal rebuilds the current rank-1 plan from
  current-day ideas and fresh market/account data, reruns health and risk
  gates, performs a fresh broker preflight, and submits at most once. A
  successful rebuild stays silent. If rebuilding or placement still fails,
  Kamandal sends one deduplicated attention alert and records that no position
  opened. A risk cap that merely exists remains self-handled; a cap that blocks
  the auto-selected entry uses this same attention path.
- Live health performs bounded self-healing for stale local entry approvals
  before it scores the book. A prior-market-day `pending_approval` entry ticket
  is retired locally as `retired_stale_entry_approval`; it is not a broker
  action and should disappear from Control Tower/Blackboard on the next Lathi
  projection. Pending lower-ranked entries under `auto_top_plan` are also
  self-handled and do not page.
- RED is a safety classification, not by itself a paging decision. Events
  marked `self_healing` or `self_handled` remain silent. A RED event without
  recovery metadata still fails safe and alerts.
- YELLOW live health alerts only for configured operator-action reasons. The
  default is `close_order_stale`.
- Reconciliation review requests use the dedicated external-review surface, so
  the live-health reporter does not send a second Telegram message for the same
  blocker.
- Live-health incidents have a stable fingerprint. An unchanged open incident
  sends once, remains visible in Control Tower, and can notify again only after
  it clears or its affected reason/group/order changes.
- `exit_pipeline_stalled` is RED. It means policy approved a close locally but
  `live-approved-orders` did not submit it within
  `live.health.exit_pipeline_stalled_minutes`.
- `urgent_close_order_stale` is RED. It means a broker-working `max_loss` or
  `pre_event` close is still open past
  `live.health.urgent_close_order_stale_minutes`.
- `scheduled-job-health` alerts when a launchd job is missing, stale, or failed
  in the expected window.

Override the YELLOW reasons with:

```bash
KAMANDAL_HEALTH_NOTIFY_REASONS=close_order_stale
```

## Live Exit Pipeline

Live exit submission has two modes:

```yaml
live:
  exit_submit_source: sheet   # sheet | ledger
```

`sheet` is the legacy bridge: `live-management` writes
`APPROVE_LIVE_CLOSE` rows and `live-approved-orders` reads them. It is kept as a
rollback path.

`ledger` is the preferred live mode: when `exit_approval_mode=auto_rules`,
`live-management` writes approved close intents directly to SQLite as
`approved_close_pending_submit`; `live-approved-orders` drains every eligible
close up to `live.max_close_submits_per_run`. The Sheet becomes a projection,
not the execution queue.

Close lifecycle vocabulary:

| Status | Meaning |
| --- | --- |
| `approved_close_pending_submit` | Local policy approved the exit; submitter should drain it. |
| `submitted` / `repriced` | Broker acknowledged a working close. |
| `exit_pipeline_pending` | Health sees local pending pipeline state, not broker risk. |
| `exit_pipeline_stalled` | Local approved close did not drain fast enough; check `live-approved-orders` logs. |
| `urgent_close_order_stale` | Health reason for an urgent broker-working close that has not filled fast enough. |
| `expired_eod` | DAY close was cancelled/expired; management can re-stage next session. |
| `expired_stale_close_approval` | Local close intent was never submitted and aged past the stale approval window. |
| `rejected_by_operator` | Operator rejected a local not-yet-submitted close using `REJECT_CLOSE`. |

Profit-target close repricing is sign-aware and floor-aware. The floor is the
minimum acceptable close cashflow, computed from the larger of
`exit_pricing.min_profit_to_trigger` and
`target_profit * exit_pricing.profit_floor_pct / 100`. For credit spreads this
prevents repricing through the maximum acceptable close debit; for debit/long
premium positions it prevents repricing below the minimum acceptable close
credit. The full close ticket stores both net values and broker limit prices so
future reprice attempts do not have to infer strategy direction from shape.

Operator commands:

- `APPROVE_LIVE_CLOSE` remains the gate only for `exit_approval_mode=sheet_approval`.
- `REJECT_CLOSE` in the `daily_plan` row retires a local, not-yet-submitted
  ledger close. It does **not** cancel a broker-working order.

On oldmac, the live Kamandal profile currently sends through the Jasper receipt
bot (`jasper_receipts`) with `live_send_enabled=true`. This is a transitional
compatibility path. The target architecture is documented in
[`lathi_control_tower_kamandal_jobs.md`](lathi_control_tower_kamandal_jobs.md):
Lathi owns live decision collection, Lathi Bus owns the Telegram/Obsidian
surface protocol, and Lane Host is retired from the Kamandal operator path.

## Operator Review Behavior

Reconciliation auto-repairs that are proven safe are applied locally and stay in
the ledger. A broker-flat ghost remains `pending_confirmation` until
`broker_flat_confirmations_required` is met, then auto-retires without paging.
Only an ambiguity that remains after the available recovery policy becomes an
operator review request.

Kamandal persists the guarded request; Lathi's external-review sync owns the
single Telegram card and callback. The direct Lathi Bus sender remains a manual
fallback for `send-pending-review-requests`, configured by:

```yaml
live:
  operator_review:
    transport: lathi_bus
    lathi_profile: kamandal-northstar
```

The review card contains bounded actions such as `dismiss`, `hold`, and
`retire_local`. The fallback text remains:

```text
kamandal review <request_id> <action> [note]
```

Live entry approval sends also support Lathi Bus `telegram-ask`. Button presses
should be collected by a Lathi-owned collector after the sole-poller cutover.
Until that cutover is complete, fallback text/manual CLI approval remains the
decision path.

## Oldmac Verification

After deployment:

```bash
cd /Users/sunny/Documents/kamandal_v2
git rev-parse --short HEAD
PYTHONPATH=src .venv/bin/python -m pytest tests/test_ops_alerts.py tests/test_launchd_job.py tests/test_operator_review_reconciliation.py tests/test_live_health.py -q
cd /Users/sunny/code/lathi-bus && python3 -m lathi_bus.cli doctor --profile kamandal-northstar
cd /Users/sunny/code/lathi-bus && python3 -m lathi_bus.cli telegram-doctor --profile kamandal-northstar
cd /Users/sunny/Documents/kamandal_v2 && PYTHONPATH=src .venv/bin/python -m kamandal_v2.tools.launchd_job live-health-report --force --alert-mode spool
cd /Users/sunny/Documents/kamandal_v2 && PYTHONPATH=src .venv/bin/python -m kamandal_v2.tools.launchd_job scheduled-job-health --force --alert-mode spool
scripts/launchd/install_kamandal_launchd.sh install
launchctl list | grep com.kamandal.v2
.venv/bin/kamandal live-health --json
```

Log tails live under:

```text
data/logs/launchd/com.kamandal.v2.<label>.out.log
data/logs/launchd/com.kamandal.v2.<label>.err.log
```
