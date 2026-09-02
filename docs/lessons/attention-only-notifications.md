---
title: Page on exhausted recovery, not workflow status
type: decision
area: live operations and Telegram notifications
date: 2026-07-16
tags: [alerts, reconciliation, lathi, operations]
refs: [commit:9ea9f1e, commit:a12655e, scripts/run_live_management.sh:19, scripts/run_live_approved_orders.sh:17, src/kamandal_v2/live/execution.py:252, src/kamandal_v2/live/reconciliation.py:575, src/kamandal_v2/stores/sqlite.py:664, src/kamandal_v2/tools/launchd_job.py, tests/test_live_lane.py:32]
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
CLI output, Control Tower, and the daily report. A completed entry attempt that
opened no position is routine evidence rather than an interruption: the canonical
order ledger and daily report distinguish it from inactivity.

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
- A close deferred by the session guard with `retryable_current_session` or
  `retryable_next_session` remains owned by lifecycle recovery. The direct
  placement summary must not turn that normal waiting state into an error page.
- An opening basket already terminalized as canceled or expired is routine
  unfilled evidence. A later execution tick must not translate that status into
  a generic `selected entry not placed` page.
- A patient DAY resting-profit order is neither stale while broker-working nor a
  failed close when it expires unfilled. Health continues to surface it in
  structured counts while keeping Beacon quiet; genuine rejection or ownership
  ambiguity remains visible.
- Scheduled health compares observations only with ticks that occurred after
  the current plist activation. Installing a changed schedule after an earlier
  tick cannot make the new job retroactively stale.
- A stable reason/group/order fingerprint suppresses an unchanged incident until
  it clears or materially changes (`src/kamandal_v2/tools/launchd_job.py:310`).
- High-frequency and additive source-lane launchd jobs persist a stable failure
  fingerprint, absorb the first two identical failures as bounded retry
  evidence, page once on the third, and send one recovery notice only if an
  incident was previously paged.
  Scheduled health excludes failures already owned by this direct incident
  state instead of projecting the same problem a second time.
- Derived health conditions do not create a second owner. In particular, a
  stale account snapshot blocks entries fail-closed, while the planner job owns
  the refresh failure and its one incident page.
- Intraday daily reports are passive JSON/Markdown evidence for TradeLab and
  operator surfaces. They do not send Telegram status or repeat an incident
  already owned by planning, execution, reconciliation, or live health.
- A broker-confirmed terminal unfilled entry retains its attempts, reprices,
  limit path, expiration, and terminal state in the order ledger. The daily
  report surfaces those intents; immediate Telegram delivery is disabled in
  production while the opt-in notification path remains testable.
- The ledger status transition is an atomic claim, so overlapping sync cycles
  cannot both send the terminal summary (`src/kamandal_v2/stores/sqlite.py:664`).
- Production-host tests must default every notification capability to effect-off
  instead of inheriting the host's live or spool posture. A test may opt in only
  after replacing the external sender with a fake (`tests/test_live_lane.py:32`).

## Apply It Next Time

When adding a new alert, first name the recovery path and the exact action only a
human can take. If either is missing, store the event but do not add a Telegram
send. Give actionable incidents a stable identity so scheduled checks update one
incident rather than generating repeated pages. For a non-actionable receipt,
require a terminal state, a concrete visibility gap it closes, and an idempotent
claim before sending. Before running tests on oldmac, prove notification settings
are forced off by the fixture rather than merely assuming `spool` is harmless;
spool still mutates the operator outbox even when it performs no network call.

## Dead Ends

- Paging every RED or YELLOW state confuses safety classification with human
  attention.
- Sending both directly from an app and through Lathi creates two cards for one
  decision.
- Sending success receipts makes the pager an execution feed and hides the rare
  event that genuinely needs intervention.
- Sending submit, reprice, cancellation, or terminal-unfilled milestones recreates
  a noisy execution feed. Keep the lineage in canonical evidence and summarize it
  through the daily report unless the exhausted workflow requires operator action.
- Treating sandbox spool as effect-free is incorrect on an operator host: it can
  leave realistic fixture receipts in the real outbox and confuse later audits.
