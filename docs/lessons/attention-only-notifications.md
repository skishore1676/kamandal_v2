---
title: Page on exhausted recovery, not workflow status
type: decision
area: live operations and Telegram notifications
date: 2026-07-16
tags: [alerts, reconciliation, lathi, operations]
refs: [commit:9ea9f1e, scripts/run_live_management.sh:19, scripts/run_live_approved_orders.sh:17, src/kamandal_v2/live/execution.py:252, src/kamandal_v2/live/reconciliation.py:575, src/kamandal_v2/stores/sqlite.py:664, src/kamandal_v2/tools/launchd_job.py]
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
CLI output, and Control Tower. The narrow informational exception is a completed
entry attempt that opened no position: without one terminal summary, the operator
cannot distinguish inactivity from an attempted order that expired unfilled.

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
- High-frequency launchd jobs persist a stable failure fingerprint, absorb the
  first two identical failures as bounded retry evidence, page once on the
  third, and send one recovery notice only if an incident was previously paged.
  Scheduled health excludes failures already owned by this direct incident
  state instead of projecting the same problem a second time.
- Derived health conditions do not create a second owner. In particular, a
  stale account snapshot blocks entries fail-closed, while the planner job owns
  the refresh failure and its one incident page.
- Intraday daily reports are passive JSON/Markdown evidence for TradeLab and
  operator surfaces. They do not send Telegram status or repeat an incident
  already owned by planning, execution, reconciliation, or live health.
- A broker-confirmed terminal unfilled entry sends one informational summary of
  its attempts, reprices, limit path, and expiration while keeping intermediate
  submit and reprice milestones silent (`src/kamandal_v2/live/execution.py:252`).
- The ledger status transition is an atomic claim, so overlapping sync cycles
  cannot both send the terminal summary (`src/kamandal_v2/stores/sqlite.py:664`).

## Apply It Next Time

When adding a new alert, first name the recovery path and the exact action only a
human can take. If either is missing, store the event but do not add a Telegram
send. Give actionable incidents a stable identity so scheduled checks update one
incident rather than generating repeated pages. For a non-actionable receipt,
require a terminal state, a concrete visibility gap it closes, and an idempotent
claim before sending.

## Dead Ends

- Paging every RED or YELLOW state confuses safety classification with human
  attention.
- Sending both directly from an app and through Lathi creates two cards for one
  decision.
- Sending success receipts makes the pager an execution feed and hides the rare
  event that genuinely needs intervention.
- Sending every submit, reprice, cancel, and expiration step recreates the noisy
  execution feed. Summarize the lineage once, after the broker confirms the entry
  ended without a position.
