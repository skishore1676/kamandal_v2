---
title: Derived entry paths must preserve economics and canonical money gates
type: pattern
area: live-entry-execution
date: 2026-08-21
tags: [entry-pricing, plan-fallback, broker-ticks, authorization]
refs: [src/kamandal_v2/live/pricing.py, src/kamandal_v2/live/execution.py, src/kamandal_v2/strategy_engine/planning.py, tests/test_entry_pricing_plan_fallback.py]
---

# Derived Entry Paths Must Preserve Economics And Canonical Money Gates

## What We Learned

Any derived live-entry path—tick normalization, replacement, or a fallback portfolio plan—must preserve both the original economic direction and every authorization gate used by the canonical first-entry path. Syntactically valid prices and successful helper tests are insufficient proof.

## Context and Evidence

A broker nickel retry initially rounded credit P2/P3 downward and debit P2/P3 upward. The prices were valid nickel increments, but P2 had already conceded through midpoint and P3 could exceed its frozen allowance. The safe invariant is simple: round every credit magnitude upward and every debit magnitude downward, then collapse duplicate stages.

Plan-2 fallback also called the low-level ticket executor directly. That preserved ticket freshness and broker preflight but skipped the outer live-submit confirmation, daily policy/stage authorization, and cluster-cap checks. `_fallback_submission_gate` now runs those checks before broker-adapter construction, and fallback replanning requires the rank-one daily snapshot date and hash.

A later repair exposed two more versions of the same mistake. Debit campaign
pricing treated a mixed-unit legacy percentage as a price ceiling, which made
valid calendars and diagonals impossible to enter. The correct non-circular
boundary is the Sheet-owned per-contract `live_max_bpr_per_order`, converted to
per-share option price. And a Plan-2 replan must invoke only the live book; using
the normal two-book orchestration would advance unrelated shadow state during a
live retry.

Google Sheets is also part of the live effect boundary, not a report copied
later. A fallback plan must replace today's `live_advisory` projection before
broker construction. If that projection fails, submission fails closed.

## When It Applies

Apply this whenever a new code path reaches `_execute_ticket`, constructs replacement prices, retries a rejected broker increment, or advances a later portfolio plan. It remains necessary even when the feature is disabled by default.

## Apply It Next Time

Trace backward from the broker call and compare the full canonical gate stack, not only the helper being reused. For price transformations, assert economic inequalities in addition to tick divisibility:

- credit P2 must be at or above frozen midpoint and normalized P3 must be no worse than frozen P3;
- debit P2 must be at or below frozen midpoint and normalized P3 must be no worse than frozen P3;
- later-plan submission must recheck submit confirmation, policy snapshot, stage authorization, health, underlying/cluster caps, session, freshness, and broker preflight.
- debit economic headroom must come from an explicit Sheet money cap, never a
  percentage of the debit candidate's own circular BPR estimate;
- a live fallback must not run the shadow book; and
- the current fallback portfolio must be visible in `daily_plan` before any
  broker effect.

Use the focused regression suite in `tests/test_entry_pricing_plan_fallback.py`, then run the complete repository suite because these paths share the live executor.

## Dead Ends

- Checking only that every retry price is divisible by `$0.05`; this missed economically adverse rounding.
- Calling `_execute_ticket` directly and assuming its internal freshness/preflight checks represented the whole live-entry authorization stack.
