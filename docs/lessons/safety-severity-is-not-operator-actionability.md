---
title: Safety severity is not operator actionability
type: decision
area: live-health
date: 2026-07-26
tags: [risk-manager, control-tower, market-calendar, account-snapshot]
refs: [src/kamandal_v2/live/health.py, src/kamandal_v2/tools/launchd_status.py, e042460]
---

# Safety Severity Is Not Operator Actionability

## What We Learned

Keep the entry safety verdict separate from the Control Tower lifecycle. A stale
account snapshot should continue to make `risk_manager.blocked=true` and
`overall=RED`, but it should not ask for human intervention when the market is
closed or before the first scheduled account-snapshot refresh can reasonably
finish.

## Context and Evidence

Kamandal correctly skipped all Sunday launchd jobs, but the continuously polled
owner status compared Friday's 14:40 CT snapshot against a flat 1,440-minute
wall-clock limit. The result was a Sunday `risk_account_snapshot_stale` Tower
card despite there being no failed job and no expected refresh.

## When It Applies

Use this distinction for fail-closed guards whose automatic recovery is bounded
by a known market schedule. Do not suppress attention after the scheduled
recovery opportunity plus grace time, or for a red event that is immediately
actionable regardless of market hours.

## Apply It Next Time

1. Preserve the breaker and severity.
2. On weekends and market holidays, mark snapshot staleness `self_handled`.
3. Before the first 08:50 CT unified-plan refresh plus grace, mark it
   `self_healing`.
4. After that deadline, escalate unchanged staleness to `operator_needed`.
5. Test all four boundaries: weekend, holiday, pre-refresh, and post-grace.
