---
title: Unified management needs one effect owner and an ordered recovery cycle
type: migration
area: live order and lifecycle orchestration
date: 2026-08-24
tags: [unified-lifecycle, recovery, ownership, launchd, notifications]
refs: [scripts/run_unified_lifecycle_management.sh:8, scripts/run_live_approved_orders.sh:8, src/kamandal_v2/live/execution.py:101, src/kamandal_v2/strategy_engine/management.py:37]
---

# Unified Management Needs One Effect Owner and an Ordered Recovery Cycle

## What We Learned

Unifying lifecycle decisions is not enough. A live management cycle must also
own effect sequencing and recovery: refresh broker state, evaluate live
lifecycles, drain close/adjust tickets, refresh and clean up, then run shadow.
Any parallel job that can claim or reprice the same management ticket creates a
second owner even if it never makes strategy decisions.

## Context and Evidence

The initial unified runner evaluated live, then shadow, and only afterward
drained live closes. Meanwhile the open-order runner could prioritize a newly
staged close. Near the session cutoff, the open runner claimed that ticket
while the unified runner was still processing shadow and converted a normal
next-session deferral into a direct error notification.

The bounded entry/exit repricing machinery had not been removed. The regression
was orchestration: inline pre/post synchronization disappeared, live effects
were separated from their decision by shadow work, and two scheduled jobs could
advance management state.

Independent owners can also collide at the persistence layer without violating
effect ownership. Runtime evidence on August 25 and August 31 showed three
`OperationalError: database is locked` failures in live lifecycle management;
the next five-minute tick recovered each one. Planning and open-entry recovery
legitimately write the same SQLite database, so changing schedule minutes would
only reduce one observed overlap. The durable recovery is one idempotent
lifecycle-branch replay after the existing 30-second busy wait, before the
guarded broker-effect executor runs.

## When It Applies

Apply this whenever a decision engine stages effects for another command or
when live and shadow work share one invocation. Idempotent ledgers reduce
duplicate broker effects, but they do not make competing effect owners correct;
the wrong owner can still consume a ticket, apply the wrong session semantics,
or page before recovery is exhausted.

## Apply It Next Time

1. Identify exactly one scheduled owner for each effect class: opens versus
   lifecycle close/adjust work.
2. Keep live decision, live effect, and immediate readback contiguous.
3. Let a per-lifecycle error record a failed receipt without suppressing effects
   already staged by successful lifecycles.
4. Run broker-inert shadow only after the live effect boundary completes.
5. Treat `retryable_current_session` and `retryable_next_session` as
   machine-owned states; page only from terminal recovery state.
6. Retry a lifecycle branch only when every failure is the exact transient
   SQLite lock signature. Never replay mixed failures, and never retry after
   the broker-effect boundary.

The fastest regression check is to stage a close and prove the open-entry
executor leaves it unchanged while the unified close executor drains it.

## Dead Ends

Filtering close tickets out of the open executor while leaving mutating order
synchronization in both jobs is incomplete: both jobs can still race on close
repricing or expiry. The open runner may perform a read-only status refresh as
an entry precondition; active-order recovery belongs to the unified cycle.

Staggering launchd minutes is an optimization, not a lock-correctness contract:
job duration varies with broker and market-data latency, so two writers can
still overlap on another day.
