---
title: A resting multileg profit order is lifecycle state, not another trigger
type: decision
area: lifecycle management and live order lineage
date: 2026-08-28
tags: [options, profit-target, lifecycle, cancel-replace, shadow]
refs: [src/kamandal_v2/strategy_lanes/management_runtime.py, src/kamandal_v2/live/execution.py, tests/test_resting_profit_orders.py]
---

# A Resting Multileg Profit Order Is Lifecycle State

## What We Learned

An early profit offer must remain inside the canonical lifecycle. Midpoint still
values the package and decides when the offer may arm; the broker limit itself
enforces the accepted economics. Treating the offer as a parallel manager or as
an ordinary midpoint-to-natural exit would either duplicate ownership or donate
the target during repricing.

## Context and Evidence

The canonical manager previously staged a profit close only after midpoint
reached the full target. The safe extension stages one DAY full-package ticket
at the exact target after partial progress, using original target-profit dollars
and cumulative filled lifecycle cashflow. It never concedes through the generic
close reprice ladder. Tests cover credit, debit, adjusted cashflow, invalid
quotes, same-day deduplication, next-day re-arm, shadow working state,
higher-priority arbitration, staged cancellation, full parent-fill races, and
partial-fill reconciliation.

## When It Applies

Use this pattern for non-marketable package limits that may remain working while
the same lifecycle continues to observe event, time, loss, emergency, or
adjustment conditions. It does not relax actionable-quote requirements, adverse
loss confirmation, session gates, atomic package ownership, or broker preflight.

## Apply It Next Time

1. Give the standing offer a reason class and immutable execution envelope.
2. Make its order identity stable within the DAY and new across dates.
3. Recognize it as the lifecycle's own working order, not a generic conflict.
4. Let higher-priority actions persist a child before cancel, wait for terminal
   unfilled parent state, then verify position and preflight before submission.
5. Abort the child on a parent fill; force reconciliation on a partial fill.
6. Keep source deployment, shadow activation, live activation, and economic
   proof as separate gates.

## Dead Ends

- Reusing the executable-profit reprice ladder: it can move below the exact
  target and changes the trade being offered.
- Letting a working target block every later action: urgent lifecycle duties can
  stall indefinitely.
- Cancelling before the replacement child is durable: a crash loses management
  intent and makes recovery ambiguous.
