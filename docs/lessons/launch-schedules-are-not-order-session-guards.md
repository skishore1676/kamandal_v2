---
title: Launch schedules are not option submission safety guards
type: decision
area: live execution
date: 2026-07-24
tags: [live-orders, options, launchd, safety]
refs: [src/kamandal_v2/live/option_sessions.py, src/kamandal_v2/live/execution.py, src/kamandal_v2/ops/launchd_registry.py]
---

# Launch Schedules Are Not Option Submission Safety Guards

## What We Learned

A launchd schedule may wake Kamandal at a useful time, but only a product-aware
check immediately before broker submission can prevent an after-close order.
Entry, close, and replacement orders all need that same last-mile guard.

## Context and Evidence

A SPY close-management cycle began at 15:15 CT and the broker rejected its
order with `Orders not accepted after close`. The schedule permitted the job,
but did not account for the work performed before submission or for different
option-session close times.

Kamandal now schedules final executable management cycles with a ten-minute
margin and independently enforces regular, extended-symbol, and early-close
cutoffs in `option_sessions.py`. A late close is recorded as
`deferred_market_closed` and re-evaluated next session; it is not submitted.

## When It Applies

Use this rule for every broker call whose validity depends on an exchange
session. Update the explicit product and early-close configuration when the
broker's supported sessions change; do not broaden the extended-symbol list by
assumption.

## Apply It Next Time

When a broker reports an hours-related rejection, inspect both the scheduled
start and the actual submit timestamp. Fix the immediate pre-submit boundary
first, then adjust the schedule for operating margin.
