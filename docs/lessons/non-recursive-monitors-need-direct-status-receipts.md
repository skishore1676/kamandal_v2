---
title: Non-recursive monitors need direct status receipts
type: pattern
area: launchd health projection
date: 2026-09-11
tags: [launchd, health, receipts, projection]
refs: [src/kamandal_v2/tools/launchd_job.py, src/kamandal_v2/tools/launchd_status.py, tests/test_lathi_control_contract.py, e3ab9cc]
---

# Non-Recursive Monitors Need Direct Status Receipts

## What We Learned

A health monitor should not monitor itself recursively, but excluding it from
the monitored-job set must not erase its own status. The operator projection
should combine actual launchd loaded state with the monitor's own terminal
receipt.

## Context and Evidence

`scheduled-job-health` intentionally inspects the other scheduled jobs and is
absent from `MONITORED_JOBS`. The status builder assumed every registered job
would have a row in that report, so it projected the monitor as disabled with no
last run even while launchd showed 365 runs, last exit 0, and the job's own log
contained a fresh `status=ok` receipt.

Commit `e3ab9cc` keeps `MONITORED_JOBS` non-recursive. The status builder reads
launchd's loaded-label inventory once and falls back to the registered job's
own `KAMANDAL_LAUNCHD_JOB` receipt when no monitor row exists. The full 933-test
suite passed; oldmac then reported the monitor loaded, enabled, armed, and
successful with `status_source=owner_receipt`.

## When It Applies

Use this pattern for a scheduler watchdog, alert sweeper, reconciliation
supervisor, or any observer intentionally omitted from the set it evaluates.
Do not add a self-row merely to make a dashboard field green if doing so creates
recursive freshness or alert semantics.

## Apply It Next Time

First compare the registry, monitor membership, installed plist, `launchctl`
state, and the job's own terminal receipt. If the job is intentionally outside
the monitor set, teach the read model to use direct owner evidence. Keep domain
health, scheduler availability, and human actionability as separate fields.

## Dead Ends

- Adding the monitor to `MONITORED_JOBS` makes it judge its own in-progress run
  and couples availability to recursive freshness.
- Inferring enabled state from a missing report row turns an information gap
  into a false operator fact.
- Trusting plist presence alone cannot distinguish installed from loaded; use
  the launchd inventory when it is available.
