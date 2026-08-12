---
title: Timeouts must own process groups and verifiable lock leases
type: pattern
area: launchd runtime
date: 2026-08-12
tags: [launchd, reconciliation, timeout, locks, observability]
refs: [scripts/common.sh, src/kamandal_v2/tools/launchd_job.py, src/kamandal_v2/ops/stage_receipt.py]
---

# Timeouts Must Own Process Groups and Verifiable Lock Leases

## What We Learned

A job timeout is not autonomous recovery unless it terminates the whole process
group, preserves partial diagnostics, and leaves a lock whose owner can be
verified. A directory-only lock plus `subprocess.run(..., timeout=...)` can kill
the wrapper before its `EXIT` trap runs, permanently suppressing later runs.

## Context and Evidence

The live-reconciliation wrapper was killed after the generic 1,800-second
deadline. Its empty directory lock survived, so later schedules looked like
successful no-ops. The wrapper also lost partial output, leaving no evidence of
whether broker reads, local/order reconciliation, or Sheets had stalled.

The repaired contract is:

- each lock records PID, script, start time, and a unique lease token;
- only a provably dead owner is recoverable, with the old lock moved aside;
- live or ambiguous ownership fails closed with a structured status;
- the runner terminates the complete process group and retains output tails;
- live reconciliation has a five-minute deadline, bounded Sheets I/O, and an
  atomic stage receipt at `data/run/live_reconciliation/latest.json`.

## When It Applies

Use this pattern for scheduled jobs that combine shell wrappers with child
processes or external services. A longer timeout is appropriate only when the
normal stage latency is measured and intentionally requires it; it is not a
substitute for network timeouts or stage-level evidence.

## Apply It Next Time

On a timeout, inspect the stage receipt first. Then inspect the lock owner PID
and command. Recover automatically only when the owner is recorded and dead;
otherwise leave the lock untouched and surface an operator-visible failure.
