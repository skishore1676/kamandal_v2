---
title: Page on exhausted recovery, not workflow status
type: decision
area: live operations and Telegram notifications
date: 2026-07-16
tags: [alerts, reconciliation, lathi, operations]
refs: [scripts/run_live_management.sh:19, scripts/run_live_approved_orders.sh:17, src/kamandal_v2/live/reconciliation.py:575, src/kamandal_v2/tools/launchd_job.py:265]
---

# Page on Exhausted Recovery, Not Workflow Status

## Context

Normal live execution receipts and transient post-fill reconciliation mismatches
were reaching Telegram even though Kamandal could finish or repair the work
without operator help. The same unresolved health state could also be sent again
on every scheduled report.

## What We Learned

Severity and progress are not operator attention. Beacon should fire only when a
condition is unresolved, a human action is required, and safe automatic recovery
is exhausted or unavailable. Everything else belongs in SQLite, launchd logs,
CLI output, and Control Tower.

## Why / When It Applies

This applies to execution, reconciliation, health, and future Kamandal operator
surfaces. A RED state may be safely fail-closed or self-handled; an order fill may
be important evidence but require no action. Paging must therefore use recovery
and operator-state metadata rather than color or lifecycle milestones alone.

## Specifics

- Live entry and exit runners no longer translate successful command output into
  Telegram receipts (`scripts/run_live_management.sh:19` and
  `scripts/run_live_approved_orders.sh:17`).
- Broker-flat ghost positions stay `pending_confirmation` for the configured
  confirmation window. They auto-retire silently when confirmed
  (`src/kamandal_v2/live/reconciliation.py:734`).
- Kamandal persists unresolved review requests with `send=False`; Lathi's
  external-review bridge is the single routine Telegram projector
  (`src/kamandal_v2/live/reconciliation.py:575`).
- Health events carry `operator_state`. `self_healing` and `self_handled` events
  do not page; delegated review events use their dedicated surface
  (`src/kamandal_v2/tools/launchd_job.py:265`).
- A stable reason/group/order fingerprint suppresses an unchanged incident until
  it clears or materially changes (`src/kamandal_v2/tools/launchd_job.py:310`).

## Apply It Next Time

When adding a new alert, first name the recovery path and the exact action only a
human can take. If either is missing, store the event but do not add a Telegram
send. Give actionable incidents a stable identity so scheduled checks update one
incident rather than generating repeated pages.

## Dead Ends

- Paging every RED or YELLOW state confuses safety classification with human
  attention.
- Sending both directly from an app and through Lathi creates two cards for one
  decision.
- Sending success receipts makes the pager an execution feed and hides the rare
  event that genuinely needs intervention.
