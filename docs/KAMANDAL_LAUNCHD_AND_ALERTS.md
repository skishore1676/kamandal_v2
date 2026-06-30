# Kamandal Launchd and Lathi Alerts

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
| `x_bookmarks` | `x-bookmarks` | Weekdays 08:55 |
| `youtube` | `youtube` | Weekdays 09:15, 11:45, 14:30 |
| `my_ideas` | `my-ideas` | Weekdays 08:05, 09:20 |
| `live_reconciliation` | `live-reconciliation` | Weekdays 08:35, 10:30, 12:30, 14:30 |
| `live_advisory` | `live-advisory` | Weekdays 09:25, 11:55, 14:40 |
| `live_approved_orders` | `live-approved-orders` | Weekdays every 5 minutes, 09:00-15:15 |
| `live_management` | `live-management` | Weekdays every 15 minutes, 09:00-15:15 |
| `live_health_report` | `live-health-report` | Weekdays 09:10, 11:45, 14:45, 15:20 |
| `earnings` | `earnings` | Weekdays 08:40 |
| `iv` | `iv` | Weekdays 08:45 |
| `iv_afternoon` | `iv-afternoon` | Weekdays 14:45 |
| `weekly_reviewer` | `weekly-reviewer` | Fridays 10:00 |

There is no `RunAtLoad` for live trading jobs. Manual smoke tests should use
`--force` and `--alert-mode spool`.

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
nonzero on failure.

Safe local smoke:

```bash
PYTHONPATH=src python3 -m kamandal_v2.tools.launchd_job live-health-report --force --alert-mode spool
```

## Lathi Alert Modes

Operational receipts and launchd failure alerts go through
`kamandal_v2.ops.alerts.send_lathi_alert`.

| Mode | Behavior |
| --- | --- |
| `off` | Do not attempt an alert. |
| `spool` | Write/send through Lathi without `--live`; useful for smoke tests. |
| `live` | Pass `--live` and require Lathi receipt `network_call_performed=true`. |

Defaults:

```bash
KAMANDAL_LAUNCHD_ALERT_MODE=live
KAMANDAL_LATHI_PROFILE=jarvis-northstar
```

Optional overrides:

```bash
KAMANDAL_LATHI_BUS_CMD='python3 -m lathi_bus.cli'
KAMANDAL_LATHI_BUS_CWD=/Users/sunny/code/lathi-bus
KAMANDAL_ALERT_TIMEOUT_SECONDS=30
```

The alert layer redacts token fields, bearer strings, and URL auth parameters
before storing command output.

## Operator Review Behavior

Reconciliation auto-repairs that are proven safe are applied locally and reported
as receipts. Ambiguous reconciliation issues become operator review requests.

Operator review defaults to Lathi `telegram-ask`:

```yaml
live:
  operator_review:
    transport: lathi
    lathi_profile: jarvis-northstar
```

The review card contains bounded actions such as `dismiss`, `hold`, and
`retire_local`. The fallback text remains:

```text
kamandal review <request_id> <action> [note]
```

Live entry approvals are intentionally still on the existing approval path. Do
not move those to Lathi button collection until Lathi is the sole poller for that
Telegram bot token.

## Oldmac Verification

After deployment:

```bash
cd /Users/sunny/Documents/kamandal_v2
git rev-parse --short HEAD
PYTHONPATH=src .venv/bin/python -m pytest tests/test_ops_alerts.py tests/test_launchd_job.py tests/test_operator_review_reconciliation.py tests/test_live_health.py -q
cd /Users/sunny/code/lathi-bus && python3 -m lathi_bus.cli doctor --profile jarvis-northstar
cd /Users/sunny/code/lathi-bus && python3 -m lathi_bus.cli telegram-doctor --profile jarvis-northstar
cd /Users/sunny/Documents/kamandal_v2 && PYTHONPATH=src .venv/bin/python -m kamandal_v2.tools.launchd_job live-health-report --force --alert-mode spool
scripts/launchd/install_kamandal_launchd.sh install
launchctl list | grep com.kamandal.v2
.venv/bin/kamandal live-health --json
```

Log tails live under:

```text
data/logs/launchd/com.kamandal.v2.<label>.out.log
data/logs/launchd/com.kamandal.v2.<label>.err.log
```

